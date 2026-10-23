"""任务 B —— 科技/市政路径规划。

文明 6 的科技/市政树是 **AND 前置**的有向无环图:研究一个节点必须先研究
它的**全部**前置。所以"到目标的最短路径"不是 Dijkstra 选边——是**前置闭包**:
目标的所有未研究祖先 + 目标自身,按拓扑序排列,代价 = Σ(Cost × 折扣)。
(计划书写"标准 Dijkstra",实现时按 AND 语义修正为闭包计算——对单一目标
不存在"选路";真正的选择发生在**比较不同目标**时,本模块因此输出多候选。)

数据来源(全部动态加载,零硬编码):
    Technologies/TechnologyPrereqs   68 节点 / 90 边
    Civics/CivicPrereqs              51 节点 / 69 边
    Units/Districts/Buildings        解锁计数(PrereqTech/PrereqCivic 反查)
    Projects                         太空竞赛链(MaxPlayerInstances=1 且有
                                     PrereqDistrict 的项目 = 5 个发射项目)
    District_Adjacencies+Adjacency_YieldChanges
                                     "哪个区域产哪种产出"→ 各胜利路线的
                                     关键区域科技/市政,不写死区域名
    Governments                      下一个政体的市政目标
    Eras                             时代序(限制统治路线的候选窗口)

⚠️ 回合估算基于**标准速度**的裸 Cost。快速/马拉松等其他速度有全局修正
系数,该系数不在导出表里——其他速度下回合数按比例失真,趋势判断仍有效。

尤里卡折扣由 boost_planner 提供(路径上的科技若有已触发/可达的 boost,
其权重 ×0.6;精确比例未导出,见 boost_planner 的说明)。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
import math
from functools import lru_cache
from pathlib import Path

_GAMEDATA = gamedata_path()

# 两条候选路线回合数相差 <10% 时,两条都列出让模型选
NEAR_TIE_RATIO = 0.10
# 统治路线只考虑当前时代 +2 以内解锁的单位——再远的不构成"下一步"
DOMINATION_ERA_WINDOW = 2


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 图构建(科技树与市政树各自独立,绝不互接)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _tech_nodes() -> dict[str, dict]:
    out = {}
    for r in _rows("Technologies"):
        key = r.get("TechnologyType")
        if not key:
            continue
        try:
            cost = int(r.get("Cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        out[key] = {
            "name": r.get("Name") or key,
            "cost": cost,
            "era": r.get("EraType") or "",
        }
    return out


@lru_cache(maxsize=1)
def _civic_nodes() -> dict[str, dict]:
    out = {}
    for r in _rows("Civics"):
        key = r.get("CivicType")
        if not key:
            continue
        try:
            cost = int(r.get("Cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        out[key] = {
            "name": r.get("Name") or key,
            "cost": cost,
            "era": r.get("EraType") or "",
        }
    return out


@lru_cache(maxsize=1)
def _tech_prereqs() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for r in _rows("TechnologyPrereqs"):
        node, pre = r.get("Technology"), r.get("PrereqTech")
        if node and pre:
            out.setdefault(node, []).append(pre)
    return {k: tuple(sorted(v)) for k, v in out.items()}


@lru_cache(maxsize=1)
def _civic_prereqs() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for r in _rows("CivicPrereqs"):
        node, pre = r.get("Civic"), r.get("PrereqCivic")
        if node and pre:
            out.setdefault(node, []).append(pre)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def _graph(kind: str) -> tuple[dict[str, dict], dict[str, tuple[str, ...]]]:
    if kind == "civic":
        return _civic_nodes(), _civic_prereqs()
    return _tech_nodes(), _tech_prereqs()


def node_cn(key: str) -> str:
    info = _tech_nodes().get(key) or _civic_nodes().get(key)
    return info["name"] if info else key


def node_cost(key: str) -> int:
    info = _tech_nodes().get(key) or _civic_nodes().get(key)
    return info["cost"] if info else 0


# ---------------------------------------------------------------------------
# 前置闭包(AND 语义下的"最短路径")
# ---------------------------------------------------------------------------


def prereq_closure(target: str, have: set[str], kind: str = "tech") -> list[str]:
    """到达 target 需要研究的全部节点(含 target),拓扑序(前置在前)。

    have 里已有的不出现。target 未知或已研究 → 空列表。同层节点按
    (Cost, 名称) 排序,输出确定可复现。不抛异常。
    """
    nodes, prereqs = _graph(kind)
    if target not in nodes or target in have:
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(n: str) -> None:
        if n in have or n in seen or n not in nodes:
            return
        if n in visiting:  # 数据环(不该有,防御一下)
            return
        visiting.add(n)
        for pre in sorted(prereqs.get(n, ()), key=lambda p: (node_cost(p), p)):
            visit(pre)
        visiting.discard(n)
        seen.add(n)
        ordered.append(n)

    visit(target)
    return ordered


def path_cost(path: list[str], discounts: dict[str, float] | None = None) -> float:
    """闭包总代价。discounts: 节点 → 权重系数(尤里卡 0.6),缺省 1.0。"""
    d = discounts or {}
    return sum(node_cost(n) * d.get(n, 1.0) for n in path)


def estimate_turns(cost: float, per_turn: float) -> int:
    """标准速度下的回合估算。产出未知(≤0)返回 0 表示无法估算。"""
    if per_turn <= 0 or cost <= 0:
        return 0
    return max(1, math.ceil(cost / per_turn))


# ---------------------------------------------------------------------------
# 解锁反查("最有用路径"的依据)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _unlocks_by_prereq() -> dict[str, list[str]]:
    """PrereqTech/PrereqCivic → 它解锁的东西(中文名)。"""
    out: dict[str, list[str]] = {}
    for table, key_col in (
        ("Units", "UnitType"),
        ("Districts", "DistrictType"),
        ("Buildings", "BuildingType"),
        ("Governments", "GovernmentType"),
    ):
        for r in _rows(table):
            name = r.get("Name") or r.get(key_col, "")
            for col in ("PrereqTech", "PrereqCivic"):
                pre = r.get(col)
                if pre:
                    out.setdefault(pre, []).append(name)
    return out


def unlock_count(node: str) -> int:
    return len(_unlocks_by_prereq().get(node, []))


def unlock_summary(node: str, limit: int = 3) -> str:
    names = _unlocks_by_prereq().get(node, [])
    if not names:
        return ""
    head = "、".join(names[:limit])
    more = f" 等 {len(names)} 项" if len(names) > limit else ""
    return head + more


# ---------------------------------------------------------------------------
# 胜利路线 → 目标节点(全部从数据反查,不写死名字)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _district_yield_map() -> dict[str, set[str]]:
    """DistrictType → 它的相邻加成产出集合。"""
    ay = {r.get("ID"): r.get("YieldType") for r in _rows("Adjacency_YieldChanges")}
    out: dict[str, set[str]] = {}
    for r in _rows("District_Adjacencies"):
        y = ay.get(r.get("YieldChangeId"))
        d = r.get("DistrictType")
        if d and y:
            out.setdefault(d, set()).add(y)
    return out


def _district_prereq_for_yield(yield_type: str, kind: str) -> str:
    """产出类型 → 产它的区域的前置科技/市政(取解锁最早=Cost最低的那个)。"""
    col = "PrereqCivic" if kind == "civic" else "PrereqTech"
    districts = {r.get("DistrictType"): r for r in _rows("Districts")}
    best = ""
    best_cost = 10**9
    for dtype, yields in _district_yield_map().items():
        if yield_type not in yields:
            continue
        pre = (districts.get(dtype) or {}).get(col) or ""
        if pre and node_cost(pre) < best_cost:
            best, best_cost = pre, node_cost(pre)
    return best


@lru_cache(maxsize=1)
def _space_chain_techs() -> list[str]:
    """太空竞赛项目的前置科技,按 Cost 升序(早→晚)。
    过滤器:MaxPlayerInstances=1 且有 PrereqDistrict 的项目 = 5 个发射项目。"""
    techs = {
        r.get("PrereqTech")
        for r in _rows("Projects")
        if r.get("MaxPlayerInstances") == "1"
        and r.get("PrereqDistrict")
        and r.get("PrereqTech")
    }
    return sorted(techs, key=node_cost)


@lru_cache(maxsize=1)
def _era_rank() -> dict[str, int]:
    out = {}
    for r in _rows("Eras"):
        try:
            out[r.get("EraType", "")] = int(r.get("ChronologyIndex") or 0)
        except (TypeError, ValueError):
            continue
    return out


def _current_era_idx(current: set[str], kind: str = "tech") -> int:
    nodes, _ = _graph(kind)
    ranks = _era_rank()
    idx = 1
    for n in current:
        info = nodes.get(n)
        if info:
            idx = max(idx, ranks.get(info["era"], 1))
    return idx


@lru_cache(maxsize=1)
def _unique_units() -> frozenset[str]:
    """文明独有单位(UnitReplaces 的替换方)——不知道玩家文明时,
    统治候选一律排除它们:推荐一个造不出来的单位比不推荐更糟。"""
    return frozenset(
        r.get("CivUniqueUnitType", "")
        for r in _rows("UnitReplaces")
        if r.get("CivUniqueUnitType")
    )


def _domination_target_techs(
    current: set[str], my_uu: dict[str, str] | None = None
) -> list[tuple[str, str, int]]:
    """(前置科技, 单位中文名, 战力),按战力降序,限当前时代+2 内,去重科技。

    my_uu:本文明 UU → 被替换基础单位(来自世界扫描的引擎数据)。
    2026-08-02 校准局 0c:此前一律排除 UU,罗马进入古典时代、炼铁术刚
    完成的当回合,主线仍推荐 367 回合外的火枪手,只字未提当回合可产的
    军团。有 my_uu 时:本文明 UU 纳入候选,被它替换的基础单位剔除
    (造不出来);其他文明的 UU 照旧排除。
    """
    era_cap = _current_era_idx(current) + DOMINATION_ERA_WINDOW
    ranks = _era_rank()
    nodes = _tech_nodes()
    seen: set[str] = set()
    out: list[tuple[str, str, int]] = []
    my_uu = my_uu or {}
    replaced_bases = set(my_uu.values())
    units = sorted(
        _rows("Units"),
        key=lambda r: -int(r.get("Combat") or 0),
    )
    # 本文明 UU 保证入榜(占第一个席位):UU 是限时强度尖峰,文明就是
    # 冲它选的。纯按战力排,军团(40)永远输给两个时代外的骑士(50)——
    # T46 实机:炼铁术完成当回合,主线仍推 367 回合外的火枪手。
    if my_uu:
        for r in units:
            ut = r.get("UnitType")
            if ut not in my_uu:
                continue
            pre = r.get("PrereqTech")
            if not pre or pre in current:
                continue
            info = nodes.get(pre)
            if not info or ranks.get(info["era"], 99) > era_cap:
                continue
            try:
                combat = int(r.get("Combat") or 0)
            except (TypeError, ValueError):
                continue
            if combat > 0:
                seen.add(pre)
                out.append((pre, r.get("Name") or ut, combat))
                break
    for r in units:
        pre = r.get("PrereqTech")
        if not pre or pre in current or pre in seen:
            continue
        ut = r.get("UnitType")
        if ut in replaced_bases:
            continue  # 被本文明 UU 替换的基础单位,造不出来
        if ut in _unique_units():
            continue  # UU 已在上面的保证席位处理过
        info = nodes.get(pre)
        if not info or ranks.get(info["era"], 99) > era_cap:
            continue
        try:
            combat = int(r.get("Combat") or 0)
        except (TypeError, ValueError):
            continue
        if combat <= 0:
            continue
        seen.add(pre)
        out.append((pre, r.get("Name") or r.get("UnitType", ""), combat))
        if len(out) >= 2:
            break
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _render_target(
    label: str,
    target: str,
    current: set[str],
    per_turn: float,
    kind: str,
    discounts: dict[str, float],
) -> tuple[list[str], int]:
    """一个目标的闭包摘要 → (行, 估算回合)。目标已研究/未知 → ([], 0)。"""
    path = prereq_closure(target, current, kind)
    if not path:
        return [], 0
    eff = path_cost(path, discounts)
    raw = path_cost(path)
    turns = estimate_turns(eff, per_turn)
    steps = []
    for n in path[:4]:
        step = node_cn(n)
        t = estimate_turns(node_cost(n) * discounts.get(n, 1.0), per_turn)
        if t:
            step += f"({t}t)"
        if discounts.get(n, 1.0) < 1.0:
            step += "⚡"
        steps.append(step)
    chain = "→".join(steps)
    if len(path) > 4:
        chain += f"→…共 {len(path)} 步"
    line = f"· {label}:{chain}"
    tail_bits = []
    if turns:
        tail_bits.append(f"约 {turns} 回合")
    if eff < raw:
        tail_bits.append(f"尤里卡已计入,省 {raw - eff:.0f} 锤")
    if tail_bits:
        line += f" —— {';'.join(tail_bits)}"
    unlock = unlock_summary(target, 2)
    if unlock:
        line += f"。到手解锁:{unlock}"
    return [line], turns


def _discounts(
    kind: str,
    current: set[str],
    completed_boosts: set[str] | None,
    game_state: dict | None,
) -> dict[str, float]:
    """尤里卡折扣表。boost_planner 不可用/无数据时返回空表(全额权重)。"""
    try:
        from civ_mcp import boost_planner

        return boost_planner.boost_discounts(
            kind, current, completed_boosts or set(), game_state or {}
        )
    except Exception:
        return {}


def _advice(
    kind: str,
    current: set[str],
    per_turn: float,
    victory_type: str,
    game_state: dict | None,
    completed_boosts: set[str] | None,
) -> list[str]:
    nodes, _ = _graph(kind)
    if not nodes:
        return []
    current = {c for c in (current or set()) if c}
    vt = (victory_type or "").upper()
    discounts = _discounts(kind, current, completed_boosts, game_state)

    header = "== 科技路径 ==" if kind == "tech" else "== 市政路径 =="
    out: list[str] = [header]
    rendered: list[tuple[list[str], int]] = []
    extras: list[str] = []  # 排在 rendered 之后的补充行

    if kind == "tech":
        if vt == "SCIENCE":
            campus = _district_prereq_for_yield("YIELD_SCIENCE", "tech")
            if campus and campus not in current:
                rendered.append(
                    _render_target(
                        "科研区域(先立起来)", campus, current, per_turn, kind, discounts
                    )
                )
            space = [t for t in _space_chain_techs() if t not in current]
            if space:
                path = prereq_closure(space[0], current, kind)
                if len(path) > 12:
                    # 太空链尚远:全量展示是噪音,一行摘要即可
                    extras.append(
                        f"· 太空链锚点:{node_cn(space[0])},还隔 {len(path)} 个科技"
                        f"——远期方向,当下按科研区域推进。"
                    )
                else:
                    rendered.append(
                        _render_target(
                            "太空链下一关键",
                            space[0],
                            current,
                            per_turn,
                            kind,
                            discounts,
                        )
                    )
        elif vt == "DOMINATION":
            my_uu = (game_state or {}).get("my_unique_units") or {}
            for pre, unit_name, combat in _domination_target_techs(current, my_uu):
                rendered.append(
                    _render_target(
                        f"军事升级({unit_name} 战力{combat})",
                        pre,
                        current,
                        per_turn,
                        kind,
                        discounts,
                    )
                )
        elif vt == "RELIGION":
            holy = _district_prereq_for_yield("YIELD_FAITH", "tech")
            if holy and holy not in current:
                rendered.append(
                    _render_target("信仰区域", holy, current, per_turn, kind, discounts)
                )
        if not rendered:
            # 通用:可立即研究(前置齐)且解锁最多的前沿科技
            frontier = [
                n
                for n in nodes
                if n not in current
                and all(p in current for p in _tech_prereqs().get(n, ()))
            ]
            frontier.sort(key=lambda n: (-unlock_count(n), node_cost(n)))
            for n in frontier[:2]:
                rendered.append(
                    _render_target(
                        f"前沿({unlock_count(n)} 项解锁)",
                        n,
                        current,
                        per_turn,
                        kind,
                        discounts,
                    )
                )
    else:
        gov_civics = sorted(
            {
                r.get("PrereqCivic")
                for r in _rows("Governments")
                if r.get("PrereqCivic") and r.get("PrereqCivic") not in current
            },
            key=lambda c: path_cost(prereq_closure(c, current, "civic")),
        )
        if gov_civics:
            rendered.append(
                _render_target(
                    "下一个政体", gov_civics[0], current, per_turn, kind, discounts
                )
            )
        if vt == "CULTURE":
            theater = _district_prereq_for_yield("YIELD_CULTURE", "civic")
            if theater and theater not in current:
                rendered.append(
                    _render_target(
                        "文化区域", theater, current, per_turn, kind, discounts
                    )
                )

    rendered = [(lines, t) for lines, t in rendered if lines]
    if not rendered and not extras:
        return []
    for lines, _t in rendered:
        out.extend(lines)
    out.extend(extras)
    # 两条候选回合数相近 → 明说让模型选
    turns = [t for _ls, t in rendered if t > 0]
    if len(turns) >= 2 and abs(turns[0] - turns[1]) / max(turns) < NEAR_TIE_RATIO:
        out.append("   两条路线回合数相近(差距 <10%),按局面与地形选,不必纠结。")
    out.append("   (回合估算基于标准速度;其他速度按全局系数等比放缩)")
    return out


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def get_tech_advice(
    current_techs: set[str],
    current_science: float,
    victory_type: str = "",
    game_state: dict | None = None,
    completed_boosts: set[str] | None = None,
) -> list[str]:
    """科技路径建议。无可推荐目标时返回空列表,不抛异常。"""
    try:
        return _advice(
            "tech",
            set(current_techs or ()),
            current_science,
            victory_type,
            game_state,
            completed_boosts,
        )
    except Exception:
        return []


def get_civic_advice(
    current_civics: set[str],
    current_culture: float,
    victory_type: str = "",
    game_state: dict | None = None,
    completed_boosts: set[str] | None = None,
) -> list[str]:
    """市政路径建议。无可推荐目标时返回空列表,不抛异常。"""
    try:
        return _advice(
            "civic",
            set(current_civics or ()),
            current_culture,
            victory_type,
            game_state,
            completed_boosts,
        )
    except Exception:
        return []
