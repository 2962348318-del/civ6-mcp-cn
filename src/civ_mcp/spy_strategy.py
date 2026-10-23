"""任务 AE —— 间谍后期运营。

间谍是后期唯一能【直接干扰对手胜利进度】的手段,而且不必宣战。
但它的操作面很宽(10 种任务 × N 个目标城 × 11 种晋升),AI 通常的做法是
把间谍派出去做反间谍就再也不管了 —— 等于把一个 225 锤、每回合 4 维护费的
单位放着发霉。

本模块做四件确定性的事:

1. **偷科技排序** —— 对手已研究、我没研究的科技,按 `Technologies.json`
   的 Cost 从贵到便宜排。偷最贵的那个,收益最大。
2. **进攻任务选择** —— 按对手正在逼近哪条胜利路线选任务:
   科技领先者 → 破坏产能(SABOTAGE_PRODUCTION)/ 偷尤里卡;
   文化领先者 → 窃取巨作(GREAT_WORK_HEIST);
   金币充裕者 → 抽取资金(SIPHON_FUNDS)。
3. **反间谍防御排序** —— 我方哪个区域最值钱、最可能被盯上。
   宇航中心 > 学院 > 工业区 > 剧院广场 —— 排序随我方胜利路线调整。
4. **晋升推荐** —— 按打算做的任务推荐晋升。

数据来源:
    Technologies.json  — 科技 Cost / Name(偷科技排序的唯一依据)
    Districts.json     — 区域名(反间谍目标必须是真实存在的区域)
    UnitPromotions.json— PROMOTION_CLASS_SPY 的 11 个晋升(名字来自导出表)

⚠️ 晋升 → 任务的对应关系是【从晋升的中文名推断】的:导出表里的
Description 列只有 LOC_ 本地化键,没有实际文案。所以每条推荐都标注了
"按晋升名推断",拿不准的晋升一律不推荐,不猜。

任务标识(COUNTERSPY / STEAL_TECH_BOOST / ...)取自 lua/espionage.py 里
已实测验证过的 _SPY_OP_HASHES,不是自己起的名。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

_GAMEDATA = gamedata_path()

# 与 lua/espionage.py 的 _SPY_OP_HASHES 保持一致。
OFFENSIVE_OPS = (
    "STEAL_TECH_BOOST",
    "SABOTAGE_PRODUCTION",
    "GREAT_WORK_HEIST",
    "SIPHON_FUNDS",
    "RECRUIT_PARTISANS",
    "NEUTRALIZE_GOVERNOR",
    "FABRICATE_SCANDAL",
    "GAIN_SOURCES",
)
DEFENSIVE_OP = "COUNTERSPY"


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_techs() -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for row in _rows("Technologies"):
        ttype = row.get("TechnologyType")
        if not ttype:
            continue
        try:
            cost = int(row.get("Cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        out[ttype] = (row.get("Name") or ttype, cost)
    return out


def _load_districts() -> dict[str, str]:
    return {
        r["DistrictType"]: r.get("Name") or r["DistrictType"]
        for r in _rows("Districts")
        if r.get("DistrictType")
    }


def _load_spy_promotions() -> dict[str, str]:
    return {
        r["UnitPromotionType"]: r.get("Name") or r["UnitPromotionType"]
        for r in _rows("UnitPromotions")
        if r.get("PromotionClass") == "PROMOTION_CLASS_SPY"
        and r.get("UnitPromotionType")
    }


TECHS = _load_techs()
DISTRICTS = _load_districts()
SPY_PROMOTIONS = _load_spy_promotions()

# 晋升 → 它最配合哪个任务。[按晋升名推断] —— 导出表的 Description 只有 LOC 键。
# 名字含义不明确的(语言学家 / 军需官 / 王牌驾驶员)不映射,不猜。
_PROMOTION_FOR_OP: dict[str, str] = {
    "STEAL_TECH_BOOST": "PROMOTION_SPY_TECHNOLOGIST",
    "GREAT_WORK_HEIST": "PROMOTION_SPY_CAT_BURGLAR",
    "SABOTAGE_PRODUCTION": "PROMOTION_SPY_DEMOLITIONS",
    "SIPHON_FUNDS": "PROMOTION_SPY_CON_ARTIST",
    "RECRUIT_PARTISANS": "PROMOTION_SPY_GUERILLA_LEADER",
    "COUNTERSPY": "PROMOTION_SPY_SEDUCTION",
}

# 我方区域的防守价值。数值只用于排序,不是游戏内数值。
_DEFENSE_VALUE: dict[str, int] = {
    "DISTRICT_SPACEPORT": 100,
    "DISTRICT_CAMPUS": 60,
    "DISTRICT_INDUSTRIAL_ZONE": 50,
    "DISTRICT_THEATER": 40,
    "DISTRICT_COMMERCIAL_HUB": 30,
}
# 锁定路线时,对应区域的防守价值再抬一档。
_DEFENSE_BY_VICTORY: dict[str, str] = {
    "SCIENCE": "DISTRICT_SPACEPORT",
    "CULTURE": "DISTRICT_THEATER",
    "DOMINATION": "DISTRICT_INDUSTRIAL_ZONE",
}

# 对手的威胁类型 → 该对他做什么
_OP_FOR_THREAT: dict[str, tuple[str, str]] = {
    "SCIENCE": ("SABOTAGE_PRODUCTION", "破坏他的产能,太空项目卡的是产能不是科技"),
    "CULTURE": ("GREAT_WORK_HEIST", "窃取巨作 —— 巨作被偷走他的旅游业绩直接掉"),
    "GOLD": ("SIPHON_FUNDS", "抽取资金,顺便补自己的国库"),
}


@dataclass(frozen=True)
class Spy:
    """一个间谍的实时状态。字段与 lua/models.py 的 SpyInfo 对齐。"""

    unit_id: int
    name: str = ""
    rank: int = 1
    city_name: str = "none"
    city_owner: int = -1
    status: str = "idle"
    is_escaping: bool = False
    current_mission: str = "none"
    available_ops: tuple[str, ...] = ()


@dataclass(frozen=True)
class RivalIntel:
    """一个对手的情报摘要。"""

    player_id: int
    civ_name: str
    # 他已研究、我没研究的科技(TechnologyType)
    techs_i_lack: tuple[str, ...] = ()
    # 他离哪条胜利最近:"SCIENCE" / "CULTURE" / "GOLD" / ""
    threat_type: str = ""
    capital_name: str = ""
    is_at_war: bool = False


@dataclass
class SpyContext:
    """间谍决策所需的事实,全部来自实时查询。"""

    turn: int = 0
    victory_type: str = ""
    spies: list[Spy] = field(default_factory=list)
    rivals: list[RivalIntel] = field(default_factory=list)
    # 我方城市 -> 该城已建的区域(DistrictType 列表)
    my_districts: dict[str, list[str]] = field(default_factory=dict)
    counterspy_cities: tuple[str, ...] = ()  # 已经有反间谍驻守的城市


def tech_cn(tech: str) -> str:
    entry = TECHS.get(tech)
    return entry[0] if entry else tech


def tech_cost(tech: str) -> int:
    entry = TECHS.get(tech)
    return entry[1] if entry else 0


def rank_steal_targets(techs: tuple[str, ...], top_n: int = 3) -> list[str]:
    """按 Cost 从贵到便宜排。表里没有的科技丢弃 —— 不猜价值。"""
    known = [t for t in techs if t in TECHS]
    known.sort(key=lambda t: (-tech_cost(t), t))
    return known[:top_n]


def defense_priority(ctx: SpyContext) -> list[tuple[str, str, int]]:
    """我方最该派反间谍的地方 → [(城市, 区域, 分值), ...],已排序。"""
    boost = _DEFENSE_BY_VICTORY.get((ctx.victory_type or "").upper(), "")
    out: list[tuple[str, str, int]] = []
    for city, districts in ctx.my_districts.items():
        for district in districts:
            value = _DEFENSE_VALUE.get(district)
            if value is None:
                continue
            if district == boost:
                value += 50
            out.append((city, district, value))
    out.sort(key=lambda item: (-item[2], item[0]))
    return out


def promotion_for(op: str) -> tuple[str, str] | None:
    """某个任务最配的晋升 → (晋升标识, 中文名)。没有把握就返回 None。"""
    promo = _PROMOTION_FOR_OP.get(op)
    if not promo or promo not in SPY_PROMOTIONS:
        return None
    return promo, SPY_PROMOTIONS[promo]


def get_spy_advice(ctx: SpyContext, max_items: int = 4) -> list[str]:
    """间谍运营建议。没有间谍时返回空列表。"""
    if not ctx.spies or not TECHS:
        return []

    out: list[str] = []

    # 0. 逃脱中的间谍最优先 —— 被抓住就是 225 锤没了
    for spy in ctx.spies:
        if spy.is_escaping:
            out.append(
                f"🔴 间谍 {spy.name or spy.unit_id} 已暴露,正在 "
                f"{spy.city_name} 逃脱 —— 立刻用 spy_action 选逃脱路线,"
                f"被抓住等于损失 225 锤和这一整条情报线。"
            )

    idle = [
        s
        for s in ctx.spies
        if not s.is_escaping and s.current_mission == "none" and s.status == "idle"
    ]

    # 1. 闲置间谍 → 按对手威胁类型派进攻任务
    threats = [
        r for r in ctx.rivals if r.threat_type in _OP_FOR_THREAT and not r.is_at_war
    ]
    threats.sort(key=lambda r: 0 if r.threat_type == "SCIENCE" else 1)
    for spy, rival in zip(idle, threats, strict=False):
        op, why = _OP_FOR_THREAT[rival.threat_type]
        if spy.available_ops and op not in spy.available_ops:
            continue
        target = rival.capital_name or f"{rival.civ_name}的城市"
        line = (
            f"💡 间谍 {spy.name or spy.unit_id} 空闲 → 对 {rival.civ_name} 做 {op}"
            f"(目标 {target}):{why}。"
        )
        promo = promotion_for(op)
        if promo:
            line += f" 晋升优先选「{promo[1]}」(按晋升名推断)。"
        out.append(line)

    # 2. 偷科技排序 —— 有明确的、可量化的目标才提
    for rival in ctx.rivals:
        targets = rank_steal_targets(rival.techs_i_lack)
        if not targets:
            continue
        listed = "、".join(f"{tech_cn(t)}({tech_cost(t)} 科研)" for t in targets)
        out.append(
            f"💡 {rival.civ_name} 已研究但你没有的科技,按成本从高到低:{listed}。"
            f"STEAL_TECH_BOOST 偷最贵的那个收益最大。"
        )
        break  # 一条就够,不要每个对手刷一遍

    # 3. 反间谍防御
    priorities = defense_priority(ctx)
    undefended = [
        (city, district)
        for city, district, _ in priorities
        if city not in ctx.counterspy_cities
    ]
    if undefended and len(idle) > len(threats):
        city, district = undefended[0]
        out.append(
            f"💡 {city} 的{DISTRICTS.get(district, district)}是你最值钱的目标"
            f"且无反间谍驻守 —— 派一个间谍做 {DEFENSIVE_OP}。"
            f"对手破坏它的代价远低于你重建它的代价。"
        )

    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条间谍建议已省略)")
    return ["== 间谍 ==", *out]
