"""任务 G —— 外交博弈(联合宣战 / 世界议会投票)。

opponents.py 做的是"看懂对手"(议程画像、军力排名、行为日志);这里做的是
"用外交改变局面":花钱让 A 去打 B、在议会里把票投给能赢的一方。

═══════════════════════════════════════════════════════════════════════════
⚠️ 先说第二个函数的硬事实:**世界议会在本规则集里不存在。**

    实测 `docs/gamedata/_all.json`(RULESET_STANDARD)全表搜
    `WORLD_CONGRESS` / `VICTORY_DIPLOMATIC` —— 零命中。搜 `FAVOR` 只命中两条
    议程参数(TRAIT_BEFRIEND_MINOR_CIV_HOME_CONTINENT 的 `Favored` 字段),
    与外交favor无关。世界议会、外交favor、外交胜利都是 Gathering Storm 内容。

    ⇒ `get_wc_voting_advice` 因此是**数据自门控**的:
      `WORLD_CONGRESS_AVAILABLE` 从 Victories 表算出来,本规则集为 False。
      没有议会数据传进来时一律返回 [],一个字都不输出。
      但函数体按真实规则写完整了 —— 换 GS 规则集重导 gamedata 后自动生效,
      算术部分不用改一行。这和 lategame.track_dvp_progress 是同一套处理。

    第一个函数 `get_joint_war_advice` 不受影响:联合宣战靠的是普通外交交易
    (propose_trade),原版就有。
═══════════════════════════════════════════════════════════════════════════

数据来源:
    Victories.json   判定本规则集有没有外交胜利/议会体系
    议程画像         复用 opponents.AGENDA_PROFILES(43 条),不重复定义
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from functools import lru_cache
from pathlib import Path

from civ_mcp.opponents import AGENDA_PROFILES

_GAMEDATA = gamedata_path()

# 两个邻居同时构成威胁时才谈"买通一个打另一个"
JOINT_WAR_MIN_THREATS = 2
# 军力比低于这个数的邻居打不动目标,买通了也是送
JOINT_WAR_MIN_RATIO = 0.7


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@lru_cache(maxsize=1)
def _victory_types() -> frozenset[str]:
    return frozenset(
        r["VictoryType"] for r in _rows("Victories") if r.get("VictoryType")
    )


WORLD_CONGRESS_AVAILABLE: bool = "VICTORY_DIPLOMATIC" in _victory_types()


def wc_availability_note() -> str:
    """给集成方看的一句话:本规则集到底有没有世界议会。"""
    if WORLD_CONGRESS_AVAILABLE:
        return "本规则集支持外交胜利,世界议会投票建议有效。"
    return (
        "本规则集(Victories 表无 VICTORY_DIPLOMATIC)没有世界议会/外交favor/"
        "外交胜利 —— get_wc_voting_advice 会保持静默。"
    )


# ---------------------------------------------------------------------------
# 联合宣战
# ---------------------------------------------------------------------------


def _agenda_profile(agendas) -> tuple[str, str]:
    """取第一个能识别的议程 → (它看重什么, 战争倾向)。识别不了返回空。"""
    for a in agendas or ():
        name = a.get("name") if isinstance(a, dict) else a
        prof = AGENDA_PROFILES.get(str(name or ""))
        if prof:
            return prof.cares, prof.war_risk
    return "", ""


def get_joint_war_advice(
    neighbors: list[dict], game_state: dict | None = None, max_items: int = 2
) -> list[str]:
    """联合宣战建议:被两面夹击时,花钱让其中一个去打另一个。

    neighbors: [{"civ_name","military_strength","war_risk"(高/中/低),
                 "is_at_war"(与你),"agendas":[...]}]
    game_state: {"my_military", "gold", "gold_per_turn"}
    没有可操作的联合宣战机会时返回空列表,不抛异常。
    """
    try:
        return _joint_inner(
            [n for n in (neighbors or []) if isinstance(n, dict)],
            game_state if isinstance(game_state, dict) else {},
            max_items,
        )
    except Exception:
        return []


def _joint_inner(neighbors: list[dict], gs: dict, max_items: int) -> list[str]:
    def _mil(n) -> int:
        try:
            return int(n.get("military_strength") or 0)
        except (TypeError, ValueError):
            return 0

    threats = [
        n
        for n in neighbors
        if str(n.get("war_risk") or "") == "高" or n.get("is_at_war")
    ]
    if len(threats) < JOINT_WAR_MIN_THREATS:
        return []

    try:
        my_mil = int(gs.get("my_military") or 0)
    except (TypeError, ValueError):
        my_mil = 0
    gold = gs.get("gold")
    gpt = gs.get("gold_per_turn")

    # 目标 = 威胁里最强的那个;打手 = 其余威胁里打得动它的
    threats.sort(key=_mil, reverse=True)
    target = threats[0]
    t_name = str(target.get("civ_name") or "最强威胁")
    t_mil = _mil(target)

    out: list[str] = ["== 外交博弈:联合宣战 =="]
    out.append(
        f"你正被 {len(threats)} 个高风险邻居夹击"
        f"({('、'.join(str(n.get('civ_name') or '?') for n in threats[:3]))})"
        f"——同时对付两条战线是最常见的翻车方式。"
    )

    hired = 0
    for cand in threats[1:]:
        c_name = str(cand.get("civ_name") or "?")
        c_mil = _mil(cand)
        if t_mil > 0 and c_mil < t_mil * JOINT_WAR_MIN_RATIO:
            out.append(
                f"· {c_name}(军力 {c_mil})打不动 {t_name}(军力 {t_mil})"
                f"——买通了也是送人头,不值这笔钱。"
            )
            continue
        cares, risk = _agenda_profile(cand.get("agendas"))
        line = (
            f"· 买通 {c_name}(军力 {c_mil})去打 {t_name}(军力 {t_mil}):"
            f"他的兵力会被牵制在另一条战线,你正面的压力立刻减半。"
        )
        if cares:
            line += f" 他看重{cares} —— 出价时优先给这个,比单纯给金币便宜。"
        if risk == "高":
            line += " 他本身就是好战型,推一把就动,成本更低。"
        out.append(line)
        hired += 1
        if hired >= max_items:
            break

    if hired:
        budget = ""
        if gold is not None:
            budget = f"当前国库 {gold} 金"
            if gpt is not None:
                budget += f"、{gpt} 金/回合"
            budget += " —— "
        out.append(
            f'   {budget}用 propose_trade(mode="test") 先测他的接受线,'
            f"再正式提。价格不可预测(受关系/议程/局势影响),别凭感觉出价。"
        )
    if my_mil and t_mil and t_mil >= my_mil * 1.5:
        out.append(
            f"   注意:{t_name} 的军力是你的 {t_mil / my_mil:.1f} 倍 —— "
            f"就算成功转移他的注意力,也别趁机去啃他,守住发育窗口就是赚。"
        )
    return out if len(out) > 2 else []


# ---------------------------------------------------------------------------
# 世界议会投票(数据自门控:本规则集不存在)
# ---------------------------------------------------------------------------


def get_wc_voting_advice(
    resolutions: list[dict],
    my_favor: int = 0,
    victory_type: str = "",
    max_items: int = 3,
) -> list[str]:
    """世界议会投票建议。

    resolutions: [{"name","effect_a","effect_b","targets_me"(bool),
                   "dvp_reward"(int),"dvp_penalty"(int),
                   "predicted_winner"("A"/"B"/""),"my_preference"("A"/"B"/"")}]
    本规则集没有世界议会且没有议会数据传入时返回空列表,不抛异常。
    """
    try:
        rows = [r for r in (resolutions or []) if isinstance(r, dict)]
        if not WORLD_CONGRESS_AVAILABLE and not rows:
            return []
        if not rows:
            return []
        return _wc_inner(
            rows, int(my_favor or 0), str(victory_type or "").upper(), max_items
        )
    except Exception:
        return []


def _wc_inner(rows: list[dict], favor: int, vt: str, max_items: int) -> list[str]:
    out = [f"== 世界议会(外交点 {favor}) =="]
    scored: list[tuple[int, str]] = []

    for r in rows:
        name = str(r.get("name") or "决议")
        try:
            reward = int(r.get("dvp_reward") or 0)
            penalty = int(r.get("dvp_penalty") or 0)
        except (TypeError, ValueError):
            reward = penalty = 0
        winner = str(r.get("predicted_winner") or "")
        mine = str(r.get("my_preference") or "")

        # ① 会扣我 DVP 的决议:最高优先级,压票比拿新分更划算
        if r.get("targets_me") or penalty:
            scored.append(
                (
                    0,
                    f"🔴 「{name}」会扣你 {penalty or '若干'} DVP —— "
                    f"倒退比原地不动更糟。外交点优先砸在压掉这条上,"
                    f"而不是去抢别的决议。",
                )
            )
            continue

        # ② 能拿 DVP 的决议
        if reward:
            if winner and mine and winner != mine:
                scored.append(
                    (
                        1,
                        f"🟡 「{name}」赢了给 {reward} DVP,但票数预测是 {winner} 方胜 —— "
                        f"逆风翻盘要砸重金,先算这 {reward} DVP 值不值这笔外交点。",
                    )
                )
            else:
                scored.append(
                    (
                        1,
                        f"💡 「{name}」赢了给 {reward} DVP,而且票势不逆 —— "
                        f"这是性价比最高的一票,优先投。",
                    )
                )
            continue

        # ③ 无 DVP 的普通决议:按"投能赢的那一方赚外交资本"处理
        if winner and mine and winner == mine:
            scored.append(
                (
                    2,
                    f"· 「{name}」:你的倾向和预测赢家一致({winner})—— "
                    f"顺水推舟,花小钱站队赚外交资本。",
                )
            )
        elif winner and mine:
            scored.append(
                (
                    3,
                    f"· 「{name}」:你倾向 {mine} 但预测 {winner} 方胜 —— "
                    f"逆势投票是纯亏,除非这条决议直接打击你的 {vt or '胜利'} 路线。",
                )
            )
        else:
            scored.append((3, f"· 「{name}」:票数分布未知,先看别人怎么投再跟。"))

    if len(out) == 1 and not scored:
        return []
    scored.sort(key=lambda p: p[0])
    out.extend(t for _s, t in scored[:max_items])
    if len(scored) > max_items:
        out.append(f"   (另有 {len(scored) - max_items} 条决议未列出)")
    out.append("   投票原则:投能赢的一方赚资本,只在决议直接伤害你时才逆势砸点。")
    return out
