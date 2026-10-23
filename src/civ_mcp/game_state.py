"""High-level game state API with server-side narration.

Wraps GameConnection + lua into typed async methods that return
both structured data and human-readable narrated text. Has ZERO MCP
dependency — enabling multi-agent architectures where specialist servers
import the same GameState class but expose different tool subsets.
"""

from __future__ import annotations

from civ_mcp.resources import runtime_data_path

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from civ_mcp import discipline, knowledge, safety, world_model
from civ_mcp import lua as lq
from civ_mcp.connection import GameConnection
from civ_mcp.narrate import (
    narrate_combat_estimate,
    narrate_move_discoveries,
    narrate_settle_candidates,
    narrate_test_trade,
)

if TYPE_CHECKING:
    from civ_mcp.spatial import SpatialTracker

log = logging.getLogger(__name__)

# 平民生产的安全距离(格)。威胁离该城超过这个距离时,红色警报不拦它的平民生产。
# 取 5:远古骑乘单位移动 4,留 1 格余量。比这更远的威胁够不到当回合出生的平民。
CIVILIAN_SAFE_DISTANCE = 5

# 残血近战攻击的拦截地板(血)。打完低于这条线,基本等于把单位送给对方
# 下一回合收人头。取 20:大多数同期单位一次攻击的期望伤害在 20-40,
# 剩 20 以下几乎必死;剩 25-30 还有撤退回血的余地。
# 2026-08-07 实机 T159 的实证:33 血战车打完剩 6 血,下回合阵亡。
LOW_HP_ATTACK_FLOOR = 20

# 亏本交易的拦截比(缺陷59,2026-08-09 希腊局 T39)。承受伤害达到造成伤害
# 的这个倍数、且这一击打不死目标时,拦下来要 force。取 1.5:1:1 的对拼在
# 有城市/援军接应时是合理的,1:1.5 起就是纯亏 —— 实机那次是 1:2.2
# (打16 吃35),打完两回合就把单位赔进去了。
UNFAVORABLE_EXCHANGE_RATIO = 1.5


# 2026-08-05: get_units 门禁的"本回合读过了吗"跨进程持久化。
# mcp_call 每次调用都是新进程，内存 set 必丢 → 门禁误报。
# 单局单文件即可（同一时间只跑一局），原子替换避免并发写坏。
_TURN_READS_PATH = (
    runtime_data_path("turn_reads.json")
)


def _looks_unmoved(result: str, pos_lines: list[str]) -> bool:
    """这次位置读数是否显示"单位还在起点"。纯函数,可测。

    只用于决定「要不要延迟一帧再读一次」——读数为真时不做任何结论,
    因为 InGame 的移动是异步结算的,第一帧读到起点是常态而非阻塞证据。
    """
    m = re.search(r"\|from:(\d+),(\d+)", result or "")
    if not m:
        return False
    fx, fy = int(m.group(1)), int(m.group(2))
    for line in pos_lines or []:
        if line.startswith("POS|") and "GONE" not in line:
            parts = line.split("|")
            try:
                return int(parts[1]) == fx and int(parts[2]) == fy
            except (IndexError, ValueError):
                return False
    return False


def _coord(v) -> int:
    """地图坐标归一化。缺失/不可解析 → -1(下游据此判"无坐标")。

    单独写成函数是因为 `int(v or -1)` 这个惯用法在坐标上是错的:
    **0 是合法坐标**(地图边缘),会被 `or` 当成假值换掉。
    """
    if v is None:
        return -1
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _load_turn_reads() -> set[str]:
    try:
        import json

        with open(_TURN_READS_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_turn_reads(reads: set[str]) -> None:
    try:
        import json
        import os

        _TURN_READS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TURN_READS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(reads), f)
        os.replace(tmp, _TURN_READS_PATH)
    except Exception as e:
        # 静默丢弃会丢失读档检测的持久化数据(缺陷:档案读档记录写不进去),
        # 至少记录一次,方便在日志里定位。
        log.debug("Failed to persist turn-reads to %s: %s", _TURN_READS_PATH, e)


