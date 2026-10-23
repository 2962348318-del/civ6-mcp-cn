"""End-turn state machine — snapshot, blocker resolution, turn advancement."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import civ_mcp.narrate as nr
from civ_mcp import advice as advice_mod
from civ_mcp import debrief, lategame, opponents, params, policy
from civ_mcp import end_turn_guard as etg
from civ_mcp import lua as lq
from civ_mcp import world_model as wm_mod
from civ_mcp.connection import LuaError
from civ_mcp.game_lifecycle import cleanup_old_autosaves, save_game
from civ_mcp.hexgrid import hex_distance

if TYPE_CHECKING:
    from civ_mcp.game_state import GameState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AI-turn hang watchdog
# ---------------------------------------------------------------------------


async def _find_game_pid() -> int | None:
    """Locate the Civ 6 game process id (Windows)."""
    cmd = (
        "Get-Process | Where-Object { $_.ProcessName -like '*Civilization*' -or "
        "$_.ProcessName -like '*Civ6*' } | "
        "Select-Object -First 1 -ExpandProperty Id"
    )
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pid = (out.stdout or "").strip()
        return int(pid) if pid.isdigit() else None
    except Exception:
        return None


async def _game_cpu_seconds(pid: int) -> float:
    """Total CPU seconds consumed by the game process."""
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).CPU",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        txt = (out.stdout or "").strip()
        return float(txt) if txt else 0.0
    except Exception:
        return 0.0


class _AiHangWatchdog:
    """Detect CPU-pegged AI turns (the infinite-loop hang).

    The Civ 6 AI can enter a CPU-pegged infinite loop during turn
    processing (measured 5x in this project; triggered by stale diplomacy
    sessions and multi-war state). end_turn previously polled for up to
    ~9 minutes before giving up — a silent multi-minute hang for the user.
    This watchdog samples the game process CPU via a side-channel
    (PowerShell, independent of the wedged game): if the process stays
    fully pegged for 2 consecutive sampling windows, bail early with an
    actionable reload hint.
    """

    def __init__(self, now_fn=None) -> None:
        self._pid: int | None = None
        self._last_cpu: float | None = None
        self._last_wall: float | None = None
        self._peg_windows = 0
        self._now = now_fn or time.monotonic

    async def sample(self) -> bool:
        """Record one CPU sample; True when the hang is confirmed."""
        if self._pid is None:
            self._pid = await _find_game_pid()
            if self._pid is None:
                return False
            self._last_cpu = await _game_cpu_seconds(self._pid)
            self._last_wall = time.monotonic()
            return False
        now = self._now()
        cpu = await _game_cpu_seconds(self._pid)
        prev_wall = self._last_wall if self._last_wall is not None else now
        prev_cpu = self._last_cpu if self._last_cpu is not None else cpu
        wall_dt = now - prev_wall
        cpu_dt = cpu - prev_cpu
        self._last_cpu = cpu
        self._last_wall = now
        if wall_dt <= 0:
            return False
        cores_pegged = cpu_dt / wall_dt
        if cores_pegged >= 4.0:
            self._peg_windows += 1
        else:
            self._peg_windows = 0
        return self._peg_windows >= 2


async def _check_mid_turn_diplomacy(
    gs: GameState,
    lua: str,
    turn_before: int | None,
) -> tuple[str | None, bool]:
    """Probe for AI diplomatic proposals during end_turn polling.

    Returns (message, advanced) where message is a string to return to the
    agent if diplomacy was found, or None if no diplomacy detected.
    ``advanced`` is True if the turn advanced during war-declaration handling.

    Handles war auto-dismiss, deal formatting, and session info.  Extracted
    from the Phase 3 inline logic so both the early probe (Phase 2, ~45s)
    and the full-timeout fallback (Phase 3) can share the same code.
    """
    try:
        mid_sessions = await gs.get_diplomacy_sessions()
        if not mid_sessions:
            return None, False

        # DiplomacyActionView text can take 1-2s to populate after session
        # opens during AI processing. If text is empty, retry once.
        if any(not s.dialogue_text for s in mid_sessions):
            await asyncio.sleep(2.0)
            mid_sessions = await gs.get_diplomacy_sessions()

        # Auto-dismiss war declarations — these are informational only
        # (you can't decline a war). Dismiss and report to the agent.
        war_sessions = [s for s in mid_sessions if s.is_at_war]
        if war_sessions:
            war_names = []
            for ws in war_sessions:
                close_lua = lq.build_diplomacy_respond(ws.other_player_id, "EXIT")
                await gs.conn.execute_write(close_lua)
                war_names.append(f"{ws.other_civ_name} ({ws.other_leader_name})")
                log.info(
                    "Auto-dismissed war declaration from %s",
                    ws.other_civ_name,
                )
            # Remove war sessions from the list
            mid_sessions = [s for s in mid_sessions if not s.is_at_war]
            # If only war sessions, resume polling (original ACTION_ENDTURN
            # is still in flight — do NOT re-send or turns will skip)
            if not mid_sessions:
                war_msg = ", ".join(war_names)
                advanced = False
                for _ in range(10):
                    await asyncio.sleep(2.0)
                    turn_after = await _get_turn_number(gs)
                    if (
                        turn_after is not None
                        and turn_before is not None
                        and turn_after > turn_before
                    ):
                        advanced = True
                        break
                if advanced:
                    return None, True  # turn advanced, caller handles snapshot
                # Original ACTION_ENDTURN was consumed — next call must re-send
                gs._pending_end_turn = False
                gs._pending_end_turn_from = None
                return (
                    f"WAR DECLARED by {war_msg}! Session dismissed.\n"
                    f"Turn did not advance — call end_turn again.\n"
                    f"Reassess: check unit positions, city defenses, and military strength."
                ), False

        if not mid_sessions:
            # All sessions were war declarations and turn advanced
            return None, True if war_sessions else False

        # Auto-resolve non-deal encounter sessions with POSITIVE responses
        # (first meets, denouncements, friendship requests). Deals are left
        # open for the agent.
        encounter_sessions = [s for s in mid_sessions if not s.deal_summary]
        if encounter_sessions:
            from civ_mcp.game_lifecycle import auto_resolve_diplomacy_sessions

            actions = await auto_resolve_diplomacy_sessions(gs.conn)
            if actions:
                log.info("Auto-resolved mid-turn diplomacy: %s", "; ".join(actions))
            mid_sessions = await gs.get_diplomacy_sessions()
            if not mid_sessions:
                # All sessions handled — resume polling (original
                # ACTION_ENDTURN is still in flight — do NOT re-send)
                names = ", ".join(s.other_civ_name for s in encounter_sessions)
                advanced = False
                for _ in range(10):
                    await asyncio.sleep(2.0)
                    turn_after = await _get_turn_number(gs)
                    if (
                        turn_after is not None
                        and turn_before is not None
                        and turn_after > turn_before
                    ):
                        advanced = True
                        break
                if advanced:
                    return None, True  # turn advanced, caller handles snapshot
                gs._pending_end_turn = False
                gs._pending_end_turn_from = None
                return (
                    f"Diplomacy from {names} auto-resolved (POSITIVE response).\n"
                    f"Turn did not advance — call end_turn again."
                ), False

        # Remaining sessions (deals) — format for agent
        session_info = []
        for s in mid_sessions:
            phase = (
                "deal"
                if s.deal_summary
                else ("goodbye" if s.buttons == "GOODBYE" else "active")
            )
            session_info.append(f"{s.other_civ_name} ({s.other_leader_name}) [{phase}]")
        has_deal = any(s.deal_summary for s in mid_sessions)
        lines: list[str] = []
        if war_sessions:
            war_names_str = ", ".join(f"{ws.other_civ_name}" for ws in war_sessions)
            lines.append(f"WAR DECLARED by {war_names_str}! (auto-dismissed)")
        lines.append(
            f"Turn paused — AI diplomatic proposal from {', '.join(session_info)}.",
        )
        for s in mid_sessions:
            if s.dialogue_text:
                lines.append(f'{s.other_civ_name} says: "{s.dialogue_text}"')
            if s.reason_text:
                lines.append(f"Reason: {s.reason_text}")
            if s.deal_summary:
                lines.append(f"Deal from {s.other_civ_name}: {s.deal_summary}")
        if has_deal:
            lines.append(
                "Use respond_to_trade(other_player_id=X, accept=True/False) to handle it, then end_turn again."
            )
        else:
            lines.append("Use respond_to_diplomacy to handle it, then end_turn again.")
        return "\n".join(lines), False
    except Exception:
        log.debug("Mid-turn diplomacy check failed", exc_info=True)
        return None, False


async def _get_turn_number(gs: GameState) -> int | None:
    """Read the current game turn number."""
    try:
        lines = await gs.conn.execute_read(
            'print(Game.GetCurrentGameTurn()); print("---END---")'
        )
        if lines:
            return int(lines[0])
    except (LuaError, ValueError, IndexError):
        pass
    return None


async def _check_victory_proximity(gs: GameState) -> list[lq.TurnEvent]:
    """Lightweight per-turn check for foreign victory threats."""
    events: list[lq.TurnEvent] = []
    lines = await gs.conn.execute_write(lq.build_victory_proximity_query())
    enabled: set[str] = set()
    for line in lines:
        if line.startswith("VENABLED|"):
            enabled.add(line.split("|", 1)[1])
    for line in lines:
        if line.startswith("REL_THREAT|"):
            if enabled and "VICTORY_RELIGIOUS" not in enabled:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                civ_name, rel_name = parts[1], parts[2]
                count, total = int(parts[3]), int(parts[4])
                if count >= total:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=f"!!! RELIGIOUS VICTORY IMMINENT: {civ_name}'s {rel_name} is majority in ALL {total} civilizations!",
                        )
                    )
                elif count >= total - 1:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=f"!! RELIGIOUS VICTORY THREAT: {civ_name}'s {rel_name} is majority in {count}/{total} civilizations!",
                        )
                    )
        elif line.startswith("DIPLO_THREAT|"):
            if enabled and "VICTORY_DIPLOMATIC" not in enabled:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                dvp = int(parts[2])
                if dvp >= 20:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=f"!!! DIPLOMATIC VICTORY IMMINENT: {parts[1]} has {dvp}/20 DVP — wins immediately, does NOT wait for World Congress!",
                        )
                    )
                elif dvp >= 18:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=f"!! DIPLOMATIC VICTORY THREAT: {parts[1]} has {dvp}/20 DVP — wins IMMEDIATELY at 20, does not wait for WC. Must strip DVP at next World Congress BEFORE they reach 20.",
                        )
                    )
                elif dvp >= 15:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=f"!! DIPLOMATIC VICTORY THREAT: {parts[1]} has {dvp}/20 DVP!",
                        )
                    )
                else:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="victory",
                            message=f"Diplomatic race: {parts[1]} has {dvp}/20 DVP.",
                        )
                    )
        elif line.startswith("SCI_THREAT|"):
            if enabled and "VICTORY_TECHNOLOGY" not in enabled:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                vp, needed = int(parts[2]), int(parts[3])
                if vp >= needed - 1:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=f"!! SCIENCE VICTORY IMMINENT: {parts[1]} has {vp}/{needed} space race projects!",
                        )
                    )
                elif vp >= 1:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="victory",
                            message=f"Science race: {parts[1]} has {vp}/{needed} space race projects.",
                        )
                    )
        elif line.startswith("SCI_THREAT_STD|"):
            # Standard 规则集哨兵:VP API 不存在,只能看见太空港数。
            if enabled and "VICTORY_TECHNOLOGY" not in enabled:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                ports = int(parts[2])
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="victory",
                        message=(
                            f"Science race: {parts[1]} has {ports} completed spaceport(s) "
                            "(Standard ruleset — project progress not exposed; treat every port as active)."
                        ),
                    )
                )
        elif line.startswith("CUL_THREAT|"):
            # 缺陷55:文化胜利临近监控。T384 败局:美国 375 游客 vs 门柱
            # 381,差 7 人时零预警,通知弹出后 1 回合就输。gap 阈值给宽:
            # 游客增速晚期可达 1-2/回合,80 的余量 ≈ 40-80 回合预警窗。
            if enabled and "VICTORY_CULTURE" not in enabled:
                continue
            parts = line.split("|")
            if len(parts) >= 5:
                ft, gate = int(parts[2]), int(parts[3])
                gap = gate - ft
                # 缺陷58(2026-08-09 希腊局 T18-T37 实测):开局全场游客都是
                # 个位数时,"0 游客 vs 门柱 2、差 2"照样触发最高级警报,每回合
                # 刷屏"苏美尔即将文化胜利"。绝对差在门柱量级很小的时候没有
                # 意义 —— 文化胜利要求的是几百个游客。门柱没到 CULTURE_GATE_
                # FLOOR 之前,这条线还没开始比赛,静默。
                if not culture_threat_is_live(ft, gate):
                    continue
                if gap <= 10:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=(
                                f"!!! CULTURE VICTORY IMMINENT: {parts[1]} has {ft} foreign tourists — "
                                f"only {max(gap, 0)} below the last gate ({parts[4]}'s {gate} domestic). "
                                "Turns, not eras. Break their tourism output NOW (pillage/occupy theater+wonder cities) or lose."
                            ),
                        )
                    )
                elif gap <= 80:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="victory",
                            message=(
                                f"!! CULTURE VICTORY THREAT: {parts[1]} {ft} foreign tourists vs last gate {gate} "
                                f"({parts[4]}) — gap {gap}. Plan the counter (war on their tourism cities / "
                                "rock bands boost OUR gate? No — gate is domestic-only; hitting their output is the lever)."
                            ),
                        )
                    )
                elif gap <= 200:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="victory",
                            message=f"Culture race: {parts[1]} {ft} foreign tourists, gap {gap} to last gate ({parts[4]} {gate}).",
                        )
                    )
    return events


# 维护费占毛收入的预警阈值(P2, 2026-08-05 T335 复盘)。
# 赤字警报(DEFICIT)只在国库快见底时才响,而结构性失衡早在那之前就定型:
# T335 实测收入的六成在养 18 个打不动城墙的弩手,直到 -20/回合才被动止血。
MAINTENANCE_BURDEN_RATIO = 0.6

# 文化胜利哨兵的门柱下限(缺陷58,2026-08-09 希腊局实测)。
# 文化胜利需要几百个外国游客;开局阶段全场 domestic 游客是个位数,
# 此时的"差 2 个游客"没有任何预警价值,却会每回合刷最高级警报,把
# 真警报的可信度一起烧掉。50 是保守取值:低于它任何一方都离胜利
# 极远(实测 T384 输局时的门柱是 381)。
CULTURE_GATE_FLOOR = 50

# 对手自己的外国游客下限(2026-08-10 罗马局):门柱过线后,0 游客的对手
# 仍被逐个报成"文化威胁 CLOSING",六家刷屏 8 回合。追赶者没有游客就
# 不构成威胁 —— 报警要求它至少有 CULTURE_RIVAL_FLOOR 个,且到门柱 1/3。
CULTURE_RIVAL_FLOOR = 15


def culture_threat_is_live(foreign_tourists: int, gate: int) -> bool:
    """这家对手的文化胜利威胁是否真的在跑(决定要不要报警)。

    两道下限,缺一不可:
      · 门柱本身要够高(CULTURE_GATE_FLOOR)—— 开局全场个位数游客时,
        "0 vs 2、差 2"不是威胁(缺陷58)。
      · 追赶者自己也得上路 —— 2026-08-10 罗马局:我方门柱涨到 55 后,
        六家【0 游客】的对手每回合各刷一条 "CLOSING",连刷 8 回合。
        差值只看门柱、不看对手,于是我方文化越强、假警报越多。
    """
    if gate < CULTURE_GATE_FLOOR:
        return False
    return foreign_tourists >= max(CULTURE_RIVAL_FLOOR, gate // 3)


# 主力兵种落后当前时代的报警阈值(2026-08-05"作战不能只看纸面数值")。
# T335 实证:军力 1137"全场第一",拆开是 13 个中世纪弩手打现代战争 ——
# 纸面分按数量堆,代差完全隐形,1.57× 的军力比其实是虚高。
ARMY_ERA_LAG = 2


def army_obsolescence_event(units, era_name: str) -> "lq.TurnEvent | None":
    """军队中位时代 vs 游戏当前时代,落后 ≥2 个时代 → 预警。纯函数可测。"""
    try:
        from civ_mcp import war_planner as wp

        game_idx = wp.era_index(era_name)
        if game_idx <= 0:
            return None
        mil_types = [
            str(getattr(u, "unit_type", "") or "")
            for u in units or []
            if int(getattr(u, "combat_strength", 0) or 0) > 0
        ]
        prof = wp.army_era_profile(mil_types)
        if not prof:
            return None
        lag = game_idx - prof["median"]
        if lag < ARMY_ERA_LAG:
            return None
        top_idx, top_n = prof["lagging"][0]
        return lq.TurnEvent(
            priority=2,
            category="military",
            message=(
                f"ARMY OBSOLETE: {top_n}/{prof['total']} military units are "
                f"{wp.era_cn(top_idx)} units while the game is in "
                f"{wp.era_cn(game_idx)} ({lag} eras behind). The military "
                f"score is inflated by unit count — treat the ratio as a "
                f"paper lead. upgrade_unit the frontline first, and do not "
                f"start a war on this number."
            ),
        )
    except Exception:
        return None


def maintenance_burden_event(
    overview, trade_cap: tuple[int, int] | None = None, luxuries: int = 0
) -> lq.TurnEvent | None:
    """维护费 ≥ 毛收入 60% → 结构性预警 + 修复清单。纯函数,可测。

    2026-08-06(T64 局)升级为计划:警报本身是对的(T61 起持续触发),
    但只说了"病"没说"药" —— agent 要自己想解法,而且想错了
    (它认为造商人得先有市场;实际「对外贸易」市政就解锁商人)。
    这与营地/扩张是同一形态,所以用同一个解法:诊断 → 带数字的清单。

    收入端优先于支出端:解散单位是**永久损失战力**,而闲置商路是
    **白扔的钱**。先捡钱,再考虑砍战力。
    """
    try:
        income = float(getattr(overview, "gold_income", 0) or 0.0)
        maint = float(getattr(overview, "total_maintenance", 0) or 0.0)
        unit_maint = int(getattr(overview, "unit_maintenance", 0) or 0)
    except (TypeError, ValueError):
        return None
    if income <= 0 or maint < income * MAINTENANCE_BURDEN_RATIO:
        return None

    fixes: list[str] = []
    if trade_cap:
        cap, active = trade_cap
        idle = max(0, int(cap) - int(active))
        if idle > 0:
            fixes.append(
                f"① 收入端先做:{idle} 条商路容量闲置 —— 造/派商人就是白拿的金币"
                f"(「对外贸易」市政即解锁商人,**不需要先有市场**)"
            )
    if luxuries > 0:
        fixes.append(f"② 有 {luxuries} 种多余奢侈品可卖(get_trade_options 看报价)")
    fixes.append(
        f"③ 支出端最后动:解散过时单位(unit_action delete,当前单位维护 {unit_maint}/t)"
        f"—— 这是永久损失战力,排在收入端之后"
    )
    return lq.TurnEvent(
        priority=2,
        category="economy",
        message=(
            f"MAINTENANCE BURDEN: 维护 {maint:.0f}/t 吃掉毛收入 {income:.0f} 的 "
            f"{maint / income:.0%}(其中单位 {unit_maint}/t)。这是结构性的,"
            f"不会自己好。修复顺序:\n  " + "\n  ".join(fixes)
        ),
    )


# 存量攒到多少「回合产量」算囤积。相对阈值,不是游戏常数 ——
# 早期 200 信仰是巨款,后期 200 信仰不够买一个单位。
HOARD_TURNS = 20
# 低于这个绝对量不提(开局那点零头不值得占一行简报)
HOARD_MIN = 200
# 金币安全垫:低于它就别再花(实测 T110 花到 0 触发强制解散单位)。
# 按维护费的倍数算,不写死金额 —— 后期维护费是三位数,固定值会失效。
CUSHION_TURNS = 5
HOARD_MIN_CUSHION = 100


def idle_resource_event(overview, cities, wm=None) -> lq.TurnEvent | None:
    """信仰/金币囤着没花 → 提示可花项。纯函数,可测。

    罗马局实证:信仰囤到 3338 才在 T358 想起来花,整局的伟人竞价与
    神权买兵窗口全错过了。每回合的机会成本没有任何人提过一个字 ——
    存量不会报警,因为它看起来"很安全"。

    **只报"你囤着"和"能花在哪",不报"该花哪个"** —— 花什么是决策
    (取决于路线和局势),忘了花才是缺陷。这条边界与全项目一致。

    合法的囤积理由要认:大本钟建成瞬间国库翻倍,建成前囤金是正确操作
    (late_economy 已经在教这件事),这时不该反过来催人花钱。
    """
    if overview is None:
        return None
    try:
        faith = float(getattr(overview, "faith", 0) or 0)
        gold = float(getattr(overview, "gold", 0) or 0)
        gpt = float(getattr(overview, "gold_per_turn", 0) or 0)
        maint = abs(float(getattr(overview, "total_maintenance", 0) or 0))
    except (TypeError, ValueError):
        return None
    # 帝国信仰产出:overview 没有这个字段,按各城信仰产出求和
    fpt = 0.0
    for c in cities or []:
        try:
            fpt += float(getattr(c, "faith", 0) or 0)
        except (TypeError, ValueError):
            continue

    bits: list[str] = []
    if fpt > 0 and faith >= max(HOARD_MIN, fpt * HOARD_TURNS):
        bits.append(
            f"信仰 {faith:.0f}(约 {faith / fpt:.0f} 回合产量)—— "
            f"可招募伟人、神权政体下可买兵、可买建筑"
        )
    # 攒金若是为大本钟,那是正确操作,不催
    saving_for_big_ben = bool(getattr(wm, "saving_for_big_ben", False))
    if not saving_for_big_ben and gpt > 0 and gold >= max(HOARD_MIN, gpt * HOARD_TURNS):
        # ⚠️ 报的是【可自由支配余额】,不是总额(2026-08-07 苏美尔局 T110 实测)。
        # 此前只说"该花",没说"留多少" —— 我照着提示连续金购+换政策卡,
        # 把余额压到 0,触发**国库破产 → 单位强制解散**(赔了一个披甲战士)。
        # 当时收入 +15/回合、维护仅 5/回合,本来完全健康:钱是被"花光"花掉的。
        # 安全垫按维护费算(金币为负时会强制解散单位来平账)。
        cushion = max(HOARD_MIN_CUSHION, maint * CUSHION_TURNS)
        spendable = gold - cushion
        if spendable > 0:
            bits.append(
                f"金币可自由支配 {spendable:.0f}"
                f"(总额 {gold:.0f} − 安全垫 {cushion:.0f})—— "
                f"可升级单位、金购区域/建筑、买地块。"
                f"**别花过安全垫**:金库见底会强制解散单位来平账"
            )
    if not bits:
        return None
    return lq.TurnEvent(
        priority=2,
        category="economy",
        message=(
            "存量囤积:"
            + ";".join(bits)
            + "。囤着不花是每回合的机会成本 —— 罗马局把信仰囤到 3338 才想起来用,"
            "整局的伟人竞价窗口都错过了。花什么由你定,但别忘了它存在"
        ),
    )


def _record_victory_eta(gs, turn: int, vp) -> None:
    """把本次胜利进度采样记入历史(E1 路线 ETA 的数据源)。

    挂在已有的 10 回合完整快照上 —— 零新增往返。采样间隔 10 回合对
    "还剩几十回合"这个量级足够,而且累积量本来就不该按回合抖动读。
    """
    try:
        from civ_mcp import victory_eta as ve

        cache = _expert_cache(gs)
        hist = cache.get("eta_history")
        if hist is None:
            hist = ve.ETAHistory()
            cache["eta_history"] = hist
        hist.record(turn, ve.rows_from_progress(vp))
    except Exception:
        log.debug("胜利 ETA 采样失败", exc_info=True)


def victory_eta_lines(gs, wm, my_eta_turns: int | None) -> list[str]:
    """路线 ETA 对比行。数据不足时返回空列表(诚实静默)。"""
    try:
        from civ_mcp import victory_eta as ve

        hist = _expert_cache(gs).get("eta_history")
        if hist is None:
            return []
        # 按 player_id 认自己 —— VictoryProgress.players 包含我方,
        # 按名字过滤会把自己报成"最快的对手"。单人局本方恒为 0,
        # 但仍读实际值,不写死。
        my_pid = int(getattr(gs, "_local_player_id", 0) or 0)
        etas: list[ve.VictoryETA] = []
        seen: set[tuple[str, str]] = set()
        for rec in hist.records:
            for row in rec.get("rows") or []:
                key = (row.get("who", ""), row.get("victory", ""))
                if key in seen:
                    continue
                seen.add(key)
                e = hist.eta(key[0], key[1])
                if e is not None:
                    etas.append(e)
        return ve.compare(
            my_eta_turns, getattr(wm, "victory_type", "") or "", etas, my_pid
        )
    except Exception:
        log.debug("路线 ETA 对比失败", exc_info=True)
        return []


def pending_decision_labels(notifications) -> list[str]:
    """游戏正在等玩家做的决策。纯函数,可测。

    2026-08-07 苏美尔局实测:回合迟迟不提交的真实原因**不只是**单位没下命令,
    还有"选择研究"/"选择市政"/"填补政策槽位"/"选择生产项目"。此前的实现只查
    单位移动力,那三类一个都抓不到 —— 于是照样空等 240 秒再报假 AI_TURN_HANG。

    权威源是游戏自己的通知:`GameNotification.is_action_required` 已经把
    "需要你决定"的项标好了(get_notifications 早就在返回,只是没人消费)。
    """
    out: list[str] = []
    for n in notifications or []:
        if not getattr(n, "is_action_required", False):
            continue
        label = str(getattr(n, "summary", "") or getattr(n, "name", "") or "").strip()
        if label:
            out.append(label)
    return out


async def _engine_turn_blocker(gs) -> tuple[bool, str]:
    """引擎权威的"回合为什么没走":(我方回合是否仍活跃, 阻塞项文案)。

    缺陷57(2026-08-09 希腊局 T11 实测):法典研完后引擎挂起
    「填补政策槽位」阻塞,`IsTurnActive=true`、`GetFirstEndTurnBlocking!=0`
    —— 回合根本没提交,而工具按 CPU 满载判据报 AI_TURN_HANG,还建议杀
    游戏重载存档。上一轮修的 `pending_decision_labels` 走的是解析器的
    `is_action_required` 启发式,这一类阻塞它认不出来;而引擎自己的
    `GetFirstEndTurnBlocking` 从来没人消费过 —— 又一个"数据已有、没人接线"。

    只在慢路径调用(一次 Lua 往返),返回 (False, "") 表示查不到时静默降级。
    """
    try:
        lines = await gs.conn.execute_write(
            "local me = Game.GetLocalPlayer(); "
            "local act = tostring(Players[me]:IsTurnActive()); "
            "local b = NotificationManager.GetFirstEndTurnBlocking(me); "
            "local msg = ''; "
            "if b and b ~= 0 then "
            "  local n = NotificationManager.Find(me, b); "
            "  if n then msg = tostring(n:GetMessage()) end; "
            "  if msg == '' or msg == 'nil' then "
            "    local ids = NotificationManager.GetList(me); "
            "    if ids then for _, id in ipairs(ids) do "
            "      local nn = NotificationManager.Find(me, id); "
            "      if nn then msg = msg .. tostring(nn:GetMessage()) .. '; ' end "
            "    end end "
            "  end "
            "end; "
            'print("BLOCKER|" .. act .. "|" .. tostring(b) .. "|" .. msg)'
        )
        for ln in lines:
            if ln.startswith("BLOCKER|"):
                parts = ln.split("|", 3)
                active = parts[1] == "true"
                blocking_id = parts[2] if len(parts) > 2 else "0"
                msg = parts[3].strip() if len(parts) > 3 else ""
                if not msg and blocking_id not in ("0", "-1", "nil"):
                    msg = f"引擎阻塞通知 id={blocking_id}(内容未导出)"
                return active, msg
    except Exception:
        log.debug("引擎阻塞查询失败", exc_info=True)
    return False, ""


async def _pending_decisions(gs) -> list[str]:
    """回合没提交的原因清单。查不到就返回空列表(退回旧的看门狗行为)。"""
    labels: list[str] = []
    try:
        labels = pending_decision_labels(await gs.get_notifications())
    except Exception:
        log.debug("待决策通知查询失败", exc_info=True)
    # 通知里没有"指挥单位"时,再用移动力兜底 —— 两个来源互补,
    # 通知偶尔滞后一帧,而移动力抓不到选科技那三类。
    try:
        for u in await gs.get_units() or []:
            if float(getattr(u, "moves_remaining", 0) or 0) <= 0:
                continue
            act = str(getattr(u, "activity", "") or "").upper()
            if any(k in act for k in ("FORTIFY", "SLEEP", "ALERT", "HEAL", "AUTOMATE")):
                continue
            name = str(getattr(u, "name", "") or getattr(u, "unit_type", "?") or "?")
            labels.append(
                f"单位待命令:{name}({getattr(u, 'x', '?')},{getattr(u, 'y', '?')})"
            )
    except Exception:
        log.debug("待命令单位查询失败", exc_info=True)
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def growth_stall_event(cities) -> lq.TurnEvent | None:
    """人口增长停滞 → 点名城市与原因。纯函数,可测。

    人口是分数大项,而且卡人口会连锁卡区域(区域数有人口门槛)——
    一座住房满了的城,后面每一项产出都跟着停。这件事没有任何被动提示,
    正是「感官缺失效应」要补的那类。

    ⚠️ 只用语义明确的两个字段:
      - housing:GetHousing() 的住房上限,人口 >= 上限即增长冻结
      - food_surplus:粮食净盈余,<= 0 即不再成长
    **不用 amenities** —— 该字段在 lua/cities.py 里是
    `GetAmenitiesNeeded() + GetAmenities()`(需求+持有的复合值),
    不是净值,拿它判"宜居不足"会得出错误结论。
    """
    stalled: list[str] = []
    for c in cities or []:
        try:
            pop = int(getattr(c, "population", 0) or 0)
            housing = float(getattr(c, "housing", 0) or 0)
            food = float(getattr(c, "food_surplus", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pop <= 0:
            continue
        name = str(getattr(c, "name", "?") or "?")
        if housing > 0 and pop >= housing:
            stalled.append(f"{name}(住房满 {pop}/{housing:.0f})")
        elif food <= 0:
            stalled.append(f"{name}(粮盈余 {food:+.1f})")
    if not stalled:
        return None
    return lq.TurnEvent(
        priority=2,
        category="growth",
        message=(
            "增长停滞:"
            + "、".join(stalled[:4])
            + (f" 等 {len(stalled)} 座" if len(stalled) > 4 else "")
            + " —— 人口既是分数大项也是区域解锁门槛。"
            "住房满:建水渠/邻里/改良农场;粮盈余为负:改市民到粮食格"
            "(set_city_focus food)或修被掠夺的农场"
        ),
    )


# 军事伟人靠【站位光环】生效(2 格内 +5 战力 +1 移动),不激活也在干活;
# 其余伟人必须 activate 才产生任何效果 —— 站着就是纯浪费。
_AURA_GREAT_PEOPLE = ("GREAT_GENERAL", "GREAT_ADMIRAL")


def idle_great_person_event(units) -> lq.TurnEvent | None:
    """场上有未激活的伟人 → 点名催用。纯函数,可测。

    2026-08-12 罗马 T384 实机:场上同时站着 2 个大科学家、2 个大作家、
    1 个大艺术家、1 个大音乐家、1 个大商人 —— 全部招募后从未激活。
    那局走的正是科学胜利,两个大科学家的尤里卡就那么放着。

    判据本身很硬:**伟人单位存在 = 还有充能没用**(充能耗尽单位即消失),
    所以不需要查充能数,看见就是待办。工具层早有 activate 入口,缺的
    只是「没人提醒」—— 这类『能做但没人说』的缺口,和感官缺失同源。
    """
    pending: list[str] = []
    aura: list[str] = []
    for u in units or []:
        utype = str(getattr(u, "unit_type", "") or "")
        if "GREAT_" not in utype:
            continue
        short = utype.replace("UNIT_", "")
        where = f"({getattr(u, 'x', '?')},{getattr(u, 'y', '?')})"
        if any(a in utype for a in _AURA_GREAT_PEOPLE):
            aura.append(f"{short}{where}")
        else:
            pending.append(f"{short}{where}")
    if not pending and not aura:
        return None
    parts: list[str] = []
    if pending:
        parts.append(
            "未激活伟人:"
            + "、".join(pending[:5])
            + (f" 等 {len(pending)} 个" if len(pending) > 5 else "")
            + " —— 伟人不激活等于没招募(充能用完才消失,还站着就是还没用)。"
            "移到对应区域后 unit_action(action='activate')"
        )
    if aura:
        parts.append(
            "光环伟人:"
            + "、".join(aura[:3])
            + " —— 靠站位生效(2 格内 +5 战力 +1 移动),确认它跟着主力部队,"
            "别留在后方空转"
        )
    return lq.TurnEvent(priority=2, category="great_people", message=" ‖ ".join(parts))


def dominance_stall_event(snap, units) -> lq.TurnEvent | None:
    """碾压档却停止扩张 → 催促兑现。纯函数,可测。

    2026-08-12 用户点破:罗马那局 T383 手握 3 架喷气式轰炸机、9 门火箭炮、
    11 个机械化步兵、满树科技、每回合 +379 金,统治进度却只有 3/9 ——
    「力量早就够了,缺的是把它花出去」。用户自己接手同类局面几十回合就
    推平了。

    档位文案「收官:直取胜利条件」太抽象,没有回答「这回合做什么」。
    这条事件补的正是那一句:优势每回合都在贬值(对手在追科技、你在付
    维护费),而它能换的城市数是有限窗口。
    """
    if snap is None:
        return None
    try:
        tier = int(getattr(snap, "tier", 0) or 0)
    except (TypeError, ValueError):
        return None
    if tier < 3:  # PowerTier.AHEAD
        return None
    if getattr(snap, "is_survival_mode", False):
        return None
    rate = getattr(snap, "my_expansion_rate", None)
    if rate is None or rate > 0.02:  # 近 20 回合有过新城 → 在兑现,不催
        return None
    air = [
        u
        for u in units or []
        if any(k in str(getattr(u, "unit_type", "")) for k in ("BOMBER", "FIGHTER"))
    ]
    melee = [
        u
        for u in units or []
        if int(getattr(u, "combat_strength", 0) or 0) > 0
        and int(getattr(u, "ranged_strength", 0) or 0) == 0
    ]
    if air:
        how = (
            f"你有 {len(air)} 架战机 —— **有几架就能同时开几条战线**:"
            f"飞机清空守军 → 近战进城 → 转场去下一条线。"
            f"手上近战 {len(melee)} 个,够占 {min(len(air), len(melee))} 座城。"
        )
    else:
        how = "选最近的一座敌方原始首都(统治只认原始首都),凑齐远程削墙+近战进城再动手。"
    return lq.TurnEvent(
        priority=1,
        category="military",
        message=(
            f"⚡ 优势在贬值:已是{'统治级' if tier >= 4 else '领先'}档,"
            f"但近 20 回合没有新增城市。军事优势是易腐品 —— "
            f"对手在追科技,你在付维护费。{how} "
            f"能算出「几十回合能推平」就该现在开始推,而不是先把基建补完。"
        ),
    )


def corps_formation_event(units, civics) -> lq.TurnEvent | None:
    """解锁了民族主义/动员且手上有同型单位可合编 → 提醒编队。纯函数,可测。

    2026-08-12 罗马 T384 实机:23 个军事单位,民族主义与动员都早已解锁,
    全程一次没编过队 —— 军团 +10、大军 +17 战力就那么放着。这是市政树
    白送的两次力量尖峰,同样的存量兵力合编完等于凭空涨一个时代代差。
    (工具入口同日补齐:unit_action 的 form_corps / form_army。)

    只在**真的有同型单位成对**时才响,免得变成每回合噪音。
    """
    civ_set = {str(c) for c in (civics or ())}
    has_army = any("MOBILIZATION" in c for c in civ_set)
    has_corps = has_army or any("NATIONALISM" in c for c in civ_set)
    if not has_corps:
        return None
    counts: dict[str, int] = {}
    for u in units or []:
        # ⚠️ 判据用 combat_strength 而不是 is_military —— _ScanUnit 的
        # __slots__ 里根本没有后者,getattr 会恒返回 False 把整条检测变成
        # 死代码(写这个函数时就踩了一次)。真实 UnitInfo 也有 combat 字段,
        # 两种来源都成立。
        try:
            if int(getattr(u, "combat_strength", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        utype = str(getattr(u, "unit_type", "") or "")
        # 已编队的单位在引擎里是同一个 UnitType,只是 MilitaryFormationType 不同;
        # 扫描拿不到编队等级,所以这里只按「有几个同型」给下限提示,不夸大。
        if utype and "GREAT_" not in utype:
            counts[utype] = counts.get(utype, 0) + 1
    pairs = [(t, n // 2) for t, n in counts.items() if n >= 2]
    if not pairs:
        return None
    pairs.sort(key=lambda p: -p[1])
    total = sum(n for _, n in pairs)
    label = "大军(+17)" if has_army else "军团(+10)"
    named = "、".join(f"{t.replace('UNIT_', '')}×{n * 2}" for t, n in pairs[:3])
    return lq.TurnEvent(
        priority=2,
        category="military",
        message=(
            f"可编队 {total} 组{label}:{named} —— "
            f"同型单位两两合编,战力约等于白涨一个时代的代差,"
            f"维护费还少一份。用 unit_action(action='"
            f"{'form_army' if has_army else 'form_corps'}', "
            f"target_x/y=另一个同型单位所在格)"
        ),
    )


def _check_infrastructure_gaps(cities: list) -> lq.TurnEvent | None:
    """基建缺口审计(2026-08-11 用户点名:「每个城市基础设施要健全,
    不能打完就什么都不管」)。

    见 city_infra 模块的说明:缺口一直以零散的低优先级提示出现,从没按城
    汇总过,于是本局两次结构性塌陷(GNP 垫底、科研被反超)都是攒出来的。
    """
    try:
        from civ_mcp import city_infra

        msg = city_infra.narrate(cities)
    except Exception:
        log.debug("基建缺口审计失败", exc_info=True)
        return None
    if not msg:
        return None
    return lq.TurnEvent(priority=2, category="city", message=msg)


async def _check_empire_warnings(
    gs: GameState,
    snap: lq.TurnSnapshot | None,
) -> tuple[list[lq.TurnEvent], int | None]:
    """Lightweight alerts that compensate for the Sensorium Effect.

    Surfaces information a human player would notice via passive visual cues:
    scoreboard position, idle trade routes, resource caps, loyalty crises,
    military imbalance, and gold deficits.

    Returns (events, score) where score is the current game score if available.
    """
    events: list[lq.TurnEvent] = []
    game_score: int | None = None

    # --- Loyalty crisis (from snapshot cities) ---
    if snap:
        for cs in snap.cities.values():
            if cs.loyalty_per_turn < -5:
                turns_left = (
                    int(cs.loyalty / abs(cs.loyalty_per_turn))
                    if cs.loyalty_per_turn < 0
                    else 99
                )
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="city",
                        message=(
                            f"LOYALTY CRISIS: {cs.name} losing {cs.loyalty_per_turn:+.1f}/t "
                            f"(loyalty {cs.loyalty:.0f}) — will rebel in ~{turns_left} turns!"
                        ),
                    )
                )
            elif cs.loyalty < 30 and cs.loyalty_per_turn < 0:
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="city",
                        message=f"LOYALTY WARNING: {cs.name} at {cs.loyalty:.0f} loyalty ({cs.loyalty_per_turn:+.1f}/t)",
                    )
                )

    # --- Resource cap (from snapshot stockpiles) ---
    if snap:
        for s in snap.stockpiles:
            net = s.per_turn - s.demand + s.imported
            if s.cap > 0 and s.amount >= s.cap and net > 0:
                events.append(
                    lq.TurnEvent(
                        priority=3,
                        category="economy",
                        message=(
                            f"RESOURCE CAP: {s.name} {s.amount}/{s.cap} ({net:+d}/t) "
                            f"— excess is wasted. Trade surplus or spend it."
                        ),
                    )
                )

    # --- Gold deficit (overview: 复用快照刚查过的,审查 F1-8) ---
    overview = getattr(gs, "_last_overview", None)
    if overview is None or snap is None or overview.turn != snap.turn:
        try:
            ov_lines = await gs.conn.execute_write(lq.build_overview_query())
            overview = lq.parse_overview_response(ov_lines)
        except Exception:
            log.debug("Overview query for warnings failed", exc_info=True)
            overview = None

    # 接线穿透:overview/库存 此前用后即弃,现在留给专家建议采集复用(0 新增往返)
    # 查询失败时保留上一回合的旧值(审查 F1-9):旧数据比 None 更接近真相 ——
    # 覆盖成 None 会让下游经济建议拿着 gold=0 自信地给出错误结论。
    if overview is not None:
        _expert_cache(gs)["overview"] = overview
    if snap is not None:
        _expert_cache(gs)["stockpiles"] = getattr(snap, "stockpiles", None) or []

    if overview:
        game_score = overview.score
        if (
            overview.gold_per_turn < 0
            and overview.gold < abs(overview.gold_per_turn) * 20
        ):
            turns_to_zero = (
                int(overview.gold / abs(overview.gold_per_turn))
                if overview.gold_per_turn < 0
                else 99
            )
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="economy",
                    message=(
                        f"DEFICIT: Gold {overview.gold_per_turn:+.0f}/t with {overview.gold:.0f} in treasury "
                        f"— bankrupt in ~{turns_to_zero} turns."
                    ),
                )
            )
        _burden = maintenance_burden_event(overview, _expert_cache(gs).get("trade_cap"))
        if _burden:
            events.append(_burden)
        _obsolete = army_obsolescence_event(
            list(snap.units.values()) if snap else [], overview.era_name
        )
        if _obsolete:
            events.append(_obsolete)
        # 注:增长停滞挂在下面的 cities_list(CityInfo)上,不在这里 ——
        # snap.cities 是 CitySnapshot,**没有 housing 字段**,在这里判
        # 会让住房上限那一支永远静默(只剩粮食那一支起作用)。

    # --- Idle trade routes (2026-08-03 降频:贸易容量只在解锁新商业中心/
    #     港口或造出商人时变,每 3 回合足够;闲置路线不会因为晚一回合发现而消失)
    try:
        _tc = _expert_cache(gs)
        _tturn = snap.turn if snap else 0
        if (
            _tc.get("trade_lines") is not None
            and _tturn - _tc.get("trade_turn", -99) < 3
        ):
            tr_lines = _tc["trade_lines"]
        else:
            tr_lines = await gs.conn.execute_write(lq.build_trade_capacity_check())
            _tc["trade_lines"], _tc["trade_turn"] = tr_lines, _tturn
        for line in tr_lines:
            if line.startswith("TRCAP|"):
                parts = line.split("|")
                cap, active = int(parts[1]), int(parts[2])
                idle = cap - active
                _expert_cache(gs)["trade_cap"] = (cap, active)  # 接线穿透
                if idle > 0:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="economy",
                            message=(
                                f"IDLE TRADE ROUTE: {idle} unused route "
                                f"{'capacity' if idle == 1 else 'capacities'} "
                                f"({active}/{cap} active). Build a Trader or assign an idle one."
                            ),
                        )
                    )
                break
    except Exception:
        log.debug("Trade capacity check failed", exc_info=True)

    # --- Scoreboard + military disparity (rival snapshot, every 5 turns) ---
    turn = snap.turn if snap else 0
    if turn > 0 and turn % 5 == 0:
        try:
            rival_lines = await gs.conn.execute_write(lq.build_rival_snapshot_query())
            # 本回合的原始响应留给 measure_power 复用 —— 每 10 回合的局势测量
            # 与这里的每 5 回合快照必然撞在同一回合,重复查是纯浪费。
            _expert_cache(gs)["rival_lines"] = (turn, rival_lines)
            rivals = lq.parse_rival_snapshot_response(rival_lines)
            if rivals and overview:
                our_sci = overview.science_yield
                # Compute science rankings
                all_sci = [(r.name, r.sci) for r in rivals] + [("You", our_sci)]
                all_sci.sort(key=lambda x: x[1], reverse=True)
                our_rank = next(i + 1 for i, (n, _) in enumerate(all_sci) if n == "You")
                leader_name, leader_sci = all_sci[0]
                if our_rank > 1 and len(all_sci) > 2:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="scoreboard",
                            message=(
                                f"SCOREBOARD: Your science ({our_sci:.1f}/t) ranks "
                                f"{our_rank} of {len(all_sci)}. "
                                f"Leader: {leader_name} at {leader_sci:.1f}/t."
                            ),
                        )
                    )

                # Military disparity
                our_mil_lines = await gs.conn.execute_read(
                    "local me = Game.GetLocalPlayer(); "
                    "print(Players[me]:GetStats():GetMilitaryStrength()); "
                    'print("---END---")'
                )
                our_mil = 0
                if our_mil_lines:
                    try:
                        our_mil = int(float(our_mil_lines[0]))
                    except (ValueError, IndexError):
                        pass
                if our_mil > 0:
                    for r in rivals:
                        if r.mil >= our_mil * 2:
                            events.append(
                                lq.TurnEvent(
                                    priority=2,
                                    category="military",
                                    message=(
                                        f"MILITARY WARNING: {r.name} has {r.mil} military "
                                        f"({r.mil / our_mil:.1f}x ours at {our_mil})."
                                    ),
                                )
                            )
        except Exception:
            log.debug("Rival snapshot for warnings failed", exc_info=True)

    return events, game_score


def _check_save_scumming(gs: GameState) -> tuple[list[lq.TurnEvent], bool]:
    """Detect save-scumming patterns from recent save load history.

    Benchmark runs should play forward — save loads are only legitimate for
    recovering from engine hangs or loading the initial scenario. Repeated
    loads across different turns indicate the agent is rolling back to retry
    unfavorable outcomes.

    Thresholds (tuned against Opus T326 legitimate deadlock debugging and
    Gemini's 19-load scumming run):
      - MINOR warn: 3+ loads across 3+ distinct turns (span >= 10)
      - STRONG warn: 5+ loads across 5+ distinct turns (span >= 20)
      - HARD STOP: 8+ loads across 8+ distinct turns (span >= 30)

    The "distinct turns" signal is critical — 25 loads all at T326 is a
    deadlock, 25 loads spread across T100-T300 is scumming.

    Returns (events, hard_stop).
    """
    events: list[lq.TurnEvent] = []
    history = gs._save_load_history

    if len(history) < 3:
        return events, False

    # Only consider in-play loads (high water turn > 0)
    play_loads = [(ts, turn, name) for ts, turn, name in history if turn > 0]
    if len(play_loads) < 3:
        return events, False

    n_loads = len(play_loads)
    distinct_turns = sorted({turn for _, turn, _ in play_loads})
    n_distinct = len(distinct_turns)
    span = distinct_turns[-1] - distinct_turns[0] if distinct_turns else 0

    # Hard stop — abort the run
    if n_loads >= 8 and n_distinct >= 8 and span >= 30:
        events.append(
            lq.TurnEvent(
                priority=1,
                category="abuse",
                message=(
                    f"!!! RUN ABORTED — save scumming threshold exceeded. "
                    f"{n_loads} save loads across {n_distinct} distinct turns "
                    f"(span {span}). Benchmark runs must play forward from a "
                    f"single starting save. Repeated save loads to retry "
                    f"turns are considered cheating and invalidate the run. "
                    f"No further actions will be processed."
                ),
            )
        )
        return events, True

    # Strong warning
    if n_loads >= 5 and n_distinct >= 5 and span >= 20:
        events.append(
            lq.TurnEvent(
                priority=1,
                category="abuse",
                message=(
                    f"SAVE SCUMMING CRITICAL: {n_loads} save loads across "
                    f"{n_distinct} distinct turns (span {span}). STOP loading "
                    f"saves — this is a benchmark run. Play forward from the "
                    f"current state. The next load will abort the run."
                ),
            )
        )
        return events, False

    # Soft warning
    if n_loads >= 3 and n_distinct >= 3 and span >= 10:
        events.append(
            lq.TurnEvent(
                priority=2,
                category="abuse",
                message=(
                    f"SAVE SCUMMING WARNING: {n_loads} save loads across "
                    f"{n_distinct} different turns. Benchmark runs must play "
                    f"forward — save loads are only for recovering from engine "
                    f"hangs. Continuing to reload will result in disqualification."
                ),
            )
        )

    return events, False


async def execute_end_turn(gs: GameState) -> str:
    """End the turn with snapshot-diff event detection.

    2026-08-03:全程分阶段计时,结果附在返回值末尾的 ⏱ 行。
    用户报告「end_turn 要好几分钟」,而按往返次数估算只有 ~6 秒 ——
    差了两个数量级,说明瓶颈不在查询数量而在某个等待。埋点是为了
    把「好几分钟」拆成可定位的段,而不是继续凭直觉优化。
    """
    gs._phase_timings = {}
    gs._t_enter = time.monotonic()
    # 0a. Run aborted due to save scumming — refuse to advance
    if gs._run_aborted:
        return (
            "RUN ABORTED — save scumming threshold exceeded. "
            "This benchmark run has been invalidated because the agent "
            "loaded saves across too many distinct turns. Benchmark runs "
            "must play forward from a single starting save. No further "
            "actions will be processed."
        )

    # 政体变更标记在回合推进时清掉 —— 它只用于拦本回合内的 set_policies
    # (共用待确认变更集,排卡会撤销未结算的政体变更)。
    gs._pending_government = None

    # 0. Game-over check — don't try to advance a finished game
    gameover = await gs.check_game_over()
    if gameover is not None:
        gs._pending_end_turn = False
        gs._pending_end_turn_from = None
        gs._last_game_over = gameover
        vtype = gameover.victory_type.replace("VICTORY_", "").replace("_", " ").title()
        if gameover.is_defeat:
            return (
                f"GAME OVER — DEFEAT. {gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory. "
                f"The game has ended. No further actions are possible."
            )
        else:
            return (
                f"GAME OVER — VICTORY! You won a {vtype} victory! The game has ended."
            )

    # Record turn number at entry so we can detect external advancement
    # (e.g. game auto-ends turn when skip_remaining_units finishes all moves)
    turn_at_entry = await _get_turn_number(gs)

    # ---- Cross-process end-turn guard (2026-08-05) ----
    # mcp_call spawns a fresh server process per batch, and the FireTuner port
    # races between processes. If ACTION_ENDTURN was sent and the turn
    # advanced but the response was lost, a retried request in a new process
    # would send ACTION_ENDTURN AGAIN and burn a fresh turn. The on-disk
    # marker lets a new process detect an already-advanced turn and report
    # instead of re-sending.
    marker = etg.read_marker()
    sent_action = False  # True only if THIS call sends ACTION_ENDTURN
    if marker:
        guard_from = marker["from_turn"]
        guard_age = time.time() - marker["written_at"]
        if turn_at_entry is not None and turn_at_entry > guard_from:
            # 缺陷52(2026-08-09):pending marker 也要有年龄上限。挂死→杀游戏→
            # 手动重载后落在 turn+1,几小时前的 pending marker 会把这次全新的
            # end_turn 误判成丢响应重试,静默吞掉提交(假报告"372->373")。
            if (marker["advanced"] and guard_age >= etg.ADVANCED_GRACE_SECONDS) or (
                not marker["advanced"]
                and guard_age >= etg.PENDING_ADVANCE_STALE_SECONDS
            ):
                # Normal new-turn call: the previous end_turn completed long
                # ago, the agent has seen the report and played this turn.
                # The marker is history -- discard and proceed normally.
                etg.clear()
                log.info(
                    "end_turn guard: marker from turn %s aged %.0fs; clearing",
                    guard_from,
                    guard_age,
                )
            else:
                # Previous end_turn already advanced the game and the response
                # was likely lost (retry in a fresh process). Report without
                # re-sending, or the fresh turn gets burned.
                etg.clear()
                gs._pending_end_turn = True
                gs._pending_end_turn_from = guard_from
                log.info(
                    "end_turn guard: turn already advanced %s -> %s; "
                    "reporting without re-sending",
                    guard_from,
                    turn_at_entry,
                )
        elif (
            turn_at_entry is not None
            and turn_at_entry == guard_from
            and guard_age < etg.STALE_AFTER_SECONDS
        ):
            # Previous ACTION_ENDTURN may still be in flight (AI turn in
            # progress). Re-enter the pending flow: poll, never re-send.
            gs._pending_end_turn = True
            gs._pending_end_turn_from = guard_from
            log.info(
                "end_turn guard: ACTION_ENDTURN still in flight "
                "(from turn %s, %.0fs ago); polling",
                guard_from,
                guard_age,
            )
        elif turn_at_entry is not None and turn_at_entry == guard_from:
            # Marker stale and turn unchanged: previous send presumed dead.
            etg.clear()
            log.info(
                "end_turn guard: stale marker (turn %s, %.0fs ago); clearing",
                guard_from,
                guard_age,
            )
        else:
            # Turn regressed (different save loaded) -- discard stale marker.
            etg.clear()
            log.info(
                "end_turn guard: turn %s vs marker %s; discarding",
                turn_at_entry,
                guard_from,
            )

    # 0.5. Force-clean stale diplomacy sessions + restore UI before advancing.
    #      Stale sessions (war declarations / peace deals whose cleanup task
    #      died with the mcp_call process) leave DiplomacyActionView stuck in
    #      OVERVIEW_MODE, which blocks later RequestSession calls and can
    #      wedge the AI turn (measured 5x CPU-pegged infinite loops, all
    #      following diplomatic actions). This sweep is idempotent and safe:
    #      sessions the AI cares about re-open on its own turn.
    # 审查 2026-08-05 F6:上一 ACTION_ENDTURN 仍在途(重入)时,跳过整个
    # 预发送阶段(0.5-3)。这些步骤全是 InGame 查询/弹窗清理 —— 在 AI 回合
    # 处理中发 InGame 查询正是下方 CRITICAL 注释里量到 5 次 CPU 卡死的根因;
    # 而且重入时我们只需要轮询推进,блocker 清理毫无意义。
    if not gs._pending_end_turn:
        try:
            await gs.conn.execute_write(lq.build_cleanup_stale_sessions())
        except Exception:
            log.warning("Pre-turn stale session cleanup failed", exc_info=True)

    # 1. Diplomacy sessions block turn advancement — auto-resolve them.
    #    Encounters (first meets, denouncements, agenda chatter) get POSITIVE
    #    responses; war declarations are dismissed; incoming trade deals are
    #    left open and reported with actionable player IDs.
    sessions = [] if gs._pending_end_turn else await gs.get_diplomacy_sessions()
    if sessions:
        from civ_mcp.game_lifecycle import auto_resolve_diplomacy_sessions

        actions = await auto_resolve_diplomacy_sessions(gs.conn)
        if actions:
            log.info("Auto-resolved pre-turn diplomacy: %s", "; ".join(actions))
        sessions = await gs.get_diplomacy_sessions()
    if sessions:
        session_info = []
        for s in sessions:
            phase = (
                "deal"
                if s.deal_summary
                else ("goodbye" if s.buttons == "GOODBYE" else "active")
            )
            session_info.append(
                f"{s.other_civ_name} ({s.other_leader_name}) "
                f"[player_id={s.other_player_id}, {phase}]"
            )
        return (
            f"Cannot end turn: diplomacy session pending with {', '.join(session_info)}. "
            f"For deals use get_pending_trades + respond_to_trade(other_player_id=..., accept=...); "
            f"otherwise respond_to_diplomacy(other_player_id=..., response='POSITIVE')."
        )

    # 1b. Check for incoming trade deal offers (e.g. delegations from other civs)
    try:
        deals = [] if gs._pending_end_turn else await gs.get_pending_deals()
        if deals:
            return (
                "Cannot end turn: incoming trade deal pending.\n"
                + nr.narrate_pending_deals(deals)
            )
    except Exception:
        log.debug("Pending deal check failed", exc_info=True)

    # 2. Pre-dismiss any ExclusivePopupManager popups (wonder, disaster, era)
    # that may hold engine locks blocking turn advancement.
    if not gs._pending_end_turn:
        try:
            pre_dismiss = await gs.dismiss_popup()
            if "Dismissed" in pre_dismiss:
                log.info("Pre-turn popup dismissed: %s", pre_dismiss)
        except Exception:
            log.debug("Pre-turn dismiss failed", exc_info=True)

    # 2b. World Congress gate — if WC fires this turn and no handler is
    #     registered, block end_turn and tell the agent to vote first.
    #     The WC session opens+closes within ACTION_ENDTURN synchronously,
    #     so we MUST register a handler BEFORE sending ACTION_ENDTURN.
    try:
        wc_status = None if gs._pending_end_turn else await gs.get_world_congress()
        if wc_status is not None and (
            wc_status.turns_until_next <= 0 or wc_status.is_in_session
        ):
            n_res = len(wc_status.resolutions) if wc_status.resolutions else 0
            # Skip gate when WC fires with 0 resolutions — nothing to vote on
            if n_res == 0 and not wc_status.is_in_session:
                log.info("WC fires this turn with 0 resolutions — auto-proceeding")
            else:
                handler_lines = await gs.conn.execute_write(
                    f'print(__civmcp_wc_handler and "HANDLER_SET" or "NO_HANDLER"); '
                    f'print("{lq.SENTINEL}")'
                )
                handler_set = any("HANDLER_SET" in l for l in handler_lines)
                if not handler_set:
                    return (
                        f"World Congress fires this turn ({n_res} resolution(s), {wc_status.favor} favor). "
                        f"Use get_world_congress() to review resolutions and targets, "
                        f"then queue_wc_votes() to register your votes, "
                        f"then call end_turn() again."
                    )
    except Exception:
        log.debug("WC imminence check failed", exc_info=True)

    # 3. Check ALL EndTurnBlocking notifications at once, auto-resolve soft
    #    blockers, and report remaining hard blockers in a single message.
    #    (重入时 range(0) —— 见上方 F6 注释。)
    for _round in range(0 if gs._pending_end_turn else 3):
        try:
            blocking_lines = await gs.conn.execute_write(
                lq.build_end_turn_blocking_query()
            )
            blockers = lq.parse_end_turn_blocking(blocking_lines)
            if not blockers:
                break  # nothing blocking

            resolved_any = False
            hard_blockers: list[tuple[str, str]] = []

            for blocking_type, blocking_msg in blockers:
                # --- Auto-resolvable soft blockers ---

                if blocking_type == "ENDTURN_BLOCKING_GOVERNOR_IDLE":
                    await gs.conn.execute_write(
                        f"local me = Game.GetLocalPlayer(); "
                        f"local list = NotificationManager.GetList(me); "
                        f"if list then "
                        f"  for _, nid in ipairs(list) do "
                        f"    local e = NotificationManager.Find(me, nid); "
                        f"    if e and not e:IsDismissed() then "
                        f"      local bt = e:GetEndTurnBlocking(); "
                        f"      if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_GOVERNOR_IDLE then "
                        f"        pcall(function() NotificationManager.SendActivated(me, nid) end); "
                        f"        pcall(function() NotificationManager.Dismiss(me, nid) end) "
                        f"      end "
                        f"    end "
                        f"  end "
                        f"end; "
                        f'print("OK"); print("{lq.SENTINEL}")'
                    )
                    resolved_any = True
                    continue

                if blocking_type == "ENDTURN_BLOCKING_CONSIDER_GOVERNMENT_CHANGE":
                    # 2026-08-08 T193-T216 政策卡幽灵抹除的根因:政体与政策共用
                    # 一个【待定变更集】(fix 21 已证)。新政体解锁后这个 blocker
                    # 每回合出现,这里每回合调 SetGovernmentChangeConsidered(true)
                    # 关掉它 —— 而这个调用会把待定集整个丢弃,把本回合 set_policies
                    # 刚排上、尚未跨回合结算的卡一并回滚。城市规划卡 T193/T197/
                    # T200/T214/T215 五次入槽五次消失,全是它干的。
                    # 修法与 fix(21) 同款:标记"已考虑"之前先无参提交待定集。
                    await gs.conn.execute_write(
                        f"local me = Game.GetLocalPlayer(); "
                        f"local pc = Players[me]:GetCulture(); "
                        f"pcall(function() pc:RequestPolicyChanges({{}}, {{}}) end); "
                        f"pc:SetGovernmentChangeConsidered(true); "
                        f'print("OK"); print("{lq.SENTINEL}")'
                    )
                    resolved_any = True
                    continue

                if blocking_type == "ENDTURN_BLOCKING_WORLD_CONGRESS_LOOK":
                    await gs.conn.execute_write(
                        f"local me = Game.GetLocalPlayer(); "
                        f"UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_LOOKED_AT_AVAILABLE, {{}}); "
                        f"local list = NotificationManager.GetList(me); "
                        f"if list then "
                        f"  for _, nid in ipairs(list) do "
                        f"    pcall(function() "
                        f"      local e = NotificationManager.Find(me, nid); "
                        f"      if e and not e:IsDismissed() then "
                        f"        local bt = e:GetEndTurnBlocking(); "
                        f"        if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_WORLD_CONGRESS_LOOK then "
                        f"          NotificationManager.Dismiss(me, nid) "
                        f"        end "
                        f"      end "
                        f"    end) "
                        f"  end "
                        f"end; "
                        f'local i = ContextPtr:LookUpControl("/InGame/WorldCongressIntro"); '
                        f"if i then i:SetHide(true) end; "
                        f'local p = ContextPtr:LookUpControl("/InGame/WorldCongressPopup"); '
                        f"if p then p:SetHide(true) end; "
                        f'print("OK"); print("{lq.SENTINEL}")'
                    )
                    resolved_any = True
                    continue

                if blocking_type == "ENDTURN_BLOCKING_WORLD_CONGRESS_SESSION":
                    # NEVER auto-resolve session blockers — the agent must
                    # call get_world_congress() and queue_wc_votes()
                    # to deploy diplomatic favor strategically.
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # Catch-all for any other World Congress blocking types
                # (e.g. special session proposals, emergency discussions)
                # Replicates the game UI's "Pass" button: LOOKED_AT_AVAILABLE
                # + dismiss all WC-related blocking notifications.
                if "WORLD_CONGRESS" in blocking_type:
                    try:
                        wc_dismiss_lines = await gs.conn.execute_write(
                            f"local me = Game.GetLocalPlayer(); "
                            f"UI.RequestPlayerOperation(me, PlayerOperations.WORLD_CONGRESS_LOOKED_AT_AVAILABLE, {{}}); "
                            f"local dismissed = 0; "
                            f"local list = NotificationManager.GetList(me); "
                            f"if list then "
                            f"  for _, nid in ipairs(list) do "
                            f"    pcall(function() "
                            f"      local e = NotificationManager.Find(me, nid); "
                            f"      if e and not e:IsDismissed() then "
                            f"        local bt = e:GetEndTurnBlocking(); "
                            f"        if bt and bt ~= 0 then "
                            f"          for k, v in pairs(EndTurnBlockingTypes) do "
                            f'            if v == bt and k:find("WORLD_CONGRESS") then '
                            f"              NotificationManager.Dismiss(me, nid); "
                            f"              dismissed = dismissed + 1; "
                            f"              break "
                            f"            end "
                            f"          end "
                            f"        end "
                            f"      end "
                            f"    end) "
                            f"  end "
                            f"end; "
                            f'local i = ContextPtr:LookUpControl("/InGame/WorldCongressIntro"); '
                            f"if i then i:SetHide(true) end; "
                            f'local p = ContextPtr:LookUpControl("/InGame/WorldCongressPopup"); '
                            f"if p then p:SetHide(true) end; "
                            f'print("DISMISSED:" .. dismissed); print("{lq.SENTINEL}")'
                        )
                        if any(
                            "DISMISSED:" in l and not l.endswith(":0")
                            for l in wc_dismiss_lines
                        ):
                            resolved_any = True
                            log.info("Auto-dismissed WC blocker: %s", blocking_type)
                            continue
                    except Exception:
                        log.debug("WC catch-all auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_CONSIDER_DISLOYAL_CITY":
                    try:
                        result = await gs.resolve_city_capture("keep")
                        if "Error" not in result:
                            log.info("Auto-kept disloyal city: %s", result)
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Disloyal city auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_CONSIDER_RAZE_CITY":
                    try:
                        result = await gs.resolve_city_capture("keep")
                        if "Error" not in result:
                            log.info("Auto-kept captured city: %s", result)
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Captured city auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_GIVE_INFLUENCE_TOKEN":
                    try:
                        envoy_lines = await gs.conn.execute_write(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local inf = Players[me]:GetInfluence(); "
                            f"local tokens = inf:GetTokensToGive(); "
                            f"if tokens == 0 then "
                            f"  inf:SetGivingTokensConsidered(true); "
                            f'  print("AUTO_RESOLVED"); '
                            f'else print("HAS_TOKENS|" .. tokens); end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        if any("AUTO_RESOLVED" in l for l in envoy_lines):
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Envoy auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                if blocking_type == "ENDTURN_BLOCKING_PRODUCTION":
                    try:
                        corruption_lines = await gs.conn.execute_write(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local corrupted = {{}}; "
                            f"for i, c in Players[me]:GetCities():Members() do "
                            f"  local bq = c:GetBuildQueue(); "
                            f"  if bq:GetSize() > 0 and bq:GetCurrentProductionTypeHash() == 0 then "
                            f'    table.insert(corrupted, Locale.Lookup(c:GetName()) .. " (id:" .. c:GetID() .. ")") '
                            f"  end "
                            f"end; "
                            f"if #corrupted > 0 then "
                            f'  print("CORRUPTED|" .. table.concat(corrupted, ",")) '
                            f'else print("CLEAN") end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        is_corrupted = any(
                            cl.startswith("CORRUPTED|") for cl in corruption_lines
                        )
                        if is_corrupted:
                            city_names = next(
                                cl.split("|", 1)[1]
                                for cl in corruption_lines
                                if cl.startswith("CORRUPTED|")
                            )
                            dismiss_lines = await gs.conn.execute_write(
                                f"local me = Game.GetLocalPlayer(); "
                                f"local dismissed = 0; "
                                f"local list = NotificationManager.GetList(me); "
                                f"if list then "
                                f"  for _, nid in ipairs(list) do "
                                f"    local e = NotificationManager.Find(me, nid); "
                                f"    if e and not e:IsDismissed() then "
                                f"      local bt = e:GetEndTurnBlocking(); "
                                f"      if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_PRODUCTION then "
                                f"        NotificationManager.Dismiss(me, nid); dismissed = dismissed + 1 "
                                f"      end "
                                f"    end "
                                f"  end "
                                f"end; "
                                f'print("DISMISSED|" .. dismissed); '
                                f'print("{lq.SENTINEL}")'
                            )
                            if any(
                                "DISMISSED|" in l and not l.endswith("|0")
                                for l in dismiss_lines
                            ):
                                log.info(
                                    "Auto-dismissed corrupted production for: %s",
                                    city_names,
                                )
                                resolved_any = True
                                continue
                    except Exception:
                        log.debug("Corruption check failed", exc_info=True)

                    # Empty-queue detection: RequestOperation can silently
                    # no-op, leaving cities with size==0 queues that block
                    # turn advancement. Name them in the blocker so the
                    # agent doesn't have to round-trip get_cities.
                    try:
                        empty_lines = await gs.conn.execute_write(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local empty = {{}}; "
                            f"for i, c in Players[me]:GetCities():Members() do "
                            f"  local bq = c:GetBuildQueue(); "
                            f"  if bq:GetSize() == 0 then "
                            f'    table.insert(empty, Locale.Lookup(c:GetName()) .. " (id:" .. c:GetID() .. ")") '
                            f"  end "
                            f"end; "
                            f"if #empty > 0 then "
                            f'  print("EMPTY|" .. table.concat(empty, ", ")) '
                            f'else print("CLEAN") end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        empty_cities = next(
                            (
                                el.split("|", 1)[1]
                                for el in empty_lines
                                if el.startswith("EMPTY|")
                            ),
                            None,
                        )
                        if empty_cities:
                            blocking_msg = (
                                f"Production — empty queue in {empty_cities}. "
                                f"Set production with set_city_production then "
                                f"retry end_turn."
                            )
                    except Exception:
                        log.debug("Empty-queue check failed", exc_info=True)

                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Stale research/civic notifications ---
                # If tech/civic is already set but the notification persists,
                # force-dismiss it (set_research may have been called but
                # the notification wasn't cleared — e.g. before MCP restart).
                if blocking_type in (
                    "ENDTURN_BLOCKING_RESEARCH",
                    "ENDTURN_BLOCKING_CIVIC",
                ):
                    try:
                        dismiss_lua = (
                            f"local me = Game.GetLocalPlayer() "
                            f"local pTechs = Players[me]:GetTechs() "
                            f"local pCulture = Players[me]:GetCulture() "
                            f"local researching = pTechs:GetResearchingTech() "
                            f"local civicing = pCulture:GetProgressingCivic() "
                            f"local isSet = false "
                            f'if "{blocking_type}" == "ENDTURN_BLOCKING_RESEARCH" and researching >= 0 then isSet = true end '
                            f'if "{blocking_type}" == "ENDTURN_BLOCKING_CIVIC" and civicing >= 0 then isSet = true end '
                            f"if isSet then "
                            f"  local list = NotificationManager.GetList(me) "
                            f"  if list then "
                            f"    for _, nid in ipairs(list) do "
                            f"      local e = NotificationManager.Find(me, nid) "
                            f"      if e and not e:IsDismissed() then "
                            f"        local bt = e:GetEndTurnBlocking() "
                            f"        if bt and bt == EndTurnBlockingTypes.{blocking_type} then "
                            f"          pcall(function() NotificationManager.SendActivated(me, nid) end) "
                            f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                            f"        end "
                            f"      end "
                            f"    end "
                            f"  end "
                            f'  print("AUTO_CLEARED") '
                            f'else print("NOT_SET") end '
                            f'print("{lq.SENTINEL}")'
                        )
                        result_lines = await gs.conn.execute_write(dismiss_lua)
                        if any("AUTO_CLEARED" in l for l in result_lines):
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug(
                            "Research/civic notification auto-clear failed",
                            exc_info=True,
                        )
                    # Research/civic was unset — add diagnostic hint
                    kind = "tech" if "RESEARCH" in blocking_type else "civic"
                    enhanced_msg = (
                        (
                            f"{blocking_msg} (no {kind} selected — "
                            f"this can happen after diplomacy events or tech completion)"
                        )
                        if blocking_msg
                        else (
                            f"No {kind} selected — "
                            f"this can happen after diplomacy events or tech completion"
                        )
                    )
                    hard_blockers.append((blocking_type, enhanced_msg))
                    continue

                # --- Stale promotion notifications ---
                # GameCore SetPromotion doesn't consume XP or advance level,
                # so CanPromote() perpetually returns TRUE. Use XP-threshold
                # formula (matching promote_unit's post-promote dismiss) to
                # determine if any unit genuinely has enough XP for another
                # promotion: needed = T1 * (promoCount+1) * (promoCount+2) / 2
                if blocking_type == "ENDTURN_BLOCKING_UNIT_PROMOTION":
                    try:
                        # Step 1 (GameCore): Check XP formula AND zero out stored
                        # promotions on units that don't genuinely need one.
                        # ChangeStoredPromotions zeroes the engine counter that
                        # causes the blocker to regenerate after Dismiss().
                        check_lines = await gs.conn.execute_read(
                            f"local me = Game.GetLocalPlayer(); "
                            f"local anyNeed = false; "
                            f"local cleared = 0; "
                            f"for i, u in Players[me]:GetUnits():Members() do "
                            f"  if u:GetX() ~= -9999 then "
                            f"    local ok, exp = pcall(function() return u:GetExperience() end); "
                            f"    if ok and exp then "
                            f"      local ui = GameInfo.Units[u:GetType()]; "
                            f'      local promClass = ui and ui.PromotionClass or ""; '
                            f'      if promClass ~= "" then '
                            f"        local promoCount = 0; "
                            f"        for p in GameInfo.UnitPromotions() do "
                            f"          if p.PromotionClass == promClass and exp:HasPromotion(p.Index) then "
                            f"            promoCount = promoCount + 1 "
                            f"          end "
                            f"        end; "
                            f"        local t1 = exp:GetExperienceForNextLevel(); "
                            f"        local xp = exp:GetExperiencePoints(); "
                            f"        local needed = t1 * (promoCount + 1) * (promoCount + 2) / 2; "
                            f"        if xp >= needed then "
                            f"          anyNeed = true "
                            f"        else "
                            f"          local stored = 0; "
                            f"          pcall(function() stored = exp:GetStoredPromotions() end); "
                            f"          if stored > 0 then "
                            f"            pcall(function() exp:ChangeStoredPromotions(-stored) end); "
                            f"            cleared = cleared + 1 "
                            f"          end "
                            f"        end "
                            f"      end "
                            f"    end "
                            f"  end "
                            f"end; "
                            f'print(anyNeed and "NEEDS_PROMO" or ("NO_PROMO_NEEDED|cleared=" .. cleared)); '
                            f'print("{lq.SENTINEL}")'
                        )
                        needs_promo = any(
                            "NEEDS_PROMO" == l.strip() for l in check_lines
                        )
                        log.debug(
                            "Promotion blocker: needs_promo=%s (check=%s)",
                            needs_promo,
                            [l for l in check_lines if "PROMO" in l or "cleared" in l],
                        )
                        if not needs_promo:
                            # Step 2: InGame dismiss — NotificationManager is InGame-only.
                            # Dismiss BOTH the end-turn blocker AND the regular notification
                            # (NOTIFICATION_UNIT_PROMOTION_AVAILABLE) which is a separate
                            # object that regenerates every turn due to stale CanPromote().
                            await gs.conn.execute_write(
                                f"local me = Game.GetLocalPlayer(); "
                                f"local list = NotificationManager.GetList(me); "
                                f"if list then "
                                f"  for _, nid in ipairs(list) do "
                                f"    local e = NotificationManager.Find(me, nid); "
                                f"    if e and not e:IsDismissed() then "
                                f"      local bt = e:GetEndTurnBlocking(); "
                                f"      if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_UNIT_PROMOTION then "
                                f"        pcall(function() NotificationManager.SendActivated(me, nid) end); "
                                f"        pcall(function() NotificationManager.Dismiss(me, nid) end) "
                                f"      else "
                                f"        local tn = ''; "
                                f"        pcall(function() tn = e:GetTypeName() end); "
                                f"        if tn == 'NOTIFICATION_UNIT_PROMOTION_AVAILABLE' then "
                                f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                                f"        end "
                                f"      end "
                                f"    end "
                                f"  end "
                                f"end; "
                                f'print("{lq.SENTINEL}")'
                            )
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug(
                            "Promotion notification auto-clear failed", exc_info=True
                        )
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Units blocking: auto-skip if all have 0 moves ---
                if blocking_type == "ENDTURN_BLOCKING_UNITS":
                    try:
                        check_lua = (
                            f"local me = Game.GetLocalPlayer(); "
                            f"local anyMoves = false; "
                            f"for _, u in Players[me]:GetUnits():Members() do "
                            f"  if u:GetX() ~= -9999 and u:GetMovesRemaining() > 0 then "
                            f"    anyMoves = true; break end end; "
                            f"if not anyMoves then "
                            f"  for _, u in Players[me]:GetUnits():Members() do "
                            f"    if u:GetX() ~= -9999 then UnitManager.FinishMoves(u) end "
                            f'  end; print("AUTO_SKIPPED") '
                            f'else print("UNITS_NEED_ORDERS") end; '
                            f'print("{lq.SENTINEL}")'
                        )
                        skip_lines = await gs.conn.execute_read(check_lua)
                        if any("AUTO_SKIPPED" in l for l in skip_lines):
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Auto-skip 0-move units failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Spy escape route: auto-pick fastest district ---
                if blocking_type == "ENDTURN_BLOCKING_SPY_CHOOSE_ESCAPE_ROUTE":
                    try:
                        escape_lines = await gs.conn.execute_write(
                            lq.build_spy_escape_route()
                        )
                        if any("OK:ESCAPE_ROUTE" in l for l in escape_lines):
                            log.info(
                                "Auto-resolved spy escape: %s",
                                next(
                                    (l for l in escape_lines if "OK:" in l),
                                    "",
                                ),
                            )
                            resolved_any = True
                            continue
                    except Exception:
                        log.debug("Spy escape auto-resolve failed", exc_info=True)
                    hard_blockers.append((blocking_type, blocking_msg))
                    continue

                # --- Unrecognized blocker → always hard ---
                hard_blockers.append((blocking_type, blocking_msg))

            # If we have hard blockers, check if turn advanced externally
            # (e.g. game auto-end-turn after skip_remaining_units)
            if hard_blockers:
                turn_now = await _get_turn_number(gs)
                if (
                    turn_now is not None
                    and turn_at_entry is not None
                    and turn_now > turn_at_entry
                ):
                    log.info(
                        "Turn advanced externally (%s -> %s), skipping blocker report",
                        turn_at_entry,
                        turn_now,
                    )
                    break  # fall through to snapshot/diff flow

                # Ask the game if turn can actually end despite our blockers.
                # Safe here — we haven't started AI processing yet (pre-end-turn phase).
                try:
                    can_end_lines = await gs.conn.execute_write(
                        f"local can = UI.CanEndTurn(); "
                        f'print(can and "CAN_END" or "CANNOT_END"); '
                        f'print("{lq.SENTINEL}")'
                    )
                    if any(l == "CAN_END" for l in can_end_lines):
                        log.info(
                            "UI.CanEndTurn()=true despite blockers %s — proceeding",
                            [bt for bt, _ in hard_blockers],
                        )
                        break  # fall through to end_turn request
                except Exception:
                    log.debug("UI.CanEndTurn check failed", exc_info=True)

                lines_out: list[str] = ["Cannot end turn — resolve these blockers:"]
                for bt, bm in hard_blockers:
                    hint = lq.BLOCKING_TOOL_MAP.get(
                        bt, "Resolve the blocking notification"
                    )
                    display = (
                        bt.replace("ENDTURN_BLOCKING_", "").replace("_", " ").title()
                    )
                    line = f"  - {display}"
                    if bm:
                        line += f" ({bm})"
                    line += f"  ->  {hint}"
                    lines_out.append(line)
                return "\n".join(lines_out)

            # All blockers were soft-resolved — loop to re-check
            if resolved_any:
                continue
            break  # no blockers left
        except Exception:
            log.debug("Blocking check failed, proceeding anyway", exc_info=True)
            break

    # Take pre-turn snapshot.
    # When re-entering after mid-turn diplomacy (_pending_end_turn=True),
    # the turn may have already advanced. Use the previous call's snapshot
    # as the baseline so the diff captures what changed across the turn.
    snap_fresh = False  # 门禁只吃"本次调用刚拍"的快照,旧快照宁可回退查询
    if gs._pending_end_turn and gs._last_snapshot is not None:
        snap_before = gs._last_snapshot
        log.debug(
            "Using previous snapshot (turn %s) as baseline for pending end-turn",
            snap_before.turn,
        )
    else:
        try:
            snap_before = await gs._take_snapshot()
            snap_fresh = True
        except Exception:
            log.debug("Pre-turn snapshot failed", exc_info=True)
            snap_before = gs._last_snapshot

    # Pre-turn threat scan (for fog-of-war direction tracking)
    threats_before: list[lq.ThreatInfo] = []
    try:
        pre_threat_lines = await gs.conn.execute_read(lq.build_threat_scan_query())
        threats_before = lq.parse_threat_scan_response(pre_threat_lines)
    except Exception:
        log.debug("Pre-turn threat scan failed", exc_info=True)

    turn_before = snap_before.turn if snap_before else await _get_turn_number(gs)

    # ---- 外部推进检测(审查 2026-08-05 F5)----
    # skip_remaining_units 清完最后一个单位时游戏会自动结束回合;此前这里
    # 会照常再发一个 ACTION_ENDTURN,把刚开始的新回合零操作烧掉。
    # turn_before 来自刚取的快照,无需额外往返;命中时直接走报告流程,
    # 基线换成上一回合末的快照,diff 才是这个被外部结束的回合的变化。
    turn_after: int | None = None
    advanced = False
    # 「单位待命令」只查一次(一次往返),查完就不再查 —— 若第一次没查到,
    # 后面再查也不会凭空冒出来,而轮询期每次多一个往返会拖慢真正的长回合。
    _blocked_report_done = False
    if (
        not gs._pending_end_turn
        and turn_at_entry is not None
        and turn_before is not None
        and turn_before > turn_at_entry
    ):
        log.info(
            "Turn advanced externally (%s -> %s) — reporting without re-sending",
            turn_at_entry,
            turn_before,
        )
        if gs._last_snapshot is not None and gs._last_snapshot.turn <= turn_at_entry:
            snap_before = gs._last_snapshot
        advanced = True
        turn_after = turn_before
        turn_before = turn_at_entry

    # ---- 研究队列自动接续(2026-08-09,治"卡科技/路线规划不清")----
    # 科技/市政研完后 Research: None 空转等人工接续,本局至少 5 次;规划
    # 明明早写在日记里,损失全在"计划没接进引擎"。队列是 fallback:只在
    # 当前槽位为空时消费,手动 set_research 永远优先。队列为空时零往返。
    if not gs._pending_end_turn and not advanced:
        try:
            from . import research_queue as rqm

            civ_id, seed_id = await gs.get_game_identity()
            game_key = f"{civ_id}_{seed_id}"
            has_tech_q = bool(rqm.get_queue(game_key, "tech"))
            has_civic_q = bool(rqm.get_queue(game_key, "civic"))
            if has_tech_q or has_civic_q:
                cur = await gs.conn.execute_read(
                    "local me = Game.GetLocalPlayer(); "
                    'print("RQ|" .. tostring(Players[me]:GetTechs():GetResearchingTech()) '
                    '.. "|" .. tostring(Players[me]:GetCulture():GetProgressingCivic()))'
                )
                cur_tech, cur_civic = 0, 0
                for _ln in cur:
                    if _ln.startswith("RQ|"):
                        _p = _ln.split("|")
                        cur_tech = int(_p[1]) if _p[1].lstrip("-").isdigit() else 0
                        cur_civic = int(_p[2]) if _p[2].lstrip("-").isdigit() else 0
                msgs = getattr(gs, "_research_queue_events", None)
                if msgs is None:
                    msgs = []
                    gs._research_queue_events = msgs
                if cur_tech < 0 and has_tech_q:
                    head = rqm.pop_next(game_key, "tech")
                    if head:
                        res = await gs.set_research(head)
                        left = len(rqm.get_queue(game_key, "tech"))
                        if "RESEARCHING" in res:
                            msgs.append(
                                f"Research queue auto-advanced: {head} ({left} queued after it)"
                            )
                        else:
                            msgs.append(
                                f"Research queue item {head} FAILED ({res}) — queue paused, set research manually"
                            )
                if cur_civic < 0 and has_civic_q:
                    head = rqm.pop_next(game_key, "civic")
                    if head:
                        res = await gs.set_civic(head)
                        left = len(rqm.get_queue(game_key, "civic"))
                        if "PROGRESSING" in res or "OK" in res:
                            msgs.append(
                                f"Civic queue auto-advanced: {head} ({left} queued after it)"
                            )
                        else:
                            msgs.append(
                                f"Civic queue item {head} FAILED ({res}) — queue paused, set civic manually"
                            )
        except Exception:
            log.debug("research queue auto-advance failed", exc_info=True)

    # ---- 生产队列自动接续(2026-08-10,缺陷65,用户点名优先)----
    # 城市完工后 Building: nothing 空转等人工接续 —— 罗马局亚琛/奥斯蒂亚/
    # 拉文那/法兰克福/巴黎/阿奎莱亚各空转过 1-2 回合。同研究队列的契约:
    # 只在槽位为空时消费,手动 set_city_production 永远优先,队列空时零往返。
    if not gs._pending_end_turn and not advanced:
        try:
            from . import production_queue as pqm

            civ_id, seed_id = await gs.get_game_identity()
            game_key = f"{civ_id}_{seed_id}"
            pq_all = pqm.get_all(game_key)
            if pq_all:
                # 2026-08-10 上线首回合实测:BuildQueue 的 GetSize/
                # GetCurrentProductionTypeHash 在 GameCore 上下文【不存在】,
                # pcall 全失败 → 所有城被误判空转 → 队列头覆盖了罗马在建的
                # 大学。城市查询必须走 InGame(execute_write),并且判空转的
                # 前提是探测确实成功(PQPROBE_OK 哨兵),探测失败宁可不消费。
                idle_lines = await gs.conn.execute_write(
                    "local me = Game.GetLocalPlayer() "
                    "for _, c in Players[me]:GetCities():Members() do "
                    "local bq = c:GetBuildQueue() "
                    "local size = nil "
                    "local ok = pcall(function() size = bq:GetSize() end) "
                    "if ok and size ~= nil then "
                    'print("PQPROBE_OK|" .. c:GetID()) '
                    "if size == 0 then "
                    'print("PQIDLE|" .. c:GetID()) '
                    "else "
                    "local cur = nil "
                    "pcall(function() cur = bq:GetCurrentProductionTypeHash() end) "
                    "if cur == 0 then "
                    'print("PQIDLE|" .. c:GetID()) end end end end'
                )
                if not any(_l.startswith("PQPROBE_OK|") for _l in idle_lines):
                    raise RuntimeError("production idle probe failed — not consuming")
                idle_ids: set[int] = set()
                for _ln in idle_lines:
                    if _ln.startswith("PQIDLE|"):
                        _v = _ln.split("|", 1)[1]
                        if _v.lstrip("-").isdigit():
                            idle_ids.add(int(_v))
                msgs = getattr(gs, "_research_queue_events", None)
                if msgs is None:
                    msgs = []
                    gs._research_queue_events = msgs
                for cid, _q in pq_all.items():  # noqa: PERF102  (test anchors on this literal)
                    if cid not in idle_ids:
                        continue
                    # 一项失败就【接着试下一项】,不要让这座城空转整回合。
                    #
                    # 2026-08-11 罗马局 T269(缺陷 94):奥斯蒂亚队列头是
                    # BUILDING_FORBIDDEN_CITY,奇观必须带坐标而队列里没有 ——
                    # 消费时报 MISSING_COORDS,队列暂停,城市"finished building
                    # nothing"整整一回合。区域缺坐标能在【排队时】就拒掉
                    # (缺陷 90),奇观不行:队列层是纯 Python,看 BUILDING_ 前缀
                    # 分不出哪些是奇观。既然拦不住,就别让它把整条队列噎住。
                    #
                    # 用户对这一类的原话是「生产断了」—— 空转一回合的代价
                    # 是实打实的产能,而队列后面往往还排着完全能造的东西。
                    failures: list[str] = []
                    started = False
                    while not started:
                        head = pqm.pop_next(game_key, cid)
                        if not head:
                            break
                        res = await gs.set_city_production(
                            cid,
                            head.get("item_type", ""),
                            head.get("item_name", ""),
                            head.get("target_x"),
                            head.get("target_y"),
                        )
                        if "PRODUCING" in res:
                            started = True
                            left = len(pqm.get_queue(game_key, cid))
                            skipped = (
                                f";已跳过失败项:{'; '.join(failures)}"
                                if failures
                                else ""
                            )
                            msgs.append(
                                f"Production queue auto-advanced (city {cid}): "
                                f"{head.get('item_name')} ({left} queued after it)"
                                f"{skipped}"
                            )
                        else:
                            failures.append(f"{head.get('item_name')}({res})")
                    if not started and failures:
                        msgs.append(
                            f"Production queue for city {cid} exhausted — every item "
                            f"failed: {'; '.join(failures)} — set production manually"
                        )
        except Exception:
            log.debug("production queue auto-advance failed", exc_info=True)

    # ---- 任务 J:回合完成度门禁(引擎 blocker 之外的质量层)----
    # 只在真正要发 ACTION_ENDTURN 时把关;上一请求仍在途时不重复拦。
    if not gs._pending_end_turn and not advanced:
        try:
            gate_msg = await _turn_completion_gate(
                gs, turn_before, threats_before, snap_before if snap_fresh else None
            )
        except Exception:
            gate_msg = ""
            log.debug("回合门禁检查失败,放行", exc_info=True)
        if gate_msg:
            # 2026-08-11 罗马局 T237(缺陷 88):门禁报「T237 不能结束回合」,
            # 但此刻回合其实已经推进到 T238 —— 三条独立证据:喀山城血从 113
            # 回到 133(正好一回合 +20 回血)、所有单位移动力刷新、下一次
            # end_turn 报的是「238 -> 239」。门禁自身是前置的(在 build_end_turn
            # 之前 return),推进另有来源:中途外交会话会让原始 ACTION_ENDTURN
            # 留在途中继续结算(俄罗斯丢城后连送两个和平提案)。
            #
            # 危害是双重的,而且都很隐蔽:
            #   ① 调用方以为还在旧回合,继续按旧局面下令;
            #   ② 重调 end_turn 时复用的反思会落到下一个回合号上,
            #      日记与实际内容整体错位一格 —— 和 (83) 同一类记忆污染。
            #
            # 不依赖根因的便宜防御:返回拦截消息之前复查回合号。真变了就别再
            # 谎称"不能结束回合",直接按「回合已被外部结束」走报告流程。
            turn_now = await _get_turn_number(gs)
            if (
                turn_now is not None
                and turn_before is not None
                and turn_now > turn_before
            ):
                log.warning(
                    "门禁拦截期间回合已从 %s 推进到 %s —— 不报拦截,转报告流程",
                    turn_before,
                    turn_now,
                )
                advanced = True
            else:
                return gate_msg

    # Request end turn — but skip if a previous ACTION_ENDTURN is still in flight.
    # This prevents duplicate requests that cause turns to skip (e.g. 412 → 415).
    # After mid-turn diplomacy/deals, the game auto-continues AI processing
    # with the original request, so we only need to poll for advancement.
    lua = lq.build_end_turn()
    if advanced:
        pass  # 回合已被外部结束 —— 不发新请求,直接进报告流程
    elif gs._pending_end_turn:
        log.info(
            "Skipping ACTION_ENDTURN — previous request still in flight (from turn %s)",
            gs._pending_end_turn_from,
        )
        # Use the original turn number as baseline for advancement detection.
        # The current turn_before may already be advanced if the game auto-continued.
        if gs._pending_end_turn_from is not None:
            turn_before = gs._pending_end_turn_from
    else:
        # 进 AI 回合前先存档(P0, 2026-08-05):AI 挂死后恢复点 = 本回合操作
        # 完成态,而不是回合开局(否则玩家操作全丢,T335 实测重做了 3 遍)。
        # 与回合推进后的 0_MCP_{turn_after} 同名 —— 有意覆盖,让同一编号的
        # 存档始终指向"该回合最后已知状态"。
        from .autosave import saves_work_on_this_platform

        if turn_before is not None and saves_work_on_this_platform():
            try:
                _save_res = await save_game(gs.conn, f"0_MCP_{turn_before:04d}")
                # 审查 F10:挂死提示会断言"预存档已含本回合操作"——
                # 只有确认成功才允许那句话出现,失败时不能把人指回旧档。
                gs._presave_ok_turn = (
                    turn_before if _save_res.startswith("Saved") else None
                )
            except Exception:
                gs._presave_ok_turn = None
                log.debug(
                    "Pre-end-turn save failed for T%s", turn_before, exc_info=True
                )
        if turn_before is not None:
            etg.write_pending(turn_before)
        await gs.conn.execute_write(lua)
        sent_action = True
        gs._pending_end_turn = True
        gs._pending_end_turn_from = turn_before

    # Poll for turn advancement using GameCore-only queries.
    # CRITICAL: Do NOT send InGame queries while AI civs are processing
    # their turns.  InGame queries (diplomacy sessions, UI.CanEndTurn,
    # popup dismissal) force context switches that can stall the AI
    # diplomacy subsystem, causing infinite hangs (seen in Games 1-5).
    _t_wait0 = time.monotonic()

    # Phase 1: 快检。2026-08-03 用户报「end_turn 要好几分钟」——
    # 与其猜,先把各阶段耗时量出来(见函数末尾的 ⏱ 行)。
    # 轮询间隔从固定 0.5s 改为前密后疏:每次探测本身就是一次约 324ms 的
    # 往返,所以真正的检测延迟 ≈ 间隔 + 往返。前几次密一点能显著降低
    # 「AI 其实早就跑完、我们还在睡」的浪费。
    if not advanced:
        for _delay in (0.2, 0.3, 0.5, 0.5, 0.8, 0.8, 1.0, 1.0):
            await asyncio.sleep(_delay)
            turn_after = await _get_turn_number(gs)
            if (
                turn_after is not None
                and turn_before is not None
                and turn_after > turn_before
            ):
                advanced = True
                break

    # Phase 2: Slow polling (5 min) — AI can take 1-5 min on large maps,
    # especially during wars with many units. GameCore-only queries.
    if not advanced:
        # 10 min total: AI can take several minutes on large maps with wars.
        # Quick polls early (catch fast turns), then escalate to 30s intervals.
        diplomacy_probed = False
        watchdog = _AiHangWatchdog()
        cumulative_wait = 4.0  # Phase 1 already waited ~4s
        for delay in [
            1.0,
            1.0,
            1.5,
            1.5,
            2.0,
            2.0,
            3.0,
            3.0,
            5.0,
            5.0,  # 25s: catch fast turns
            10.0,
            10.0,
            10.0,
            10.0,
            10.0,
            10.0,  # 80s: mid wait
            15.0,
            15.0,
            15.0,
            15.0,  # 140s
            20.0,
            20.0,
            20.0,
            20.0,  # 220s
            30.0,
            30.0,
            30.0,
            30.0,
            30.0,
            30.0,
            30.0,  # 430s
            30.0,
            30.0,
            30.0,
            30.0,  # 550s (~9 min)
        ]:
            await asyncio.sleep(delay)
            cumulative_wait += delay
            turn_after = await _get_turn_number(gs)
            if (
                turn_after is not None
                and turn_before is not None
                and turn_after > turn_before
            ):
                advanced = True
                break
            # 「等玩家」不是「AI 卡死」(2026-08-07 苏美尔局 T5/T6/T15/T17 实测)。
            # 游戏拒绝提交回合的原因永远是**它在等你做一个决定**:单位没下命令、
            # 要选科技、要选市政、要选政策、要选生产项目。回合号一直不动。
            # 此前的看门狗只看 CPU —— 而这台机器空闲时 CPU 也是满的(实测 5 秒
            # 墙钟烧 8.7 秒 CPU),判据恒为真,于是傻等 240 秒后报 AI_TURN_HANG,
            # 还建议杀游戏重载(会白丢进度)。
            # 实测:给那个单位下一个 fortify,同一个回合 11.8 秒就过了。
            #
            # 这一条大概率也是用户报的「T300+ 卡死」的主因:后期单位几十个,
            # 漏一个没下命令就卡住,而错误的死循环诊断会把人引去重启游戏。
            if delay >= 10.0 and not _blocked_report_done:
                _blocked_report_done = True
                stuck = await _pending_decisions(gs)
                # 缺陷57:通知解析器的 is_action_required 认不出「填补政策
                # 槽位」这类阻塞。引擎的 GetFirstEndTurnBlocking 是权威源,
                # 补进来 —— 两个来源互补,任一命中都要点名。
                _blk_active, _blk_msg = await _engine_turn_blocker(gs)
                if _blk_active and _blk_msg and _blk_msg not in stuck:
                    stuck.insert(0, _blk_msg)
                # 缺陷54(2026-08-09 T384 实测):宣战唤醒的 fortify 单位是软阻
                # 塞,第二次 ACTION_ENDTURN 引擎直接放行 —— 回合其实已提交、
                # AI 在跑,但通知快照里"单位待命令"还挂着,这里就把它当成
                # "回合没提交"返回了。agent 信了文案继续下单位命令,全被
                # CanStartOperation=false 拒绝。返回前必须复核 IsTurnActive:
                # 已不活跃 = 提交成功,继续轮询等 AI,别报假 blocker。
                if stuck:
                    turn_active = True
                    try:
                        act_lines = await gs.conn.execute_write(
                            "local me = Game.GetLocalPlayer(); "
                            'print("TA|" .. tostring(Players[me]:IsTurnActive()))'
                        )
                        for _al in act_lines:
                            if _al.startswith("TA|"):
                                turn_active = _al.split("|", 1)[1] == "true"
                    except Exception:
                        log.debug("IsTurnActive recheck failed", exc_info=True)
                    if not turn_active:
                        log.info(
                            "end_turn: %d pending decisions but turn is no longer "
                            "active — submit succeeded, resuming AI poll",
                            len(stuck),
                        )
                        stuck = []
                if stuck:
                    gs._pending_end_turn = False
                    gs._pending_end_turn_from = None
                    names = "\n  · ".join(stuck[:8])
                    more = f"\n  ...共 {len(stuck)} 项" if len(stuck) > 8 else ""
                    return (
                        f"TURN_NOT_SUBMITTED|回合没有提交 —— 游戏在等你做这些决定:\n"
                        f"  · {names}{more}\n"
                        f"这【不是】AI 卡死:回合号根本没动过,游戏还在你的回合里。\n"
                        f"处理:选科技/市政用 set_research;补政策槽用 set_policies;"
                        f"定生产用 set_city_production;单位待命令就给明确命令"
                        f"(move / fortify / skip),或用 skip_remaining_units 一次性跳过。"
                        f"处理完重新 end_turn。\n"
                        f"⚠️ 被威胁的单位不要用 skip —— skip 不会撤退。先移到安全格再跳。"
                    )

            # Check for game-over during longer polling intervals.
            # An opponent victory (Science, Culture, etc.) fires during
            # their turn — without this we'd wait the full 9-min timeout.
            if delay >= 10.0:
                gameover = await gs.check_game_over()
                if gameover is not None:
                    gs._pending_end_turn = False
                    gs._pending_end_turn_from = None
                    gs._last_game_over = gameover
                    vtype = (
                        gameover.victory_type.replace("VICTORY_", "")
                        .replace("_", " ")
                        .title()
                    )
                    if gameover.is_defeat:
                        return (
                            f"GAME OVER — DEFEAT. {gameover.winner_leader} "
                            f"of {gameover.winner_name} won a {vtype} victory. "
                            f"The game has ended. No further actions are possible."
                        )
                    else:
                        return (
                            f"GAME OVER — VICTORY! You won a {vtype} victory! "
                            f"The game has ended."
                        )
                # CPU hang watchdog: bail early on a pegged AI turn instead of
                # waiting out the full ~9 min timeout in silence.
                #
                # 2026-08-05: Only allow a hang verdict after >= 4 minutes of
                # cumulative wait. Late-game turns with 8 AI civs + wars + WC
                # genuinely take 3-10 minutes at full CPU (see CHANGELOG
                # v1.1.7/v1.1.9) — the 79s verdict was a false positive that
                # caused agents to kill the game mid-turn and redo their work.
                if cumulative_wait >= 240 and await watchdog.sample():
                    # 缺陷57:CPU 满载在这台机器上不是可靠判据(空闲也满)。
                    # 报"AI 死循环 + 杀游戏重载"之前,先问引擎回合到底在谁
                    # 手里:我方回合仍活跃 = 引擎在等我做决定,不是 AI 卡死。
                    _blk_active, _blk_msg = await _engine_turn_blocker(gs)
                    if _blk_active:
                        gs._pending_end_turn = False
                        gs._pending_end_turn_from = None
                        detail = f"\n  · {_blk_msg}" if _blk_msg else ""
                        return (
                            "TURN_NOT_SUBMITTED|回合【没有】提交 —— 引擎报告"
                            "我方回合仍然活跃(IsTurnActive=true),它在等你做"
                            f"一个决定,这【不是】AI 卡死。{detail}\n"
                            "常见阻塞:填补政策槽位(set_policies)、选科技/市政"
                            "(set_research)、选生产项目(set_city_production)、"
                            "单位待命令(unit_action / skip_remaining_units)。\n"
                            "处理完再调 end_turn。不要杀游戏、不要重载存档 —— "
                            "回合号没动过,进度一点没丢。"
                        )
                    hang_save = "0_MCP_xxxx"
                    try:
                        from .autosave import get_autosave_for_turn

                        hang_save = get_autosave_for_turn(
                            turn_before if turn_before is not None else 0
                        )
                    except Exception:
                        pass
                    return (
                        "AI_TURN_HANG|Game process CPU stayed fully pegged for "
                        "two sampling windows — the AI infinite-loop hang "
                        "(known issue). The end_turn session is still pending. "
                        f"If the game is still at full CPU, this may instead be "
                        "a legitimately long late-game AI turn (8 civs can take "
                        "up to 10 min — see CHANGELOG). FIRST check "
                        "get_game_overview a few times over the next 2-3 minutes; "
                        "if the turn HAS advanced, continue normally. If it has "
                        "NOT advanced and CPU has come back down, RETRY end_turn "
                        "directly — the hang is probabilistic (T336→T337 retry "
                        "passed in 3.3s). Only if the turn has NOT advanced and "
                        "CPU stays pegged for several more minutes, kill the game "
                        f"and reload '{hang_save}' to continue. "
                        + (
                            "That 0_MCP_ save was confirmed written right before "
                            "this AI turn started, so it already contains this "
                            "turn's completed orders — after reloading, run "
                            "get_diary to verify before redoing anything."
                            if getattr(gs, "_presave_ok_turn", None) == turn_before
                            else "The pre-turn save was NOT confirmed for this "
                            "turn — after reloading, run get_diary FIRST to "
                            "check which of this turn's orders survived."
                        )
                    )
                # Early diplomacy probe — ONE InGame query after ~45s of silence.
            # The CRITICAL constraint (Games 1-5) was about REPEATED InGame
            # queries in a tight loop. A single probe after 45s is safe: if
            # the AI paused for a trade deal, the game is idle. If the AI is
            # still processing, the query may be slow/fail (caught below).
            if not diplomacy_probed and cumulative_wait >= 45:
                diplomacy_probed = True
                diplo_msg, diplo_advanced = await _check_mid_turn_diplomacy(
                    gs, lua, turn_before
                )
                if diplo_msg is not None:
                    return diplo_msg
                if diplo_advanced:
                    advanced = True
                    break

    # Phase 3: After ~5 min, now safe to check InGame state.
    # AI processing either completed (blocker is on our side) or is
    # truly hung.  Do ONE round of InGame checks, not a loop.
    if not advanced:
        # Check for AI diplomatic proposals (reuses the same helper
        # as the early Phase 2 probe — Phase 3 is the fallback if the
        # probe didn't fire or missed the diplomacy window).
        diplo_msg, diplo_advanced = await _check_mid_turn_diplomacy(
            gs, lua, turn_before
        )
        if diplo_msg is not None:
            return diplo_msg
        if diplo_advanced:
            advanced = True

    if not advanced:
        # Check for incoming trade deals
        try:
            mid_deals = await gs.get_pending_deals()
            if mid_deals:
                return (
                    "Turn paused — incoming trade deal:\n"
                    + nr.narrate_pending_deals(mid_deals)
                )
            # 2026-08-04 P2-9:交易通知与 get_pending_trades 读不到并存。
            # 通知存在但 INCOMING working deal 为空(AI 提议已过期/被撤销,
            # 或只是挂着的 UI 提示)—— 旧代码静默放过,通知下回合还挂着,
            # 反复制造「有新交易却读不到」的困惑。探测通知计数,非零则
            # 明确说明,让调用方知道该主动核实而不是继续等。
            try:
                notif_lines = await gs.conn.execute_write(
                    "local me = Game.GetLocalPlayer() "
                    "local n = 0 "
                    "local list = NotificationManager.GetList(me) "
                    "if list then "
                    "  for _, nid in ipairs(list) do "
                    "    local e = NotificationManager.Find(me, nid) "
                    "    if e and not e:IsDismissed() then "
                    "      local t = tostring(e:GetType() or '') "
                    "      if t:find('TRADE') or t:find('DEAL') or t:find('DIPLOMACY') then n = n + 1 end "
                    "    end "
                    "  end "
                    "end "
                    'print("TRADE_NOTIF|" .. n) '
                    'print("' + lq.SENTINEL + '")'
                )
                for nl in notif_lines:
                    if nl.startswith("TRADE_NOTIF|"):
                        n = int(nl.split("|")[1])
                        if n > 0:
                            return (
                                "Turn paused — "
                                + str(n)
                                + " 条未读交易/外交通知,但读不到具体交易内容"
                                " (提议可能已过期或被撤销)。用 get_diplomacy / "
                                "get_trade_options 主动核实后再 end_turn。"
                            )
                        break
            except Exception:
                log.debug("Trade notification probe failed", exc_info=True)
        except Exception:
            log.debug("Mid-turn deal check failed", exc_info=True)

        # Single popup dismiss attempt (NOT a loop — looped dismissal
        # during AI processing was a primary cause of AI hangs).
        try:
            dismissed = await gs.dismiss_popup()
            if "Dismissed" in dismissed:
                log.info("Post-timeout popup dismissed: %s", dismissed)
                await gs.conn.execute_write(lua)
                for _ in range(5):
                    await asyncio.sleep(2.0)
                    turn_after = await _get_turn_number(gs)
                    if (
                        turn_after is not None
                        and turn_before is not None
                        and turn_after > turn_before
                    ):
                        advanced = True
                        break
        except Exception:
            log.debug("Post-timeout dismiss failed", exc_info=True)

    if not advanced:
        # Final verification — turn may have slipped through
        await asyncio.sleep(2.0)
        turn_after = await _get_turn_number(gs)
        if (
            turn_after is not None
            and turn_before is not None
            and turn_after > turn_before
        ):
            advanced = True

    if not advanced:
        # Check if game ended during turn transition (victory/defeat)
        gameover = await gs.check_game_over()
        if gameover is not None:
            gs._pending_end_turn = False
            gs._pending_end_turn_from = None
            gs._last_game_over = gameover
            vtype = (
                gameover.victory_type.replace("VICTORY_", "").replace("_", " ").title()
            )
            if gameover.is_defeat:
                return (
                    f"GAME OVER — DEFEAT. {gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory. "
                    f"The game has ended. No further actions are possible."
                )
            else:
                return (
                    f"GAME OVER — VICTORY! You won a {vtype} victory! "
                    f"The game has ended."
                )

        # Provide specific blocker info instead of generic message
        details: list[str] = []
        try:
            sessions = await gs.get_diplomacy_sessions()
            if sessions:
                names = [s.other_civ_name for s in sessions]
                details.append(f"Open diplomacy session with: {', '.join(names)}")
        except Exception:
            pass
        try:
            blocking_lines = await gs.conn.execute_write(
                lq.build_end_turn_blocking_query()
            )
            blockers = lq.parse_end_turn_blocking(blocking_lines)
            for bt, bm in blockers:
                display = bt.replace("ENDTURN_BLOCKING_", "").replace("_", " ").title()
                details.append(f"Blocker: {display}" + (f" ({bm})" if bm else ""))
        except Exception:
            pass
        # Turn didn't advance — clear the pending flag so next call re-sends
        gs._pending_end_turn = False
        gs._pending_end_turn_from = None
        if details:
            # Before returning blocker, check if game actually ended —
            # victory can trigger during AI processing while blockers coexist
            gameover = await gs.check_game_over()
            if gameover is not None:
                gs._pending_end_turn = False
                gs._pending_end_turn_from = None
                gs._last_game_over = gameover
                vtype = (
                    gameover.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                if gameover.is_defeat:
                    return (
                        f"GAME OVER — DEFEAT. {gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory. "
                        f"The game has ended. No further actions are possible."
                    )
                else:
                    return f"GAME OVER — VICTORY! You won a {vtype} victory! The game has ended."
            return f"End turn blocked (turn {turn_after or turn_before}): {'; '.join(details)}"
        # No blockers, no diplomacy, no game over — true AI turn hang.
        # Return structured HANG: prefix so server.py can auto-recover.
        turn_num = turn_after or turn_before
        if turn_num is not None:
            from .autosave import get_autosave_for_turn

            hang_save = get_autosave_for_turn(turn_num)
            return (
                f"HANG:{turn_num}:{hang_save}|"
                f"End turn requested (turn is still {turn_num}). "
                f"AI turn processing appears stuck."
            )
        return f"End turn requested (turn is still {turn_num}). Check get_pending_diplomacy or dismiss_popup."

    # Turn advanced — clear the pending flag; persist an "advanced" marker so
    # a lost response / process restart can detect this without re-sending.
    advanced_from = gs._pending_end_turn_from
    gs._pending_end_turn = False
    gs._pending_end_turn_from = None
    if advanced_from is not None and sent_action:
        etg.write_advanced(advanced_from)
    # 埋点:等 AI 跑完这一段花了多久。这是「好几分钟」最可能的去处 ——
    # 它取决于游戏本身(地图大小/单位数/战争规模),不是我们的查询数量。
    try:
        gs._phase_timings["等AI回合"] = time.monotonic() - _t_wait0
        gs._phase_timings["_到推进为止"] = time.monotonic() - gs._t_enter
    except Exception:
        pass

    # Turn regression detection — catch accidental wrong-save loads
    if turn_after is not None and gs._high_water_turn > 0:
        if turn_after < gs._high_water_turn - 1:
            from .autosave import get_autosave_for_turn

            latest_autosave = get_autosave_for_turn(gs._high_water_turn)
            log.warning(
                "Turn regressed from %d to %d — possible wrong save loaded",
                gs._high_water_turn,
                turn_after,
            )
            return (
                f"CRITICAL: Turn regressed from {gs._high_water_turn} to {turn_after}. "
                f"You may have loaded the wrong save file. "
                f"Your most recent MCP autosave is {latest_autosave}. "
                f'Use load_game_save("{latest_autosave}") to recover.'
            )
    if turn_after is not None:
        # Reset per-turn counters only on TRUE advance. Blocker turns have
        # turn_after == turn_before, so the counter must NOT reset — this
        # prevents the agent from advisor-spamming between blocker retries
        # within a single game turn.
        if turn_after > gs._high_water_turn:
            gs._advisor_calls_this_turn = 0
            # L4:回合真正推进后清掉上一回合的攻击记账
            # (clear_turn_reads 挪到报告收尾处 —— 审查 F2:报告构建路径
            #  战时会调 gs.get_units,在这里清会把下一回合预标记成"已读",
            #  偷懒检测因此永远沉默。)
            try:
                gs.combat_memory.new_turn(turn_after)
            except Exception:
                pass
        gs._high_water_turn = max(gs._high_water_turn, turn_after)

    # Post-advance game-over check — victory can trigger during the turn
    # transition (e.g. science vessel arriving, diplo VP threshold).
    # Must check here so "GAME OVER" appears in result for log_game_over.
    gameover = await gs.check_game_over()
    if gameover is not None:
        gs._last_game_over = gameover
        vtype = gameover.victory_type.replace("VICTORY_", "").replace("_", " ").title()
        if gameover.is_defeat:
            return (
                f"Turn {turn_before} -> {turn_after}\n"
                f"GAME OVER — DEFEAT. {gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory. "
                f"The game has ended. No further actions are possible."
            )
        else:
            return (
                f"Turn {turn_before} -> {turn_after}\n"
                f"GAME OVER — VICTORY! You won a {vtype} victory! The game has ended."
            )

    # Take post-turn snapshot and diff
    snap_after = None
    try:
        snap_after = await gs._take_snapshot()
        gs._last_snapshot = snap_after
    except Exception:
        log.warning("Post-turn snapshot failed — events will be limited", exc_info=True)

    # MCP per-turn autosave — fire-and-forget after successful turn advance.
    # On Linux (Aspyr port), Network.SaveGame silently fails for custom names.
    # We rely on the game's own AutoSave_NNNN instead.
    from .autosave import saves_work_on_this_platform

    if turn_after is not None and saves_work_on_this_platform():
        try:
            await save_game(gs.conn, f"0_MCP_{turn_after:04d}")
            cleanup_old_autosaves(keep=8)
        except Exception:
            log.debug("MCP autosave failed for T%s", turn_after, exc_info=True)

    events: list[lq.TurnEvent] = []
    if snap_before and snap_after:
        events = gs._diff_snapshots(snap_before, snap_after)
        # 研究队列自动接续的回执(pre-send 阶段生成,这里进事件流让用户看见)
        _rq_msgs = getattr(gs, "_research_queue_events", None)
        if _rq_msgs:
            for _m in _rq_msgs:
                events.append(lq.TurnEvent(priority=2, category="research", message=_m))
            gs._research_queue_events = []

    # Query active notifications
    notifications: list[lq.GameNotification] = []
    try:
        notif_lines = await gs.conn.execute_write(lq.build_notifications_query())
        notifications = lq.parse_notifications_response(notif_lines)
    except Exception:
        log.debug("Notification query failed", exc_info=True)

    # Check for pending trade deals (AI may propose during their turn).
    # 2026-08-03:优先级从 2 提到 0。用户实机反馈:游戏里弹出了阿拉伯的
    # 求和提议,而系统既没读到、也没提醒 —— 那个弹窗还会把回合卡住
    # (实测 116->116 不推进)。「对方在等你回话」属于本回合必须处理的事,
    # 不是一条可选情报。
    try:
        deals = await gs.get_pending_deals()
        if deals:
            who = "、".join(d.other_player_name for d in deals)
            events.append(
                lq.TurnEvent(
                    priority=0,
                    category="diplomacy",
                    message=(
                        f"🔴 有 {len(deals)} 份提议在等你回话({who})——"
                        f"不处理可能卡住回合推进。\n"
                        f"{nr.narrate_pending_deals(deals)}\n"
                        f"  → 用 respond_to_trade(other_player_id=..., accept=True/False) 表态;"
                        f"提议挂在弹窗里没打开会话也没关系,工具会自己补开。"
                    ),
                )
            )
    except Exception:
        log.debug("Trade deal check failed", exc_info=True)

    # 2026-08-10 用户反馈:「交易界面你经常无视很多次了」——"有新的可用交易"
    # 通知在 T106-T160 间反复出现,反复被当成噪音跳过;而把富余奢侈品卖给
    # AI 是最稳的白捡金币(本局 GNP 恒垫底的结构性解药之一)。该通知存在时
    # 升为独立事件行,附可直接执行的动作引导 —— 不再依赖模型自觉。
    try:
        _has_trade_notif = any(
            ("可用交易" in (n.message or ""))
            or ("NEW_DEAL" in (n.type_name or "").upper())
            or (
                "TRADE_ROUTE" not in (n.type_name or "").upper()
                and "DEAL" in (n.type_name or "").upper()
            )
            for n in notifications
        )
        if _has_trade_notif:
            events.append(
                lq.TurnEvent(
                    priority=1,
                    category="diplomacy",
                    message=(
                        "💰 引擎提示有新的可谈交易 —— 别再跳过:"
                        "①get_pending_trades 看有没有挂起提议;"
                        "②富余奢侈品(第2份宜居度为0,卖=白捡钱)用 "
                        "propose_trade(mode='test') 逐家试价再 send;"
                        "③缺的奢侈品也可以反向买。金收入垫底时这是最快的修复。"
                    ),
                )
            )
    except Exception:
        log.debug("Trade-available notification surfacing failed", exc_info=True)

    # Threat scan — check for hostile units near cities
    threats: list[lq.ThreatInfo] = []
    try:
        threat_lines = await gs.conn.execute_read(lq.build_threat_scan_query())
        threats = lq.parse_threat_scan_response(threat_lines)
        for t in threats:
            rs_str = f" RS:{t.ranged_strength}" if t.ranged_strength > 0 else ""
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="unit",
                    message=f"THREAT: {t.owner_name} {t.unit_type} CS:{t.combat_strength}{rs_str} HP:{t.hp}/{t.max_hp} spotted {t.distance} tiles away at ({t.x},{t.y})",
                )
            )
    except Exception:
        log.debug("Threat scan failed", exc_info=True)

    # 敌机在场而我方零对空(2026-08-05"作战不能只看纸面数值"):
    # T335 实证,阿拉伯一架战斗机(CS100)压制整个后方,建造者不敢出门
    # 修煤田 —— 而军力总分 1137 对此完全无感。对空 = CLASS_ANTI_AIR
    # (防空炮/战列舰/导弹巡洋舰)或战斗机(可拦截)。
    try:
        from civ_mcp import war_planner as _wp

        _air = [
            t
            for t in threats
            if _wp.is_aircraft(str(getattr(t, "unit_type", "") or ""))
        ]
        if _air and snap_after is not None:
            _have_aa = any(
                _wp.can_anti_air(str(getattr(u, "unit_type", "") or ""))
                for u in snap_after.units.values()
            )
            if not _have_aa:
                _t0 = _air[0]
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="military",
                        message=(
                            f"NO ANTI-AIR: enemy aircraft {_t0.unit_type} at "
                            f"({_t0.x},{_t0.y}) and you have ZERO anti-air "
                            f"capable units (AA class or fighters). Civilians "
                            f"and siege units inside its strike range are not "
                            f"safe — route around it and research toward "
                            f"Flight/AA before pushing that direction."
                        ),
                    )
                )
    except Exception:
        log.debug("Anti-air check failed", exc_info=True)

    # Fog-of-war direction tracking — diff pre/post threats
    if threats_before:
        try:
            disappeared, _, _ = lq.diff_threats(threats_before, threats)
            if disappeared:
                positions = [(t.x, t.y) for t in disappeared]
                fog_lines = await gs.conn.execute_read(
                    lq.build_fog_neighbor_query(positions)
                )
                fog_dirs = lq.parse_fog_neighbor_response(fog_lines)
                for t in disappeared:
                    dirs = fog_dirs.get((t.x, t.y), [])
                    if dirs:
                        dir_str = "/".join(dirs)
                        msg = (
                            f"LOST CONTACT: {t.owner_name} {t.unit_type} "
                            f"HP:{t.hp}/{t.max_hp} last seen at ({t.x},{t.y}) "
                            f"— likely moved {dir_str} into fog"
                        )
                    else:
                        msg = (
                            f"VANISHED: {t.owner_name} {t.unit_type} "
                            f"HP:{t.hp}/{t.max_hp} last at ({t.x},{t.y}) "
                            f"— no adjacent fog (killed or garrisoned?)"
                        )
                    events.append(
                        lq.TurnEvent(priority=1, category="unit", message=msg)
                    )
        except Exception:
            log.debug("Fog direction tracking failed", exc_info=True)

    events.sort(key=lambda e: e.priority)

    # 对手胜利逼近检查。2026-08-03 从每回合改为每 3 回合(+ 每 10 回合的
    # 完整快照仍在):胜利进度是**累积量**,一回合的变化不足以改变任何决策,
    # 而它是一次独立往返。真正紧急的阈值事件由 10 回合完整快照兜底。
    if turn_after is None or turn_after % 3 == 0 or turn_after <= 2:
        try:
            victory_events = await _check_victory_proximity(gs)
            events.extend(victory_events)
            _expert_cache(gs)["victory_events"] = victory_events
        except Exception:
            log.warning("Victory proximity check failed", exc_info=True)
    else:
        # 沿用上次结果:对手在逼近胜利这件事,不会因为这回合没查就消失
        events.extend(_expert_cache(gs).get("victory_events") or [])

    # Every 10 turns: full victory progress snapshot
    if turn_after is not None and turn_after % 10 == 0:
        try:
            vp = await gs.get_victory_progress()
            summary = nr.narrate_victory_progress(vp)
            events.append(
                lq.TurnEvent(
                    priority=3,
                    category="victory",
                    message=f"10-TURN VICTORY SNAPSHOT (T{turn_after}):\n{summary}",
                )
            )
            _record_victory_eta(gs, turn_after, vp)
        except Exception:
            log.debug("10-turn victory check failed", exc_info=True)

    # Growth alerts from post-turn city state
    if snap_after:
        for cs in snap_after.cities.values():
            if cs.food_surplus < 0:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="city",
                        message=f"STARVING: {cs.name} ({cs.food_surplus:+.1f} food/t) — will lose population!",
                    )
                )
            elif cs.food_surplus == 0 and cs.turns_to_grow <= 0:
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="city",
                        message=f"STAGNANT: {cs.name} (0 food surplus) — needs farm, granary, or trade route",
                    )
                )
            elif cs.turns_to_grow > 15:
                events.append(
                    lq.TurnEvent(
                        priority=3,
                        category="city",
                        message=f"SLOW GROWTH: {cs.name} ({cs.turns_to_grow}t to next pop, {cs.food_surplus:+.1f}/t)",
                    )
                )

    # Empire-wide warnings (scoreboard, idle trade, loyalty, military, gold)
    game_score = None
    try:
        warning_events, game_score = await _check_empire_warnings(gs, snap_after)
        events.extend(warning_events)
    except Exception:
        log.debug("Empire warnings failed", exc_info=True)

    # Save scumming detection
    try:
        scum_events, hard_stop = _check_save_scumming(gs)
        events.extend(scum_events)
        if hard_stop:
            gs._run_aborted = True
    except Exception:
        log.debug("Save scumming check failed", exc_info=True)

    events.sort(key=lambda e: e.priority)

    # L0 硬知识注入:规则必须每回合重新出现在返回值里,写在提示词里等于没写
    # (改造方案 铁律一)。查不到时代就退化成只发恒定规则,不阻断回合。
    # 时代查询缓存:一局最多变 8 次,却曾是每回合一次独立往返(~324ms)。
    # 2026-08-03 用户点名 end_turn 太慢 —— 这类「变化极慢却每回合查」的项
    # 是主要浪费。每 5 回合刷新一次,中间用缓存;查失败也回落到缓存。
    era = None
    _cache = _expert_cache(gs)
    _turn_now = turn_after or turn_before or 0
    if _cache.get("era") and _turn_now - _cache.get("era_turn", -99) < 5:
        era = _cache["era"]
    else:
        try:
            era = await _get_current_era(gs)
            if era:
                _cache["era"], _cache["era_turn"] = era, _turn_now
        except Exception:
            era = _cache.get("era")
            log.debug("时代查询失败,沿用缓存", exc_info=True)

    # L1 世界模型:观测缺失 ≠ 事实消失(铁律二)。失败就跳过,不阻断回合。
    world_briefing = ""
    _t_brief = time.monotonic()
    try:
        world_briefing = await _update_world_model(
            gs, turn_after or turn_before, events, threats, era=era
        )
    except Exception:
        log.debug("世界模型更新失败,跳过情报段", exc_info=True)
    try:
        gs._phase_timings["世界模型+简报"] = time.monotonic() - _t_brief
    except Exception:
        pass

    report = gs._build_turn_report(
        turn_before,
        turn_after,
        events,
        notifications,
        stockpiles=snap_after.stockpiles if snap_after else None,
        score=game_score,
        era=era,
        world_briefing=world_briefing,
    )
    # 任务 J:门禁的警告档(不阻止,只提醒)贴在报告尾部
    gate_warnings = _pop_gate_warnings(gs)
    if gate_warnings:
        report += "\n\n⏳ 回合完成度提醒:\n" + "\n".join(gate_warnings)
    report += _render_timings(gs)
    # 读取记账清零放在报告构建【之后】(审查 F2):报告路径战时会调
    # gs.get_units,早清会把下一回合预标记成"已读",偷懒检测永远沉默。
    if turn_after is not None and turn_after > turn_before:
        gs.clear_turn_reads()
    return report


def _render_timings(gs) -> str:
    """把分阶段耗时渲染成一行。总耗时低于 5 秒时不显示 —— 没问题就别占版面。

    这一行的目的是让「end_turn 好慢」变成一个可定位的数字:
    是等游戏跑 AI 回合(取决于地图与战争规模,我们改不了),
    还是我们自己的查询与简报(可以优化)。
    """
    try:
        total = time.monotonic() - gs._t_enter
        if total < 5.0:
            return ""
        t = dict(gs._phase_timings or {})
        wait = t.get("等AI回合", 0.0)
        brief = t.get("世界模型+简报", 0.0)
        other = max(0.0, total - wait - brief)
        line = (
            f"\n\n⏱ 本次 end_turn 共 {total:.1f}s = "
            f"等游戏跑AI回合 {wait:.1f}s + 简报生成 {brief:.1f}s + 其余查询 {other:.1f}s"
        )
        if wait > total * 0.6:
            line += (
                "\n   (大头是游戏自己在跑 AI 回合 —— 与地图大小、单位总数、"
                "同时进行的战争数有关,不是工具层能优化的)"
            )
        elif brief > total * 0.4:
            line += "\n   (大头是简报生成 —— 这部分可以优化,请反馈)"
        return line
    except Exception:
        return ""


def _world_path(gs: GameState) -> Path:
    civ, seed = gs._game_identity or ("unknown", 0)
    return wm_mod.storage_path(civ, seed)


async def _update_world_model(
    gs: GameState,
    turn: int,
    events: list[lq.TurnEvent],
    threats: list[lq.ThreatInfo] | None = None,
    era: str | None = None,
) -> str:
    """扫描世界 → 更新持久模型 → 评估计划链 → 威胁仲裁 → 落盘 → 叙事简报。

    era 由调用方传入(2026-08-01 修复:此前函数体引用了不存在的 era 变量,
    NameError 被中后期段的 except 静默吞掉 —— "== 中后期 =="从未实机渲染过。
    这类静默失败正是改造报告点名的头号敌人)。
    """
    path = _world_path(gs)
    key = str(path)
    if gs._world_loaded_for != key:
        # load_best 跨候选位置取最完整的一份 —— 换客户端启动(环境变量不同)
        # 曾把整局 L1 历史读成空,详见 world_model.candidate_paths。
        civ, seed = gs._game_identity or ("unknown", 0)
        gs.world, src = wm_mod.WorldModel.load_best(civ, seed)
        gs._world_loaded_for = key
        if str(src) != key and gs.world.camps:
            log.info("世界模型从 %s 迁移到 %s(取内容更完整的一份)", src, path)
    wm = gs.world
    wm.current_turn = turn
    wm.game_id = key

    lines = await gs.conn.execute_read(wm_mod.build_world_scan_query())
    scan = wm_mod.parse_world_scan(lines)
    # 接线穿透:科技/市政集合供按需工具复用(assess_attack 的解锁判断等)
    _expert_cache(gs)["techs"] = scan.techs
    _expert_cache(gs)["civics"] = scan.civics
    wm.tiles_revealed, wm.tiles_total = scan.revealed, scan.total
    wm.city_count, wm.military_count = scan.cities, scan.military
    wm.total_production, wm.my_science = scan.production, scan.science
    wm.has_spaceport = scan.has_spaceport
    if scan.difficulty:
        wm.difficulty = scan.difficulty
    if scan.unique_units:
        wm.my_unique_units = dict(scan.unique_units)
    if scan.military_strength:
        wm.military_strength = scan.military_strength
    if scan.population:
        wm.my_population = scan.population
    if scan.techs:
        # 持久化已研科技:按需工具(get_strategy_notes 的 UU 解锁判断等)
        # 单独调用时没有本回合扫描,拿不到这个集合就会误报「还需某科技」。
        wm.researched_techs = sorted(scan.techs)
    # 威胁坐标:把「威胁等级红色」这个全局判断落到具体城市上
    try:
        wm.threat_positions = [
            [
                int(t.x),
                int(t.y),
                int(getattr(t, "base_moves", 2) or 2)
                + int(getattr(t, "range", 0) or 0),
                str(getattr(t, "owner_name", "") or ""),
            ]
            for t in (threats or [])
        ]
    except Exception:
        log.debug("威胁坐标记录失败", exc_info=True)

    seen: set[str] = set()
    for x, y, dist in scan.camps:
        seen.add(f"{x},{y}")
        wm.observe_camp(x, y, turn, dist)

    # 拆除判定:对本次没见到的已知营地,亲眼确认那格没营地了才算拆掉
    destroyed_now: list[wm_mod.Camp] = []
    unseen = [(c.x, c.y) for c in wm.active_camps() if c.key not in seen]
    if unseen:
        try:
            chk_lines = await gs.conn.execute_read(
                wm_mod.build_camp_check_query(unseen)
            )
            destroyed_now = wm.apply_camp_checks(wm_mod.parse_camp_check(chk_lines))
        except Exception:
            log.debug("营地拆除检测失败,维持原状", exc_info=True)

    # 从本回合事件里提取失联记录 —— end_turn.py 早就在生成这条消息,
    # 此前用完即扔,现在落盘成持久情报。
    for e in events:
        msg = getattr(e, "message", "") or ""
        if msg.startswith("LOST CONTACT:") or msg.startswith("VANISHED:"):
            m = re.search(
                r"(?:LOST CONTACT|VANISHED):\s*(\S+)\s+(\S+)\s+HP:(\S+)\s+"
                r"last (?:seen )?at \((\d+),(\d+)\)",
                msg,
            )
            if m:
                owner, utype, hp, lx, ly = m.groups()
                d = re.search(r"likely moved (\S+) into fog", msg)
                wm.record_lost_contact(
                    utype,
                    owner,
                    int(lx),
                    int(ly),
                    turn,
                    hp,
                    d.group(1) if d else "",
                )

    # 失联销档(2026-08-02 接线,5c):本回合视野里的敌军拿去和失联条目
    # 对账 —— 同类型且落在推测圈内即视为重新看见,销档。此前
    # reacquire_contacts 定义了但从未接线,条目半径一路膨胀到 22。
    if threats:
        try:
            wm.reacquire_contacts({f"{t.unit_type}:{t.x},{t.y}" for t in threats})
            wm.reacquire_by_sightings([(t.unit_type, t.x, t.y) for t in threats], turn)
        except Exception:
            log.debug("失联销档失败", exc_info=True)

    wm.prune_contacts(turn)

    # L2 计划链评估:完成条件用服务端事实判定,完成即自动激活后继
    facts = {
        "cities": scan.cities,
        "military": scan.military,
        "exploration": wm.exploration_pct,
        "destroyed_camps": {c.key for c in wm.camps.values() if c.destroyed},
        "techs": scan.techs,
    }
    wm.evaluate_plans(facts, turn)

    # L3 威胁仲裁:确定性红黄绿,红色时简报头部给唯一目标
    wm.threat_level, wm.threat_reason = wm_mod.compute_threat_level(wm, threats or [])
    for c in destroyed_now:
        # 拆营地是红色解除的最常见原因,值得单独庆祝一句
        wm.threat_reason = f"营地 ({c.x},{c.y}) 已确认拆除!" + wm.threat_reason

    # 死亡检测接线(2026-08-01):diff_units 此前定义了却从未被调用 ——
    # 单位快照现在由扫描 Lua 顺带产出(UNIT| 行,0 新增往返)。
    #
    # ⚠️ 必须在 wm.save() 【之前】跑:diff_units 会把本回合快照写进
    # wm.last_units,晚于 save 就永远落不了盘,于是每回合加载回来都是空的、
    # 每回合都走"首次初始化"分支 —— 死亡永远检测不到。
    # (2026-08-01 实机抓到:建造者阵亡而 death_tiles 仍为空,就是这个顺序错。)
    lost_units: list[tuple[str, int, int]] = []
    try:
        if scan.units:
            # 2026-08-05: 过滤本回合主动解散（delete）的单位位置 ——
            # Lua 侧 ExposedMembers.MCPDeletedUnits 记录，跨连接保留。
            deleted_now = set()
            try:
                deleted_now = await gs.get_deleted_units()
            except Exception:
                log.debug("读取解散记录失败", exc_info=True)
            if deleted_now:
                log.info("本回合解散 %d 个单位（不报被杀）", len(deleted_now))
            lost_units = wm.diff_units(scan.units, turn, exclude=deleted_now)
    except Exception:
        log.debug("单位死亡检测失败", exc_info=True)

    try:
        wm.save(path)
    except OSError:
        log.debug("世界模型落盘失败: %s", path, exc_info=True)

    # —— 采集各建议源(逐个降级,谁失败谁缺席,不阻断回合)——
    pace_lines: list[str] = []
    try:
        pace_lines = [
            f"⚠️ {p}"
            for p in params.check_pace(
                turn,
                scan.cities,
                scan.military,
                wm.difficulty,
                rival_city_median=_rival_city_median(gs),
            )[:2]
        ]
    except Exception:
        log.debug("节奏检查失败", exc_info=True)

    opp_reads: list = []
    opp_text = ""
    try:
        opp_reads = await _opponent_reads(gs, wm)
        if opp_reads:
            opp_text = opponents.render_opponents(
                opp_reads, has_barbarians=bool(wm.active_camps())
            )
    except Exception:
        log.debug("对手推演失败", exc_info=True)

    # 敌方宣战 → 真实 TurnEvent(审查 2026-08-05 F1-3)。
    # 此前危机手册(war_declared_crisis)/暂停建议/战争目标三处消费方都在
    # 扫 events 里的「宣战/DECLARED WAR」,而全仓没有任何生产者往 events
    # 里写过这两个词 —— 最高价值的危机手册上线以来一次都没触发过。
    # 这里复用 _opponent_reads 刚缓存的 civs 做跨回合 at-war diff,零新增
    # 往返。(状态在进程内存,mcp_call 每调用一进程的开发模式下退化为静默。)
    try:
        _cache = _expert_cache(gs)
        _civs_now = _cache.get("civs") or []
        _now_at_war = {
            int(getattr(c, "player_id", -1))
            for c in _civs_now
            if getattr(c, "is_at_war", False)
        }
        _prev_at_war = _cache.get("at_war_ids_prev")
        if _prev_at_war is not None:
            for _c in _civs_now:
                _pid = int(getattr(_c, "player_id", -1))
                if _pid in _now_at_war and _pid not in _prev_at_war:
                    events.append(
                        lq.TurnEvent(
                            priority=1,
                            category="war",
                            message=(
                                f"WAR DECLARED / 宣战:与 "
                                f"{getattr(_c, 'civ_name', '?')} (player {_pid}) "
                                f"进入战争状态!先定战争目标(打哪座城、"
                                f"止损线在哪),再动第一个单位。"
                            ),
                        )
                    )
        _cache["at_war_ids_prev"] = _now_at_war
    except Exception:
        log.debug("宣战事件桥接失败", exc_info=True)

    # 城市与政策数据上提:policy 渲染、忠诚危机、后期经济共用,只查一次
    cities_list: list = []
    try:
        cities_list, _ = await gs.get_cities()
        # 城市坐标落盘,供 RED_ALERT_CIVILIAN 按【该城】距离判定
        wm.city_positions = {
            str(getattr(c, "city_id", i)): [
                int(getattr(c, "x", 0)),
                int(getattr(c, "y", 0)),
            ]
            for i, c in enumerate(cities_list)
        }
        # A4 增长停滞 + A10 闲置资源:都要 CityInfo(housing / 各城信仰产出),
        # snap.cities 那份快照没有这两个字段,所以挂在这里。
        _stall = growth_stall_event(cities_list)
        if _stall:
            events.append(_stall)
        _infra = _check_infrastructure_gaps(cities_list)
        if _infra:
            events.append(_infra)
        _idle = idle_resource_event(_expert_cache(gs).get("overview"), cities_list, wm)
        if _idle:
            events.append(_idle)
    except Exception:
        log.debug("城市查询失败", exc_info=True)
    # 未激活伟人:吃世界扫描里的单位快照(0 新增往返)。挂在城市块之外 ——
    # 它不依赖 CityInfo,城市查询失败时这条检测不该跟着消失。
    try:
        _views = _scan_unit_views(scan)
        _gp = idle_great_person_event(_views)
        if _gp:
            events.append(_gp)
        _corps = corps_formation_event(_views, getattr(scan, "civics", ()))
        if _corps:
            events.append(_corps)
    except Exception:
        log.debug("闲置伟人/编队检测失败", exc_info=True)
    # 政策查询降频(2026-08-03 耗时优化):政策槽只在【解锁新市政/换政体】时变,
    # 而那两件事都有专门的通知与阻断提示。每 3 回合查一次,中间用缓存。
    policy_status = None
    _pc = _expert_cache(gs)
    if _pc.get("policy_status") is not None and turn - _pc.get("policy_turn", -99) < 3:
        policy_status = _pc["policy_status"]
    else:
        try:
            policy_status = await gs.get_policies()
            _pc["policy_status"], _pc["policy_turn"] = policy_status, turn
        except Exception:
            policy_status = _pc.get("policy_status")
            log.debug("政策查询失败,沿用缓存", exc_info=True)

    policy_text = ""
    try:
        policy_text = _render_policy_advice(
            policy_status, cities_list, wm, _pc.get("civics")
        )
    except Exception:
        log.debug("政策检查失败", exc_info=True)

    lategame_text = ""
    try:
        # 2026-08-09 T276 实机:太空项目是【单城】建造(太空港所在城),
        # 但 ETA 一直拿全帝国产能当分母 —— "7800/157≈50 回合" 实际是
        # 乌鲁克 28/t 的 280 回合。整个 T310-320 的胜利规划都被这个
        # 数字误导。分母 = 太空港城产能;没有太空港时用最高产城(那是
        # 太空港最可能落地的城)。
        _sp_prod = 0
        _best_prod = 0
        _sp_building = ""
        for _c in cities_list or []:
            _p = int(getattr(_c, "production", 0) or 0)
            _best_prod = max(_best_prod, _p)
            if any(
                d.split("@", 1)[0] == "DISTRICT_SPACEPORT"
                for d in (getattr(_c, "districts", []) or [])
            ):
                _sp_prod = max(_sp_prod, _p)
                _sp_building = str(getattr(_c, "currently_building", "") or "")
        # 2026-08-12 缺陷110:completed_projects 从来没有写入者,恒为空 ——
        # 简报因此永远说"0/5 已完成,还剩 7800 产能"(7800 = 五件的总和),
        # 而同一回合的 get_victory_progress 说 2/5。太空港城在造第 N 层就
        # 意味着前面各层已完成,这个信号每回合都在手边,零新增往返。
        # 只做并集:这个集合必须单调递增,否则"这回合没在造项目"会把它清零。
        try:
            _done_now = lategame.infer_completed_projects(
                _sp_building, wm.completed_projects
            )
            if len(_done_now) != len(wm.completed_projects or []):
                wm.completed_projects = sorted(_done_now)
        except Exception:
            log.debug("太空项目完成推断失败", exc_info=True)
        lg_ctx = lategame.LateGameContext(
            turn=turn,
            era=era or "",
            victory_type=wm.victory_type,
            total_production=wm.total_production,
            city_count=wm.city_count,
            has_spaceport=wm.has_spaceport,
            completed_projects=set(wm.completed_projects),
            techs=scan.techs,
            my_science=wm.my_science,
            project_city_production=_sp_prod or _best_prod,
        )
        lategame_text = lategame.render_lategame(lategame.check_lategame(lg_ctx))
        # E1 路线 ETA:每 _POWER_INTERVAL 回合与分数归因同频上屏。
        # 「里程碑」回答下一步做什么,「ETA」回答这条路还赢不赢得了 ——
        # 罗马局活到 T362 每步都有里程碑,却从没人问过后者。
        if turn % _POWER_INTERVAL == 0:
            _expert_cache(gs)["victory_eta_lines"] = (
                turn,
                victory_eta_lines(gs, wm, lategame.my_eta_turns(lg_ctx)),
            )
    except Exception:
        log.debug("中后期检查失败", exc_info=True)

    # L4 复盘:每回合把事实落盘(服务端写,模型改不了)。
    # lost_units 必须传 —— 见 _record_debrief docstring(2026-08-12 罗马 T384)。
    try:
        _record_debrief(gs, wm, scan, turn, lost_units)
    except Exception:
        log.debug("复盘记录失败", exc_info=True)

    # —— 专家建议采集(2026-08-01 wiring pass:18 模块接线)——
    expert_items: list[advice_mod.Advice] = []
    try:
        expert_items = await _gather_expert_advice(
            gs,
            wm,
            scan,
            turn,
            era or "",
            opp_reads,
            cities_list,
            policy_status,
            lost_units,
            events,
        )
    except Exception:
        log.debug("专家建议采集失败,该组缺席", exc_info=True)

    # —— L1/L2/L3 策略层(2026-08-03,方案 v4)——
    power = None
    kit = None
    units_seen: list = []
    try:
        power = await measure_power(gs, wm, scan, turn)
        kit = derive_civ_kit(gs, wm, scan)
        civs_raw = _expert_cache(gs).get("civs") or []
        # 单位查询只在【交战中】才做:决策树 13 是唯一要单位血量的树,
        # 和平期查了也用不上。用户已明确抱怨过 end_turn 慢,不能每回合白加往返。
        if any(getattr(c, "is_at_war", False) for c in civs_raw):
            try:
                units_seen = await gs.get_units()
            except Exception:
                log.debug("交战期单位查询失败,决策树 13 降级", exc_info=True)
        if not units_seen:
            units_seen = _scan_unit_views(scan)
        # 碾压期不兑现 → 顶格催促(priority 1)。挂在 power 之后是因为它
        # 要 my_expansion_rate;events 在叙事引擎处才被消费,这里 append 仍有效。
        try:
            _dom = dominance_stall_event(power, units_seen)
            if _dom:
                events.append(_dom)
        except Exception:
            log.debug("碾压期兑现检测失败", exc_info=True)
        expert_items += strategy_items(
            gs,
            wm,
            scan,
            turn,
            power,
            kit,
            civs_raw,
            threats or [],
            units_seen,
            cities_list,
            era or "",
        )
    except Exception:
        log.debug("策略层接线失败,该组缺席", exc_info=True)

    # —— 叙事简报引擎(AG/AH/AJ/AL/AN 五合一)。引擎自身出错时回退到
    #    旧的碎片式拼接:简报可以难看,不能空白。——
    try:
        return _compose_narrative(
            gs,
            wm,
            scan,
            turn,
            era or "",
            threats or [],
            key,
            pace_lines,
            opp_text,
            policy_text,
            lategame_text,
            events,
            expert_items,
            power,
            kit,
            _beneficiary_facts(units_seen),
        )
    except Exception:
        log.debug("叙事简报引擎失败,回退碎片式", exc_info=True)
        sections = [wm_mod.render_briefing(wm, turn, threats or [])]
        if pace_lines:
            sections.append("== 发展节奏 ==\n" + "\n".join(pace_lines))
        sections.extend([opp_text, policy_text, lategame_text])
        return "\n\n".join(s for s in sections if s)


# ===========================================================================
# 叙事简报引擎 —— 任务 AG/AH/AJ/AL/AN 五合一
#
# 旧流程:各模块往 9 段里塞文本 → 拼成一堵墙。模型拿到的是碎片。
# 新流程:各源 → advice.from_legacy() 统一成 Advice 流 → 降噪(AH)
#        → 暂停点注入(AL) → advice.arbitrate() 排序/截断/五段式(AG/AN)。
# 风险语言(AJ)内建在 Advice.confidence:确定/推荐直接陈述,
# 可选/冒险才在句内亮数字区间 —— 由 advice.render_item 落地,这里不重复。
#
# 人类高手读完简报后脑子里是一个故事:"我是谁、谁在威胁我、这回合最要紧的
# 三件事、顺了怎么走、砸了怎么退"。引擎的职责是把 30+ 条碎片建议压成这个
# 故事的骨架 —— 把三件事讲成因果链是模型的工作,把对的三件事放进前三行
# 是引擎的工作(advice.py §6)。
#
# 跨回合状态(AH 的降噪记忆/AL 的时代水位)存进程级字典,按对局隔离。
# 进程重启即清零 —— 代价是重启后头几回合可能重复一次已压制的建议,
# 换来的是零持久化负担;这不值得为它改 WorldModel 的落盘格式。
# ===========================================================================

BRIEF_LINE_BUDGET = 30  # 建议区硬上限(计划书 AN;局面/预案段另计)
_SITUATION_MAX_LINES = 12  # 【当前局面】段的行数上限
_NOISE_REPEAT_LIMIT = 3  # 连续 N 回合原样重复且未采纳 → 休眠
_NOISE_SLEEP_TURNS = 5  # 休眠回合数
_EXPL_REMIND_GAP = 5  # 探索度 8-15% 档的提醒间隔(AH)

_NARRATIVE_STATE: dict[str, dict] = {}  # game_key → 跨回合记忆


def _noise_state(game_key: str) -> dict:
    return _NARRATIVE_STATE.setdefault(
        game_key, {"items": {}, "era": None, "expl_last": -999}
    )


def _filter_noise(
    items: list[advice_mod.Advice], st: dict, turn: int
) -> tuple[list[advice_mod.Advice], int]:
    """AH 重复抑制:同一建议连续 3 回合原样出现(未采纳的可观测代理:
    文本一字未变 = 局面没动)→ 休眠 5 回合。文本变了 = 有进展,计数归零。

    两类豁免:
      · 生存/阻断档 —— 安全信息宁可烦人
      · 常驻上下文(`standing=True`)—— 科技链/政体链/路线指标每回合本来就
        长一样,它不是"又提醒了一次",而是这一回合的坐标系。T15 实测:
        8 条建议休眠后整个「主线」段消失,正是因为主线被当成重复建议压掉了。
    """
    kept: list[advice_mod.Advice] = []
    slept = 0
    for a in items:
        if a.priority <= advice_mod.Priority.BLOCKING or a.standing:
            kept.append(a)
            continue
        rec = st["items"].setdefault(
            a.identity, {"run": 0, "last": -9, "until": -1, "text": ""}
        )
        if turn < rec["until"]:
            slept += 1
            continue
        if rec["last"] == turn - 1 and rec["text"] == a.action:
            rec["run"] += 1
        elif rec["last"] != turn:  # 同回合重复调用不涨计数
            rec["run"] = 1
        rec["last"], rec["text"] = turn, a.action
        if rec["run"] > _NOISE_REPEAT_LIMIT:
            rec["until"] = turn + _NOISE_SLEEP_TURNS
            rec["run"] = 0
            slept += 1
            continue
        kept.append(a)
    return kept, slept


def _pause_advice(
    st: dict, turn: int, era: str, events: list
) -> list[advice_mod.Advice]:
    """AL 强制暂停点 → BLOCKING 档建议(优先级仅次于生存,永不被截断)。"""
    out: list[advice_mod.Advice] = []

    # 时代转换:新政体/新单位/新区域解锁 = 策略重审窗口
    prev = st.get("era")
    if era and prev and era != prev:
        out.append(
            advice_mod.Advice(
                module="pause",
                priority=advice_mod.Priority.BLOCKING,
                action=f"时代已进入 {era} —— 停一步,重审策略",
                reason="新政体/新单位/新区域类型随时代解锁,上个时代的打法不自动适用",
                consequence="用 get_tech_civics 和 get_policies 核对新解锁项;"
                "尤其检查:该换政体吗?军事单位该升级换代吗?",
                confidence=advice_mod.Confidence.CERTAIN,
                dedup_key=f"pause:era:{era}",
            )
        )
    if era:
        st["era"] = era

    # 被宣战:战争目标必须先于战争动作
    for e in events or []:
        msg = str(getattr(e, "message", "") or "")
        if "宣战" in msg or "DECLARED WAR" in msg.upper():
            out.append(
                advice_mod.Advice(
                    module="pause",
                    priority=advice_mod.Priority.BLOCKING,
                    action="先回答再动手:这场仗的目标是什么?打到什么程度停?",
                    reason="被动卷入的战争最容易打成无目标消耗战",
                    consequence="没有战争目标就没有停战判据 —— 用 assess_attack "
                    "评估每个可打目标,守不住的别硬守,够不着的别硬追",
                    confidence=advice_mod.Confidence.CERTAIN,
                    dedup_key=f"pause:war:{turn}",
                )
            )
            break
    return out


def _situation_block(
    gs: GameState,
    wm,
    scan,
    turn: int,
    era: str,
    threats: list,
    opp_text: str,
    st: dict,
) -> str:
    """【当前局面】—— AN 第一段。素材与 render_briefing 同源,但按叙事
    优先级重排,并执行 AH 的频率/条数规则。上限 12 行。"""
    lines: list[str] = []

    icon = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}.get(wm.threat_level, "🟢")
    head = f"{icon} 威胁 {wm.threat_level}"
    if wm.threat_reason:
        head += f" — {wm.threat_reason}"
    if wm.victory_type:
        head += f" ‖ 胜利路线:{wm.victory_type}(T{wm.victory_locked_turn} 锁定)"
    lines.append(head)
    if wm.threat_level == "RED":
        lines.append(wm_mod.red_directive(wm))

    # 对手推演(能力上界,★★★★★ 实测最有用的一段)—— 紧跟威胁
    projection = wm_mod.render_enemy_projection(threats)
    if projection:
        lines.extend(projection.split("\n")[:4])

    # 帝国一行账。
    # 2026-08-03 传感器审计:此前这里写「军事 {military_count}」——
    # 那是**单位数**,而 L1 分档、军力排名、对手推演读的都是
    # GetMilitaryStrength 的真值。同一个词指两个数正是缺陷 1e 的形态
    # (真值 191 显示成 7)。现在两个数都给,而且各自带单位。
    empire = f"帝国:{wm.city_count} 城 ‖ 军事 {wm.military_count} 支"
    if wm.military_strength:
        empire += f"(军力 {wm.military_strength})"
    empire += f" ‖ 产能 {wm.total_production} ‖ 科技 {wm.my_science}/回合"
    if era:
        empire += f" ‖ {era}"
    lines.append(empire)

    # 军队编成(2026-08-07,用户点名"全是前排全是后排")。挂在帝国账下面 ——
    # 上一行报的是军力标量,这一行报它拆开之后能不能办事。
    # 数据源与 army_era_profile 同一处(快照单位 + combat_strength>0),
    # 不新增往返。
    try:
        from civ_mcp import war_planner as _wp2

        _snap_c = getattr(gs, "_last_snapshot", None)
        if _snap_c is not None:
            _gap = _wp2.composition_gap(
                [
                    str(getattr(u, "unit_type", "") or "")
                    for u in _snap_c.units.values()
                    if int(getattr(u, "combat_strength", 0) or 0) > 0
                ]
            )
            if _gap:
                lines.append(_gap)
    except Exception:
        log.debug("军队编成检查失败", exc_info=True)

    # 探索度(AH 自适应频率:<8% 每回合;8-15% 每 5 回合;>15% 沉默)
    if wm.tiles_total:
        pct = wm.exploration_pct
        if pct < wm_mod.EXPLORATION_POOR:
            lines.append(
                f"🔴 地图仅探明 {pct:.1%} —— 看不见的营地不会停止出兵,"
                f"探图优先级高于再造一个建造者。"
            )
            st["expl_last"] = turn
        elif (
            pct < wm_mod.EXPLORATION_LOW and turn - st["expl_last"] >= _EXPL_REMIND_GAP
        ):
            lines.append(f"⚠️ 探明 {pct:.1%},视野偏窄 —— 闲置单位继续探图。")
            st["expl_last"] = turn

    # 营地:最多 2 条 + 汇总(AH 条数上限)
    camps = sorted(
        wm.active_camps(),
        key=lambda c: c.dist_to_capital if c.dist_to_capital >= 0 else 999,
    )
    for c in camps[:2]:
        d = f"距首都 {c.dist_to_capital} 格" if c.dist_to_capital >= 0 else "距离未知"
        lines.append(f"🏕 营地 ({c.x},{c.y}) {d},T{c.first_seen_turn} 发现,未拆除。")
    if len(camps) > 2:
        lines.append(f"   (另有 {len(camps) - 2} 个已知营地未列出)")

    # 失联:只留最近 2 条 + 汇总(AH 明文规则,治 T15 后的刷屏)
    stale = sorted(wm.stale_contacts(), key=lambda c: -c.last_turn)
    shown = 0
    for c in stale:
        if turn - c.last_turn <= 0:
            continue
        if shown >= 2:
            break
        name = c.unit_type.replace("UNIT_", "")
        lines.append(
            f"👁 失联 {c.owner} {name} — T{c.last_turn} 见于 "
            f"({c.last_x},{c.last_y}),可能在半径 {c.guess_radius(turn)} 内。"
        )
        shown += 1
    rest = max(0, len([c for c in stale if turn - c.last_turn > 0]) - shown)
    if rest:
        lines.append(f"   (还有 {rest} 条失联记录已省略)")

    # 对手推演的意图层:只保留交战/高风险的头两行(缩进的议程行是展开信息)
    if opp_text:
        hostile = [ln for ln in opp_text.split("\n") if ln.startswith(("⚔️", "⚠️"))]
        lines.extend(hostile[:2])

    # 计划链:一眼看到"在做什么、下一步是什么"
    plan_bits = []
    for p in [p for p in wm.plans if p.status == "done" and p.done_turn >= turn - 1][
        :1
    ]:
        plan_bits.append(f"✔#{p.id} {p.goal}")
    for p in wm.active_plans()[:2]:
        plan_bits.append(f"▶#{p.id} {p.goal}")
    for p in wm.pending_plans()[:1]:
        plan_bits.append(f"⏸#{p.id} {p.goal}")
    if plan_bits:
        lines.append("📋 " + " ‖ ".join(plan_bits))
    elif not wm.active_plans() and not wm.pending_plans():
        lines.append("📋 没有进行中的计划 —— 用 plan_add 挂下一阶段目标。")

    # AK:开局前三回合注入往局教训 —— 上一局怎么死的,这一局开局就该知道
    if turn <= 3:
        try:
            from civ_mcp import learning_store

            lessons = learning_store.render_lessons_briefing()
            if lessons:
                lines.extend(lessons.split("\n")[:4])
        except Exception:
            pass
        # 高玩知识库的开局提醒:20 回合例行审视 T20 才首现,而「侦察兵
        # 别死/原地建城」恰恰是 T1-3 最值钱 —— 开局阶段单独注入一行
        try:
            from civ_mcp import expert_playbook

            hl = expert_playbook.phase_headline(turn)
            if hl:
                lines.append(hl)
        except Exception:
            pass

    # AL:每 20 回合放大局面段(注入 AM 的路线复审)。
    # 审查 2026-08-05 F2-6:此前直接 append 在末尾再 lines[:12] 截断 ——
    # 中后期局面一忙(营地/失联/敌对占满 12 行),复审段整块被切掉,
    # 20 回合一次的"你还在对的路线上吗"从没真正出现过。现在单独成块,
    # 从 12 行预算里为它预留槽位,其余内容让位。
    review: list[str] = []
    if turn > 0 and turn % 20 == 0:
        review.append(f"── T{turn} 例行审视:你还在对的路线上吗? ──")
        try:
            from civ_mcp import opening_assessor

            civ, _seed = gs._game_identity or ("", 0)
            mid = opening_assessor.assess_midgame(
                {"civ": civ, "turn": turn, "map_type": wm.map_type},
                wm.victory_type,
            )
            if mid:
                review.extend(mid.split("\n")[:4])
            else:
                raise ValueError("评估器无输出")
        except Exception:
            review.append(
                "   自查三问:对手的胜利进度查了吗(get_victory_progress)?"
                "路线推进速度符合预期吗?有没有更顺的路?"
            )
        # 高玩知识库的阶段提醒:例行审视是唯一固定出现的反思时刻,
        # 一行阶段要点 + 入口指引,完整条目按需拉取
        try:
            from civ_mcp import expert_playbook

            hl = expert_playbook.phase_headline(turn)
            if hl:
                review.append(hl)
        except Exception:
            pass

    budget = max(0, _SITUATION_MAX_LINES - len(review))
    return "\n".join(lines[:budget] + review)


def _prepend_strategy(situation: str, turn: int, power, kit, scan, gap_lines=()) -> str:
    """把 L1 档位放在局面段第一行,L2 套件按 _KIT_SHOW_EVERY 复述。

    档位在**最前**是刻意的:它是读其余每一行的解释框架 ——
    同一句"军力 148"在领先档是进攻资本,在被碾压档是最后的家底。

    gap_lines 是分数归因/斜率(score_gap),排在档位之后:档位说"你在
    哪一档",分数说"这一档是在变好还是变坏"。
    """
    try:
        from civ_mcp import civ_kit as ck
        from civ_mcp import power_balance as pb

        head: list[str] = []
        if power is not None:
            head.append(pb.headline(power))
        head.extend(gap_lines or ())
        if kit is not None and (turn <= 3 or turn % _KIT_SHOW_EVERY == 0):
            text = ck.render(kit, researched=set(getattr(scan, "techs", ()) or ()))
            if text:
                # 只取窗口结论行,完整套件走 get_strategy_notes 按需拉
                head.extend(
                    [ln for ln in text.split("\n") if ln.startswith(("【", "⇒"))][:3]
                )
        if not head:
            return situation
        return "\n".join(head + ([situation] if situation else []))
    except Exception:
        return situation


def _plan_a_line(wm) -> str:
    """【如果顺利】—— 主线的下一站,来自计划链 + 胜利路线的关键指标。"""
    bits = []
    active = wm.active_plans()
    if active:
        bits.append("按计划链走:" + " → ".join(f"#{p.id} {p.goal}" for p in active[:2]))
    path = lategame.VICTORY_PATHS.get(wm.victory_type)
    if path:
        bits.append(f"每回合盯住:{path.key_metric}")
    return ";".join(bits)


def _plan_b_line(wm) -> str:
    """【如果出问题】—— 头号输法 + 换道出口,来自威胁仲裁与路线播书。"""
    if wm.threat_level != "GREEN" and wm.threat_reason:
        return (
            f"威胁已是 {wm.threat_level}({wm.threat_reason[:30]})—— "
            f"生存优先,所有发展项让路;解除后再回主线。"
        )
    path = lategame.VICTORY_PATHS.get(wm.victory_type)
    if path:
        loss = path.common_loss.split("。")[0]
        return (
            f"这条路的头号输法:{loss}。征兆出现就评估换道"
            f"(set_victory_goal)—— 沉没成本不是留下的理由。"
        )
    return (
        "尚未锁定胜利路线 —— 探图与邻居情报到位后尽早用 "
        "set_victory_goal 锁定,没有路线就没有取舍标准。"
    )


def _compose_narrative(
    gs: GameState,
    wm,
    scan,
    turn: int,
    era: str,
    threats: list,
    game_key: str,
    pace_lines: list[str],
    opp_text: str,
    policy_text: str,
    lategame_text: str,
    events: list,
    expert_items: list | None = None,
    power=None,
    kit=None,
    facts: dict | None = None,
) -> str:
    """AN 五段式组装:局面 → 三件事+分层建议 → 顺利/出问题 → 省略汇总。

    power/kit 是 L1/L2 的产物,可为 None(测量失败或尚未接线)——
    缺了只是局面段少两行,不影响其余结构。
    """
    st = _noise_state(game_key)

    items: list[advice_mod.Advice] = []
    items += _pause_advice(st, turn, era, events)
    items += advice_mod.from_legacy("pace", pace_lines)
    items += advice_mod.from_legacy(
        "policy", policy_text.split("\n") if policy_text else []
    )
    items += advice_mod.from_legacy(
        "lategame", lategame_text.split("\n") if lategame_text else []
    )
    items += list(expert_items or [])  # wiring pass:18 模块的建议流

    # P6:先删掉受益对象不存在的建议,再降噪 —— 顺序不能反,
    # 否则一条无效建议会先占掉降噪配额再被删。
    items, no_beneficiary = advice_mod.verify_beneficiaries(items, facts)
    items, slept = _filter_noise(items, st, turn)
    situation = _situation_block(gs, wm, scan, turn, era, threats, opp_text, st)
    # 分数归因只在实测回合(每 _POWER_INTERVAL 回合)上屏 —— 中间回合
    # 数字不会变,复述就是零价值行(过程指标 #2 要压的正是这个)。
    _cache = _expert_cache(gs)
    _gap = _cache.get("score_gap_lines")
    _eta = _cache.get("victory_eta_lines")
    situation = _prepend_strategy(
        situation,
        turn,
        power,
        kit,
        scan,
        gap_lines=list(_gap[1] if _gap and _gap[0] == turn else ())
        + list(_eta[1] if _eta and _eta[0] == turn else ()),
    )

    # F3:CORE+ 建议自动落盘。放在 arbitrate 之前 —— 记的是"系统给过
    # 什么",不是"简报最后显示了什么";被行数预算截掉的硬建议同样算给过。
    try:
        from civ_mcp import advice_log

        advice_log.record(turn, items)
    except Exception:
        log.debug("建议留痕失败(不影响简报)", exc_info=True)

    body = advice_mod.arbitrate(
        items,
        situation=situation,
        plan_a=_plan_a_line(wm),
        plan_b=_plan_b_line(wm),
        line_budget=BRIEF_LINE_BUDGET,
    )
    if not body:
        body = f"【当前局面】{situation}" if situation else ""
    if slept:
        body += (
            f"\n(降噪:{slept} 条连续 {_NOISE_REPEAT_LIMIT} 回合未被采纳的"
            f"建议休眠中,{_NOISE_SLEEP_TURNS} 回合后回访)"
        )
    if no_beneficiary:
        body += (
            f"\n(已删 {len(no_beneficiary)} 条受益对象不存在的建议:"
            f"{';'.join(no_beneficiary[:2])})"
        )
    return body


def _render_policy_advice(policy_status, cities_list, wm, civics=None) -> str:
    """政策确定性检查(2026-08-01 重构:数据由调用方上提,不再自查)。"""
    if policy_status is None:
        return ""
    slots: dict[str, list[str]] = {}
    for s in getattr(policy_status, "slots", None) or []:
        slots.setdefault(s.slot_type, []).append(s.current_policy or "")
    avail = {
        getattr(p, "policy_type", "")
        for p in getattr(policy_status, "available_policies", None) or []
    }
    producing = [
        str(getattr(c, "currently_building", "") or "")
        for c in cities_list or []
        if getattr(c, "currently_building", "")
    ]
    ctx = policy.PolicyContext(
        slots=slots,
        available=avail,
        producing=producing,
        threat_level=wm.threat_level,
        victory_type=wm.victory_type,
        exploration=wm.exploration_pct,
        government=str(getattr(policy_status, "government_type", "") or ""),
        completed_civics=set(civics or ()),
    )
    return policy.render_policy_advice(policy.check_policies(ctx))


async def _opponent_reads(gs: GameState, wm) -> list:
    """查外交 → 推演读数(2026-08-01 重构:返回结构化 reads,
    渲染/联合宣战/军力排名三个消费方共用同一次查询)。"""
    try:
        civs = await gs.get_diplomacy()
    except Exception:
        return []
    # 原始 CivInfo 留一份:决策树 12/13 要 agendas(本地化名)与 visible_cities,
    # 这些在 OpponentRead 里被压掉了。同一次查询多个消费方,不加往返。
    try:
        _expert_cache(gs)["civs"] = civs
        # 领袖类型索引(审查 F2-8):doctrine 议程直查的数据源 —— 此前
        # 全仓没有任何写入点,cache.get("leader_types") 恒为空 dict,
        # 隐藏议程(???)的历史议程兜底从没生效过。
        _expert_cache(gs)["leader_types"] = {
            c.player_id: c.leader_type for c in civs if getattr(c, "leader_type", "")
        }
    except Exception:
        pass
    return [
        opponents.read_opponent(
            c, wm.military_strength or wm.military_count, wm.city_count
        )
        for c in civs
        if getattr(c, "has_met", False)
    ]


# ===========================================================================
# 专家建议采集 —— 2026-08-01 wiring pass
#
# 42 个任务的专家模块此前是"库存"状态:函数存在、测试通过,但主循环一行
# 都不调用。这里把它们分三档接进简报流,全部产出 Advice 进 arbitrate:
#
#   每回合(0 新增往返)  tech_path/boost(吃扫描的 techs/civics)、危机预案
#                        (吃 diff_units/事件/上提的 cities)、联合宣战(吃
#                        opponents reads)、区域效应/太空收尾(纯计算)
#   相位错开的周期查询   T%5==1 总督 ‖ T%5==2 城邦 ‖ T%5==4 尤里卡状态缓存
#                        T%10==3 胜利阻断 ‖ T%10==5 军力排名 ‖ T%10==7 伟人
#                        T%10==9 间谍 —— 每回合最多 1 次额外往返(~324ms)
#   条件触发             有空贸易容量才查贸易目的地(TRCAP 穿透判定)
#
# 每个源独立 try/except:谁失败谁缺席,绝不阻断回合。数据缺口如实降级
# (civics 扫描失败 → 市政/尤里卡建议整组跳过,空集会产生错误建议)。
# 仍未接线(数据源不存在):citizen_optimizer(无公民分配查询)、
# chop_overflow(建造者任务查询无地貌产出信息)、wonder_tier(做按需工具)。
# F2-4 三模块接线状态(2026-08-05 第二轮,部分接通):
#   event_window   ✅ 升级窗口/战略资源揭示已接(tech_civics 缓存 + 快照
#                  单位 + 库存);政体/时代/对手奇观窗口仍缺数据源,静默。
#   resource_trade ✅ 接进 get_trade_options 工具(DealOptions 就是
#                  RivalHoldings 的设计数据源);我方份数未知 → 盈余段
#                  静默,只做"你没有而他有"的补缺方向。
#   culture_endgame✅ 胜利路线=文化时出旅游加成清单(政策槽位取
#                  policy_status,数据缺失整段静默);开放边界/贸易路线
#                  段与国家公园地块仍缺数据源。
# ===========================================================================

_MID_ERAS = ("ERA_MEDIEVAL", "ERA_RENAISSANCE")
_LATE_ERAS = (
    "ERA_INDUSTRIAL",
    "ERA_MODERN",
    "ERA_ATOMIC",
    "ERA_INFORMATION",
    "ERA_FUTURE",
)


# ===========================================================================
# L1/L2/L3 策略层接线(2026-08-03,方案 v4)
#
# 顺序刻意是 L1 → L2 → L3:策略引擎的每个分支都读 L1 的实测档位,
# 没有档位时 doctrine 会自己降级为不出手(见 doctrine.py §1)。
#
# 成本:L1 每 10 回合一次 rival 快照(1 次往返);L2 纯计算读 gamedata;
# L3 纯计算。合计每 10 回合 +1 往返 —— 与方案 v4「不新增独立 Lua 往返」
# 的约束一致(这一次是复用已有的 build_rival_snapshot_query)。
# ===========================================================================

_POWER_INTERVAL = 10  # 局势测量间隔(方案 v4 §L1.1)
_KIT_SHOW_EVERY = 20  # 文明套件在简报里的复述间隔


def _expert_cache(gs) -> dict:
    """挂在 GameState 实例上的接线缓存(overview/贸易容量/尤里卡状态)。"""
    c = getattr(gs, "_expert_cache", None)
    if c is None:
        c = {}
        try:
            gs._expert_cache = c
        except Exception:
            pass
    return c


class _ScanUnit:
    """世界扫描里的单位轻量视图 —— 只有类型/坐标/是否军事,**没有血量**。

    和平期不为了策略层多发一次 get_units(用户点名过 end_turn 慢),但
    决策树 1/2/10 只需要"有几个军事单位",扫描里就有。血量留 0:
    `_hp_pct` 对 max_health<=0 返回 100,于是所有依赖血量的建议
    (轮换/低血攻城)在没有真实血量时**自动静默**,而不是拿假的满血下判断。
    """

    __slots__ = (
        "name",
        "unit_type",
        "x",
        "y",
        "combat_strength",
        "ranged_strength",
        "health",
        "max_health",
    )

    def __init__(self, unit_type: str, x: int, y: int, is_mil: bool) -> None:
        from civ_mcp import civ_kit

        row = civ_kit.unit_row(unit_type)
        self.name = row.get("Name") or unit_type.replace("UNIT_", "")
        self.unit_type = unit_type
        self.x, self.y = x, y
        try:
            self.combat_strength = int(float(row.get("Combat") or 0))
            # 攻城类的远程在 Bombard 列(T291):只读 RangedCombat 会把投石机、
            # 攻城炮、大炮的主武器读成 0。
            self.ranged_strength = max(
                int(float(row.get("RangedCombat") or 0)),
                int(float(row.get("Bombard") or 0)),
            )
        except (TypeError, ValueError):
            self.combat_strength = 1 if is_mil else 0
            self.ranged_strength = 0
        self.health = 0
        self.max_health = 0


def _rival_city_median(gs) -> int | None:
    """上一次局势测量里的对手城市数中位数。没测过返回 None(回落到经验表)。

    用缓存而不是现测,是因为节奏检查发生在 measure_power 之前;
    城市数变化很慢,上一次(≤10 回合前)的中位数完全够用。
    """
    try:
        snap = _expert_cache(gs).get("power_snapshot")
        if snap is None or not snap.rivals:
            return None
        counts = sorted(s.cities for s in snap.rivals)
        mid = len(counts) // 2
        return (
            counts[mid] if len(counts) % 2 else ((counts[mid - 1] + counts[mid]) // 2)
        )
    except Exception:
        return None


_CAVALRY_CLASSES = ("PROMOTION_CLASS_LIGHT_CAVALRY", "PROMOTION_CLASS_HEAVY_CAVALRY")
_RELIGIOUS_TOKENS = ("MISSIONARY", "APOSTLE", "INQUISITOR", "GURU")


def _beneficiary_facts(units: list) -> dict[str, int]:
    """数出建议里会提到的受益对象。**只放能可靠观测的键**。

    键缺席 = 没观测到 = 放行(见 advice.verify_beneficiaries)。
    使者/间谍/巨作没有低成本的每回合观测,所以不进这张表 —— 宁可不判,
    也不要因为"我没查到"就把一条正确建议删掉。
    """
    from civ_mcp import civ_kit

    facts = {"cavalry": 0, "naval": 0, "siege": 0, "religion_units": 0}
    for u in units or []:
        utype = getattr(u, "unit_type", "") or ""
        row = civ_kit.unit_row(utype)
        promo = row.get("PromotionClass", "")
        if promo in _CAVALRY_CLASSES:
            facts["cavalry"] += 1
        if promo == "PROMOTION_CLASS_SIEGE":
            facts["siege"] += 1
        if row.get("Domain") == "DOMAIN_SEA":
            facts["naval"] += 1
        if any(tok in utype for tok in _RELIGIOUS_TOKENS):
            facts["religion_units"] += 1
    return facts


def _scan_unit_views(scan) -> list:
    """把 scan.units 的 "type|x|y|is_mil" 串还原成轻量单位视图。"""
    out: list = []
    for raw in (getattr(scan, "units", None) or {}).values():
        parts = str(raw).split("|")
        if len(parts) < 4:
            continue
        try:
            out.append(
                _ScanUnit(parts[0], int(parts[1]), int(parts[2]), parts[3] == "1")
            )
        except (TypeError, ValueError):
            continue
    return out


async def measure_power(gs: GameState, wm, scan, turn: int):
    """L1:每 _POWER_INTERVAL 回合实测一次局势档位。返回 PowerSnapshot 或 None。

    我方数字全部取自本回合的世界扫描(与简报同源,不会出现"简报说 148、
    分档用 7"这种两套口径 —— 那正是校准局 1e 缺陷的形态)。
    """
    from civ_mcp import power_balance

    cache = _expert_cache(gs)
    snap = cache.get("power_snapshot")
    if snap is not None and 0 <= turn - snap.turn < _POWER_INTERVAL:
        return snap
    try:
        # 复用本回合已取的 rival 快照(每 5 回合的记分板查询),避免重复往返
        cached = cache.get("rival_lines")
        if cached and cached[0] == turn:
            lines = cached[1]
        else:
            lines = await gs.conn.execute_write(lq.build_rival_snapshot_query())
        rivals = [
            power_balance.Side.from_rival(r)
            for r in lq.parse_rival_snapshot_response(lines)
        ]
        # 我方军力的**唯一权威源**。世界扫描走 GameCore 读不到这个 API,
        # 于是 wm.military_strength 一直是 0,军力排名/对手推演/危机判断
        # 全部回落到单位数(缺陷 1e 的真正根因,2026-08-03 实机定位)。
        own = lq.parse_own_military_strength(lines)
        if own:
            wm.military_strength = own
    except Exception:
        log.debug("对手快照查询失败,局势测量跳过", exc_info=True)
        return snap
    me = power_balance.Side(
        name="我方",
        player_id=getattr(gs, "_local_player_id", 0),
        cities=scan.cities or wm.city_count,
        techs=len(scan.techs),
        civics=len(scan.civics),
        science=float(scan.science or wm.my_science or 0),
        culture=float(scan.culture or 0),
        military=int(scan.military_strength or wm.military_strength or 0),
        pop=int(getattr(scan, "population", 0) or getattr(wm, "my_population", 0)),
        # 分数归因(score_gap)的我方数据源。overview 每回合都会刷新进
        # cache,这里读到的是本回合的值;读不到给 0,score_gap 会因此
        # 静默(诚实缺数据),不会编一条归因。
        score=int(getattr(cache.get("overview"), "score", 0) or 0),
    )
    # 军队代差(2026-08-05"科技是第一生产力"):主力落后 ≥2 个时代时,
    # 军力比的升档作废 —— T335 的 13 个中世纪弩手撑起的 1.57× 是纸面分。
    army_lag = None
    try:
        from civ_mcp import war_planner as _wp

        _ov = cache.get("overview")
        _snap_units = getattr(gs, "_last_snapshot", None)
        if _ov is not None and _snap_units is not None:
            _prof = _wp.army_era_profile(
                [
                    str(getattr(u, "unit_type", "") or "")
                    for u in _snap_units.units.values()
                    if int(getattr(u, "combat_strength", 0) or 0) > 0
                ]
            )
            _gidx = _wp.era_index(getattr(_ov, "era_name", "") or "")
            if _prof and _gidx > 0:
                army_lag = max(0, _gidx - _prof["median"])
    except Exception:
        army_lag = None
    snap = power_balance.measure(
        turn,
        me,
        rivals,
        army_era_lag=army_lag,
        difficulty=getattr(wm, "difficulty", "") or "",
    )
    hist = cache.get("power_history")
    if hist is None:
        hist = power_balance.PowerHistory()
        cache["power_history"] = hist
    power_balance.attach_history(snap, hist)
    cache["power_snapshot"] = snap
    # 分数归因 + 斜率(提分计划 P0-1/P0-2):跟着局势测量的节奏走
    # (每 _POWER_INTERVAL 回合),渲染侧按 turn 匹配只在测量回合上屏。
    # attach_history 之后才算 —— 斜率要用刚记进去的这一条。
    try:
        from civ_mcp import score_gap as _sg

        cache["score_gap_lines"] = (turn, _sg.render(_sg.build(snap, hist)))
    except Exception:
        log.debug("分数归因计算失败,跳过", exc_info=True)
    return snap


def derive_civ_kit(gs: GameState, wm, scan):
    """L2:本文明的能力向量。实机 UU 数据优先,推导结果缓存整局。"""
    from civ_mcp import civ_kit

    cache = _expert_cache(gs)
    pairs = dict(
        getattr(scan, "unique_units", None)
        or getattr(wm, "my_unique_units", None)
        or {}
    )
    civ_type = ""
    try:
        civ_type = (getattr(gs, "_game_identity", None) or ("", 0))[0] or ""
    except Exception:
        pass
    key = (civ_type, tuple(sorted(pairs)))
    if cache.get("kit_key") == key and cache.get("kit") is not None:
        return cache["kit"]
    kit = civ_kit.derive(civ_type=civ_type, unique_units=pairs or None)
    cache["kit_key"] = key
    cache["kit"] = kit
    return kit


def strategy_items(
    gs: GameState,
    wm,
    scan,
    turn: int,
    power,
    kit,
    civs: list,
    threats: list,
    units: list,
    cities_list: list,
    era: str = "",
) -> list[advice_mod.Advice]:
    """L3:十三棵决策树(playbook 1-10 + doctrine 11-13)。

    任何一步失败都只是少几条建议,不阻断回合。
    """
    from civ_mcp import doctrine, playbook

    try:
        cache = _expert_cache(gs)
        overview = cache.get("overview")
        inp = doctrine.DoctrineInput(
            turn=turn,
            power=power,
            kit=kit,
            civs=civs or [],
            threats=threats or [],
            my_units=units or [],
            my_cities=cities_list or [],
            my_military=int(getattr(scan, "military_strength", 0) or 0),
            leader_types=cache.get("leader_types") or {},
            era=era or "",
            victory_type=getattr(wm, "victory_type", "") or "",
            # 威胁等级已在手,传下去让 L3 能写"红警让步"分支(2026-08-06)
            threat_level=getattr(wm, "threat_level", "") or "",
            gold=float(getattr(overview, "gold", 0.0) or 0.0),
            production=int(getattr(scan, "production", 0) or 0),
            researched=set(getattr(scan, "techs", None) or ()),
            civics_done=set(getattr(scan, "civics", None) or ()),
            boosted=set(cache.get("boosts") or ()),
            camps=list(wm.active_camps()) if hasattr(wm, "active_camps") else [],
            exploration_pct=float(getattr(wm, "exploration_pct", 0.0) or 0.0),
            difficulty=getattr(wm, "difficulty", "") or "",
            # 军力窗口倒计时的数据源(war_campaign)
            power_history=cache.get("power_history"),
        )
        return playbook.evaluate(inp) + doctrine.evaluate(inp)
    except Exception:
        log.debug("策略引擎失败,该组缺席", exc_info=True)
        return []


async def _gather_expert_advice(
    gs: GameState,
    wm,
    scan,
    turn: int,
    era: str,
    opp_reads: list,
    cities_list: list,
    policy_status,
    lost_units: list,
    events: list,
) -> list[advice_mod.Advice]:
    from civ_mcp import (
        boost_planner,
        city_state_advisor,
        crisis_manager,
        diplomacy_advisor,
        district_planner,
        governor_advisor,
        late_economy,
        spy_strategy,
        tech_path,
        trade_optimizer,
    )

    items: list[advice_mod.Advice] = []
    cache = _expert_cache(gs)
    overview = cache.get("overview")
    is_mid_plus = era in _MID_ERAS or era in _LATE_ERAS

    def add(module: str, lines: list[str]) -> None:
        if lines:
            try:
                items.extend(advice_mod.from_legacy(module, lines))
            except Exception:
                log.debug("建议翻译失败: %s", module, exc_info=True)

    # ---- 尤里卡状态缓存(每 5 回合刷新;boost 状态变化很慢) ----
    boosts = cache.get("boosts")
    if boosts is None or turn % 5 == 4:
        try:
            tc = await gs.get_tech_civics()
            boosts = set()
            for lst in (tc.available_techs, tc.locked_techs):
                boosts |= {
                    getattr(o, "tech_type", "")
                    for o in lst or []
                    if getattr(o, "boosted", False)
                }
            for lst in (tc.available_civics, tc.locked_civics):
                boosts |= {
                    getattr(o, "civic_type", "")
                    for o in lst or []
                    if getattr(o, "boosted", False)
                }
            boosts.discard("")
            cache["boosts"] = boosts
            # 整个 TechCivicStatus 留一份(2026-08-05 F2-4 接线):
            # event_window 的升级窗口要 current_research + turns,同一次
            # 查询多个消费方,零新增往返。
            cache["tech_civics"] = tc
        except Exception:
            log.debug("尤里卡状态查询失败", exc_info=True)
            boosts = cache.get("boosts") or set()

    # ---- 每回合:科技/市政路径 + 尤里卡链(0 新增往返) ----
    try:
        gsd = {
            "cities": scan.cities,
            "military_units": scan.military,
            "known_camps": len(wm.active_camps()),
            # 0c 接线:军事升级主线要认得本文明 UU(军团),
            # 并跳过被 UU 替换掉的基础单位(剑士)
            "my_unique_units": dict(wm.my_unique_units or {}),
        }
        if overview is not None:
            gsd["empire_population"] = getattr(overview, "total_population", 0)
            gsd["has_religion"] = bool(getattr(overview, "our_religion", None))
        tcap = cache.get("trade_cap")
        if tcap:
            gsd["trade_capacity"], gsd["active_trade_routes"] = tcap
        add(
            "tech_path",
            tech_path.get_tech_advice(
                scan.techs, scan.science, wm.victory_type, gsd, boosts
            ),
        )
        if scan.civics:  # 空 = 扫描失败;空集会把已完成市政当未研究,宁缺勿错
            add(
                "tech_path",
                tech_path.get_civic_advice(
                    scan.civics, scan.culture, wm.victory_type, gsd, boosts
                ),
            )
            add(
                "boost",
                boost_planner.get_boost_advice(
                    scan.techs, boosts, gsd, scan.civics, scan.science
                ),
            )
    except Exception:
        log.debug("科技路径建议失败", exc_info=True)

    # ---- 每回合:时间窗口(审查 F2-4 接线,0 新增往返) ----
    # 数据源:tech_civics 缓存(5 回合一刷)+ 快照单位 + 库存缓存。
    # 现阶段能亮的窗口:单位升级窗口(军事学→膛线正是 T335 的场景)与
    # 战略资源揭示;政体/时代/对手奇观窗口缺数据源,各自静默降级。
    try:
        from civ_mcp import event_window

        _tc_cached = cache.get("tech_civics")
        _snap_u = getattr(gs, "_last_snapshot", None)
        if _tc_cached is not None and _snap_u is not None:
            _stock: dict[str, int] = {}
            for s in cache.get("stockpiles") or []:
                _key = event_window.resource_type_from_name(
                    str(getattr(s, "name", "") or "")
                )
                if _key:
                    _stock[_key] = int(getattr(s, "amount", 0) or 0)
            ev_ctx = event_window.EventContext(
                turn=turn,
                researching_tech=event_window.tech_type_from_name(
                    str(getattr(_tc_cached, "current_research", "") or "")
                ),
                tech_turns_left=int(
                    getattr(_tc_cached, "current_research_turns", 99) or 99
                ),
                my_unit_types=tuple(
                    str(getattr(u, "unit_type", "") or "")
                    for u in _snap_u.units.values()
                ),
                my_strategic_stock=_stock,
                victory_type=wm.victory_type or "",
            )
            add("event_window", event_window.get_event_window_advice(ev_ctx))
    except Exception:
        log.debug("时间窗口建议失败", exc_info=True)

    # ---- 文化胜利冲刺:旅游加成清单(审查 F2-4 部分接线) ----
    # 只在胜利路线=文化时出手。政策槽位取自 policy_status(3 回合缓存),
    # 5 个旅游加成里 3 个是政策,槽位数据缺失时政策项会假报"缺"——
    # 所以 policy_status 为 None 时整段静默,不硬凑。
    # 开放边界/贸易路线段缺数据源,传空 civ 列表让它自然静默。
    try:
        if (wm.victory_type or "").upper() == "CULTURE" and policy_status is not None:
            from civ_mcp import culture_endgame

            _slotted = tuple(
                s.current_policy
                for s in getattr(policy_status, "slots", None) or []
                if getattr(s, "current_policy", "")
            )
            add(
                "culture_endgame",
                culture_endgame.get_culture_endgame_advice(
                    {
                        "victory_type": "CULTURE",
                        "techs": set(scan.techs or ()),
                        "civics": set(scan.civics or ()),
                        "slotted_policies": _slotted,
                        "known_civs": (),
                        "faith": float(getattr(overview, "faith", 0) or 0)
                        if overview is not None
                        else 0.0,
                    }
                ),
            )
    except Exception:
        log.debug("文化冲刺建议失败", exc_info=True)

    # ---- 每回合:危机预案(0 新增往返) ----
    try:
        # 6b(2026-08-02 校准局):破产回合的"单位被杀"可能实为解散 ——
        # 快照差分分不出死因,但两者的应对完全相反(复盘战术 vs 修经济)。
        # 国库为 0 且有单位消失时,显式提示两种可能。
        if (
            lost_units
            and overview is not None
            and int(getattr(overview, "gold", 1) or 1) <= 0
        ):
            add(
                "crisis",
                [
                    "⚠️ 本回合国库为 0:上面的『单位被杀』可能实为破产解散。"
                    "先查经济(维护费/收入),再复盘战术。"
                ],
            )
        for utype, x, y in (lost_units or [])[:1]:
            camps_near = sum(
                1 for c in wm.active_camps() if hex_distance(c.x, c.y, x, y) <= 6
            )
            add(
                "crisis",
                crisis_manager.civilian_killed_crisis(
                    utype, (x, y), {"camps_nearby": camps_near}
                ),
            )
        war_declared = any(
            "宣战" in str(getattr(e, "message", ""))
            or "DECLARED WAR" in str(getattr(e, "message", "")).upper()
            for e in events or []
        )
        if war_declared and opp_reads:
            at_war = [r for r in opp_reads if r.at_war]
            if at_war:
                attacker = max(at_war, key=lambda r: r.their_military)
                add(
                    "crisis",
                    crisis_manager.war_declared_crisis(
                        {
                            "civ_name": attacker.name,
                            "military_strength": attacker.their_military,
                        },
                        {
                            "my_military": wm.military_strength or wm.military_count,
                            "gold": getattr(overview, "gold", 0) if overview else 0,
                        },
                    ),
                )
        for c in cities_list or []:
            lpt = getattr(c, "loyalty_per_turn", 0) or 0
            if lpt < 0:
                add(
                    "crisis",
                    crisis_manager.loyalty_collapse_crisis(
                        {
                            "name": getattr(c, "name", "?"),
                            "loyalty": getattr(c, "loyalty", 100),
                        },
                        lpt,
                    ),
                )
                break  # 一回合报一城,最危的
    except Exception:
        log.debug("危机预案失败", exc_info=True)

    # ---- 每回合:联合宣战(复用 opponents reads,0 新增往返) ----
    try:
        if opp_reads:
            neighbors = [
                {
                    "civ_name": r.name,
                    "military_strength": r.their_military,
                    "war_risk": r.war_risk,
                    "is_at_war": r.at_war,
                    "agendas": list(r.agendas),
                }
                for r in opp_reads
            ]
            add(
                "diplomacy",
                diplomacy_advisor.get_joint_war_advice(
                    neighbors,
                    {
                        "my_military": wm.military_strength or wm.military_count,
                        "gold": getattr(overview, "gold", None) if overview else None,
                        "gold_per_turn": getattr(overview, "gold_per_turn", None)
                        if overview
                        else None,
                    },
                ),
            )
    except Exception:
        log.debug("联合宣战建议失败", exc_info=True)

    # ---- 每回合:区域效应共享(纯计算)/科学收尾(era 门控) ----
    try:
        if is_mid_plus and len(cities_list or []) >= 2:
            add(
                "district",
                district_planner.get_area_effect_advice(
                    [
                        {
                            "name": getattr(c, "name", "?"),
                            "x": getattr(c, "x", 0),
                            "y": getattr(c, "y", 0),
                            "districts": list(getattr(c, "districts", []) or []),
                        }
                        for c in cities_list
                    ]
                ),
            )
    except Exception:
        log.debug("区域效应建议失败", exc_info=True)

    # ---- 每回合:空区域(2026-08-07,用户问"科技是不是太慢了"查出来的)----
    # 实机 T169:西巴尔/埃利都各建了学院区却一个建筑都没放,
    # 科研 7/6,而有全套的乌鲁克是 18。全仓此前没有一处检查这件事。
    # 按胜利路线过滤 —— 用户同一轮点出的:"不能一起都建,要看你想怎么胜利"。
    try:
        if cities_list:
            add(
                "district",
                district_planner.empty_district_advice(
                    [
                        {
                            "name": getattr(c, "name", "?"),
                            "districts": list(getattr(c, "districts", []) or []),
                            "buildings": list(getattr(c, "buildings", []) or []),
                        }
                        for c in cities_list
                    ],
                    researched_techs=set(getattr(scan, "techs", None) or ()),
                    completed_civics=set(getattr(scan, "civics", None) or ()),
                    victory_type=wm.victory_type or "",
                ),
            )
    except Exception:
        log.debug("空区域建议失败", exc_info=True)
    try:
        if wm.victory_type == "SCIENCE" and era in (
            "ERA_ATOMIC",
            "ERA_INFORMATION",
            "ERA_FUTURE",
        ):
            # 缺陷107:districts 的元素带 "@x,y" 后缀,直接 `in` 判断恒假 ——
            # 这一行曾经让 spaceport_cities 永远是空元组,整个科学收尾模块
            # 都以为太空港没建。必须走 has_district 归一。
            ports = tuple(
                getattr(c, "name", "?")
                for c in cities_list or []
                if district_planner.has_district(
                    getattr(c, "districts", None), "DISTRICT_SPACEPORT"
                )
            )
            alu = 0
            for s in cache.get("stockpiles") or []:
                if (
                    "铝" in getattr(s, "name", "")
                    or "ALUMINUM" in getattr(s, "name", "").upper()
                ):
                    alu = getattr(s, "amount", 0)
            add(
                "science_endgame",
                lategame.science_endgame(
                    lategame.ScienceEndgameContext(
                        turn=turn,
                        techs=scan.techs,
                        completed_projects=set(wm.completed_projects),
                        spaceport_cities=ports,
                        aluminum_stock=alu,
                        total_production=wm.total_production,
                    )
                ),
            )
    except Exception:
        log.debug("科学收尾建议失败", exc_info=True)

    # ---- 相位错开的周期查询(每回合 ≤1 次额外往返) ----
    # 相位表(全部 mod 10,互斥):2 城邦 ‖ 3 胜利阻断 ‖ 5 军力排名 ‖
    # 6 总督 ‖ 7 伟人 ‖ 9 间谍。mod 5 会和 mod 10 撞相(T37%5==2 抢走
    # 伟人相位的教训),统一用 mod 10。
    try:
        if turn % 10 == 6:
            st = await gs.get_governors()
            appointed = [
                governor_advisor.Governor(
                    governor_type=getattr(g, "governor_type", ""),
                    name=getattr(g, "name", ""),
                    assigned_city_id=-1,
                    assigned_city_name="",
                    is_established=True,
                    turns_to_establish=0,
                    available_promotions=(),
                )
                for g in getattr(st, "appointed", []) or []
            ]
            add(
                "governor_advisor",
                governor_advisor.get_governor_advice(
                    governor_advisor.GovernorContext(
                        points_available=getattr(st, "points_available", 0),
                        appointed=appointed,
                        available_types=tuple(
                            getattr(g, "governor_type", "")
                            for g in getattr(st, "available_to_appoint", []) or []
                        ),
                        victory_type=wm.victory_type,
                        city_names={},
                        threatened_cities=(),
                    )
                ),
            )
        elif turn % 10 == 2:
            es = await gs.get_city_states()
            css = [
                city_state_advisor.CityState(
                    player_id=c.player_id,
                    name=c.name,
                    city_state_type=getattr(c, "city_state_type", "Unknown"),
                    envoys_sent=getattr(c, "envoys_sent", 0),
                    suzerain_id=getattr(c, "suzerain_id", -1),
                    suzerain_name=getattr(c, "suzerain_name", "None"),
                    can_send_envoy=getattr(c, "can_send_envoy", True),
                )
                for c in getattr(es, "city_states", []) or []
            ]
            my_id = getattr(overview, "player_id", 0) if overview else 0
            add(
                "city_state_advisor",
                city_state_advisor.get_city_state_advice(
                    city_state_advisor.CityStateContext(
                        tokens_available=getattr(es, "tokens_available", 0),
                        city_states=css,
                        victory_type=wm.victory_type,
                        my_player_id=my_id,
                    )
                ),
            )
            mine = [
                {"name": c.name, "key": "", "type_name": c.city_state_type}
                for c in css
                if c.suzerain_id == my_id and my_id
            ]
            add(
                "city_state_advisor",
                city_state_advisor.get_suzerain_bonus_advice(mine, wm.victory_type),
            )
        elif turn % 10 == 3 and turn >= 30:
            vp = await gs.get_victory_progress()
            # 每 10 回合用引擎的权威判定校准一次完成集合(缺陷110)。
            # 逐回合的层级推断只能推到"层",同层三件火星项目要靠这里补齐。
            try:
                _vp_done = {
                    str(getattr(sp, "project_type", "") or "")
                    for sp in (getattr(vp, "space_projects", []) or [])
                    if getattr(sp, "status", "") == "completed"
                }
                _vp_done.discard("")
                if _vp_done - set(wm.completed_projects or ()):
                    wm.completed_projects = sorted(
                        set(wm.completed_projects or ()) | _vp_done
                    )
            except Exception:
                log.debug("太空项目完成集合校准失败", exc_info=True)
            add(
                "victory_block",
                lategame.check_rival_victory_threat(
                    lategame.RivalVictoryContext(
                        turn=turn,
                        my_victory_type=wm.victory_type,
                        total_civs=len(getattr(vp, "players", []) or [])
                        or len(opp_reads) + 1,
                        religion_majority=dict(
                            getattr(vp, "religion_majority", {}) or {}
                        ),
                        space_projects=dict(getattr(vp, "space_projects", {}) or {}),
                        original_capitals=dict(getattr(vp, "capitals_held", {}) or {}),
                        at_war_with=tuple(r.name for r in opp_reads if r.at_war),
                    )
                ),
            )
        elif turn % 10 == 5 and opp_reads:
            # 1e fix:用真实军力值,不再拿单位数当军力(把 0.82 倍差距
            # 报成 20 倍并推出「你在下半区」的错误结论)
            add(
                "ranking",
                opponents.get_military_ranking(
                    [
                        {
                            "civ_name": r.name,
                            "military_strength": r.their_military,
                            "player_id": r.player_id,
                        }
                        for r in opp_reads
                    ],
                    wm.military_strength or wm.military_count,
                ),
            )
        elif turn % 10 == 7 and era and era != "ERA_ANCIENT":
            gps = await gs.get_great_people()
            offers = [
                lategame.GreatPersonOffer(
                    individual_name=getattr(g, "individual_name", ""),
                    cost=getattr(g, "cost", 0),
                    player_points=getattr(g, "player_points", 0),
                    gold_cost=getattr(g, "gold_cost", 0),
                    faith_cost=getattr(g, "faith_cost", 0),
                    can_recruit=getattr(g, "can_recruit", False),
                    claimant=getattr(g, "claimant", "Unclaimed"),
                    ability=getattr(g, "ability", ""),
                )
                for g in gps or []
            ]
            if offers:
                add(
                    "great_person",
                    lategame.evaluate_great_person(
                        lategame.GreatPersonContext(
                            victory_type=wm.victory_type,
                            gold=getattr(overview, "gold", 0) if overview else 0,
                            faith=getattr(overview, "faith", 0) if overview else 0,
                            offers=offers,
                        )
                    ),
                )
        elif turn % 10 == 9 and era in _LATE_ERAS:
            spies = await gs.get_spies()
            add(
                "spy_strategy",
                spy_strategy.get_spy_advice(
                    spy_strategy.SpyContext(
                        turn=turn,
                        victory_type=wm.victory_type,
                        spies=[
                            spy_strategy.Spy(
                                unit_id=s.unit_id,
                                name=s.name,
                                rank=s.rank,
                                city_name=getattr(s, "city_name", ""),
                                city_owner=getattr(s, "city_owner", ""),
                                status=getattr(s, "status", ""),
                                is_escaping=getattr(s, "is_escaping", False),
                                current_mission=getattr(s, "current_mission", ""),
                                available_ops=tuple(
                                    getattr(s, "available_ops", []) or []
                                ),
                            )
                            for s in spies or []
                        ],
                        rivals=[],
                        my_districts={},
                        counterspy_cities=(),
                    )
                ),
            )
    except Exception:
        log.debug("周期建议查询失败", exc_info=True)

    # ---- 条件触发:有空贸易容量才查目的地(TRCAP 穿透判定) ----
    try:
        tcap = cache.get("trade_cap")
        if tcap and tcap[0] - tcap[1] > 0 and turn % 3 == 0:
            tr = await gs.get_trade_routes()
            idle_traders = list(getattr(tr, "traders", []) or [])
            if idle_traders:
                uid = (
                    idle_traders[0]
                    if isinstance(idle_traders[0], int)
                    else getattr(
                        idle_traders[0],
                        "unit_index",
                        getattr(idle_traders[0], "unit_id", None),
                    )
                )
                if uid is not None:
                    dests = await gs.get_trade_destinations(uid)
                    hostile = tuple(
                        r.name for r in opp_reads if r.at_war or r.war_risk == "高"
                    )
                    add(
                        "trade_optimizer",
                        trade_optimizer.get_trade_advice(
                            trade_optimizer.TradeContext(
                                idle_traders=tcap[0] - tcap[1],
                                origin_city="",
                                destinations=[
                                    trade_optimizer.Destination(
                                        city_name=d.city_name,
                                        owner_name=d.owner_name,
                                        x=d.x,
                                        y=d.y,
                                        is_domestic=d.is_domestic,
                                        is_city_state=d.is_city_state,
                                        has_quest=d.has_quest,
                                        has_trading_post=d.has_trading_post,
                                        origin_yields=str(d.origin_yields or ""),
                                        dest_yields=str(d.dest_yields or ""),
                                        pressure_out=d.pressure_out,
                                        religion_out=d.religion_out,
                                    )
                                    for d in dests or []
                                ],
                                victory_type=wm.victory_type,
                                hostile_civs=hostile,
                            )
                        ),
                    )
    except Exception:
        log.debug("贸易建议失败", exc_info=True)

    # ---- 后期经济(era 门控,复用上提数据,0 新增往返) ----
    try:
        if era in _LATE_ERAS and overview is not None and turn % 5 == 3:
            slotted, avail, empty = [], [], 0
            for s in getattr(policy_status, "slots", []) or []:
                cur = getattr(s, "current_policy", "") or ""
                slotted.append(cur)
                if not cur:
                    empty += 1
            avail = [
                getattr(p, "policy_type", "")
                for p in getattr(policy_status, "available_policies", []) or []
            ]
            # 2026-08-09 T276-T301 实机:districts 元素带 "@x,y" 坐标后缀
            # (如 "DISTRICT_INDUSTRIAL_ZONE@61,29"),原先的精确相等匹配恒为
            # False → iz 恒 0 → 乌鲁克/阿克沙克各有 IZ+工厂,简报仍连喊 25 回合
            # "9 座城一个工业区都没有"。匹配必须先剥坐标后缀。
            iz = sum(
                1
                for c in cities_list or []
                if any(
                    d.split("@", 1)[0] in ("DISTRICT_INDUSTRIAL_ZONE", "DISTRICT_HANSA")
                    for d in (getattr(c, "districts", []) or [])
                )
            )
            # 工厂/发电厂此前硬编码 0 —— iz 修好后 gap=iz-0 会立刻改报
            # "N 座城有工业区但没有工厂"(同样假)。buildings 是去前缀短名。
            fac = sum(
                1
                for c in cities_list or []
                if "FACTORY" in (getattr(c, "buildings", []) or [])
            )
            pp = sum(
                1
                for c in cities_list or []
                if any("POWER_PLANT" in b for b in (getattr(c, "buildings", []) or []))
            )
            add(
                "late_economy",
                late_economy.get_late_economy_advice(
                    late_economy.LateEconomyContext(
                        turn=turn,
                        era=era,
                        victory_type=wm.victory_type,
                        gold=getattr(overview, "gold", 0),
                        gold_per_turn=getattr(overview, "gold_per_turn", 0),
                        # 2026-08-05: 原硬编码 0 导致每次工业+时代简报都误报
                        # "大本钟 0 回合建成"（实际没有任何城市在造）。
                        # 改为 -1（未在造）—— 需要真实支持时再查我方城市生产。
                        big_ben_turns_left=-1,
                        has_big_ben=False,
                        current_government=str(
                            getattr(policy_status, "government_type", "") or ""
                        ),
                        unlocked_governments=(),
                        available_policies=tuple(avail),
                        slotted_policies=tuple(slotted),
                        empty_slots=empty,
                        cities_with_industrial_zone=iz,
                        cities_with_factory=fac,
                        cities_with_power_plant=pp,
                        city_count=len(cities_list or []),
                        international_routes=0,
                        suzerain_count=0,
                        allies=(),
                    )
                ),
            )
    except Exception:
        log.debug("后期经济建议失败", exc_info=True)

    return items


def _record_debrief(
    gs: GameState, wm, scan, turn: int, lost_units: list | None = None
) -> None:
    """把本回合事实写进复盘日志(服务端写,模型改不了)。

    2026-08-12 实机(罗马 T384):复盘报「损失单位 0」而那局打了三场战争 ——
    lost_units 由 diff_units 在同一函数体里算出来了,却从没传进这里,
    TurnFact.units_lost 恒为空。这是同一条链上第三次「定义了没接线」
    (前两次:diff_units 从未被调用、last_units 没进序列化),而它的下游
    不只是复盘表:learning_store 的「高损失」判据、debrief.contradictions()
    的自评校验都读这个字段 —— 事实层为空,两个机制一起静默失效。
    """
    civ, seed = gs._game_identity or ("unknown", 0)
    base = wm_mod.storage_path(civ, seed).parent.parent / "复盘"
    jp = base / f"{civ}_{seed}.json"
    log_obj = debrief.DebriefLog.load(jp)
    log_obj.game_id = f"{civ}_{seed}"
    log_obj.civ = civ
    log_obj.difficulty = wm.difficulty
    log_obj.victory_type = wm.victory_type
    log_obj.record(
        debrief.TurnFact(
            turn=turn,
            cities=scan.cities,
            military=scan.military,
            exploration=wm.exploration_pct,
            active_camps=len(wm.active_camps()),
            camps_near=sum(
                1
                for c in wm.active_camps()
                if 0 <= getattr(c, "dist_to_capital", -1) <= debrief.DEBRIEF_CAMP_RADIUS
            ),
            threat_level=wm.threat_level,
            units_lost=[u[0] for u in (lost_units or [])],
            production=scan.production,
            science=scan.science,
            culture=int(getattr(scan, "culture", 0) or 0),
            techs_done=len(scan.techs),
            civics_done=len(getattr(scan, "civics", ()) or ()),
        )
    )
    log_obj.save(jp, base / f"{civ}_{seed}.md")


async def _get_current_era(gs: GameState) -> str | None:
    """返回当前时代的 EraType,如 "ERA_ANCIENT"。

    注意:`Game.GetEras()` 在 RULESET_STANDARD 下不存在(2026-07-31 实测),
    所以走 `Players[me]:GetEra()` 拿 Index,再用 GameInfo.Eras 反查。
    """
    lines = await gs.conn.execute_read(
        "local me = Game.GetLocalPlayer() "
        "local idx = Players[me]:GetEra() "
        "for r in GameInfo.Eras() do "
        'if r.Index == idx then print("ERA|" .. r.EraType) break end '
        "end "
        f'print("{lq.SENTINEL}")'
    )
    for line in lines:
        if line.startswith("ERA|"):
            return line.split("|", 1)[1]
    return None


# ===========================================================================
# 任务 J:回合完成度门禁(追加)
#
# 改造方案 §6.4:「有单位仍可移动且未下指令 → 拒绝结束回合」。落地时按
# 实机现实收窄了两处,理由必须写明白:
#
#   1. **UnitInfo 没有 activity/orders 字段** —— 从数据上无法区分
#      「遗忘的单位」和「刻意驻守/已 skip 的单位」。而 end_turn() 没有
#      force 参数:一刀切阻止 = 可能造成逃不出去的死循环,这比漏拦更糟
#      (改造报告的教训:误报会让机制失去信任;死循环误报会瘫痪整局)。
#
#      2026-08-07 更新:这条限制**部分解除了**。UnitInfo 现在带 busy
#      (ActivityTypes.ACTIVITY_OPERATION / 排队中的操作),能认出
#      「正在多回合作业」的单位 —— 建矿/砍树/攻城架设都不消耗移动力,
#      此前每回合都被报成闲置平民(实机 T121)。「刻意驻守」仍无法区分,
#      所以军事单位那一档保持"警告不阻止"。
#
#   2. 生产/科研空缺**引擎原生就会 block**(ENDTURN_BLOCKING_PRODUCTION 等,
#      上方 blocker 循环已处理)—— 门禁不重复引擎的工作,只补引擎不管的:
#      「满移动力的单位」。引擎允许你 skip 它们,而 T29 侦察兵之死证明
#      skip ≠ 撤退 —— 被威胁的单位被一键跳过,就地枯站到死。
#
# 分档:
#   阻止(一次):有满移动力的**平民**(建造者/开拓者)—— 平民没有驻守语义,
#     满移动力过回合几乎必是遗忘。首次拦下点名;若是刻意(如城内等护送),
#     同一回合**再调一次 end_turn 即放行**(重调即 force,不需要改工具签名)。
#   警告(不阻止):满移动力的军事单位 —— 可能是驻守,如实列出让模型自查,
#     并重复那句用命换来的话:被威胁的单位别用 skip 糊弄,skip 不会撤退。
# ===========================================================================

# 兜底表:对象不接受属性赋值时才用(按 id(gs) 索引,有复用风险)
_GATE_STATE: dict[int, dict] = {}


def _new_gate_state() -> dict:
    return {"waived_turn": -1, "warnings": []}


def _gate_state(gs) -> dict:
    """门禁的跨调用状态(豁免回合、警告档、平民滞留基线)。

    2026-08-05 修复:此前用 `id(gs)` 做全局字典的 key —— CPython 的 id
    就是内存地址,对象被回收后地址会被**复用**,于是新建的 GameState
    会静默继承上一个对象的 waived_turn 与 settler_first_seen:
    该拦的回合不拦(以为已经豁免过)、滞留基线错乱。常驻服务里
    GameState 长期单例所以少见,但重连重建时会中招,测试里必然中招
    (全量跑挂、单独跑过,正是这个形态)。
    改为把状态挂在实例上:生命周期与对象一致,不可能串台。
    """
    st = getattr(gs, "_gate_store", None)
    if isinstance(st, dict):
        return st
    st = _new_gate_state()
    try:
        gs._gate_store = st
    except Exception:  # 只读/带 __slots__ 的对象 —— 回落到旧表
        return _GATE_STATE.setdefault(id(gs), st)
    return st


def _pop_gate_warnings(gs) -> list[str]:
    st = getattr(gs, "_gate_store", None)
    if not isinstance(st, dict):
        st = _GATE_STATE.get(id(gs))
    if not st:
        return []
    out, st["warnings"] = st["warnings"], []
    return out


def _threat_reach(t) -> tuple[int, int, int, str, int] | None:
    """一个威胁的 (x, y, 可达格数, 名字, 战斗力)。可达 = 移动力 + 射程,与
    safety.py / render_enemy_projection 同一套能力上界推演。

    战斗力(2026-08-06 追加)用于护卫强弱比:同格护卫够不够强,判据与
    safety.check_move 一致(护卫 CS × 2 ≥ 威胁 CS),两套门禁不能各说各话。

    脏数据返回 None 而不是抛异常:一条坏记录不该让整个门禁静默失效
    (外层 except 会放行,那正是我们要防的失效模式)。
    """

    def _i(attr, default=0):
        try:
            return int(getattr(t, attr, default) or default)
        except (TypeError, ValueError):
            return default

    try:
        x, y = getattr(t, "x", None), getattr(t, "y", None)
        if x is None or y is None:
            return None
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return None
    moves = _i("base_moves") or 2
    rng = _i("range")
    name = str(getattr(t, "unit_type", "") or "?").replace("UNIT_", "")
    owner = str(getattr(t, "owner_name", "") or "敌方")
    cs = _i("combat_strength")

    # 海军威胁的可达范围不能按 moves + range 算(2026-08-06):
    # 船开不上陆地,它的移动力只在水面上有意义。按陆地口径算,一艘
    # 沿岸游走的四段帆船会把内陆一大片格子标成"危险",内陆的建造者
    # 因此被反复误拦 —— world_model.compute_threat_level 早就为同一个
    # 误报修过一次(GALLEY 游走触发 RED 冻结平民生产),回合门禁漏了。
    # 保守取 range(它站着不动能打到的范围);没有射程的海军近战
    # 只能打相邻水域,取 1。我方没有沿海城市时直接判它够不着。
    if getattr(t, "is_naval", False):
        if not getattr(t, "has_coastal_city", False):
            return None
        return x, y, max(1, rng), f"{owner} {name}(海军)", cs

    return x, y, moves + rng, f"{owner} {name}", cs


# "这座城没在生产"在不同数据路径上有不同写法:Lua 侧发 "nothing",
# 模型默认值是 "NONE",字段缺失时是空串。判据必须覆盖全部,只认一种
# 拼写的门禁等于没有门禁(缺陷 103)。全部按 .upper() 后比对。
_IDLE_PRODUCTION_VALUES = frozenset({"", "NONE", "NOTHING", "NULL"})


def check_turn_completion(
    units: list, threats: list | None = None, cities: list | None = None
) -> tuple[list[str], list[str]]:
    """纯函数:单位清单(+可见威胁) → (阻止项, 警告项)。可测,不碰网络。

    判据只有一条可靠信号:moves_remaining >= max_moves(整回合没动过)。
    平民(有 build_charges 的建造者、类型含 SETTLER 的开拓者)→ 阻止项;
    军事/其他 → 警告项。fortify/sleep 无法从数据区分,警告档如实说明。

    2026-08-01 实机教训(用一个建造者换来的):初版只说"平民闲置没有收益",
    语气像个效率提醒,于是被"重调放行"轻易跳过 —— 而那个建造者当回合就被
    2 格外的蛮族弓骑手杀了。危险信息当时就在系统里(威胁扫描恰好在门禁
    前面跑完),只是没接上。现在闲置平民会和威胁的可达范围做交叉判定,
    命中的单独列为"危险"档,把后果写进拦截消息本身。
    """
    blockers: list[str] = []
    warnings: list[str] = []
    idle_civilians: list[str] = []
    endangered: list[str] = []
    idle_military: list[str] = []
    reaches = [r for r in (_threat_reach(t) for t in (threats or [])) if r]

    # 平民危险判定的两条豁免索引(2026-08-06)。先建表再遍历,避免 O(n²)。
    # 城格:CitySnapshot 此前不带坐标(构造时被丢掉了),补回来才认得出
    # "平民站在自己城里"。坐标缺失(-1)的城不参与判定 —— 宁可漏一个豁免,
    # 也不要把 (-1,-1) 当成真实城格误放行。
    own_city_tiles: set[tuple[int, int]] = set()
    for c in cities or []:
        try:
            cxx, cyy = int(getattr(c, "x", -1)), int(getattr(c, "y", -1))
        except (TypeError, ValueError):
            continue
        if cxx >= 0 and cyy >= 0:
            own_city_tiles.add((cxx, cyy))
    # 同格护卫:每格取最强的军事单位战斗力(侦察兵 CS10 也算军事单位)
    escort_cs_at: dict[tuple[int, int], int] = {}
    for u in units or []:
        try:
            ucs = int(getattr(u, "combat_strength", 0) or 0)
            if ucs <= 0:
                continue
            k = (int(getattr(u, "x", -1)), int(getattr(u, "y", -1)))
        except (TypeError, ValueError):
            continue
        escort_cs_at[k] = max(escort_cs_at.get(k, 0), ucs)

    # —— 断生产:阻断档(2026-08-03 补,用户两次点名 + T110 实机复现)——
    # 此前门禁只看单位,城市生产完全不在检查范围内。实机 T110:罗马(产能10)
    # 与奥斯蒂亚(产能12)——产能最高的两座城同时空转,而门禁一声不吭。
    # 空转的产能不会攒着,是**当场蒸发**,且事后没有任何地方会告诉你发生过。
    # 修复成本只是一次 set_city_production,所以列阻断而非提醒。
    #
    # 2026-08-11 T301 订正(缺陷 103):上面这道门【从写出来就没触发过一次】。
    # 空转城市的 currently_building 实际值是 "nothing",而判据写的是 "NONE"
    # —— "nothing".upper() 是 "NOTHING",两者永不相等。同一个值,narrate.py
    # 判对了(`not in ("NONE", "nothing")`),门禁判错了。
    # 后果:T294-T300 连续七回合有 4-6 座城空转,门禁一声不吭,直到用户
    # 第三次点名"生产别停"。用户前两次点名换来的正是这道门,而它是哑的。
    # 判据改成规范化后比对一组已知的空值写法,不再依赖单一拼写。
    idle_cities: list[str] = []
    for c in cities or []:
        building = str(getattr(c, "currently_building", "") or "").strip()
        if building.upper() in _IDLE_PRODUCTION_VALUES:
            idle_cities.append(str(getattr(c, "name", "?")))
    if idle_cities:
        blockers.append(
            f"🔴 {len(idle_cities)} 座城没有在生产:{'、'.join(idle_cities)}"
            f" —— 空转的产能不会攒着,是当场蒸发。"
            f"用 get_city_production(city_id) 看选项,再 set_city_production 定下来。"
        )

    for u in units or []:
        try:
            moves = float(getattr(u, "moves_remaining", 0) or 0)
            max_moves = float(getattr(u, "max_moves", 0) or 0)
        except (TypeError, ValueError):
            continue
        if max_moves <= 0 or moves < max_moves:
            continue  # 动过/在运输中/无移动力单位,不管
        # 2026-08-07:改用游戏自己的「待指令」判据。上面那条"移动力满"是
        # 近似量,它把【已 fortify / 已 skip / 自动化 / 商路运行中】的单位
        # 也算成"整回合没动"——所以军事单位那一档只能写成警告,还得附一句
        # "数据层分不出驻守与遗忘"。IsReadyToSelect 分得出。
        # 保留移动力那一关做前置(便宜且能过滤掉大多数),这一关做精判。
        if not bool(getattr(u, "needs_orders", True)):
            continue
        utype = str(getattr(u, "unit_type", "") or "")
        name = str(getattr(u, "name", "") or utype.replace("UNIT_", ""))
        x, y = getattr(u, "x", "?"), getattr(u, "y", "?")
        label = f"{name}({x},{y})"
        charges = int(getattr(u, "build_charges", 0) or 0)
        cs = int(getattr(u, "combat_strength", 0) or 0)
        # 2026-08-02 校准局 5e:此前只看 charges>0,把带筑垒充能的罗马军团
        # (CS40, charges:1)判成平民 —— 驻守时被建议去「改良/砍树」,
        # 护卫平民时不被承认。平民的判据是战斗力为 0,不是有没有充能。
        is_civilian = cs == 0 and (charges > 0 or "SETTLER" in utype.upper())

        # 危险判定:该格是否落在任一威胁的下回合可达范围内。
        # 2026-08-06:补上两条豁免 —— 拦截消息本来就写着"要么撤进城,
        # 要么调一个军事单位【同格】护卫",而代码两条都不认,于是照做了
        # 也照拦(实机 8+ 次全靠 force 放行,建造者始终安然无恙)。
        # 门禁喊狼来了的代价不是这一次误伤,是下一次真狼来时没人信。
        # 判据与 safety.check_move 对齐:进自己城 = 安全;同格护卫
        # CS×2 ≥ 威胁 CS = 安全,不够强则改口"护卫太弱"而不是"没人管"。
        danger = ""
        weak_escort = ""
        try:
            ux, uy = int(x), int(y)
            in_own_city = (ux, uy) in own_city_tiles
            guard_cs = escort_cs_at.get((ux, uy), 0)
            for tx, ty, reach, tname, tcs in reaches:
                d = hex_distance(ux, uy, tx, ty)
                if d > reach:
                    continue
                if in_own_city:
                    break  # 城里 —— 城市自己会开火,平民不会被抓
                if guard_cs > 0 and guard_cs * 2 >= tcs:
                    break  # 同格护卫够强
                if guard_cs > 0:
                    weak_escort = (
                        f"{tname}在 {d} 格外(可达 {reach} 格),"
                        f"同格护卫 CS{guard_cs} 挡不住 CS{tcs}"
                    )
                else:
                    danger = f"{tname}在 {d} 格外(可达 {reach} 格)"
                break
        except (TypeError, ValueError):
            pass

        if is_civilian and weak_escort:
            endangered.append(f"{label}:{weak_escort}")
        elif is_civilian and danger:
            endangered.append(f"{label}:{danger}")
        elif is_civilian:
            idle_civilians.append(label)
        elif int(getattr(u, "combat_strength", 0) or 0) > 0:
            idle_military.append(label)

    if endangered:
        blockers.append(
            f"🔴 {len(endangered)} 个平民整回合未动【且在敌军打击范围内】:"
            f"{('  '.join(endangered[:3]))}"
            f"{' 等' if len(endangered) > 3 else ''} —— "
            f"平民战斗力为 0,被贴上就是俘虏或击杀。要么撤进城/退出可达范围,"
            f"要么调一个军事单位【同格】护卫(相邻不算)。"
            f"放着不管 = 这回合把它送掉。"
        )
    if idle_civilians:
        blockers.append(
            f"{len(idle_civilians)} 个平民整回合未动:"
            f"{('、'.join(idle_civilians[:4]))}"
            f"{' 等' if len(idle_civilians) > 4 else ''} —— "
            f"建造者该改良/砍树,开拓者该走位或建城;平民闲置没有任何收益。"
        )
    if idle_military:
        warnings.append(
            f"· {len(idle_military)} 个军事单位整回合未动:"
            f"{('、'.join(idle_military[:3]))}"
            f"{' 等' if len(idle_military) > 3 else ''}"
            f"(若为刻意驻守请忽略——数据层分不出驻守与遗忘)。"
            f"被威胁的单位不要用 skip_remaining_units 糊弄:skip 不会撤退,"
            f"T29 的侦察兵就是这么死的。"
        )
    return blockers, warnings


def stall_baseline(prev, turn: int, charges: int, pos) -> tuple[int, int, tuple]:
    """平民滞留追踪的基线。纯函数,可测。

    基线重置的两个条件:
      ① 充能变了 = 刚干完活
      ② 位置变了 = 正在赶路 —— **走 6 格去上工不是闲置**

    ② 是 2026-08-07 苏美尔局实机补的。此前只有 ①,于是:
      - 走远路的建造者被报"滞留 6 回合"(走路不消耗充能)
      - 开拓者更糟:它根本没有充能,charges 恒为 0,条件 ① 永不成立,
        只要路上走超过 5 回合就【必然】误报

    误伤比漏拦贵 —— 拦错几次就会养成无脑再调一次 end_turn 的习惯,
    那时真正闲置的平民也会被一起放行。
    """
    pos = tuple(pos) if pos else (None, None)
    base_turn, base_charges, base_pos = _stale_entry(prev, charges, pos)
    if charges != base_charges or pos != base_pos:
        return turn, charges, pos
    return base_turn, base_charges, base_pos


def _stale_entry(v, charges: int, pos=None) -> tuple[int, int, tuple]:
    """滞留追踪条目,向后兼容三代格式。

    v0 裸回合数 → v1 (基线回合, 充能) → v2 (基线回合, 充能, 位置)。
    位置是 2026-08-07 加的:只看充能会把【赶路中的平民】判成滞留。
    """
    if isinstance(v, tuple):
        if len(v) == 3:
            return int(v[0]), int(v[1]), tuple(v[2]) if v[2] else (None, None)
        if len(v) == 2:
            return int(v[0]), int(v[1]), tuple(pos) if pos else (None, None)
    return int(v), charges, tuple(pos) if pos else (None, None)


async def _turn_completion_gate(
    gs: GameState, turn: int, threats: list | None = None, snap=None
) -> str:
    """门禁主体。返回非空字符串 = 阻止本次 end_turn(附解决办法)。

    首拦-重调放行:同一回合第二次调用直接放行(等价 force,末尾附提醒)。
    数据查询失败 → 放行 —— 门禁的基础设施故障绝不能卡死回合推进。

    threats 复用调用方已查好的回合前威胁扫描(0 新增往返),用于把
    "闲置平民"升级成"闲置且在打击范围内"。

    2026-08-05 审查 F2/F8:数据优先用调用方刚拍的 snap_before(同一次
    end_turn 内、秒级新鲜,UnitInfo/CitySnapshot 字段齐全)——省 2 次
    往返;更关键的是门禁不再自己调 get_units,「偷懒检测」的取证
    (has_read)从此不被门禁自身污染 —— 此前该检查因自污染永远沉默。
    """
    st = _gate_state(gs)
    # 取证必须在任何数据获取之前:门禁自己的查询不算「调用方读过局面」
    read_units_before = gs.has_read("units")
    units: list = []
    cities_now: list = []
    if snap is not None:
        units = list(getattr(snap, "units", {}).values())
        cities_now = list(getattr(snap, "cities", {}).values())
    else:
        try:
            units = await gs.get_units()
        except Exception:
            return ""
        try:
            cities_now, _ = await gs.get_cities()
        except Exception:
            log.debug("门禁城市查询失败,只检查单位", exc_info=True)
    blockers, warnings = check_turn_completion(units, threats, cities_now)

    # —— 平民滞留升级(P2, 2026-08-05 T335 复盘;审查 F2-5 扩展到建造者)——
    # 校准局 7 个开拓者闲置到 T335、9 个建造者闲置+7 个被掠夺地块没人修。
    # 「整回合未动」检查看不见被 skip 的单位(移动力已扣)。这里按
    # 【工作停滞时长】追踪:开拓者以存在时长计(存在=还没建城);
    # 建造者以「充能未变化」时长计(充能变化=在干活,基线重置)。
    # >5 回合 → 阻断;豁免后基线重置,5 回合后才再次提醒(审查 F1-7,
    # 否则合法等待(等护送/等开边)也要每回合双调 end_turn)。
    # (状态在进程内存;mcp_call 每调用一进程的开发模式下自动退化为静默。)
    stale_ids: list = []
    try:
        seen: dict = st.setdefault("settler_first_seen", {})
        now_ids: set = set()
        stale: list[str] = []
        for u in units or []:
            utype = str(getattr(u, "unit_type", "") or "").upper()
            cs = int(getattr(u, "combat_strength", 0) or 0)
            charges = int(getattr(u, "build_charges", 0) or 0)
            uid = getattr(u, "unit_id", None)
            is_settler = "SETTLER" in utype
            if cs != 0 or uid is None or not (is_settler or charges > 0):
                continue
            now_ids.add(uid)
            pos = (getattr(u, "x", None), getattr(u, "y", None))
            # 2026-08-09 T288-T300 实机:萨根 sleep 在太空港工地等完工(唯一
            # 正确用法),仍每 5 回合被点名一次,每次都要双调 end_turn 放行。
            # sleep/fortify/自动化的单位 IsReadyToSelect()==false —— 这正是
            # "刻意驻留"的引擎权威判据(同文件 4647 行已在用),滞留监控此前
            # 没消费它。豁免并把基线刷到当前回合:被吵醒(needs_orders 变回
            # True)后从醒来那刻重新计时,不会漏掉真闲置。
            if not bool(getattr(u, "needs_orders", True)):
                seen[uid] = (turn, charges, pos)
                continue
            base_turn, base_charges, base_pos = stall_baseline(
                seen.setdefault(uid, (turn, charges, pos)), turn, charges, pos
            )
            seen[uid] = (base_turn, base_charges, base_pos)
            if turn - base_turn > 5:
                # 2026-08-07 实机(T151):此前是二分法
                #   kind = "开拓者" if is_settler else "建造者"
                # 于是【伟人】(大商人有 1 充能)被报成"建造者(56,25)"——
                # 我照着去找建造者,那个坐标上站的是柯莱欧司。
                # 更糟的是下面的解法句跟着错:"建造者:get_builder_tasks 派活"
                # 对伟人完全无效,照做只会白跑一次。
                # 单位类别要按 UnitType 判,不能靠"有没有充能"反推。
                if is_settler:
                    kind = "开拓者"
                elif "GREAT_" in utype:
                    kind = "伟人"
                else:
                    kind = "建造者"
                name = str(getattr(u, "name", "") or "").strip()
                label = f"{kind}" + (f"·{name}" if kind == "伟人" and name else "")
                stale.append(
                    f"{label}({getattr(u, 'x', '?')},{getattr(u, 'y', '?')})"
                    f" 滞留 {turn - base_turn} 回合"
                )
                stale_ids.append(uid)
        for _uid in [k for k in seen if k not in now_ids]:
            del seen[_uid]  # 已建城/充能用尽/已损失,停止追踪
        if stale:
            blockers.append(
                f"{len(stale)} 个平民滞留超过 5 回合:{'、'.join(stale[:4])}"
                f"{' 等' if len(stale) > 4 else ''} —— 闲置平民只是每回合的"
                f"维护费加一个会被抓的目标(T335 校准局 7 个开拓者闲了 200 "
                f"回合,城市数被对手拉开,科研跟着全线塌方)。"
                f"开拓者:get_settle_advisor / assess_settle_location 选点 → "
                f"move → found_city。建造者:get_builder_tasks 派活,"
                f"优先修被掠夺地块。伟人:unit_action(activate) 需要站在特定"
                f"格子上,失败时报错会列出合法格 —— 一个都合法不了就说明"
                f"这位伟人在本局已经废了,别再为它留位置。"
                f"确属刻意(等护送/等开边/等忠诚窗口),"
                f"再调一次 end_turn 放行(5 回合内不再重复提醒)。"
            )
    except Exception:
        log.debug("平民滞留检查失败,跳过", exc_info=True)

    # —— 偷懒检测(2026-08-03;2026-08-05 审查 F2 修复取证)——
    # 用户反馈:换别的模型接手时,最常见的失败不是判断错,而是**跳过步骤**
    # ——不查单位、不看局面,直接 end_turn。这种回合在存档里看不出异常,
    # 但这一回合的产能与机会已经没了,而且事后无从追溯。
    # 取证用 read_units_before(进门禁前的快照):此前门禁自己调 get_units
    # 把 has_read 污染成永真,这条检查从上线起一次都没触发过。
    try:
        if not read_units_before and turn > 1:
            blockers.append(
                "🔴 本回合你没有调用过 get_units 就要结束回合 —— "
                "没看过部队位置与血量的回合,等于把这一回合的部署交给运气。"
                "先 get_units(交战中还要看 get_cities 的生产),再结束。"
            )
    except Exception:
        pass

    # —— 城数达标(2026-08-29 用户点破:整局 0 开拓者、T94 仍 3 城)——
    # 根因不是记性差,而是"每回合从一堆动作里选了看起来稳的,没选最复利的
    # (复制新城)"。要根治只能靠机制:城数落后且没有任何开拓者在产/在走,
    # 本回合就不许过。宽面=每座新城一个新人口场+一座新校园+新区域。
    # 目标基准(社区共识;王子难度保底): T0=1, T100=6, T180=8,线性插值,
    # 别一口要求一步到位,否则新手局会被卡死。
    try:
        n_cities = len(cities_now or [])
        if turn <= 100:
            target = 1 + int(5 * min(max(turn, 0), 100) / 100)
        else:
            target = 6 + int(2 * min(max(turn - 100, 0), 80) / 80)
        if n_cities < target:
            # 有开拓者在产(任一城 currently_building 含 SETTLER)或已有一支
            # 开拓者单位在场上(正在走/停在目的地)→ 视为"已经在铺",不拦。
            settler_building = any(
                "SETTLER" in str(getattr(c, "currently_building", "") or "").upper()
                for c in (cities_now or [])
            )
            settler_on_map = any(
                "SETTLER" in str(getattr(u, "unit_type", "") or "").upper()
                for u in (units or [])
            )
            if not settler_building and not settler_on_map:
                blockers.append(
                    f"[红警] 城数 {n_cities} < 目标 {target}(回合 {turn})—— 宽面复利"
                    f"落后是本局最大短板,而你此刻没有任何开拓者在产或在走。"
                    f"每座新城 = 1 人口场 + 1 校园位 + 1 商路终点,这是落后时"
                    f"唯一能撬动全局的杠杆。动作:1) 找稳人口的城(首都/高粮城)"
                    f"用 set_city_production(city_id,'UNIT','UNIT_SETTLER');"
                    f"2) 确认殖民卡(POLICY_COLONIZATION,+50%开拓者)已挂;"
                    f"3) 有金币可用 purchase_item 直接买开拓者;"
                    f"4) 铺城前 get_map_area / get_settle_advisor 定落地格。"
                    f"确属刻意暂停铺城(如 war/经济崩盘),再调一次 end_turn 放行。"
                )
    except Exception:
        log.debug("城数达标检查失败,放行", exc_info=True)

    st["warnings"] = warnings  # 警告档随后贴在报告尾部

    if not blockers:
        return ""
    danger = [b for b in blockers if b.startswith("🔴")]
    if st["waived_turn"] == turn:
        # 同回合第二次调用 = 模型知情坚持,放行;提醒转入警告档。
        # 危险档即使放行也要把后果再说一遍 —— 这是那个建造者的教训。
        # 滞留平民的基线重置到本回合(审查 F1-7):知情豁免后 5 回合内
        # 不再为同一批单位重复拦截,免得合法等待每回合都要双调 end_turn。
        try:
            seen = st.get("settler_first_seen") or {}
            for _uid in stale_ids:
                if _uid in seen:
                    # 三元组:回合/充能/位置。2026-08-07 加位置后这里也要跟着改 ——
                    # 写回 2 元组会让下一次 stall_baseline 把"位置"当成新的,
                    # 于是豁免后的 5 回合退避直接失效(实测 T108 立刻又拦)。
                    _bt, _bc, _bp = _stale_entry(seen[_uid], 0)
                    seen[_uid] = (turn, _bc, _bp)
        except Exception:
            pass
        st["warnings"] = [
            f"· (已放行,后果自负){b}"
            if b.startswith("🔴")
            else f"· (已放行)你坚持结束回合,但:{b}"
            for b in blockers
        ] + warnings
        return ""
    st["waived_turn"] = turn
    lines = [f"Cannot end turn (回合完成度门禁,T{turn}):"]
    lines.extend(f"  * {b}" for b in blockers)
    if danger:
        lines.append(
            "  -> 先救人:move_unit 撤出可达范围或退进城,"
            "或把军事单位移到平民【同格】。处理完再 end_turn。"
        )
        lines.append(
            "  -> 确实要放弃这个平民,再调一次 end_turn() 放行 —— "
            "但它这回合大概率就没了。"
        )
    else:
        lines.append(
            "  -> 处理后再 end_turn;若确属刻意(如建造者在城内等护送),"
            "直接再调一次 end_turn() 即放行。"
        )
    return "\n".join(lines)
