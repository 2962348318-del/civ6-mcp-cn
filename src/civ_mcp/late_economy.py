"""任务 AD —— 后期经济系统。

后期经济是"活到后期"和"在后期赢"之间最容易被忽略的一段。它的特点是:
每一条都是【一次性的、有窗口的】操作,错过就是永久损失,而且没有任何提示。

本模块覆盖四块,数值全部从 Modifiers + ModifierArguments 两张导出表反查:

    大本钟(BUILDING_BIG_BEN,1450 锤,TECH_ECONOMICS)
        BIG_BEN_DOUBLE_GOLD  Amount=100  → 建成瞬间国库金币 +100%(翻倍)
        BIG_BEN_ECONOMIC_GOVERNMENT_SLOT → 另加 1 个经济政策槽
        ⇒ 建成前应停止一切非必要购买,把金币囤到最高点再收割。

    民主主义(GOVERNMENT_DEMOCRACY,CIVIC_SUFFRAGE)
        槽位 = 军事1 / 经济3 / 外交2 / 通用2(Government_SlotCounts 导出值)
        DEMOCRACY_PATRONAGE_GOLD_DISCOUNT Amount=50 → 金币招募伟人便宜 50%
        ⚠️ 计划书写的"民主政体给盟友的贸易路线 +4粮+4锤"其实【不是政体给的】,
          是政策卡 POLICY_ARSENAL_OF_DEMOCRACY(同样由 CIVIC_SUFFRAGE 解锁):
          ARSENALOFDEMOCRACY_* 四条 —— 目的地端 +2粮+2锤,起点端 +2粮+2锤。
          总量对得上,但归属错了会导致"换了政体却没插卡"。

    电力(本规则集的版本)
        BUILDING_POWER_PLANT(580 锤,TECH_ELECTRICITY,建在工业区)
        ⚠️ Gathering Storm 那套"发电量 / 电力缺口 / 煤电碳排放"在
          RULESET_STANDARD 里不存在(导出表里没有任何 Power 相关表)。
          所以本模块只讲工厂/发电厂这条产能链,不讲电网。

    后期政策卡(卡名与效果均已反查)
        POLICY_FIVE_YEAR_PLAN          区域科技 +100%、区域产能 +100%
        POLICY_THIRD_ALTERNATIVE       研究实验室/军事学院/发电厂 各 +4 金币
        POLICY_INTERNATIONAL_SPACE_AGENCY  每个宗主城邦 +5% 科技
        POLICY_ECOMMERCE               国际贸易路线 +5 产能 +10 金币
        POLICY_RATIONALISM             图书馆/大学/研究实验室 科技 +100%
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

_GAMEDATA = gamedata_path()

# 大本钟建成前多少回合开始"停止花钱"的提醒
BIG_BEN_HOARD_WINDOW = 15


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_buildings() -> dict[str, dict[str, str]]:
    return {r["BuildingType"]: r for r in _rows("Buildings") if r.get("BuildingType")}


def _load_policies() -> dict[str, dict[str, str]]:
    return {r["PolicyType"]: r for r in _rows("Policies") if r.get("PolicyType")}


def _load_slot_counts() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in _rows("Government_SlotCounts"):
        gov = row.get("GovernmentType")
        slot = row.get("GovernmentSlotType")
        if not gov or not slot:
            continue
        try:
            out.setdefault(gov, {})[slot] = int(row.get("NumSlots") or 0)
        except (TypeError, ValueError):
            continue
    return out


BUILDINGS = _load_buildings()
POLICIES = _load_policies()
SLOT_COUNTS = _load_slot_counts()

BIG_BEN = "BUILDING_BIG_BEN"


@dataclass(frozen=True)
class LatePolicy:
    """一张后期政策卡。effect 里的数字全部来自 ModifierArguments 反查。"""

    policy: str
    effect: str
    victories: tuple[str, ...] = ()  # 空 = 所有路线都值得


_RAW_LATE_POLICIES: tuple[LatePolicy, ...] = (
    LatePolicy("POLICY_FIVE_YEAR_PLAN", "区域科技 +100%、区域产能 +100%", ()),
    LatePolicy(
        "POLICY_RATIONALISM", "图书馆/大学/研究实验室的科技产出各 +100%", ("SCIENCE",)
    ),
    LatePolicy(
        "POLICY_INTERNATIONAL_SPACE_AGENCY", "每有一个宗主城邦 +5% 科技", ("SCIENCE",)
    ),
    LatePolicy(
        "POLICY_THIRD_ALTERNATIVE", "研究实验室 / 军事学院 / 发电厂 各 +4 金币", ()
    ),
    LatePolicy("POLICY_ECOMMERCE", "每条国际贸易路线 +5 产能 +10 金币", ()),
    LatePolicy(
        "POLICY_ARSENAL_OF_DEMOCRACY",
        "与盟友之间的贸易路线:目的地 +2粮+2锤,起点 +2粮+2锤",
        (),
    ),
)

# 只保留 Policies.json 里真的存在的卡。
LATE_POLICIES: tuple[LatePolicy, ...] = tuple(
    p for p in _RAW_LATE_POLICIES if p.policy in POLICIES
)


def policy_cn(policy: str) -> str:
    row = POLICIES.get(policy)
    return (row or {}).get("Name") or policy


def building_cn(building: str) -> str:
    row = BUILDINGS.get(building)
    return (row or {}).get("Name") or building


def government_slots(government: str) -> dict[str, int]:
    return dict(SLOT_COUNTS.get(government, {}))


@dataclass
class LateEconomyContext:
    """后期经济判断所需的事实,全部来自实时查询。"""

    turn: int = 0
    era: str = ""
    victory_type: str = ""
    gold: int = 0
    gold_per_turn: int = 0
    # 大本钟
    big_ben_turns_left: int = -1  # -1 = 没在造
    has_big_ben: bool = False
    # 政体 / 政策
    current_government: str = ""
    unlocked_governments: tuple[str, ...] = ()
    available_policies: tuple[str, ...] = ()
    slotted_policies: tuple[str, ...] = ()
    empty_slots: int = 0
    # 产能链
    cities_with_industrial_zone: int = 0
    cities_with_factory: int = 0
    cities_with_power_plant: int = 0
    city_count: int = 0
    # 贸易 / 城邦(决定几张后期卡值不值得插)
    international_routes: int = 0
    suzerain_count: int = 0
    allies: tuple[str, ...] = field(default_factory=tuple)


_LATE_ERAS = {
    "ERA_INDUSTRIAL",
    "ERA_MODERN",
    "ERA_ATOMIC",
    "ERA_INFORMATION",
    "ERA_FUTURE",
}


def _check_big_ben(ctx: LateEconomyContext) -> list[str]:
    if ctx.has_big_ben or BIG_BEN not in BUILDINGS:
        return []
    if ctx.big_ben_turns_left < 0 or ctx.big_ben_turns_left > BIG_BEN_HOARD_WINDOW:
        return []
    doubled = ctx.gold * 2
    return [
        f"💰 {building_cn(BIG_BEN)}还有 {ctx.big_ben_turns_left} 回合建成 —— "
        f"建成瞬间国库金币翻倍(+100%)。现在国库 {ctx.gold} 金,"
        f"建成时会变成 {doubled} 金。"
        f"⇒ 从现在起停止一切非必要购买,能囤多少囤多少,建成后再花。"
        f"(它还额外给 1 个经济政策槽)"
    ]


def _check_government(ctx: LateEconomyContext) -> list[str]:
    out = []
    for gov in ctx.unlocked_governments:
        if gov == ctx.current_government or gov not in SLOT_COUNTS:
            continue
        new = government_slots(gov)
        old = government_slots(ctx.current_government)
        if not new:
            continue
        delta = {
            slot: new.get(slot, 0) - old.get(slot, 0) for slot in set(new) | set(old)
        }
        gained = {s: d for s, d in delta.items() if d > 0}
        lost = {s: -d for s, d in delta.items() if d < 0}
        if not gained and not lost:
            continue
        parts = []
        if gained:
            parts.append(
                "多 " + "、".join(f"{s} ×{d}" for s, d in sorted(gained.items()))
            )
        if lost:
            parts.append(
                "少 " + "、".join(f"{s} ×{d}" for s, d in sorted(lost.items()))
            )
        out.append(
            f"💡 可换到 {gov}:槽位相比当前{';'.join(parts)}。"
            f"槽位差异是换政体唯一确定的收益,先按这个算。"
        )
    return out[:1]


def _check_late_policies(ctx: LateEconomyContext) -> list[str]:
    if not ctx.available_policies:
        return []
    vt = (ctx.victory_type or "").upper()
    slotted = set(ctx.slotted_policies)
    out = []
    for card in LATE_POLICIES:
        if card.policy not in ctx.available_policies or card.policy in slotted:
            continue
        if card.victories and vt and vt not in card.victories:
            continue
        # 有条件的卡:条件不成立就不推荐,推荐了也是白插
        if card.policy == "POLICY_ECOMMERCE" and ctx.international_routes <= 0:
            continue
        if (
            card.policy == "POLICY_INTERNATIONAL_SPACE_AGENCY"
            and ctx.suzerain_count <= 0
        ):
            continue
        if card.policy == "POLICY_ARSENAL_OF_DEMOCRACY" and not ctx.allies:
            continue
        tail = "" if ctx.empty_slots > 0 else "(需换掉现有的卡)"
        line = f"💡 后期卡「{policy_cn(card.policy)}」可用{tail} —— {card.effect}"
        if card.policy == "POLICY_ECOMMERCE":
            line += f"。你有 {ctx.international_routes} 条国际路线。"
        elif card.policy == "POLICY_INTERNATIONAL_SPACE_AGENCY":
            line += f"。你是 {ctx.suzerain_count} 个城邦的宗主 → +{ctx.suzerain_count * 5}% 科技。"
        out.append(line)
    return out


def _check_production_chain(ctx: LateEconomyContext) -> list[str]:
    if ctx.era not in _LATE_ERAS or ctx.city_count <= 0:
        return []
    out = []
    gap = ctx.cities_with_industrial_zone - ctx.cities_with_factory
    if gap > 0:
        out.append(
            f"⚠️ {gap} 座城有工业区但没有{building_cn('BUILDING_FACTORY')} —— "
            f"工业区不建工厂等于只挖了地基。后期一切都卡产能。"
        )
    if ctx.cities_with_factory > 0 and ctx.cities_with_power_plant == 0:
        cost = (BUILDINGS.get("BUILDING_POWER_PLANT") or {}).get("Cost", "?")
        out.append(
            f"💡 还没有任何{building_cn('BUILDING_POWER_PLANT')}({cost} 锤,"
            f"TECH_ELECTRICITY,建在工业区)—— 它是工厂之后的下一级产能建筑。"
        )
    if ctx.cities_with_industrial_zone == 0 and ctx.city_count >= 3:
        out.append(
            f"⚠️ {ctx.city_count} 座城一个工业区都没有 —— "
            f"后期任何项目都要几十回合,产能不足是最常见的隐性输法。"
        )
    return out


def get_late_economy_advice(ctx: LateEconomyContext, max_items: int = 4) -> list[str]:
    """后期经济建议。不在后期或没有可操作项时返回空列表。"""
    if not BUILDINGS or not POLICIES:
        return []

    out: list[str] = []
    out.extend(_check_big_ben(ctx))
    out.extend(_check_production_chain(ctx))
    out.extend(_check_late_policies(ctx))
    out.extend(_check_government(ctx))

    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条后期经济建议已省略)")
    return ["== 后期经济 ==", *out]