class GameState:
    """High-level async API for Civ 6 game state + actions."""

    def __init__(self, connection: GameConnection):
        self.conn = connection
        self.spatial: SpatialTracker | None = None
        self._last_snapshot: lq.TurnSnapshot | None = None
        self._game_identity: tuple[str, int] | None = None  # (civ_type, seed)
        self._diary_written_turn: int | None = (
            None  # guard against double-write per turn
        )
        self._end_turn_blocked: bool = False  # last end_turn hit a blocker (diplo/WC)
        self._pending_end_turn: bool = False  # ACTION_ENDTURN already in flight
        self._pending_end_turn_from: int | None = (
            None  # turn number when ACTION_ENDTURN was sent
        )
        self._high_water_turn: int = 0  # highest turn seen (for regression detection)
        self._local_player_id: int = 0  # human player (always 0 in single-player)
        self._hang_retry_active: bool = False  # guard against recursive hang recovery
        self._last_game_over: lq.GameOverStatus | None = (
            None  # captured by execute_end_turn for server.py
        )
        # 任务 AK 闭环:本局教训是否已自动收割(存 game_id,换局自然失效)
        self._learning_harvested: str | None = None
        # 弃局孤儿日志扫描:每个身份只扫一次(存 game_id)
        self._orphan_sweep_for: str | None = None
        # 本规则集有没有忠诚度机制(None=还没探测过)。见 has_loyalty_mechanic。
        self._loyalty_ruleset: bool | None = None
        # (ts, turn, save_name) for each successful save load — used to detect
        # save scumming in _check_save_scumming(). Bounded to last 50 entries.
        self._save_load_history: list[tuple[float, int, str]] = []
        self._run_aborted: bool = False  # set when save scumming threshold is exceeded
        # Per-turn advisor call budget — prevents compulsive advisor loops
        # (e.g. Gemini Pro's 1,567 get_wonder_advisor calls in a single turn).
        # Reset in execute_end_turn on successful turn advance.
        self._advisor_calls_this_turn: int = 0
        # One-shot warning from the most recent advisor call, consumed and
        # cleared by the server wrapper.
        self._advisor_budget_warning: str | None = None
        # L1 世界模型:局内持久的"我知道的世界",只增不减。
        # 每回合由 end_turn 更新并落盘 —— 观测缺失不等于事实消失(铁律二)。
        self.world = world_model.WorldModel()
        self._world_loaded_for: str | None = None
        # L4 执行纪律:谁本回合打过、期间有没有重读单位状态。
        # 校准局两次赔单位(T22/T108)的共同根因是拿过期状态做第二次决策。
        self.combat_memory = discipline.CombatMemory()
        # 本回合调用过哪些读工具 {工具名: 回合}。用于门禁判断「有没有真的看过
        # 局面就想结束回合」。2026-08-03 用户反馈:换成别的模型接手时,
        # 最常见的失败不是判断错,而是**跳过步骤** —— 不查单位、不看城市,
        # 直接 end_turn。那种回合在存档里看不出异常,但产能与机会已经没了。
        # 2026-08-03 修:原先按回合号比对,而 note_read 用的是 _high_water_turn,
        # 它只在 end_turn 成功推进时更新。一旦两者脱节(重启后为 0、或某次
        # end_turn 没推进导致停在旧值),门禁就会误报「你没读过单位」——
        # 实机两次触发。改为【自上次回合推进以来读过什么】的集合,
        # 由 end_turn 在真正推进后清空,不再依赖任何回合号同步。
        # 2026-08-05: mcp_call 每次调用都是新进程，内存 set 跨进程必丢，
        # 导致"本回合没读过 get_units"门禁误报（实机：被迫 get_units+end_turn
        # 同批调用）。改为磁盘持久化（data/turn_reads.json），
        # note_read/has_read/clear 都走文件。
        self._turn_reads: set[str] = set()

    def note_read(self, tool: str) -> None:
        """记录一次状态读取。永不抛异常。"""
        try:
            self._turn_reads.add(str(tool))
            _save_turn_reads(self._turn_reads)
        except Exception:
            pass

    def has_read(self, tool: str) -> bool:
        try:
            return str(tool) in _load_turn_reads()
        except Exception:
            return str(tool) in self._turn_reads

    def clear_turn_reads(self) -> None:
        """回合真正推进后调用 —— 新回合要重新读。"""
        try:
            self._turn_reads.clear()
            _save_turn_reads(set())
        except Exception:
            pass

    async def ensure_world(self) -> world_model.WorldModel:
        """加载(或返回已加载的)本局世界模型。供计划链/胜利目标工具使用。"""
        civ, seed = await self.get_game_identity()
        path = world_model.storage_path(civ, seed)
        key = str(path)
        if self._world_loaded_for != key:
            # load_best:跨所有候选位置取最完整的一份(2026-08-01 事故修复)。
            # 写仍然回 storage_path —— 尊重 CIV_MCP_WORKSPACE 的设计意图,
            # 只是读的时候不因启动环境不同而丢历史。
            self.world, _src = world_model.WorldModel.load_best(civ, seed)
            self._world_loaded_for = key
        return self.world

    def save_world(self) -> None:
        """把世界模型落盘。工具改动计划/胜利目标后调用。"""
        if self._world_loaded_for:
            try:
                self.world.save(Path(self._world_loaded_for))
            except OSError:
                log.debug("世界模型落盘失败", exc_info=True)

    async def get_game_identity(self) -> tuple[str, int]:
        """Return (civ_type_lower, random_seed) for the current game.

        Always queries the game so we detect new-game loads.  When the
        identity changes, all per-game cached state is reset.
        """
        code = (
            "local me = Game.GetLocalPlayer() "
            "local cfg = PlayerConfigurations[me] "
            'print("GAMESEED|" .. cfg:GetCivilizationTypeName() '
            '.. "|" .. tostring(GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED"))) '
            'print("---END---")'
        )
        lines = await self.conn.execute_write(code)
        for line in lines:
            if line.startswith("GAMESEED|"):
                parts = line.split("|")
                civ = parts[1].replace("CIVILIZATION_", "").lower()
                seed = int(parts[2])
                new_id = (civ, seed)
                if self._game_identity is not None and new_id != self._game_identity:
                    log.info("Game changed: %s → %s", self._game_identity, new_id)
                    self._last_snapshot = None
                    self._diary_written_turn = None
                    self._last_game_over = None
                    self._save_load_history = []
                    self._run_aborted = False
                    self._advisor_calls_this_turn = 0
                    self._advisor_budget_warning = None
                self._game_identity = new_id
                # 弃局教训收割:check_game_over 只覆盖正式终局,而校准局
                # 大多中途弃局/换局结束。每次确定新身份时扫一遍孤儿复盘
                # 日志(当前局排除)—— 同身份只扫一次,增量为零。
                gid = f"{new_id[0]}_{new_id[1]}"
                if self._orphan_sweep_for != gid:
                    self._orphan_sweep_for = gid
                    try:
                        from civ_mcp import learning_store

                        n = learning_store.harvest_abandoned(exclude_game_id=gid)
                        if n:
                            log.info("弃局教训收割:%d 局入库", n)
                    except Exception:
                        log.debug("弃局教训收割失败", exc_info=True)
                return self._game_identity
        return ("unknown", 0)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_game_overview(self) -> lq.GameOverview:
        # InGame context needed for GetFavor() (nil in GameCore)
        lines = await self.conn.execute_write(lq.build_overview_query())
        ov = lq.parse_overview_response(lines)
        # Bootstrap: capture baseline snapshot for first end_turn diff
        if self._last_snapshot is None:
            try:
                self._last_snapshot = await self._take_snapshot(ov)
            except Exception:
                log.debug("Failed to bootstrap snapshot", exc_info=True)
        return ov

    async def get_diary_snapshot(self) -> lq.DiarySnapshot:
        """Full per-turn snapshot for diary JSONL. InGame context."""
        lines = await self.conn.execute_write(lq.build_diary_full_query())
        return lq.parse_diary_full_response(lines)

    async def get_rival_snapshot(self) -> list[lq.RivalSnapshot]:
        """Lightweight per-rival stats for diary entries."""
        lines = await self.conn.execute_write(lq.build_rival_snapshot_query())
        return lq.parse_rival_snapshot_response(lines)

    async def check_game_over(self) -> lq.GameOverStatus | None:
        """Check if the game has ended (victory/defeat screen showing).

        Tries InGame context first (full detection with UI checks).
        Falls back to GameCore context (read-only, survives defeat screen)
        when InGame fails — this catches victories that freeze the InGame UI.
        """
        status: lq.GameOverStatus | None
        try:
            lines = await self.conn.execute_write(lq.build_gameover_check())
            status = lq.parse_gameover_response(lines)
        except Exception:
            log.debug("Game-over check failed in InGame, trying GameCore")
            # Fallback: GameCore-only check (survives defeat screen)
            try:
                lines = await self.conn.execute_read(lq.build_gameover_check_gamecore())
                status = lq.parse_gameover_response(lines)
            except Exception:
                log.debug("Game-over check failed in GameCore too", exc_info=True)
                return None
        if status is not None and status.is_game_over:
            self._harvest_learning(status)
        return status

    def _harvest_learning(self, status: lq.GameOverStatus) -> None:
        """任务 AK 闭环:终局时把复盘日志压成教训入库。

        此前 learning_store.extract_learning 无任何调用方 —— 教训永远
        不落盘,下局开局注入的「往局教训」恒为空。终局检测是唯一
        所有胜负路径都会经过的点,收割挂在这里。每局只收一次
        (LLM 之后可用 record_learning 覆盖手写教训)。
        """
        ident = self._game_identity
        if not ident:
            return
        civ, seed = ident
        game_id = f"{civ}_{seed}"
        if self._learning_harvested == game_id:
            return
        vtype = status.victory_type.replace("VICTORY_", "").replace("_", " ").title()
        if status.is_defeat:
            result = f"失败({status.winner_leader or '对手'}以{vtype}取胜)"
        else:
            result = f"胜利({vtype})"
        try:
            from civ_mcp import learning_store

            if learning_store.harvest_game_over(
                civ,
                seed,
                result=result,
                map_type=getattr(self.world, "map_type", "") or "",
            ):
                self._learning_harvested = game_id
        except Exception:
            log.debug("局末教训自动收割失败", exc_info=True)

    async def get_units(self) -> list[lq.UnitInfo]:
        lines = await self.conn.execute_write(lq.build_units_query())
        # L4:读过单位状态就解除 UNREAD_COMBAT_STATE 拦截 —— 这条拦截的
        # 目的是逼一次重读,读了就该放行,不需要 force。
        self.combat_memory.notice_units_read()
        self.note_read("units")
        # 阻塞性外交/交易会话:挂在这里是因为回合完成度门禁强制每回合必调
        # get_units,于是探测必然发生且零额外往返。存到实例上由 server 层
        # 顶格渲染 —— 挡路的东西不能排在 16 个单位后面。
        self._blocking_sessions = lq.parse_blocking_sessions(lines)
        return lq.parse_units_response(lines)

    async def get_builder_tasks(
        self,
    ) -> tuple[list[lq.BuilderTask], list[lq.BuilderInfo]]:
        lines = await self.conn.execute_write(lq.build_builder_tasks_query())
        return lq.parse_builder_tasks(lines)

    async def get_spies(self) -> list[lq.SpyInfo]:
        lines = await self.conn.execute_write(lq.build_get_spies_query())
        return lq.parse_spies_response(lines)

    async def spy_travel(self, unit_index: int, target_x: int, target_y: int) -> str:
        lua = lq.build_spy_travel(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def spy_mission(
        self, unit_index: int, mission_type: str, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_spy_mission(unit_index, mission_type, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def get_threat_scan(self) -> list[lq.ThreatInfo]:
        lines = await self.conn.execute_read(lq.build_threat_scan_query())
        return lq.parse_threat_scan_response(lines)

    async def get_pathing_estimate(
        self, unit_index: int, target_x: int, target_y: int
    ) -> lq.PathingEstimate:
        lines = await self.conn.execute_write(
            lq.build_pathing_estimate_query(unit_index, target_x, target_y)
        )
        return lq.parse_pathing_estimate(lines)

    async def get_victory_progress(self) -> lq.VictoryProgress:
        lines = await self.conn.execute_write(lq.build_victory_progress_query())
        return lq.parse_victory_progress_response(lines)

    async def get_cities(self) -> tuple[list[lq.CityInfo], list[str]]:
        lines = await self.conn.execute_write(lq.build_cities_query())
        return lq.parse_cities_response(lines)

    async def get_enemy_city_defense(self, x: int, y: int) -> dict | None:
        """Foreign city's defense stats at (x,y), or None if unreadable.

        InGame context: GameCore's City object exposes neither GetDistricts
        nor GetDefenseStrength (2026-08-10 罗马局实测). Never raises —
        assess_attack degrades to "城防未知" when this returns None.
        """
        try:
            lines = await self.conn.execute_write(
                lq.build_enemy_city_defense_query(x, y)
            )
        except Exception:
            return None
        return lq.parse_enemy_city_defense(lines)

    async def get_map_area(
        self, center_x: int, center_y: int, radius: int = 2
    ) -> list[lq.TileInfo]:
        lines = await self.conn.execute_read(
            lq.build_map_area_query(center_x, center_y, radius)
        )
        return lq.parse_map_response(lines)

    async def get_strategic_map(self) -> lq.StrategicMapData:
        lines = await self.conn.execute_read(lq.build_strategic_map_query())
        return lq.parse_strategic_map_response(lines)

    async def get_diplomacy(self) -> list[lq.CivInfo]:
        # Uses InGame context for GetDiplomaticAI access
        lines = await self.conn.execute_write(lq.build_diplomacy_query())
        return lq.parse_diplomacy_response(lines)

    async def get_tech_civics(self) -> lq.TechCivicStatus:
        lines = await self.conn.execute_read(lq.build_tech_civics_query())
        return lq.parse_tech_civics_response(lines)

    async def get_empire_resources(
        self,
    ) -> tuple[
        list[lq.ResourceStockpile],
        list[lq.OwnedResource],
        list[lq.NearbyResource],
        dict[str, int],
    ]:
        # InGame context needed for GetResourceStockpileCap etc.
        lines = await self.conn.execute_write(lq.build_empire_resources_query())
        return lq.parse_empire_resources_response(lines)

    # ------------------------------------------------------------------
    # Action methods (run in InGame context for UnitManager access)
    # ------------------------------------------------------------------

    async def _walk_land_hops(
        self,
        unit_index: int,
        hops: list[tuple[int, int]],
        target_x: int,
        target_y: int,
    ) -> str:
        """沿预先算好的陆路逐格移动,绕开引擎的抄近道(T267)。

        每一格都重新走一遍完整的 move_unit —— 包括安全检查。中途格的威胁
        和终点格一样要看:替调用方走路不等于替它承担风险。相邻格的
        plan_land_hops 会返回空,所以不会递归下去。
        """
        walked: list[str] = []
        last = ""
        for hx, hy in hops:
            last = await self.move_unit(unit_index, hx, hy)
            if last.startswith("Error:"):
                # 第一格就失败 = 这条路走不通,如实报错;
                # 走了一半才停(多半是移动力耗尽)算部分进展,不是失败。
                if not walked:
                    return last
                break
            walked.append(f"({hx},{hy})")
            if f"now_at:{hx},{hy}" not in last:
                break  # 没走到这一格,后面的更走不到
            if (hx, hy) == (target_x, target_y):
                break
        if not walked:
            return last
        route = "→".join(walked)
        note = (
            f"|LAND_ROUTE(工具已改走陆路 {route} —— 引擎原路径要下水,登船会让战力归零)"
        )
        if (target_x, target_y) not in [
            (int(a), int(b)) for a, b in re.findall(r"\((\d+),(\d+)\)", route)
        ]:
            note += f"|未到终点 ({target_x},{target_y}),移动力用完,下回合续走"
        return last + note

    async def move_unit(
        self,
        unit_index: int,
        target_x: int,
        target_y: int,
        force: bool = False,
    ) -> str:
        # L0 安全拦截:平民单位(防御力 0)不能走进可见敌方军事单位的打击范围。
        # 检查自身失败时一律放行 —— 拦截层不该因为自己出错就卡住整局游戏。
        hops: list[tuple[int, int]] = []
        route_blocked = False
        if not force:
            try:
                lines = await self.conn.execute_read(
                    safety.build_move_safety_query(unit_index, target_x, target_y)
                )
                move_ctx = safety.parse_move_safety(lines)
                verdict = safety.check_move(move_ctx, target_x, target_y)
                if verdict.blocked:
                    return verdict.as_error()
                # 跨海拦截必须在【下指令之前】。事后警告救不了单位:
                # 移动力已耗尽,当回合撤不回来(T267 线列步兵漂在英国海域)。
                verdict = safety.check_embark(move_ctx, target_x, target_y)
                if verdict.blocked:
                    return verdict.as_error()
                hops = safety.plan_land_hops(move_ctx, target_x, target_y)
                route_blocked = move_ctx.route_blocked
            except Exception:
                log.debug("移动安全检查失败,放行", exc_info=True)
        if hops:
            return await self._walk_land_hops(unit_index, hops, target_x, target_y)
        # Pre-dismiss any blocking popups that would silently eat the move
        try:
            await self.dismiss_popup()
        except Exception:
            pass
        lua = lq.build_move_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if route_blocked:
            # 陆路探测早就算出这条路被单位或封闭边境拦死了。不拦截(挡路的
            # 下回合可能就走开),但必须说出来 —— 2026-08-11 T278 我对着同一
            # 个目标连发三次,每次引擎都撞进桑给巴尔边境,而工具一直知道。
            result += (
                "|⚠️ ROUTE_BLOCKED(陆路存在但被单位占位或封闭边境拦死 —— "
                "重发同一条指令还会撞同一堵墙。先谈开放边境/取得宗主,"
                "或换一个目标)"
            )
        # Post-move: read actual position from GameCore (move is async in InGame)
        if result.startswith("MOVING_TO") or result.startswith("CAPTURE_MOVE"):
            try:
                _q = lq.build_unit_position_query(
                    unit_index,
                    move_target_x=target_x,
                    move_target_y=target_y,
                )
                pos_lines = await self.conn.execute_read(_q)
                # ⚠️ InGame 的移动是**异步结算**的:紧接着发出的位置查询读到的
                # 往往还是起点。此前直接据此判 `now == from` → 输出
                # "BLOCKED (tile appears passable)",而单位其实已经到了目标。
                # 2026-08-07 苏美尔局一晚复现 4 次(战车/侦察兵/开拓者各有)。
                # 危害:agent 按错误坐标做后续决策,或反复重发移动指令空烧回合。
                #
                # 攻击路径的 STOPPED_SHORT 分支早就用"延迟一帧重读"治过同一个病
                # (T110),只是没推广到移动路径。这里补上:**只有在延迟重读后
                # 位置仍等于起点时,才允许下 BLOCKED 结论**。
                if _looks_unmoved(result, pos_lines):
                    await asyncio.sleep(0.3)
                    pos_lines = await self.conn.execute_read(_q)
                for line in pos_lines:
                    if line.startswith("POS|") and "GONE" not in line:
                        parts = line.split("|")
                        now_x, now_y = int(parts[1]), int(parts[2])
                        result += f"|now_at:{now_x},{now_y}"
                        from_match = re.search(r"\|from:(\d+),(\d+)", result)
                        if from_match:
                            from_x = int(from_match.group(1))
                            from_y = int(from_match.group(2))
                            if now_x == from_x and now_y == from_y:
                                reason = lq.parse_blocked_diagnostic(pos_lines)
                                result += f"|BLOCKED ({reason})"
                            else:
                                dx = now_x - from_x
                                dy = (
                                    now_y - from_y
                                )  # positive dy = south (higher Y = south in Civ 6)
                                result += f"|(moved dx:{dx:+d} dy:{dy:+d})"
                                tgt_match = re.search(
                                    r"(?:MOVING_TO|CAPTURE_MOVE)\|(\d+),(\d+)", result
                                )
                                if tgt_match:
                                    tx, ty = (
                                        int(tgt_match.group(1)),
                                        int(tgt_match.group(2)),
                                    )
                                    if (now_x, now_y) != (tx, ty):
                                        result += "|STOPPED_MID_PATH (moves exhausted)"
                        break
                # 登船警告(2026-08-10 罗马局 6 次实锤):目标是陆地格,引擎
                # 却从水面抄近道,单位登船搁浅。登船 = 战力归零、一击即死,
                # 这件事必须写在移动结果里,不能让调用方从坐标自己看出来。
                if any(ln.startswith("EMBARKED|") for ln in pos_lines):
                    result += (
                        "|⚠️ EMBARKED(单位现在在水上:路径经过水面自动登船。"
                        "登船状态战力归零,任何单位一击就能杀死它 —— "
                        "要走陆路请分段下单,一格一格挪)"
                    )
            except Exception:
                pass
        # Post-move: visibility diff for discovery feedback
        blocked = "|BLOCKED" in result
        if not blocked and self.spatial is not None and self.spatial._revealed_seeded:
            try:
                # Extract actual position from result
                now_match = re.search(r"now_at:(\d+),(\d+)", result)
                if now_match:
                    vis_x, vis_y = int(now_match.group(1)), int(now_match.group(2))
                    vis_lines = await self.conn.execute_read(
                        lq.build_post_move_visibility_query(vis_x, vis_y)
                    )
                    vis_tiles = lq.parse_post_move_visibility(vis_lines)
                    all_revealed = {(x, y) for x, y, _ in vis_tiles}
                    newly_revealed = self.spatial.mark_revealed(all_revealed)
                    if newly_revealed:
                        new_tile_data = [
                            (x, y, m)
                            for x, y, m in vis_tiles
                            if (x, y) in newly_revealed
                        ]
                        discovery_text = narrate_move_discoveries(
                            new_tile_data, len(newly_revealed)
                        )
                        if discovery_text:
                            result += "\n" + discovery_text
                        # Record discovery event in spatial tracker
                        await self.spatial.record_discovery(
                            "unit_action",
                            (vis_x, vis_y),
                            newly_revealed,
                            0,
                        )
            except Exception:
                log.debug("Post-move visibility diff failed", exc_info=True)
        return result

    async def attack_unit(
        self,
        unit_index: int,
        target_x: int,
        target_y: int,
        force: bool = False,
    ) -> str:
        # L0 安全拦截:远程单位攻击会耗尽移动力,先移动再攻击等于白扔一回合。
        # L4 执行纪律拦截同批处理 —— 同一次查询带回全部事实,不加往返。
        if not force:
            try:
                lines = await self.conn.execute_read(
                    safety.build_move_safety_query(unit_index, target_x, target_y)
                )
                ctx = safety.parse_move_safety(lines)
                verdict = safety.check_ranged_attack(ctx)
                if not verdict.blocked:
                    verdict = discipline.check_attack(
                        ctx,
                        memory=self.combat_memory,
                        unit_index=unit_index,
                        turn=self._high_water_turn,
                    )
                if verdict.blocked:
                    return verdict.as_error()
            except Exception:
                log.debug("攻击前拦截失败,放行", exc_info=True)
        # Pre-attack: dismiss any blocking popups that would silently eat the attack
        try:
            await self.dismiss_popup()
        except Exception:
            pass
        # Pre-attack: run combat estimator
        estimate_str = ""
        est: lq.CombatEstimate | None = None
        try:
            est_lua = lq.build_combat_estimate_query(unit_index, target_x, target_y)
            est_lines = await self.conn.execute_write(est_lua)
            est = lq.parse_combat_estimate(est_lines, 0, 0)
            if est:
                estimate_str = narrate_combat_estimate(est) + "\n"
                # 兵种克制提醒(2026-08-07):估算里 CS 已含加成,但没人说破
                # 【为什么】守方 CS 会比面板高。写明克制关系,才让"换个单位打"
                # 成为一个能想到的选项 —— 这是"军队编成"缺口的最小修法。
                try:
                    from civ_mcp import war_planner as _wp

                    _warn = _wp.counter_warning(
                        str(getattr(est, "attacker_type", "") or ""),
                        str(getattr(est, "defender_type", "") or ""),
                    )
                    if _warn:
                        estimate_str = _warn + "\n" + estimate_str
                except Exception:
                    log.debug("兵种克制提醒失败", exc_info=True)
        except Exception as e:
            log.debug("Combat estimate failed: %s", e)
        # 残血发起近战的执行前拦截(2026-08-07 实机 T159,损失一辆战车)。
        #
        # 当时:33 血战车打 36 血蛮族弩手,估算写着"我打24/我吃24"——
        # 数字就在眼前,我还是按"再补一下它就死了"的惯性打了,打完剩 6 血,
        # 下回合被反杀。而那个 10 血的敌人,下回合城市射击就能免费点掉。
        #
        # 简报里一直有"低于60%的单位撤出接触线"这句(T108 校准局的教训),
        # 但它是【提醒】,不是【拦截】——提醒挡不住惯性。这个项目的原则是
        # 执行前拦截优先于文档提醒,这条一直漏在攻击路径外面。
        #
        # 判据全部来自估算,不猜:打完剩余血量低于阈值就拦。远程不拦
        # (无反击,est_damage_to_attacker 恒为 0)。
        if est is not None and not getattr(est, "is_ranged", False):
            try:
                _hp = int(getattr(est, "attacker_hp", 0) or 0)
                _take = int(getattr(est, "est_damage_to_attacker", 0) or 0)
                _after = _hp - _take
                if _hp > 0 and _after <= LOW_HP_ATTACK_FLOOR and not force:
                    return (
                        f"{estimate_str}"
                        f"ERR:LOW_HP_ATTACK|该单位当前 {_hp} 血,按上面的估算打完只剩 "
                        f"{max(_after, 0)} 血 —— 低于 {LOW_HP_ATTACK_FLOOR} 就基本等于"
                        f"送给对方下一回合收人头(实机 T159 就是这么丢的一辆战车)。\n"
                        f"先问三件事:①这一击是不是非打不可(目标下回合会不会被城市"
                        f"射击/远程免费点掉)?②有没有满血单位能替它上?③把它撤回"
                        f"城里回血,两三回合后它还是个完整战力。\n"
                        f"确认这一击值得用掉这个单位,重调并传 force=True + force_reason。"
                    )
            except Exception:
                log.debug("残血攻击拦截失败,放行", exc_info=True)
        # 亏本交易拦截(缺陷59,2026-08-09 希腊局 T39-T41,损失一个勇士)。
        #
        # 当时:75 血勇士打丘陵上满血蛮族勇士,估算白纸黑字写着
        # "我打16 / 我吃35" —— 血量比是 1:2.2 的亏本买卖,而且打不死。
        # 打完 43 血,两回合后被收人头。上面的 LOW_HP 门禁没拦:打完还剩
        # 40 > 20,它管的是"打完会不会死",不管"这一击划不划算"。
        #
        # 估算是在同一次调用里【攻击执行之前】算好的,却只是打印出来 ——
        # 看到它的时候攻击已经结算了,人根本没有中止的机会。这正是
        # "提醒挡不住惯性"那条原则的漏网之鱼:判据齐全,没接到门禁上。
        #
        # 判据:近战 + 打不死 + 承受伤害 >= 造成伤害 × RATIO。地形/克制
        # 加成都已经算进估算的 CS 里,所以这一条同时覆盖了"别打丘陵上的
        # 满血单位"和"别用被克制的兵种硬顶"。
        if est is not None and not getattr(est, "is_ranged", False) and not force:
            try:
                _deal = int(getattr(est, "est_damage_to_defender", 0) or 0)
                _take = int(getattr(est, "est_damage_to_attacker", 0) or 0)
                _def_hp = int(getattr(est, "defender_hp", 0) or 0)
                _kills = _deal >= _def_hp > 0
                if (
                    not _kills
                    and _deal > 0
                    and _take >= _deal * UNFAVORABLE_EXCHANGE_RATIO
                ):
                    return (
                        f"{estimate_str}"
                        f"ERR:UNFAVORABLE_EXCHANGE|按上面的估算,这一击造成 ~{_deal} 伤害"
                        f"却要承受 ~{_take}(1:{_take / max(_deal, 1):.1f}),而且打不死"
                        f"(目标 {_def_hp} 血)。守方的地形/克制加成已经算进它的 CS 里了。\n"
                        f"先问三件事:①能不能先用远程削一轮再上近战?②换个不被克制的"
                        f"兵种打是不是就翻盘了?③目标站在丘陵/森林上时,把它引下来"
                        f"比硬冲便宜。\n"
                        f"确实需要这一击(挡路/钉住/逼退),重调并传 force=True + force_reason。"
                    )
            except Exception:
                log.debug("亏本交易拦截失败,放行", exc_info=True)
        lua = lq.build_attack_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # 2026-08-02 校准局:攻击被前置校验拒绝(OUT_OF_RANGE/ZOC/NO_MOVES…)
        # 时不再输出估算 —— 此前先打印完整估算再报错,给出的是一场
        # 不可能发生的战斗的数字。
        if result.startswith(("Error", "ERR")):
            # STOPPED_SHORT 的坐标是【移动尚未结算完】时读到的中间位置。
            # 2026-08-03 实机:报「moved to (33,35)」,而 get_units 显示单位
            # 实际已到 (32,36) —— 差一格就是「下回合能不能打到」的差别。
            # 与 0d 同一个模式:延迟一帧重读真实位置,改写消息。
            if "STOPPED_SHORT" in result:
                # 2026-08-09 (35) 落码:STOPPED_SHORT 是同帧误报高发区 ——
                # T204 战车打长枪报 stopped short 实已命中,T205 战车攻城
                # 报 stopped short 实际单位当场阵亡。证据重读三件事:
                # ① 单位还在吗(没了=攻击结算且阵亡,这不是"没打到")
                # ② 单位真实位置(中途快照不可信)
                # ③ 目标格现状(掉血=攻击已结算,勿重复攻击)
                try:
                    await asyncio.sleep(0.3)
                    pos_lines = await self.conn.execute_write(
                        lq.build_unit_hp_query(unit_index)
                    )
                    alive = False
                    for pl in pos_lines:
                        if pl.startswith("OK:UNIT_POS|"):
                            alive = True
                            real = pl.split("|")[1]
                            result += (
                                f" [实际位置已重读:{real} —— 上面括号里的坐标是"
                                f"移动结算中途的快照,以这个为准]"
                            )
                            break
                    if not alive:
                        return (
                            "ATTACK_RESOLVED_UNIT_LOST|STOPPED_SHORT 是同帧"
                            "误报 —— 延迟重读后本单位已不存在:攻击(或敌方"
                            "反击)实际已结算,单位阵亡。不要按'没打到'重试。"
                        )
                    try:
                        tgt_lines = await self.conn.execute_write(
                            lq.build_attack_followup_query(target_x, target_y)
                        )
                        tgt_now = [
                            tl
                            for tl in tgt_lines
                            if tl.startswith(("UNIT|", "CITY_DEF|"))
                        ]
                        if tgt_now:
                            result += (
                                f" [目标格重读:{'; '.join(tgt_now[:3])} —— "
                                f"若血量低于攻击前,攻击实际已结算,勿重复攻击]"
                            )
                    except Exception:
                        log.debug("STOPPED_SHORT 目标重读失败", exc_info=True)
                except Exception:
                    log.debug("STOPPED_SHORT 位置重读失败", exc_info=True)
            return result
        # L4:攻击已真正发生 —— 记账。下一次同单位攻击前必须重读 get_units。
        self.combat_memory.notice_attack(unit_index, self._high_water_turn)
        # Combat followup: the game engine processes combat asynchronously
        # after RequestOperation.  Lua state within the same turn frame
        # does NOT reflect post-combat HP regardless of how long we wait.
        # Strategy: use the combat estimate as authoritative damage source
        # and only query the tile to detect if the target was eliminated.
        is_melee = result.startswith("MELEE_ATTACK")
        is_air = result.startswith("AIR_ATTACK")
        if "|city HP:" in result or "CITY_CAPTURED" in result:
            # 城市攻击:Lua 已重读城血自报,tile followup 只会看到
            # 「无单位」并误报 Target eliminated,跳过。
            # 2026-08-04 P0:远程把城打穿(HP -> 0)且没有 CITY_CAPTURED =
            # 城市被夷平而非占领(实机:亚丁)。这是最贵的误判之一 ——
            # 本可拿下的首都从地图上消失。显式警告,逼调用方确认。
            if "CITY_CAPTURED" not in result:
                # 2026-08-11 罗马局 T232:城市分支从来没做过延迟重读,
                # NO_PROGRESS 是拿【同帧陈旧城血】判出来的 —— 于是每一次
                # 成功的攻城都被判成失败。实测:火枪手两击各打进 54 点
                # (城血 175→127→73、城墙 25→16→9),战报两次都写
                # "city HP:175 -> 175/200 NO_PROGRESS(...别再拿人填)"。
                # 这条本是伦敦那次防假成功加的,自己却成了假失败的源头,
                # 而且附带的建议会直接劝停一场正在赢的攻城。
                #
                # 另一半根因:有城墙时伤害【先扣墙】,城血原地不动是正常的。
                # 只看城血判"打不动"从一开始就是错的判据。
                result = await self._refresh_city_attack_result(
                    result, target_x, target_y, unit_index
                )
                try:
                    m = re.search(r"city HP:\d+ -> (\d+)/(\d+)", result)
                    if m and int(m.group(1)) <= 0 and int(m.group(2)) > 0:
                        result += (
                            "\n  ⚠️ 该城 HP 已归零:这次攻击已将城市夷平(远程不能占领)。"
                            "要在城市 HP 归零前用近战单位收尾才能占领。"
                        )
                except Exception:
                    log.debug("City-raze detection failed", exc_info=True)
            return estimate_str + result
        if result.startswith("RANGE_ATTACK") or is_melee or is_air:
            pre_hp = _extract_pre_hp(result)
            est_dmg = est.est_damage_to_defender if est else None
            local_id = self._local_player_id

            # 2026-08-04 P0:远程攻击城市把 HP 打到 0 = 夷平而非占领
            # (实机:弩手打掉 0 血亚丁,城市直接 eliminated 消失)。
            # RANGE_ATTACK 结果行带 pre_hp 且目标名含 "CITY",
            # followup 里 CITY_DEF 消失即确认夷平 —— 显式警告,逼调用方
            # 在城血归零前改用近战收尾。Lua 执行前的 WARN:CITY_WILL_RAZE
            # 由 _action_result 保留,这里负责执行后的确认。
            raze_suspect = (
                "RANGE_ATTACK" in result
                and "target:CITY" in result
                and pre_hp is not None
                and pre_hp <= 0
            )

            # 2026-08-11 罗马局 T229:此前是「sleep 0.5 秒读一次」,读回来的
            # 敌方 HP 仍是战前值 —— 火枪手打英国重装步兵,战报写
            # "enemy HP:100 -> 100/100"(看起来一点没打动),而几秒后
            # get_units 读到的真值是 66/100。旧注释断言「同一回合帧内无论
            # 等多久都读不到战后 HP」,实测不成立:读得到,只是 0.5 秒不够。
            # 这个假失败比伦敦那次的假成功更贵 —— 它劝你放弃一场正在赢的
            # 攻击。改成轮询到真值出现(或目标消失)为止。
            followup: list[str] = []
            for _ in range(ATTACK_FOLLOWUP_TRIES):
                await asyncio.sleep(ATTACK_FOLLOWUP_DELAY)
                try:
                    followup = await self.conn.execute_write(
                        lq.build_attack_followup_query(target_x, target_y)
                    )
                except Exception as e:
                    log.debug("Attack followup read failed: %s", e)
                    continue
                probe = _extract_post_hp(followup, local_id)
                # 目标已不在该格(击杀/占领)或血确实掉了 —— 结算完成
                if probe is None or pre_hp is None or probe < pre_hp:
                    break

            try:
                followup_str = _format_attack_followup(followup, local_id)
                city_def = _extract_city_defense(followup)

                if raze_suspect and city_def is None:
                    result += (
                        "\n  ⚠️ 城市 HP 已归零且防御消失:该城已被夷平(远程不能占领)。"
                        "要在城市 HP 归零前用近战单位收尾才能占领。"
                    )

                # Check if target was eliminated (no enemy units on tile).
                # 2026-08-10 罗马局 T157-159(缺陷:假击杀):远程打【城市】时,
                # 城格上本来就没有敌方 UNIT 行(驻军不是独立单位),旧判定
                # 连续三回合报 "damage dealt:200 (killed) / Target eliminated"
                # 而城血 187→166 好好地立着。城市目标的存亡看 CITY_DEF 行:
                # 它消失才是真的没了(夷平/占领),否则城还在。
                enemy_units = [
                    l
                    for l in followup
                    if l.startswith("UNIT|") and f"owner:{local_id}" not in l
                ]
                is_city_target = "target:CITY" in result
                if is_city_target:
                    eliminated = city_def is None and not enemy_units
                else:
                    eliminated = not enemy_units

                # Build damage report from estimate (authoritative) or followup
                post_hp = _extract_post_hp(followup, local_id)
                damage_info = ""
                if eliminated and pre_hp is not None:
                    damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"
                    # 0d fix: 把误导性的战前箭头值改写为 KILLED
                    result = re.sub(
                        r"(enemy HP:\d+ -> )\d+/\d+", r"\g<1>KILLED", result, count=1
                    )
                elif pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    # Followup reflects real change (can happen for city attacks)
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                    # 0d fix: 箭头右侧用重读到的真实战后值
                    result = re.sub(
                        r"(enemy HP:\d+ -> )\d+(/\d+)",
                        rf"\g<1>{post_hp}\g<2>",
                        result,
                        count=1,
                    )
                elif est_dmg and est_dmg > 0 and pre_hp is not None:
                    # Followup stale — use estimate as best available
                    capped_dmg = min(est_dmg, pre_hp)
                    est_post = pre_hp - capped_dmg
                    damage_info = f"|est damage dealt:~{capped_dmg}"
                    followup_str = (
                        f"~{est_post}/{pre_hp} (estimate — verify with get_units)"
                    )
                    # 轮询到头仍没读到战后血。箭头右侧照抄战前值会读成
                    # 「这一击完全没打动它」—— 那是本工具最贵的一类假象。
                    # 明写「未结算」,让人去 get_units 核实而不是放弃进攻。
                    result = re.sub(
                        r"(enemy HP:\d+ -> )\d+(/\d+)",
                        r"\g<1>?未结算\g<2>",
                        result,
                        count=1,
                    )

                if city_def:
                    w_hp, w_max, g_hp, g_max = city_def
                    if w_max > 0:
                        damage_info += (
                            f"|city walls: {w_hp}/{w_max}, garrison: {g_hp}/{g_max}"
                        )

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("Attack followup formatting failed: %s", e)

            # 0d fix(2026-08-02 校准局,6 次实证):近战返回的
            # "your HP:A -> B" 两侧都是战前值 —— 同帧读不到战后状态,
            # agent 连打两次后会以为部队 84 血、实际 66 血,第三次攻击
            # 就是送。在延迟后的独立帧重读我方 HP,改写箭头右侧;
            # 单位查不到(可能已阵亡)则显式警告。
            if is_melee:
                try:
                    hp_lines = await self.conn.execute_write(
                        lq.build_unit_hp_query(unit_index)
                    )
                    my_post: str | None = None
                    for hl in hp_lines:
                        if hl.startswith("OK:UNIT_HP|"):
                            my_post = hl.split("|")[1].split("/")[0]
                            break
                    if my_post is not None:
                        result = re.sub(
                            r"(your HP:\d+ -> )\d+", rf"\g<1>{my_post}", result, count=1
                        )
                    elif any(
                        "UNIT_NOT_FOUND" in hl or "UNIT_STALE" in hl for hl in hp_lines
                    ):
                        result += (
                            "\n  ⚠️ Your unit no longer found after combat —"
                            " it may have DIED. Verify with get_units."
                        )
                except Exception as e:
                    log.debug("Own-HP followup read failed: %s", e)
        return estimate_str + result

    async def city_attack(self, city_id: int, target_x: int, target_y: int) -> str:
        lua = lq.build_city_attack(city_id, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("CITY_RANGE_ATTACK"):
            pre_hp = _extract_pre_hp(result)
            await asyncio.sleep(0.3)
            followup: list[str] = []
            try:
                followup = await self.conn.execute_write(
                    lq.build_attack_followup_query(target_x, target_y)
                )
            except Exception:
                followup = []
            try:
                followup_str = _format_attack_followup(followup)
                post_hp = _extract_post_hp(followup)
                damage_info = ""
                if pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                elif not any(l.startswith("UNIT|") for l in followup):
                    if pre_hp is not None:
                        damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("City attack followup failed: %s", e)
        return result

    async def _refresh_city_attack_result(
        self, result: str, city_x: int, city_y: int, unit_index: int | None = None
    ) -> str:
        """把城攻战报里的同帧陈旧城血换成延迟重读的真值。

        同 (84) 的单位版:战斗异步结算,当帧读到的还是战前值。城市这一路
        此前完全没有重读,NO_PROGRESS 判定因此建立在战前值上,必然误报。
        """
        m = re.search(r"city HP:(\d+) -> (\d+)/(\d+)", result)
        if not m:
            return result
        pre_hp = int(m.group(1))
        pre_wall = _extract_reported_wall_hp(result)

        info = None
        for _ in range(ATTACK_FOLLOWUP_TRIES):
            await asyncio.sleep(ATTACK_FOLLOWUP_DELAY)
            # 2026-08-11 T260(缺陷 89):占领斯摩棱斯克的那一击又报了
            # NO_PROGRESS —— (86) 加的城主检查这次没救住,因为易主的结算比
            # 2 秒轮询还慢,窗口内读到的 owner 仍是原主、城血也仍是旧值。
            # 更早、更硬的判据是【攻击者自己站到了城格上】:近战占领在
            # Civ6 里就是移动进城,单位位置一变就是既成事实,不依赖城市
            # 对象的所有权字段何时刷新。这一条先于所有血量比较。
            if unit_index is not None:
                try:
                    pos_lines = await self.conn.execute_read(
                        lq.build_unit_position_query(unit_index)
                    )
                    for pl in pos_lines:
                        if not pl.startswith("POS|") or "GONE" in pl:
                            continue
                        parts = pl.split("|")
                        px, py = parts[1], parts[2]
                        if int(px) == city_x and int(py) == city_y:
                            cleaned = re.sub(r"\|NO_PROGRESS\([^)]*\)", "", result)
                            return (
                                cleaned + "|CITY_CAPTURED(你的单位已进入城格,该城易主)"
                            )
                except Exception as e:
                    log.debug("Attacker position re-read failed: %s", e)
            try:
                lines = await self.conn.execute_write(
                    lq.build_enemy_city_defense_query(city_x, city_y)
                )
                info = lq.parse_enemy_city_defense(lines)
            except Exception as e:
                log.debug("City post-attack re-read failed: %s", e)
                continue
            if info is None:
                # 城没了 = 被夷平或占领,交给上层的归零分支处理
                break
            if int(info.get("garrison_hp") or 0) < pre_hp:
                break
            if pre_wall is not None and int(info.get("wall_hp") or 0) < pre_wall:
                break

        if info is None:
            return result

        # 2026-08-11 T233:占领沃罗涅什的那一击,战报写的是
        # "city HP:27 -> 27/200 NO_PROGRESS(...别再拿人填)" —— 城已经易主,
        # 工具却在劝你别打了。占领必须先于任何血量比较判定:城主变成自己
        # 就是拿下了,城血读数在这一刻毫无意义(读到的是我方新城的血)。
        try:
            mine = getattr(self, "_local_player_id", None)
            if mine is not None and int(info.get("owner", -1)) == int(mine):
                result = re.sub(r"\|NO_PROGRESS\([^)]*\)", "", result)
                return result + "|CITY_CAPTURED(该城已易主,现在归你)"
        except (TypeError, ValueError):
            pass

        post_hp = int(info.get("garrison_hp") or 0)
        post_wall = int(info.get("wall_hp") or 0)
        wall_max = int(info.get("wall_max_hp") or 0)
        result = re.sub(
            r"(city HP:\d+ -> )\d+(/\d+)", rf"\g<1>{post_hp}\g<2>", result, count=1
        )
        if wall_max > 0:
            result += f"|城墙 {post_wall}/{wall_max}"

        hp_moved = post_hp < pre_hp
        wall_moved = pre_wall is not None and post_wall < pre_wall
        if hp_moved or wall_moved:
            # 真有进展 —— 把误报的 NO_PROGRESS 整段拆掉,连同它那句
            # "别再拿人填"的错误建议
            result = re.sub(r"\|NO_PROGRESS\([^)]*\)", "", result)
            gained = []
            if wall_moved:
                gained.append(f"城墙 -{pre_wall - post_wall}")
            if hp_moved:
                gained.append(f"城血 -{pre_hp - post_hp}")
            result += f"|本击实际战果:{'、'.join(gained)}"
        elif post_wall > 0 and "NO_PROGRESS" in result:
            # 城墙还立着时城血不动是正常的(伤害先扣墙),别说成"打不动"
            result = re.sub(
                r"\|NO_PROGRESS\([^)]*\)",
                f"|城血未变,但城墙还有 {post_wall}/{wall_max} —— "
                "有墙时伤害先扣城墙,这不代表打不动。先把墙打掉。",
                result,
            )
        return result

    async def resolve_city_capture(self, action: str) -> str:
        lua = lq.build_resolve_city_capture(action)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def found_city(self, unit_index: int) -> str:
        # Pre-dismiss any blocking popups (tech completion, era change, etc.)
        try:
            await self.dismiss_popup()
        except Exception:
            pass

        lua = lq.build_found_city(unit_index)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        if result.startswith("FOUNDED|"):
            # Extract coordinates from "FOUNDED|x,y"
            parts = result.split("|")[1].split(",")
            x, y = int(parts[0]), int(parts[1])
            # Verify city was actually created (RequestOperation is async)
            verify_lua = lq.build_verify_city_at(x, y)
            verify_lines = await self.conn.execute_read(verify_lua)
            verified = lq.parse_verify_city_at(verify_lines)
            if not verified:
                # Retry once — popup may have blocked the async operation
                try:
                    await self.dismiss_popup()
                    lines = await self.conn.execute_write(lua)
                    retry_result = _action_result(lines)
                    if retry_result.startswith("FOUNDED|"):
                        verify_lines = await self.conn.execute_read(verify_lua)
                        if lq.parse_verify_city_at(verify_lines):
                            result = retry_result
                            verified = True
                except Exception:
                    log.debug(
                        "found_city retry after popup dismiss failed", exc_info=True
                    )
                if not verified:
                    result = (
                        f"Error: FOUND_FAILED|Founding at {x},{y} was requested but "
                        "city did not appear despite popup dismissal."
                    )

        # On settle failure, run the settle advisor to suggest alternatives
        if result.startswith("Error: CANNOT_FOUND") or result.startswith(
            "Error: FOUND_FAILED"
        ):
            try:
                advisor_result = await self.get_settle_advisor(unit_index)
                result += "\n\n" + advisor_result
            except Exception as e:
                log.debug("Settle advisor failed: %s", e)
        return result

    async def get_settle_advisor(self, unit_index: int) -> str:
        lua = lq.build_settle_advisor_query(unit_index)
        lines = await self.conn.execute_read(lua)
        candidates = lq.parse_settle_advisor_response(lines)
        if candidates:
            return narrate_settle_candidates(candidates)
        # Auto-fallback to global scan when no local candidates
        try:
            global_candidates = await self.get_global_settle_scan()
            if global_candidates:
                header = "No valid settle locations within 5 tiles. Best sites on revealed map:\n"
                return header + narrate_settle_candidates(global_candidates[:5])
        except Exception:
            log.debug("Global settle fallback failed", exc_info=True)
        return "No valid settle locations found within 5 tiles or on revealed map."

    async def get_global_settle_scan(self) -> list[lq.SettleCandidate]:
        lua = lq.build_global_settle_scan()
        lines = await self.conn.execute_read(lua)
        return lq.parse_settle_advisor_response(lines)

    async def fortify_unit(self, unit_index: int) -> str:
        lua = lq.build_fortify_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("SLEEPING"):
            # 别再声称「这种单位不能防御」—— 火枪手当然能,失败的真实原因是
            # 单位当时的状态。说清楚代价:睡眠没有 +6 防御加成,也不会自动醒。
            return (
                "SLEEPING|无法进入防御姿态,已改为睡眠待命。"
                "注意:睡眠没有 +6 防御加成,敌人靠近也不会自动唤醒 —— "
                "前线单位请改用 alert,或先移动一格再 fortify。"
            )
        return result

    async def ensure_manual_turn_control(self) -> bool:
        """确保回合只能由 end_turn 推进。幂等,每进程最多真正执行一次。

        见 lq.build_disable_auto_end_turn 的说明:AutoEndTurn 开着时,任何把
        最后一个待办单位 FinishMoves 掉的操作都会让游戏自己结束回合,
        end_turn 连同它写日记的那一步被整个跳过。
        """
        if getattr(self, "_auto_end_turn_disabled", False):
            return False
        try:
            await self.conn.execute_write(lq.build_disable_auto_end_turn())
            self._auto_end_turn_disabled = True
            return True
        except Exception:
            # 关不掉不该让 skip 失败 —— 但必须留痕,别再静默一整局。
            log.warning("无法关闭 AutoEndTurn,回合可能被游戏自动结束", exc_info=True)
            return False

    async def skip_unit(self, unit_index: int) -> str:
        await self.ensure_manual_turn_control()
        lua = lq.build_skip_unit(unit_index)
        lines = await self.conn.execute_read(lua)
        return _action_result(lines)

    async def skip_remaining_units(self) -> str:
        await self.ensure_manual_turn_control()
        # First try to fortify/heal combat units (InGame context)
        fortify_result = ""
        try:
            lua_fort = lq.build_fortify_remaining_units()
            fort_lines = await self.conn.execute_write(lua_fort)
            fortify_result = _action_result(fort_lines)
        except Exception as e:
            log.debug("Fortify remaining failed: %s", e)
        # Then skip anything still with moves (GameCore context)
        lua = lq.build_skip_remaining_units()
        lines = await self.conn.execute_read(lua)
        skip_result = _action_result(lines)
        if fortify_result and not fortify_result.startswith("Error"):
            return f"{fortify_result}\n{skip_result}"
        return skip_result

    async def automate_explore(self, unit_index: int) -> str:
        lua = lq.build_automate_explore(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def heal_unit(self, unit_index: int) -> str:
        lua = lq.build_heal_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def alert_unit(self, unit_index: int) -> str:
        lua = lq.build_alert_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def sleep_unit(self, unit_index: int) -> str:
        lua = lq.build_sleep_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def delete_unit(self, unit_index: int) -> str:
        lua = lq.build_delete_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def get_deleted_units(self) -> set[tuple[int, int]]:
        """Read and clear the Lua-side record of disbanded unit positions.

        build_delete_unit records each disbanded unit's (x, y) into
        ExposedMembers.MCPDeletedUnits (persists across FireTuner
        connections inside the game process). end_turn's lost-unit diff
        filters these out so voluntary disbands are not reported as
        combat deaths. Returns the recorded positions.
        """
        lua = (
            "if not ExposedMembers or not ExposedMembers.MCPDeletedUnits then "
            'print("NONE"); print("{lq.SENTINEL}"); return end; '
            "for _, v in ipairs(ExposedMembers.MCPDeletedUnits) do "
            f'print("DELETED|" .. tostring(v)) end; '
            "ExposedMembers.MCPDeletedUnits = {}; "
            f'print("{lq.SENTINEL}")'
        )
        positions: set[tuple[int, int]] = set()
        try:
            lines = await self.conn.execute_write(lua)
        except Exception:
            return positions
        for line in lines:
            if line.startswith("DELETED|"):
                xy = line.split("|", 1)[1]
                try:
                    xs, ys = xy.split(",")
                    positions.add((int(xs), int(ys)))
                except (ValueError, TypeError):
                    continue
        return positions

    async def improve_tile(self, unit_index: int, improvement_name: str) -> str:
        lua = lq.build_improve_tile(unit_index, improvement_name)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def remove_feature(self, unit_index: int) -> str:
        lua = lq.build_remove_feature(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def repair_improvement(self, unit_index: int) -> str:
        lua = lq.build_repair_improvement(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def remove_improvement(self, unit_index: int) -> str:
        lua = lq.build_remove_improvement(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def sacrifice_builder_charges(self, unit_index: int) -> str:
        lua = lq.build_sacrifice_builder_charges(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def hurry_production(self, unit_index: int, kind: str = "auto") -> str:
        lua = lq.build_hurry_production(unit_index, kind)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def build_route(self, unit_index: int) -> str:
        lua = lq.build_build_route(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def set_city_production(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        target_x: int | None = None,
        target_y: int | None = None,
        force: bool = False,
    ) -> str:
        itype = item_type.upper()

        # L3 优先级仲裁:红色警报下不生产平民 —— 复盘里 T25 花 215 金买的
        # 建造者 2 回合后就被杀,那笔钱本可以买军事单位。
        #
        # 2026-08-01 实机修复:此前直接读 self.world,而 self.world 只有在
        # end_turn 跑过 _update_world_model 之后才有内容 —— 新会话接手的
        # 【第一个回合】它是空模型(threat_level 默认 GREEN),拦截静默失效。
        # 实测:T32 三个蛮族在 1-2 格内,建造者生产被直接放行。
        # 改用 ensure_world() 从磁盘加载上一回合落盘的威胁等级 —— 这正是
        # 铁律二(观测缺失 ≠ 事实消失):没有新观测时,上回合是红色就仍算红色。
        upper_name = item_name.upper()
        # 2026-08-09 T284/T300 实机:此前只认 SETTLER/BUILDER,于是考古学家、
        # 商人被偏离提醒当成"再造军队会拖慢胜利"——平民名单要按引擎的
        # FormationClass 口径列全,不能靠两个词凑。
        _CIVILIAN_MARKERS = (
            "SETTLER",
            "BUILDER",
            "TRADER",
            "ARCHAEOLOGIST",
            "MISSIONARY",
            "APOSTLE",
            "INQUISITOR",
            "GURU",
            "SPY",
            "NATURALIST",
            "ROCK_BAND",
        )
        is_civilian_item = any(m in upper_name for m in _CIVILIAN_MARKERS)
        wm = getattr(self, "world", None)
        if is_civilian_item and not force:
            # 只在真要用到威胁等级时才加载,避免给非平民生产增加开销
            try:
                wm = await self.ensure_world()
            except Exception:
                log.debug("威胁等级加载失败,放行生产", exc_info=True)
        if (
            not force
            and is_civilian_item
            and wm is not None
            and wm.threat_level == "RED"
        ):
            # 2026-08-03 实机修正:威胁等级是**全局**的,而平民在**某座城**出生。
            # 此前直接拿全局红警拦所有城 —— 罗马距那个蛮族 12 格,却因为
            # 一个侦察兵离它 3 格而被拦。误拦的代价不是这一次,是让人养成
            # 无脑 force 的习惯,那这条拦截就彻底失效了。
            dist = None
            try:
                dist = wm.threat_distance_to(city_id)
            except Exception:
                dist = None
            # 缺陷60(2026-08-09 希腊局 T43-T51 实测):蛮族散兵触发的 RED 把
            # 平民生产硬拦了八回合 —— 而当时编成是 CS28 重装步兵×2+弓箭手×2,
            # 城防 18-28,CS20 的蛮族勇士连城墙都碰不掉。八回合产能全烧在
            # 打地鼠上,第一个开拓者 T64 才出(对手 T50 已 4 城),六项经济
            # 指标全场垫底。威胁等级只量「距离」不量「谁会赢」;蛮族不会
            # 围城,平民在城里出生本来就零风险 —— 危险的只是无护送出城,
            # 那一步另有 CIVILIAN_UNESCORTED 门禁把守。
            # 判据:蛮族来源 + 我方军事单位 ≥4 → 放行,附警告不硬拦。
            barb_red = "Barbarian" in (wm.threat_reason or "") or "蛮族" in (
                wm.threat_reason or ""
            )
            if dist is not None and dist > CIVILIAN_SAFE_DISTANCE:
                pass  # 该城离威胁足够远,放行
            elif barb_red and (wm.military_count or 0) >= 4:
                log.info(
                    "RED_ALERT_CIVILIAN waived: barbarian-only threat with "
                    "%d military units — producing %s",
                    wm.military_count,
                    item_name,
                )
            else:
                where = (
                    f",最近威胁距该城 {dist} 格"
                    if dist is not None
                    else "(拿不到该城与威胁的距离,按保守处理)"
                )
                return (
                    f"Error: RED_ALERT_CIVILIAN|威胁等级红色"
                    f"({wm.threat_reason}){where}。现在生产 {item_name} "
                    f"大概率是给蛮族送人头 —— 先解除威胁,或生产军事单位。"
                    f"确认要冒险请重调并传 force=True。"
                )

        # L3 胜利路线偏离提醒(不拦截,只把跑偏这件事说出来):
        # 用户原话:"选了朝鲜打着打着跑去进攻别人,不好好走科技"。
        deviation_note = ""
        if (
            itype == "UNIT"
            and not is_civilian_item
            and wm is not None
            and wm.victory_type in ("SCIENCE", "CULTURE", "RELIGION")
            and wm.threat_level == "GREEN"
            and wm.military_count >= 4
        ):
            deviation_note = (
                f"\n⚠️ 偏离提醒:已锁定 {wm.victory_type} 胜利,"
                f"当前无威胁(绿色)且已有 {wm.military_count} 个军事单位,"
                f"再造军队会拖慢胜利进度。确认这是刻意为之吗?"
            )

        lua = lq.build_produce_item(city_id, item_type, item_name, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        # If CanStartOperation failed but CanProduce passed, verify via readback
        if any("MAYBE:" in l for l in lines):
            try:
                verify_lines = await self.conn.execute_read(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in l for l in verify_lines):
                    turns = ""
                    for vl in verify_lines:
                        if vl.startswith("CONFIRMED|"):
                            turns = vl.split("|", 1)[1]
                    return (
                        f"PRODUCING|{item_name}|{turns} "
                        f"(bypassed stale CanStartOperation){deviation_note}"
                    )
                else:
                    hint = ""
                    # 2026-08-04 P2:缺战略资源是最常见的 canStart=false
                    # 原因之一(骑士缺铁/骑兵缺马),优先于其他 hint 输出。
                    need_res = next((l for l in lines if "NEED_RESOURCE:" in l), "")
                    if need_res:
                        try:
                            res_part = need_res.split("NEED_RESOURCE:", 1)[1]
                            res_name, amt = res_part.split(":", 1)
                            hint = (
                                f" 缺战略资源 {res_name.replace('RESOURCE_', '')}"
                                f"(库存 {amt})—— 先交易/占领/改善该资源,"
                                f"或改产其他不需要它的单位。"
                            )
                        except Exception:
                            log.debug("NEED_RESOURCE parse failed", exc_info=True)
                    if itype == "DISTRICT":
                        hint = f" Tried ({target_x},{target_y})."
                        try:
                            placements = await self.get_district_advisor(
                                city_id, item_name
                            )
                            if isinstance(placements, list) and placements:
                                alts = ", ".join(
                                    f"({p.x},{p.y}) Adj +{p.total_adjacency}"
                                    for p in placements[:5]
                                )
                                hint += f" Valid tiles: {alts}."
                        except Exception:
                            pass
                        hint += " Use get_district_advisor for details."
                    elif itype == "BUILDING":
                        # Check if Lua reported pillaged districts
                        pillaged_dists = ""
                        for ml in lines:
                            if "PILLAGED:" in ml:
                                pillaged_dists = ml.split("PILLAGED:", 1)[1]
                                break
                        if pillaged_dists:
                            hint = (
                                f" Prerequisite district is pillaged:"
                                f" {pillaged_dists}. Repair it first via"
                                " set_city_production(city_id, 'DISTRICT',"
                                " 'DISTRICT_NAME', x, y) — use get_cities"
                                " to find district coordinates."
                            )
                        else:
                            bld_info = item_name.replace("BUILDING_", "")
                            hint = (
                                f" Hint: {bld_info} may require a completed"
                                " district or prerequisite building."
                            )
                    return f"Error: CANNOT_START|{item_name} cannot start.{hint}"
            except Exception:
                log.debug("Production readback failed", exc_info=True)
                return f"Error: CANNOT_START|{item_name} (readback failed)"

        # OK-path verification. RequestOperation is fire-and-forget; even
        # when CanStartOperation returned true it can silently no-op if the
        # queue is in a degenerate state. Round-trip read to confirm.
        #
        # 缺陷108(2026-08-12 罗马局 T355):这里【只读一次】就下结论,而
        # RequestOperation 是异步的 —— 请求发出后同批读回,引擎可能还没结算,
        # CurrentlyBuilding() 返回 "NONE"。实机:给拉文那设登月计划,连着两次
        # 都报 SILENT_FAILURE|current=NONE,而下一次调用读到的是
        # CurrentlyBuilding=PROJECT_LAUNCH_MOON_LANDING —— 第一次就成功了,
        # 两次"失败"报告全是假的。
        #
        # 这个假失败特别贵,因为它出现在科学胜利的核心操作上:五个太空项目
        # 每个都要设一次,每次都可能报错。而它给的建议("用 purchase_item
        # 强制提交")对项目根本不适用 —— 项目不能购买,照做只会更乱。
        #
        # 修法:NOT_SET 时再跨帧读一次,两次都失败才算真失败。判据不变,
        # 只是不再拿"同帧还没结算"当"引擎拒绝"。
        if result.startswith("PRODUCING|"):
            try:
                not_set = "NOT_SET|unknown"
                for _ in range(2):
                    verify_lines = await self.conn.execute_read(
                        lq.build_verify_production(city_id, item_name)
                    )
                    if any("CONFIRMED" in vl for vl in verify_lines):
                        return result + deviation_note
                    not_set = next(
                        (vl for vl in verify_lines if vl.startswith("NOT_SET|")),
                        "NOT_SET|unknown",
                    )
                return (
                    f"Error: SILENT_FAILURE|{item_name} appeared to set but "
                    f"the game engine did not persist it after two reads "
                    f"({not_set}). Retry the same call; for UNIT/BUILDING you "
                    f"can also purchase_item to force-commit with gold/faith "
                    f"(projects cannot be purchased)."
                )
            except Exception:
                log.debug("OK-path production verify failed", exc_info=True)

        if result.startswith("Error"):
            return result
        return result + deviation_note

    async def purchase_item(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        yield_type: str = "YIELD_GOLD",
        force: bool = False,
    ) -> str:
        if str(item_type).upper() == "UNIT" and not force:
            block = await self._upkeep_gate(str(item_name))
            if block:
                return block
        lua = lq.build_purchase_item(city_id, item_type, item_name, yield_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def _upkeep_gate(self, unit_type: str) -> str:
        """买军事单位前的维护费预算(T362 复盘)。返回非空 = 拦截。

        T361 用信仰一口气买了 5 门加农炮(每门 5 金维护),下一回合维护
        -51/回合直接破产,又解散掉 4 门 —— 2240 信仰打了水漂。买的时候
        爽,代价在下一回合,而"下一回合"没有任何提示。所以做成买之前算。

        规则:买完之后军事维护 > 毛收入 60% → 拦截,force=True 越过。
        取不到收入数据 → 放行(把关模块的故障不该卡住正常购买)。
        """
        try:
            from civ_mcp import war_planner

            add = war_planner.unit_maintenance(unit_type)
            if add <= 0:
                return ""  # 没有维护费的单位(或查不到)——不拦
            ov = getattr(self, "_last_overview", None)
            if ov is None:
                ov_lines = await self.conn.execute_write(lq.build_overview_query())
                ov = lq.parse_overview_response(ov_lines)
                self._last_overview = ov
            income = float(getattr(ov, "gold_income", 0) or 0)
            unit_upkeep = float(getattr(ov, "unit_maintenance", 0) or 0)
            if income <= 0:
                return ""
            after = unit_upkeep + add
            ceiling = war_planner.MILITARY_UPKEEP_CEILING
            if after <= income * ceiling:
                return ""
            gold = float(getattr(ov, "gold", 0) or 0)
            gpt = float(getattr(ov, "gold_per_turn", 0) or 0)
            return (
                f"BLOCKED: UPKEEP_CEILING — 买下这个 {unit_type} 会让军事维护变成 "
                f"{after:.0f}/回合,占毛收入 {income:.0f} 的 {after / income:.0%}"
                f"(红线 {ceiling:.0%})。当前国库 {gold:.0f}、净收支 {gpt:+.0f}/回合。\n"
                f"T362 实证:信仰一次买 5 门加农炮 → 下回合 -51/回合破产 → "
                f"又解散 4 门,2240 信仰白扔。买的时候爽,账单在下一回合。\n"
                f"更划算的选择:① 升级现有单位(弩手→加农炮 230 金,"
                f"比新买 1080 金便宜 4.7 倍);② 先解散过时单位腾出维护额度;"
                f"③ 提高收入(商业中心/贸易路线/卖奢侈)。\n"
                f"确认要买(如决战前最后一波),传 force=True。"
            )
        except Exception:
            log.debug("Upkeep gate failed open", exc_info=True)
            return ""

    async def list_city_production(self, city_id: int) -> list[lq.ProductionOption]:
        lua = lq.build_city_production_query(city_id)
        # Must use InGame context — bq:CanProduce() throws "Not Implemented" in GameCore
        lines = await self.conn.execute_write(lua)
        return lq.parse_city_production_response(lines)

    async def set_research(self, tech_name: str) -> str:
        lua = lq.build_set_research(tech_name)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "RESEARCHING" in result:
            # Verify InGame actually accepted it by comparing tech INDEX.
            # RequestPlayerOperation is fire-and-forget — it can silently no-op
            # while GetResearchingTech() still returns the OLD tech's index (!= -1).
            verify = await self.conn.execute_read(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Technologies() do "
                f'if row.TechnologyType == "{tech_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetTechs():GetResearchingTech(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            matched = verify and verify[0] == "MATCH"
            if not matched:
                # InGame silently failed — fall back to GameCore
                gc_lua = lq.build_set_research_gamecore(tech_name)
                gc_lines = await self.conn.execute_read(gc_lua)
                return _action_result(gc_lines)
        return result

    async def set_civic(self, civic_name: str) -> str:
        lua = lq.build_set_civic(civic_name)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "PROGRESSING" in result:
            # Verify InGame actually accepted it by comparing civic INDEX.
            verify = await self.conn.execute_read(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Civics() do "
                f'if row.CivicType == "{civic_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetCulture():GetProgressingCivic(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            matched = verify and verify[0] == "MATCH"
            if not matched:
                # InGame silently failed — fall back to GameCore
                lua_gc = lq.build_set_civic_gamecore(civic_name)
                gc_lines = await self.conn.execute_read(lua_gc)
                return _action_result(gc_lines)
        return result

    # ------------------------------------------------------------------
    # Diplomacy methods
    # ------------------------------------------------------------------

    async def get_diplomacy_sessions(self) -> list[lq.DiplomacySession]:
        lua = lq.build_diplomacy_session_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_diplomacy_sessions(lines)

    async def diplomacy_respond(self, other_player_id: int, response: str) -> str:
        # Capture dialogue text BEFORE response to detect goodbye phase
        pre_sessions = await self.get_diplomacy_sessions()
        pre_text = ""
        for s in pre_sessions:
            if s.other_player_id == other_player_id:
                pre_text = s.dialogue_text
                break

        # Phase 1: Send AddResponse only (no CloseSession — engine handles lifecycle)
        lua = lq.build_diplomacy_respond(other_player_id, response.upper())
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        # EXIT and error paths return immediately
        if "SESSION_CLOSED" in result or result.startswith("Error"):
            return result

        # Phase 2: Give engine ~9 frames (0.3s at 30fps) to process the
        # response and transition/close the session, then check state in
        # a separate TCP round-trip (same-frame checks see stale state).
        await asyncio.sleep(0.3)
        check_lines = await self.conn.execute_write(
            lq.build_check_diplomacy_session_state(other_player_id)
        )
        if not any("SESSION_OPEN" in l for l in check_lines):
            return f"OK:RESPONDED|{response.upper()}|SESSION_CLOSED"

        # Phase 3: Session still open — check if dialogue text changed.
        # If unchanged, we're in the goodbye phase. Auto-close.
        post_sessions = await self.get_diplomacy_sessions()
        post_text = ""
        for s in post_sessions:
            if s.other_player_id == other_player_id:
                post_text = s.dialogue_text
                break

        if not post_sessions:
            # Session disappeared between checks (race condition)
            return f"OK:RESPONDED|{response.upper()}|SESSION_CLOSED"

        if post_text == pre_text:
            # Dialogue unchanged → goodbye phase. Force close.
            log.info(
                "Goodbye phase detected (text unchanged) for player %d — auto-closing",
                other_player_id,
            )
            close_lua = lq.build_diplomacy_respond(other_player_id, "EXIT")
            await self.conn.execute_write(close_lua)
            return f"OK:RESPONDED|{response.upper()}|SESSION_CLOSED (auto-closed goodbye phase)"

        # Include the new dialogue text so the agent can see what the leader said
        post_reason = ""
        for s in post_sessions:
            if s.other_player_id == other_player_id:
                post_reason = s.reason_text
                break
        dialogue_note = f'\nLeader says: "{post_text}"'
        if post_reason:
            dialogue_note += f'\nReason/agenda: "{post_reason}"'
        return f"OK:RESPONDED|{response.upper()}|SESSION_CONTINUES{dialogue_note}"

    async def send_diplomatic_action(
        self, other_player_id: int, action: str, force: bool = False
    ) -> str:
        if action.upper() == "OPEN_BORDERS":
            # Session-based OPEN_BORDERS causes AI turn hang.
            # Route through the trade deal API instead (mutual open borders).
            return await self.propose_trade(
                other_player_id,
                offer_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
                request_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
            )
        is_war = action.upper().endswith("_WAR") and action.upper().startswith(
            "DECLARE_"
        )
        advisory = ""
        if is_war and not force:
            block, advisory = await self._pre_war_gate(other_player_id)
            if block:
                return block
        lua = lq.build_send_diplo_action(other_player_id, action.upper())
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        if is_war and not result.startswith("ERR:"):
            # War sessions open asynchronously (same-frame FindOpenSessionID
            # returns -1) and responses must be frame-spaced (two AddResponse
            # calls in one execution get swallowed — measured 2026-08-04).
            # Respond twice across frames, then verify+close. A background
            # task is NOT used: under mcp_call.py each tool call is a fresh
            # process, so the old 8s-delayed cleanup never ran and left stale
            # sessions that wedged the AI turn (5x CPU-pegged loops measured).
            await asyncio.sleep(1.0)
            try:
                await self.conn.execute_write(lq.build_war_respond(other_player_id))
                await asyncio.sleep(1.0)
                await self.conn.execute_write(lq.build_war_respond(other_player_id))
                await asyncio.sleep(1.0)
                verify_lines = await self.conn.execute_write(
                    lq.build_war_verify_close(other_player_id)
                )
                verify_result = _action_result(verify_lines)
                if verify_result:
                    result = verify_result
            except Exception as exc:
                log.warning("War confirm failed: %s", exc)

        if advisory and not result.startswith(("ERR", "Error")):
            result = f"{result}\n{advisory}"
        return result

    async def _pre_war_gate(self, other_player_id: int) -> tuple[str, str]:
        """宣战前把关(P2, 2026-08-05 T335 复盘)。

        返回 (拦截消息, 放行附带提醒):拦截消息非空 = 阻止宣战;
        提醒非空 = 放行但把侦察缺口贴在宣战结果后面。

        校准局两次战争都在没有破墙单位时开打:第一次(T107-T123)六个
        弓箭手磨不动开罗,以 77 金买和收场;第二次(T314-T335)弩手为
        主力,21 回合零城池、赔掉 2 火枪手+多个弩手。数据当时都在 ——
        get_diplomacy 看得到 [walls],get_units 看得到自己没攻城单位 ——
        缺的是把两件事接起来的那一步,所以做成宣战前的硬检查。

        规则:目标有【可见的带墙城市】且我方无任何破墙单位
        (war_planner.can_break_walls)→ 拦截,force=True 越过;
        目标一个可见城市都没有 → 放行但提醒先侦察(看不到城谈不上墙,
        但盲宣战正是 T314 目标选错的温床)。
        数据查询失败 → 放行 —— 把关模块自身的故障绝不能卡死正常操作。
        """
        try:
            from civ_mcp import war_planner

            civs = await self.get_diplomacy()
            target = next((c for c in civs if c.player_id == other_player_id), None)
            if target is None:
                return "", ""
            visible = target.visible_cities or []
            if not visible:
                return "", (
                    f"⚠️ 你对 {target.civ_name} 的城市一无所知(0 个可见城市)。"
                    f"用 get_map_area 侦察目标距离与驻军,优先打离你近的"
                    f"薄墙小城 —— 盲选目标是 T314 那场 21 回合烂仗的起点。"
                )
            walled = [vc for vc in visible if vc.has_walls]
            units = await self.get_units()
            utypes = [str(getattr(u, "unit_type", "") or "") for u in units]
            breakers = [t for t in utypes if war_planner.can_break_walls(t)]
            takers = [t for t in utypes if war_planner.can_capture_city(t)]

            # —— 占城单位缺失(T362 复盘,这局的直接死因)——
            # 破墙和占城是两件事:远程/攻城把城打到 0 血之后没有近战去收,
            # 只会夷平或原地僵持。T362 实测 7 加农炮 + 6 弩手 + 只有 2 个
            # 线列步兵 → 打不下任何一座城,窗口耗成维护费黑洞。
            # 这一条比破墙检查更靠前:没有占城单位,破墙再多也拿不到城。
            if visible and not takers:
                return (
                    f"BLOCKED: NO_CITY_TAKERS — 你没有任何能占领城市的单位。"
                    f"只有【陆地近战系】(近战/反骑兵/骑兵)能进城收城;"
                    f"远程、攻城、海军、空军把城打到 0 血只会夷平或僵持。"
                    f"当前军队:{len(breakers)} 个破墙单位、0 个占城单位。"
                    f"T362 实证:7 门加农炮 + 6 个弩手 + 只有 2 个近战 → "
                    f"一座城都没打下来,军力窗口全耗成维护费。"
                    f"先造/买近战单位(数量至少等于你打算拿下的城数),再宣战。"
                    f"确认只为掠夺或围点不占城,传 force=True。"
                ), ""

            if not walled:
                # 目标没有带墙城市 —— 破墙检查不适用,但占城单位够不够仍值得提
                if len(takers) < len(visible):
                    return "", (
                        f"⚠️ 占城单位 {len(takers)} 个,目标可见城市 {len(visible)} 座。"
                        f"一个近战只能收一座城(收完还要留守),打多城要按城数备近战。"
                    )
                return "", ""

            if breakers:
                if len(takers) < len(walled):
                    return "", (
                        f"⚠️ 目标有 {len(walled)} 座带墙城市,你只有 {len(takers)} 个"
                        f"占城单位({len(breakers)} 个破墙单位)。"
                        f"逐城打没问题,但同时开多个战场会两头落空 —— 集火一座,"
                        f"拿下再转下一座。"
                    )
                return "", ""

            names = "、".join(f"{vc.name}(pop{vc.population})" for vc in walled[:4])
            return (
                f"BLOCKED: NO_SIEGE_VS_WALLS — {target.civ_name} 有带墙城市:"
                f"{names}{' 等' if len(walled) > 4 else ''},而你没有任何破墙单位"
                f"(攻城系/海军远程/轰炸机/攻城槌/攻城塔/"
                f"远程强度≥{war_planner.WALL_BREAKER_MIN_RCS} 的远程)。"
                f"上一局这样宣战的结果:21 回合零城池,赔掉 2 火枪手+多个弩手,"
                f"维护费烧穿经济。宣战前:① assess_attack 做战前评估;"
                f"② get_map_area 看目标驻军与距离,优先打离你近的薄墙小城滚雪球,"
                f"不要首攻首都;③ 先造出攻城单位。"
                f"确认现在就要打(如只为掠夺/围点不攻城),传 force=True。"
            ), ""
        except Exception:
            log.debug("Pre-war gate failed open", exc_info=True)
            return "", ""

    async def _cleanup_war_diplomacy(self, other_player_id: int) -> None:
        """Background: dismiss war declaration diplomacy view after animation.

        Two-phase cleanup (must be separate Lua calls — the engine fires
        OnDiplomacySessionClosed asynchronously so the view needs a frame
        to transition from CONVERSATION_MODE to OVERVIEW_MODE):
        1. CloseSession — view transitions to OVERVIEW_MODE
        2. NaturalWonderPopup trick — forces Close() from OVERVIEW_MODE
        """
        await asyncio.sleep(8)
        try:
            # Phase 1: close session → view goes to OVERVIEW_MODE
            lua1 = lq.build_war_close_session(other_player_id)
            await self.conn.execute_write(lua1)

            # Let engine process OnDiplomacySessionClosed
            await asyncio.sleep(1)

            # Phase 2: force-dismiss the OVERVIEW_MODE view
            lua2 = lq.build_war_dismiss_view()
            await self.conn.execute_write(lua2)
        except Exception as e:
            log.warning("War diplomacy cleanup failed: %s", e)

    # ------------------------------------------------------------------
    # Trade deal methods (InGame context)
    # ------------------------------------------------------------------

    async def get_deal_options(self, other_player_id: int) -> lq.DealOptions:
        lua = lq.build_deal_options_query(other_player_id)
        lines = await self.conn.execute_write(lua)
        return lq.parse_deal_options_response(lines)

    async def get_pending_deals(self) -> list[lq.PendingDeal]:
        lua = lq.build_pending_deals_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_pending_deals_response(lines)

    async def respond_to_deal(
        self, other_player_id: int, accept: bool, force: bool = False
    ) -> str:
        # 接受 AI 的联合宣战邀请 = 立即对第三方宣战(2026-08-05 补齐:
        # 宣战门禁三个入口的最后一个)。目标玩家 ID 由 pending-deal 查询的
        # Joint War 项借道 amount 字段带回。数据缺失/查询失败 → 放行。
        if accept and not force:
            try:
                deals = await self.get_pending_deals()
                for d in deals:
                    if d.other_player_id != other_player_id:
                        continue
                    for it in [*d.items_from_them, *d.items_from_us]:
                        if (
                            it.item_type == "AGREEMENT"
                            and it.name.startswith("Joint War")
                            and it.amount > 0
                        ):
                            block, _adv = await self._pre_war_gate(it.amount)
                            if block:
                                return (
                                    f"接受 {d.other_player_name} 的这笔交易会"
                                    f"立即对目标联合宣战 —— 宣战门禁拦截:\n"
                                    f"{block}"
                                )
            except Exception:
                log.debug("Joint-war invite gate failed open", exc_info=True)
        lua = lq.build_respond_to_deal(other_player_id, accept)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def propose_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
        force: bool = False,
    ) -> str:
        # 联合宣战走交易通道,但落地后同样是"你对目标开战" ——
        # 宣战门禁(NO_SIEGE_VS_WALLS)对 joint_war_target 一体适用。
        if not force:
            jw_target = next(
                (
                    int(i.get("target_player") or 0)
                    for i in [*(offer_items or []), *(request_items or [])]
                    if str(i.get("type") or "").upper() == "AGREEMENT"
                    and str(i.get("subtype") or "").upper() == "JOINT_WAR"
                ),
                0,
            )
            if jw_target > 0:
                block, _advisory = await self._pre_war_gate(jw_target)
                if block:
                    return block
        lua = lq.build_propose_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Dismiss diplomacy UI left open by the trade session.
        # After CloseSession, the game transitions DiplomacyActionView to
        # OVERVIEW_MODE (intel screen). Need a brief delay for the C++ UI
        # state machine to settle, then dismiss it in a separate call.
        await asyncio.sleep(0.3)
        try:
            await self.conn.execute_write(
                'pcall(function() ContextPtr:LookUpControl("/InGame/DiplomacyActionView"):SetHide(true) end) '
                "pcall(function() Events.HideLeaderScreen() end) "
                "LuaEvents.DiplomacyActionView_ShowIngameUI() "
                f'print("{lq.SENTINEL}")'
            )
        except Exception:
            pass
        return result

    async def test_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_test_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute_write(lua)
        result = lq.parse_test_trade_response(lines)
        return narrate_test_trade(result)

    async def propose_peace(self, other_player_id: int) -> str:
        lua = lq.build_propose_peace(other_player_id)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("Error") or result.startswith("ERR"):
            return result
        name = result.split("|", 1)[1] if "|" in result else f"player {other_player_id}"
        # War state commits asynchronously — the first check can return stale
        # AT_WAR even after a successful deal (measured 2026-08-04: ACCEPTED
        # reported but IsAtWarWith stayed true). Re-verify across frames, then
        # force-close any leftover session so it can't wedge the AI turn.
        for attempt in range(3):
            await asyncio.sleep(1.5)
            try:
                verify_lines = await self.conn.execute_write(
                    lq.build_check_war_state(other_player_id)
                )
            except Exception:
                verify_lines = []
            if any("AT_PEACE" in l for l in verify_lines):
                await self._dismiss_stale_diplo_ui(other_player_id)
                return f"ACCEPTED|Peace established with {name}"
        await self._dismiss_stale_diplo_ui(other_player_id)
        return f"REJECTED|{name} rejected your peace offer"

    async def _dismiss_stale_diplo_ui(self, other_player_id: int) -> None:
        """Close any leftover session with a player and restore the UI."""
        try:
            await self.conn.execute_write(lq.build_cleanup_stale_sessions())
        except Exception:
            pass

    async def form_alliance(self, other_player_id: int, alliance_type: str) -> str:
        lua = lq.build_form_alliance(other_player_id, alliance_type.upper())
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Policy methods (InGame context)
    # ------------------------------------------------------------------

    async def get_policies(self) -> lq.GovernmentStatus:
        lua = lq.build_policies_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_policies_response(lines)

    async def set_policies(self, assignments: dict[int, str]) -> str:
        # 2026-08-07 P0(苏美尔局 T116 实机):换政体与排卡共用同一个待确认
        # 变更集。build_set_policies 开头的 UNLOCK_POLICIES 会重置该集合,
        # 把【本回合刚请求、尚未结算】的政体变更撤销掉。
        # T116 就是这么丢的:change_government 报成功 → set_policies →
        # 政体回退酋邦,4 张卡也全部落空,两个调用都没报错。
        # 这里做执行前拦截,不是文档提醒 —— 顺序错一次就白丢一个政体窗口。
        pend = getattr(self, "_pending_government", None)
        if pend:
            return (
                f"ERR:GOVERNMENT_PENDING|本回合刚请求换政体({pend}),它还没结算。"
                f"现在排卡会重置待定集、撤销这次政体变更(实机 T116 就是这么丢的)。"
                f"先 end_turn,下回合 get_policies 确认政体已变、槽位已刷新,再排卡 —— "
                f"槽位数和槽位类型都会变,现在排的卡本来也对不上。"
            )
        lua = lq.build_set_policies(assignments)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if not result.startswith("Error"):
            # Post-verify: RequestPolicyChanges can silently no-op (e.g. during era transitions)
            status = await self.get_policies()
            slot_map = {s.slot_index: s.current_policy for s in status.slots}
            mismatches = []
            for idx, pol in assignments.items():
                expected = None if pol.upper() == "NONE" else pol
                actual = slot_map.get(idx)
                if actual != expected:
                    wanted = "EMPTY" if expected is None else pol
                    got = actual or "EMPTY"
                    mismatches.append(f"slot {idx} (wanted {wanted}, got {got})")
            if mismatches:
                result += (
                    f"\nWARN:SILENT_FAILURE — engine rejected: {', '.join(mismatches)}. "
                    "Try a different policy or retry next turn."
                )
        self._invalidate_policy_cache()
        return result

    # ------------------------------------------------------------------
    # Governor methods (InGame context)
    # ------------------------------------------------------------------

    async def get_governors(self) -> lq.GovernorStatus:
        lua = lq.build_governors_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_governors_response(lines)

    async def appoint_governor(self, governor_type: str) -> str:
        lua = lq.build_appoint_governor(governor_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def assign_governor(self, governor_type: str, city_id: int) -> str:
        lua = lq.build_assign_governor(governor_type, city_id)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def promote_governor(self, governor_type: str, promotion_type: str) -> str:
        lua = lq.build_promote_governor(governor_type, promotion_type)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "PROMOTED" in result:
            # Verify promotion actually applied (RequestPlayerOperation is async)
            verify = await self.conn.execute_write(
                f"local me = Game.GetLocalPlayer(); "
                f"local pGovs = Players[me]:GetGovernors(); "
                f'local gov = GameInfo.Governors["{governor_type}"]; '
                f'local promo = GameInfo.GovernorPromotions["{promotion_type}"]; '
                f"if gov and promo then "
                f"  local g = pGovs:GetGovernor(gov.Hash); "
                f"  if g and g:HasPromotion(promo.Index) then "
                f'    print("VERIFIED") '
                f"  else "
                f'    print("ERR:PROMOTION_FAILED|{promotion_type} was not applied") '
                f"  end "
                f"else "
                f'  print("ERR:LOOKUP_FAILED") '
                f"end; "
                f'print("{lq.SENTINEL}")'
            )
            if any("ERR:" in l for l in verify):
                return _action_result(verify)
        return result

    # ------------------------------------------------------------------
    # Promotion methods
    # ------------------------------------------------------------------

    async def get_unit_promotions(self, unit_id: int) -> lq.UnitPromotionStatus:
        # 传完整 ID。取模会丢掉高 16 位的世代号,而槽位号会被新单位复用 ——
        # 详见 lua/_helpers.py 的 _lua_unit_lookup。
        unit_index = unit_id
        lua = lq.build_unit_promotions_query(unit_index)
        lines = await self.conn.execute_read(lua)
        return lq.parse_unit_promotions_response(lines)

    async def promote_unit(self, unit_id: int, promotion_type: str) -> str:
        # 传完整 ID。取模会丢掉高 16 位的世代号,而槽位号会被新单位复用 ——
        # 详见 lua/_helpers.py 的 _lua_unit_lookup。
        unit_index = unit_id
        lua = lq.build_promote_unit(unit_index, promotion_type)
        lines = await self.conn.execute_read(lua)  # GameCore context
        result = _action_result(lines)
        # GameCore SetPromotion doesn't clear the InGame NEEDS_PROMOTION
        # notification, which blocks end_turn until dismissed.
        # Use XP-threshold formula (matching end_turn handler) to decide
        # whether any unit still genuinely needs a promotion before dismissing.
        if not result.startswith("Error"):
            try:
                await self.conn.execute_write(
                    f"local me = Game.GetLocalPlayer(); "
                    f"local anyNeed = false; "
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
                    f"            pcall(function() exp:ChangeStoredPromotions(-stored) end) "
                    f"          end "
                    f"        end "
                    f"      end "
                    f"    end "
                    f"  end "
                    f"  if anyNeed then break end "
                    f"end; "
                    f"if not anyNeed then "
                    f"  local list = NotificationManager.GetList(me); "
                    f"  if list then "
                    f"    for _, nid in ipairs(list) do "
                    f"      local e = NotificationManager.Find(me, nid); "
                    f"      if e and not e:IsDismissed() then "
                    f"        local bt = e:GetEndTurnBlocking(); "
                    f"        if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_UNIT_PROMOTION then "
                    f"          pcall(function() NotificationManager.SendActivated(me, nid) end); "
                    f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                    f"        else "
                    f"          local tn = ''; "
                    f"          pcall(function() tn = e:GetTypeName() end); "
                    f"          if tn == 'NOTIFICATION_UNIT_PROMOTION_AVAILABLE' then "
                    f"            pcall(function() NotificationManager.Dismiss(me, nid) end) "
                    f"          end "
                    f"        end "
                    f"      end "
                    f"    end "
                    f"  end "
                    f"end; "
                    f'print("OK"); print("{lq.SENTINEL}")'
                )
            except Exception:
                pass  # non-fatal — end_turn blocker handler will catch it
        return result

    # ------------------------------------------------------------------
    # City-state / Envoy methods (InGame context)
    # ------------------------------------------------------------------

    async def get_city_states(self) -> lq.EnvoyStatus:
        lua = lq.build_city_states_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_city_states_response(lines)

    async def send_envoy(self, city_state_player_id: int) -> str:
        lua = lq.build_send_envoy(city_state_player_id)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("OK:ENVOY_SENT"):
            # Verify token actually decremented (async race condition workaround)
            await asyncio.sleep(0.1)
            try:
                verify_lines = await self.conn.execute_write(
                    f"local me = Game.GetLocalPlayer(); "
                    f"print(Players[me]:GetInfluence():GetTokensToGive()); "
                    f'print("{lq.SENTINEL}")'
                )
                if verify_lines and verify_lines[0].strip().lstrip("-").isdigit():
                    actual = int(verify_lines[0].strip())
                    result += f" (verified remaining: {actual})"
            except Exception:
                log.debug("Envoy verification failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Pantheon methods (InGame context)
    # ------------------------------------------------------------------

    async def get_pantheon_status(self) -> lq.PantheonStatus:
        lua = lq.build_pantheon_status_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_pantheon_status_response(lines)

    async def choose_pantheon(self, belief_type: str) -> str:
        lua = lq.build_choose_pantheon(belief_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Religion founding methods (InGame context)
    # ------------------------------------------------------------------

    async def get_religion_founding_status(self) -> lq.ReligionFoundingStatus:
        lua = lq.build_religion_beliefs_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_religion_beliefs_response(lines)

    async def found_religion(
        self, religion_type: str, follower_belief: str, founder_belief: str
    ) -> str:
        lua = lq.build_found_religion(religion_type, follower_belief, founder_belief)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Unit upgrade methods (InGame context)
    # ------------------------------------------------------------------

    async def check_unit_upgrade(self, unit_id: int) -> str:
        # 传完整 ID。取模会丢掉高 16 位的世代号,而槽位号会被新单位复用 ——
        # 详见 lua/_helpers.py 的 _lua_unit_lookup。
        unit_index = unit_id
        lua = lq.build_unit_upgrade_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def upgrade_unit(self, unit_id: int) -> str:
        # 传完整 ID。取模会丢掉高 16 位的世代号,而槽位号会被新单位复用 ——
        # 详见 lua/_helpers.py 的 _lua_unit_lookup。
        unit_index = unit_id
        lua = lq.build_upgrade_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Dedications / Commemorations
    # ------------------------------------------------------------------

    async def get_dedications(self) -> lq.DedicationStatus:
        lua = lq.build_dedications_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_dedications_response(lines)

    async def choose_dedication(self, dedication_index: int) -> str:
        lua = lq.build_choose_dedication(dedication_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # District / wonder advisor (with per-turn budget)
    # ------------------------------------------------------------------

    # Pathological loops (Gemini Pro's 1,567 calls in one turn) motivate a
    # per-turn budget. Opus averages 2-4 advisor calls/turn so 20 leaves
    # 5x headroom for legitimate exploration.
    ADVISOR_BUDGET_SOFT = 10
    ADVISOR_BUDGET_HARD = 20

    def _advisor_budget_check(self) -> tuple[str | None, str | None]:
        """Check advisor budget. Returns (hard_error, soft_warning).

        - hard_error: short-circuit string if budget exceeded (caller returns it)
        - soft_warning: string to prepend to the result, or None
        """
        # Increment unconditionally — the hard-cap path stays sticky until
        # the end-of-turn reset, and reporting the true call count is more
        # honest for logs and telemetry.
        self._advisor_calls_this_turn += 1
        n = self._advisor_calls_this_turn
        if n > self.ADVISOR_BUDGET_HARD:
            return (
                f"ERR:ADVISOR_BUDGET_EXCEEDED|You have made {n} advisor calls "
                f"this turn (limit {self.ADVISOR_BUDGET_HARD}). The advisors "
                f"rank placements; they are not for brute-forcing every "
                f"wonder or district. Make a decision with the information "
                f"you already have, skip this step, or end your turn. Budget "
                f"resets next turn.",
                None,
            )
        if n >= self.ADVISOR_BUDGET_SOFT:
            return (
                None,
                f"ADVISOR BUDGET WARNING: {n}/{self.ADVISOR_BUDGET_HARD} "
                f"advisor calls this turn. Consolidate your queries — the "
                f"advisors rank placements, not iterate through options.",
            )
        return None, None

    async def get_district_advisor(
        self, city_id: int, district_type: str
    ) -> list[lq.DistrictPlacement] | str:
        """Returns placements list, or an error string if placement is impossible."""
        hard_err, soft_warn = self._advisor_budget_check()
        if hard_err:
            return hard_err
        lua = lq.build_district_advisor_query(city_id, district_type)
        lines = await self.conn.execute_write(lua)
        # Check for error bail lines (parser only looks for DPLOT| and silently
        # discards errors, losing the actual reason for failure)
        for line in lines:
            if line.startswith("ERR:"):
                return line  # propagate the specific error to the agent
        # Warning only attaches to the success path — error-string returns
        # bypass the server wrapper's narration branch and would otherwise
        # leave a stale warning for the next advisor call.
        self._advisor_budget_warning = soft_warn
        return lq.parse_district_advisor_response(lines)

    async def get_wonder_advisor(
        self, city_id: int, wonder_name: str
    ) -> list[lq.WonderPlacement] | str:
        """Returns placements list, or an error string if budget exceeded."""
        hard_err, soft_warn = self._advisor_budget_check()
        if hard_err:
            return hard_err
        lua = lq.build_wonder_advisor_query(city_id, wonder_name)
        lines = await self.conn.execute_write(lua)
        # 2026-08-08:区域顾问早就在这里透传 ERR: 了,奇观顾问漏了同一段 ——
        # 解析器只认 WPLOT| 行,错误行被静默丢弃,失败原因永远看不到。
        for line in lines:
            if line.startswith("ERR:"):
                return line
        # Warning only attaches to the success path (same reason as above)
        self._advisor_budget_warning = soft_warn
        return lq.parse_wonder_advisor_response(lines)

    # ------------------------------------------------------------------
    # Tile purchase methods (InGame context)
    # ------------------------------------------------------------------

    async def get_purchasable_tiles(self, city_id: int) -> list[lq.PurchasableTile]:
        lua = lq.build_purchasable_tiles_query(city_id)
        lines = await self.conn.execute_write(lua)
        return lq.parse_purchasable_tiles_response(lines)

    async def purchase_tile(self, city_id: int, x: int, y: int) -> str:
        lua = lq.build_purchase_tile(city_id, x, y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Government change (InGame context)
    # ------------------------------------------------------------------

    def _invalidate_policy_cache(self) -> None:
        """政策缓存作废。

        2026-08-07 实机(苏美尔局 T116→T117):end_turn 的政策查询降频
        3 回合一次,注释写着「政策槽只在解锁新市政/换政体时变,而那两件事
        都有专门的通知」—— 但**全仓没有任何一处在那两件事发生时让缓存失效**。
        后果:T116 换成古典共和,T117 简报仍照着旧快照喊「政体还挂着酋邦,
        每回合白扔 2 张卡」。一条已经照做过的建议继续刷屏,比不报更伤 ——
        它会让人开始怀疑整个建议层是不是都在说过时的话。

        缓存的正确性从来不靠注释里的假设,靠写入点主动作废。
        """
        try:
            c = getattr(self, "_expert_cache", None)
            if isinstance(c, dict):
                c.pop("policy_status", None)
                c.pop("policy_turn", None)
        except Exception:
            pass

    async def change_government(self, government_type: str) -> str:
        lua = lq.build_change_government(government_type)
        lines = await self.conn.execute_write(lua)
        self._invalidate_policy_cache()
        result = _action_result(lines)
        # 未结算的政体变更要挂标记,让本回合的 set_policies 拦下来
        # (它会重置待定集并撤销这次变更)。end_turn 负责清标记。
        if "GOVERNMENT_PENDING" in result:
            self._pending_government = government_type
        elif not result.startswith(("Error", "ERR")):
            self._pending_government = None
        return result

    # ------------------------------------------------------------------
    # Great People (InGame context)
    # ------------------------------------------------------------------

    async def get_great_people(self) -> list[lq.GreatPersonInfo]:
        lua = lq.build_great_people_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_great_people_response(lines)

    async def get_gp_advisor(self, unit_index: int) -> lq.GPAdvisorResult | None:
        lua = lq.build_gp_advisor_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return lq.parse_gp_advisor_response(lines)

    async def recruit_great_person(self, individual_id: int) -> str:
        lua = lq.build_recruit_great_person(individual_id)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    async def patronize_great_person(
        self, individual_id: int, yield_type: str = "YIELD_GOLD"
    ) -> str:
        lua = lq.build_patronize_great_person(individual_id, yield_type)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    async def get_religion_status(self) -> lq.ReligionStatus:
        lines = await self.conn.execute_write(lq.build_religion_status_query())
        return lq.parse_religion_status_response(lines)

    async def reject_great_person(self, individual_id: int) -> str:
        lua = lq.build_reject_great_person(individual_id)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    # ------------------------------------------------------------------
    # Trade route methods (InGame context)
    # ------------------------------------------------------------------

    async def get_trade_routes(self) -> lq.TradeRouteStatus:
        lua = lq.build_trade_routes_query()
        lines = await self.conn.execute_write(
            lua
        )  # InGame context (GetOutgoingRoutes is InGame-only)
        return lq.parse_trade_routes_response(lines)

    async def get_trade_destinations(
        self, unit_index: int
    ) -> list[lq.TradeDestination]:
        lua = lq.build_trade_destinations_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return lq.parse_trade_destinations_response(lines)

    async def make_trade_route(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_make_trade_route(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great Person activation (InGame context)
    # ------------------------------------------------------------------

    async def activate_great_person(self, unit_index: int) -> str:
        lua = lq.build_activate_great_person(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def spread_religion(self, unit_index: int) -> str:
        lua = lq.build_spread_religion(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def excavate(self, unit_index: int) -> str:
        """考古学家在脚下遗址开挖(2026-08-09 T304 工具化)。"""
        lua = lq.build_excavate(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def fill_idle_production(self) -> str:
        """给所有空转城市补上生产(2026-08-12 工具化)。

        93 座城时 end_turn 门禁一次点名十几座空转城,而 set_city_production
        是逐城接口 —— 补一轮十几次往返。这个动作没有决策含量(挑一个造得出的
        基础建筑而已),真正需要决策的城本来就不会空转。
        """
        lua = lq.build_fill_idle_production()
        lines = await self.conn.execute_write(lua)
        filled, failed, detail = lq.parse_fill_idle_production(lines)
        if filled == 0 and failed == 0:
            return "IDLE_FILL|没有空转的城市 —— 全部都在生产中。"
        head = f"IDLE_FILL|补上 {filled} 座"
        if failed:
            head += f",{failed} 座无可造项(需手工处理)"
        body = "\n".join("  " + d for d in detail[:40])
        if len(detail) > 40:
            body += f"\n  (另有 {len(detail) - 40} 座未列出)"
        return head + "\n" + body

    async def rebase_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        """空军转场到另一个航空港/航母(2026-08-12 缺陷113 的另一半)。

        射程 15 格之外的目标只能靠转场够到,而这个入口此前不存在 ——
        整局四次转场全靠 run_lua 手发。
        """
        lua = lq.build_rebase(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def has_loyalty_mechanic(self) -> bool | None:
        """本规则集是否有忠诚度机制。每局探测一次并缓存。

        True=有 / False=没有(RULESET_STANDARD) / None=探测不出。
        忠诚度(CulturalIdentity)是 Rise & Fall 引入的;在原版上相关建议
        描述的是不存在的机制 —— 那比没有建议更危险,因为它看起来像情报。
        """
        if self._loyalty_ruleset is not None:
            return self._loyalty_ruleset
        try:
            lines = await self.conn.execute_read(lq.build_loyalty_ruleset_probe())
            self._loyalty_ruleset = lq.parse_loyalty_ruleset_probe(lines)
        except Exception:
            log.debug("忠诚度规则集探测失败", exc_info=True)
            return None
        return self._loyalty_ruleset

    async def form_unit(
        self, unit_index: int, target_x: int, target_y: int, army: bool = False
    ) -> str:
        """把目标格同型单位合编成军团/大军(2026-08-12 罗马 T384 工具化)。"""
        lua = lq.build_form_unit(unit_index, target_x, target_y, army)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Trader teleport (InGame context)
    # ------------------------------------------------------------------

    async def teleport_to_city(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_teleport_to_city(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # World Congress (InGame context)
    # ------------------------------------------------------------------

    async def get_world_congress(self) -> lq.WorldCongressStatus:
        lua = lq.build_world_congress_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_world_congress_response(lines)

    async def vote_world_congress(
        self, resolution_hash: int, option: int, target_index: int, num_votes: int
    ) -> str:
        lua = lq.build_congress_vote(resolution_hash, option, target_index, num_votes)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def submit_congress(self) -> str:
        lua = lq.build_congress_submit()
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def queue_wc_votes(self, votes: list[dict]) -> str:
        """Store agent voting preferences and register WC event handler."""
        lua = lq.build_register_wc_voter(votes=votes)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # City yield focus (InGame context)
    # ------------------------------------------------------------------

    async def set_city_focus(self, city_id: int, focus: str) -> str:
        lua = lq.build_set_yield_focus(city_id, focus)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def get_notifications(self) -> list[lq.GameNotification]:
        lua = lq.build_notifications_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_notifications_response(lines)

    # ------------------------------------------------------------------
    # Snapshot-diff for turn event detection
    # ------------------------------------------------------------------

    async def _take_snapshot(
        self, overview: lq.GameOverview | None = None
    ) -> lq.TurnSnapshot:
        """Capture current game state for diffing."""
        if overview is None:
            ov_lines = await self.conn.execute_write(lq.build_overview_query())
            overview = lq.parse_overview_response(ov_lines)
        # 全量 overview 留一份(审查 F1-8):快照只保留 turn/research 等
        # 摘要字段,而 _check_empire_warnings 随后又整查一遍 —— 同一回合
        # 内直接复用,省一次 ~324ms 往返。
        self._last_overview = overview

        unit_lines = await self.conn.execute_read(lq.build_units_query())
        units = lq.parse_units_response(unit_lines)

        city_lines = await self.conn.execute_write(lq.build_cities_query())
        cities, _ = lq.parse_cities_response(city_lines)

        try:
            stk_lines = await self.conn.execute_write(lq.build_stockpile_query())
            stockpiles = lq.parse_stockpile_response(stk_lines)
        except Exception:
            log.debug("Stockpile query failed", exc_info=True)
            stockpiles = []

        return lq.TurnSnapshot(
            turn=overview.turn,
            units={u.unit_id: u for u in units},
            cities={
                c.city_id: lq.CitySnapshot(
                    city_id=c.city_id,
                    name=c.name,
                    population=c.population,
                    currently_building=c.currently_building,
                    food_surplus=c.food_surplus,
                    turns_to_grow=c.turns_to_grow,
                    loyalty=c.loyalty,
                    loyalty_per_turn=c.loyalty_per_turn,
                    # ⚠️ 不能写 `int(getattr(c, "x", -1) or -1)`:**坐标 0 是
                    # 合法值**(地图边缘),`0 or -1` 会把它变成 -1。而 -1 在
                    # end_turn 的危险判定里表示"这座城没有坐标、不参与豁免"
                    # —— 于是 x=0 的城里的平民会被误报"在敌人打击范围内"。
                    x=_coord(getattr(c, "x", None)),
                    y=_coord(getattr(c, "y", None)),
                )
                for c in cities
            },
            current_research=overview.current_research,
            current_civic=overview.current_civic,
            stockpiles=stockpiles,
        )

    @staticmethod
    def _diff_snapshots(
        before: lq.TurnSnapshot, after: lq.TurnSnapshot
    ) -> list[lq.TurnEvent]:
        """Compare two snapshots and generate events."""
        events: list[lq.TurnEvent] = []

        # --- Unit events ---
        for uid, ub in before.units.items():
            if uid not in after.units:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="unit",
                        message=f"Your {ub.name} ({ub.unit_type}) was killed! Last seen at ({ub.x},{ub.y}).",
                    )
                )
            else:
                ua = after.units[uid]
                dmg = ub.health - ua.health
                if dmg > 0:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="unit",
                            message=f"Your {ua.name} ({ua.unit_type}) took {dmg} damage! HP: {ua.health}/{ua.max_health} at ({ua.x},{ua.y}).",
                        )
                    )
                elif dmg < 0:
                    events.append(
                        lq.TurnEvent(
                            priority=3,
                            category="unit",
                            message=f"Your {ua.name} ({ua.unit_type}) healed {-dmg} HP. HP: {ua.health}/{ua.max_health}.",
                        )
                    )

        for uid, ua in after.units.items():
            if uid not in before.units:
                events.append(
                    lq.TurnEvent(
                        priority=3,
                        category="unit",
                        message=f"New unit: {ua.name} ({ua.unit_type}) at ({ua.x},{ua.y}).",
                    )
                )

        # --- City events ---
        for cid, cb in before.cities.items():
            if cid not in after.cities:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="city",
                        message=f"City {cb.name} was lost!",
                    )
                )
            else:
                ca = after.cities[cid]
                if ca.population > cb.population:
                    events.append(
                        lq.TurnEvent(
                            priority=3,
                            category="city",
                            message=f"{ca.name} grew to population {ca.population}.",
                        )
                    )
                if (
                    cb.currently_building != "NONE"
                    and ca.currently_building != cb.currently_building
                ):
                    now = ca.currently_building
                    if now in ("NONE", "nothing"):
                        now = "nothing"
                    elif now == "CORRUPTED_QUEUE":
                        now = "nothing (queue invalidated — set new production)"
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="city",
                            message=f"{ca.name} finished building {cb.currently_building}. Now: {now}.",
                        )
                    )

        for cid, ca in after.cities.items():
            if cid not in before.cities:
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="city",
                        message=f"New city founded: {ca.name}!",
                    )
                )

        # --- Research/civic events ---
        if (
            before.current_research != "None"
            and after.current_research != before.current_research
        ):
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="research",
                    message=f"Research complete: {before.current_research}! Now: {after.current_research}.",
                )
            )

        if (
            before.current_civic != "None"
            and after.current_civic != before.current_civic
        ):
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="civic",
                    message=f"Civic complete: {before.current_civic}! Now: {after.current_civic}.",
                )
            )

        # --- Stockpile events ---
        before_stk = {s.name: s for s in before.stockpiles}
        after_stk = {s.name: s for s in after.stockpiles}
        for name, sa in after_stk.items():
            sb = before_stk.get(name)
            if sb and sb.amount > 0 and sa.amount == 0:
                net = sa.per_turn - sa.demand + sa.imported
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="resources",
                        message=f"DEPLETED: {name} stockpile hit 0 ({net:+d}/t) — units requiring {name} may be disbanded.",
                    )
                )

        events.sort(key=lambda e: e.priority)
        return events

    @staticmethod
    def _build_turn_report(
        turn_before: int,
        turn_after: int,
        events: list[lq.TurnEvent],
        notifications: list[lq.GameNotification],
        stockpiles: list[lq.ResourceStockpile] | None = None,
        score: int | None = None,
        era: str | None = None,
        world_briefing: str = "",
    ) -> str:
        """Format turn events and notifications into a scannable report."""
        header = f"Turn {turn_before} -> {turn_after}"
        if score is not None:
            header += f" | Score: {score}"
        lines = [header]

        if stockpiles:
            visible = [
                s for s in stockpiles if s.amount > 0 or s.per_turn > 0 or s.demand > 0
            ]
            if visible:
                parts = []
                for s in visible:
                    net = s.per_turn - s.demand + s.imported
                    parts.append(f"{s.name} {s.amount}/{s.cap} ({net:+d}/t)")
                lines.append(f"Resources: {', '.join(parts)}")

        if events:
            lines.append("")
            lines.append("== Events ==")
            icons = {1: "!!!", 2: ">>", 3: "--"}
            for e in events:
                icon = icons.get(e.priority, "--")
                lines.append(f"  {icon} {e.message}")

        # Use the enriched is_action_required field from the parser
        action_required = [n for n in notifications if n.is_action_required]
        # Only show informational notifications from the last 2 turns — older ones
        # are stale (e.g. "Wonder Completed" from 3 turns ago) and clutter the report.
        recent_cutoff = (turn_after or 0) - 2
        info_notifs = [
            n
            for n in notifications
            if not n.is_action_required and n.turn >= recent_cutoff
        ]

        if action_required:
            lines.append("")
            lines.append("== Action Required ==")
            for n in action_required:
                hint = f"  -> Use: {n.resolution_hint}" if n.resolution_hint else ""
                lines.append(f"  * {n.message}{hint}")

        if info_notifs:
            lines.append("")
            lines.append("== Notifications ==")
            for n in info_notifs:
                lines.append(f"  - {n.message}")

        # L1 世界情报。排在规则提醒【之前】:情报是"当下的局面",
        # 规则是"通用的约束",先看局面再看约束更符合决策顺序。
        if world_briefing:
            lines.append("")
            lines.append(world_briefing)

        # L0 硬知识注入。放在【末尾】是刻意的:模型读完这段就要开始决策,
        # 紧邻决策点的信息最不容易被忽略;放开头会被中间的事件列表挤开。
        try:
            briefing = knowledge.select_briefing(
                era, knowledge.flags_from_events(events)
            )
            if briefing:
                lines.append("")
                lines.append(briefing)
        except Exception:
            log.debug("规则简报生成失败,跳过", exc_info=True)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    async def end_turn(self) -> str:
        """End the turn with snapshot-diff event detection."""
        from civ_mcp.end_turn import execute_end_turn

        return await execute_end_turn(self)

    async def dismiss_popup(self) -> str:
        """Dismiss any blocking popup or UI overlay."""
        from civ_mcp.game_lifecycle import dismiss_popup

        return await dismiss_popup(self.conn)

    async def list_saves(self) -> str:
        """List available save files."""
        from civ_mcp.game_lifecycle import list_saves

        return await list_saves(self.conn)

    async def load_save(self, save_index: int) -> str:
        """Load a save file by index."""
        from civ_mcp.game_lifecycle import load_save

        result = await load_save(self.conn, save_index)
        if not result.startswith(("Error", "ERR", "FAILED")):
            self._record_save_load(f"index:{save_index}")
        return result

    async def load_game_save(self, save_name: str) -> str:
        """Load a save file by name (no list_saves prerequisite)."""
        from civ_mcp.game_lifecycle import load_game_save

        result = await load_game_save(self.conn, save_name)
        if not result.startswith(("Error", "ERR", "FAILED")):
            self._record_save_load(save_name)
        return result

    def _record_save_load(self, save_name: str) -> None:
        """Record a successful save load for scumming detection."""
        import time

        ts = time.time()
        turn = self._high_water_turn
        self._save_load_history.append((ts, turn, save_name))
        # Keep bounded
        if len(self._save_load_history) > 50:
            self._save_load_history = self._save_load_history[-50:]
        # 审查 2026-08-05 F1:读档后 in-flight 的 ACTION_ENDTURN 已不存在。
        # 不清这个旗标,下一次 end_turn 会"跳过发送只轮询"——对一个从没
        # 被要求结束回合的游戏白等 ~9.5 分钟,然后误报 HANG 触发杀进程重启。
        # (挂死→AI_TURN_HANG→读档恢复,正是 T335 №6 的标准流程。)
        self.clear_pending_end_turn()

    def clear_pending_end_turn(self) -> None:
        """读档/重启后清除 in-flight end-turn 状态。永不抛异常。"""
        try:
            self._pending_end_turn = False
            self._pending_end_turn_from = None
        except Exception:
            pass

    async def execute_lua(self, code: str, context: str = "gamecore") -> str:
        """Escape hatch: run arbitrary Lua code."""
        from civ_mcp.game_lifecycle import execute_lua

        return await execute_lua(self.conn, code, context)


def _action_result(lines: list[str]) -> str:
    """Parse OK:/ERR: prefixed action responses.

    Scans all lines for the first OK:/ERR: prefix, since LuaEvent
    callbacks (e.g. ShowIngameUI → BulkHide debug prints) can inject
    spurious output before the actual result line.

    WARN: lines are preserved: they are prepended to the result so
    pre-execution warnings (e.g. CITY_WILL_RAZE) reach the agent instead
    of being silently dropped (2026-08-04 P0: 远程打 0 血城市夷平警告
    之前被吞,Agent 在不知情的情况下夷平了亚丁)。
    """
    if not lines:
        return "Action completed (no response)."
    warns = [line[5:] for line in lines if line.startswith("WARN:")]
    for line in lines:
        if line.startswith("OK:"):
            body = line[3:]
            return ("⚠️ " + " ⚠️ ".join(warns) + "\n" + body) if warns else body
        if line.startswith("ERR:"):
            body = f"Error: {line[4:]}"
            return ("⚠️ " + " ⚠️ ".join(warns) + "\n" + body) if warns else body
    # No OK/ERR found — return all lines for debugging
    return "\n".join(lines)


def _format_attack_followup(lines: list[str], attacker_owner: int = 0) -> str:
    """Format the GameCore follow-up read after an attack.

    Filters out units belonging to ``attacker_owner`` so that after a melee
    kill (where the attacker moves onto the target tile) we don't misreport
    our own unit's HP as the defender's.

    Also includes city wall/garrison HP when attacking a walled city.
    """
    parts = []
    for line in lines:
        if line.startswith("UNIT|"):
            fields = line.split("|")
            if len(fields) >= 4:
                # fields: UNIT|TYPE|hp/max|owner:N
                owner_str = fields[3]  # "owner:N"
                try:
                    owner_id = int(owner_str.split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                label = "(yours) " if owner_id == attacker_owner else ""
                parts.append(f"{label}{fields[1]} {fields[2]}")
            elif len(fields) >= 3:
                parts.append(f"{fields[1]} {fields[2]}")
    city_def = _extract_city_defense(lines)
    if city_def:
        wall_hp, wall_max, gar_hp, gar_max = city_def
        if wall_max > 0:
            parts.append(f"Walls {wall_hp}/{wall_max}")
        if gar_max > 0:
            parts.append(f"City garrison {gar_hp}/{gar_max}")
    if not parts:
        return "Target eliminated"
    return ", ".join(parts)


def _extract_pre_hp(result: str) -> int | None:
    """Extract pre-attack enemy HP from attack result line."""
    import re

    # Ranged/city: pre_hp:80/100
    m = re.search(r"pre_hp:(\d+)/", result)
    if m:
        return int(m.group(1))
    # Melee: enemy HP:100 -> 80/100
    m = re.search(r"enemy HP:(\d+) ->", result)
    if m:
        return int(m.group(1))
    return None


# 攻击战后状态的轮询参数(2026-08-11 T229)。战斗在引擎里异步结算,
# 单次固定延迟读到的往往还是战前值。上限 5 × 0.4s = 2 秒:够慢到等出真值,
# 又不至于让一回合十几次攻击变成难以忍受的等待。
ATTACK_FOLLOWUP_TRIES = 5
ATTACK_FOLLOWUP_DELAY = 0.4


def _extract_reported_wall_hp(result: str) -> int | None:
    """从战报里取 Lua 自报的城墙值(城攻重读的比较基线)。"""
    m = re.search(r"city walls: (\d+)/\d+", result)
    if m:
        return int(m.group(1))
    m = re.search(r"城墙 (\d+)/\d+", result)
    return int(m.group(1)) if m else None


def _extract_post_hp(followup_lines: list[str], attacker_owner: int = 0) -> int | None:
    """Extract post-combat *enemy* HP from followup query lines.

    Followup format: UNIT|UNIT_TYPE|hp/max|owner:N
    Skips units belonging to ``attacker_owner`` (after melee kill, attacker
    occupies the target tile and would otherwise be misread as defender).
    Returns HP of first enemy unit found (None if eliminated).
    """
    for line in followup_lines:
        if line.startswith("UNIT|"):
            parts = line.split("|")
            if len(parts) >= 4:
                try:
                    owner_id = int(parts[3].split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                if owner_id == attacker_owner:
                    continue  # our unit, not the target
            if len(parts) >= 3:
                hp_part = parts[2].split("/")[0]
                try:
                    return int(hp_part)
                except ValueError:
                    pass
    return None


def _extract_city_defense(
    followup_lines: list[str],
) -> tuple[int, int, int, int] | None:
    """Extract wall and garrison HP from CITY_DEF followup line.

    Returns ``(wall_hp, wall_max, garrison_hp, garrison_max)`` or *None*
    when the target tile has no city defenses.
    """
    for line in followup_lines:
        if line.startswith("CITY_DEF|"):
            # CITY_DEF|wall:74/100|garrison:197/200
            wall_hp = wall_max = gar_hp = gar_max = 0
            for part in line.split("|")[1:]:
                if part.startswith("wall:"):
                    hp, mx = part[5:].split("/")
                    wall_hp, wall_max = int(hp), int(mx)
                elif part.startswith("garrison:"):
                    hp, mx = part[9:].split("/")
                    gar_hp, gar_max = int(hp), int(mx)
            return (wall_hp, wall_max, gar_hp, gar_max)
    return None
