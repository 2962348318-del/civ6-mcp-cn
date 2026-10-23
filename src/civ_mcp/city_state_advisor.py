"""任务 O —— 城邦策略(使者分配 / 优先度 / 宗主争夺)。

城邦是全游戏投入产出比最高的资源之一:使者是【免费的】,而 6 个使者换来的
宗主权可以顶一整座城的产出。AI 通常的做法是拿到使者就随便投一个,
于是永远卡在 2 使者——两边不靠:既没吃到 3 使者档,也没拿到宗主。

本模块只做三件确定性的事:
    1. 按胜利路线给城邦排优先级(类型 → 产出类别的映射是确定的)
    2. 使者档位算术:1/3/6 是硬阈值。算出"再投几个使者能跨过下一档"
    3. 宗主争夺算术:当前宗主投了几个、我差几个能夺过来

⚠️ 边界说明:
    · 城邦类型来自实时查询(lua/governance.py 的 csTypeMap 定义了
      Scientific / Cultural / Militaristic / Religious / Trade / Industrial
      六类),不是本模块编的。
    · **各城邦具体的宗主加成不在这里**——那是任务 W,而且 40+ 个城邦的
      宗主加成需要单独一张表,游戏库导出里没有。本模块只讲档位和归属,
      不承诺任何具体加成数值。
    · 使者档位 1/3/6 是游戏的固定阈值。它不在导出的 40 张表里
      (没有 CityStates / Influence 表),标注为 [机制常量]。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

from dataclasses import dataclass, field

# [机制常量] 使者档位。不在导出表里,但它是全局固定阈值,不随规则集变化。
ENVOY_TIERS = (1, 3, 6)
# 宗主门槛:使者数最多者且 ≥3 才是宗主。
SUZERAIN_MIN_ENVOYS = 3

# 城邦类型 → 它给的产出类别。类型名取自 lua/governance.py 的 csTypeMap。
CITY_STATE_YIELD = {
    "Scientific": "科技",
    "Cultural": "文化",
    "Militaristic": "军事产能",
    "Religious": "信仰",
    "Trade": "金币",
    "Industrial": "产能",
}

# 胜利路线 → 城邦类型优先序。越靠前越优先。
_PRIORITY: dict[str, tuple[str, ...]] = {
    "SCIENCE": (
        "Scientific",
        "Industrial",
        "Trade",
        "Cultural",
        "Militaristic",
        "Religious",
    ),
    "CULTURE": (
        "Cultural",
        "Religious",
        "Trade",
        "Scientific",
        "Industrial",
        "Militaristic",
    ),
    "RELIGION": (
        "Religious",
        "Cultural",
        "Trade",
        "Scientific",
        "Industrial",
        "Militaristic",
    ),
    "DOMINATION": (
        "Militaristic",
        "Industrial",
        "Trade",
        "Scientific",
        "Cultural",
        "Religious",
    ),
}
_DEFAULT_PRIORITY = (
    "Trade",
    "Industrial",
    "Scientific",
    "Cultural",
    "Militaristic",
    "Religious",
)


@dataclass(frozen=True)
class CityState:
    """一个已认识的城邦。字段与 lua/models.py 的 CityStateInfo 对齐。"""

    player_id: int
    name: str
    city_state_type: str = "Unknown"
    envoys_sent: int = 0  # 我投的使者数
    suzerain_id: int = -1  # -1 = 无宗主
    suzerain_name: str = "None"
    can_send_envoy: bool = True
    suzerain_envoys: int = 0  # 当前宗主投了几个(不知道时为 0)
    has_quest: bool = False  # 有可完成的任务
    quest_text: str = ""


@dataclass
class CityStateContext:
    """城邦决策所需的事实,全部来自 get_city_states 的实时返回。"""

    tokens_available: int = 0  # 可分配的使者
    city_states: list[CityState] = field(default_factory=list)
    victory_type: str = ""
    my_player_id: int = 0


def priority_order(victory_type: str) -> tuple[str, ...]:
    return _PRIORITY.get((victory_type or "").upper(), _DEFAULT_PRIORITY)


def type_rank(city_state_type: str, victory_type: str) -> int:
    """类型在当前路线下的排名。未知类型排最后。"""
    order = priority_order(victory_type)
    try:
        return order.index(city_state_type)
    except ValueError:
        return len(order)


def next_tier(envoys: int) -> int | None:
    """还没跨过的下一个使者档位。已经 ≥6 返回 None。"""
    for tier in ENVOY_TIERS:
        if envoys < tier:
            return tier
    return None


def envoys_to_suzerain(cs: CityState, my_id: int) -> int | None:
    """再投几个使者能夺下宗主?已经是宗主返回 0;信息不足返回 None。

    规则:使者最多者为宗主,且至少 SUZERAIN_MIN_ENVOYS 个。平手时在位者保留。
    """
    if cs.suzerain_id == my_id:
        return 0
    if cs.suzerain_id < 0:
        # 无人称宗主 —— 只需达到门槛
        return max(0, SUZERAIN_MIN_ENVOYS - cs.envoys_sent)
    if cs.suzerain_envoys <= 0:
        return None  # 不知道在位者投了多少,不猜
    return max(1, cs.suzerain_envoys + 1 - cs.envoys_sent)


def get_city_state_advice(ctx: CityStateContext, max_items: int = 4) -> list[str]:
    """城邦策略建议。没有已认识的城邦时返回空列表。"""
    if not ctx.city_states:
        return []

    out: list[str] = []
    vt = (ctx.victory_type or "").upper()

    # 1. 任务:免费使者,而且大多顺手就能做
    quests = [cs for cs in ctx.city_states if cs.has_quest]
    for cs in quests[:2]:
        detail = f":{cs.quest_text}" if cs.quest_text else ""
        out.append(
            f"💡 {cs.name} 有可完成的任务{detail} —— 完成送 1 个免费使者,"
            f"使者是宗主权的唯一硬通货。"
        )

    # 2. 使者分配:集中投一个跨档,好过分散投多个都卡在档间
    if ctx.tokens_available > 0:
        ranked = sorted(
            ctx.city_states,
            key=lambda cs: (
                type_rank(cs.city_state_type, vt),
                -(cs.envoys_sent),
            ),
        )
        picked = []
        for cs in ranked:
            if not cs.can_send_envoy:
                continue
            tier = next_tier(cs.envoys_sent)
            if tier is None:
                continue
            need = tier - cs.envoys_sent
            if need > ctx.tokens_available:
                continue
            yield_name = CITY_STATE_YIELD.get(cs.city_state_type, "产出")
            picked.append(
                f"· {cs.name}({cs.city_state_type}/{yield_name}):"
                f"当前 {cs.envoys_sent} 使者,再投 {need} 个跨到 {tier} 使者档。"
            )
            if len(picked) >= 2:
                break
        if picked:
            out.append(
                f"💡 有 {ctx.tokens_available} 个使者未分配 —— "
                f"集中投到跨档,好过分散投都卡在档位之间:"
            )
            out.extend(picked)

    # 3. 宗主争夺
    for cs in ctx.city_states:
        need = envoys_to_suzerain(cs, ctx.my_player_id)
        if need is None or need == 0:
            continue
        if need > ctx.tokens_available + 2:  # 太远了,提了也做不到
            continue
        # 2026-08-02 校准局 5b:必须报存量 —— 此前该建议在使者为 0 时连续
        # 60 回合出现,agent 无从执行也无从知道下一个使者从哪来。
        stock = f"你手上有 {ctx.tokens_available} 个"
        if ctx.tokens_available < need:
            stock += ",不够——使者来源:市政树/政策〔魅力型领袖/外交联盟〕"
        if cs.suzerain_id < 0:
            out.append(
                f"💡 {cs.name} 目前无宗主,再投 {need} 个使者即可称宗主"
                f"(门槛 {SUZERAIN_MIN_ENVOYS};{stock})。"
            )
        else:
            out.append(
                f"💡 {cs.name} 的宗主是 {cs.suzerain_name}"
                f"({cs.suzerain_envoys} 使者),你投 {cs.envoys_sent} 个,"
                f"再投 {need} 个可夺过来({stock})。"
            )

    if not out:
        return []
    if len(out) > max_items:
        omitted = len(out) - max_items
        out = out[:max_items]
        out.append(f"   (另有 {omitted} 条城邦建议已省略)")
    header = "== 城邦 =="
    if vt:
        header += f" 优先序({vt}):{' > '.join(priority_order(vt)[:3])}"
    return [header, *out]


# ===========================================================================
# 任务 W:宗主城邦加成追踪(追加,上方已有代码未动一行)
#
# 有些城邦的宗主加成是定义级的:日内瓦(和平时全城 +15% 科技)意味着你
# 永远不该主动宣战;耶路撒冷(首都视为圣城 + 采纳盟友宗教)是宗教路线的
# 最强工具。人类高手记得这 20+ 条,AI 不知道自己手里有什么。
#
# 宗主加成【完全可以从导出表反查】。Modifiers 里有 178 条 MINOR_CIV_*
# 修正器,命名规律:
#     MINOR_CIV_<城邦>_UNIQUE_INFLUENCE_*  → 指向真正的效果修正器
#     MINOR_CIV_<类型>_{SMALL,MEDIUM,LARGE}_INFLUENCE_BONUS
#                                          → 按使者档位的通用加成
# 效果数值在 ModifierArguments 里。所以本模块【不硬编码任何城邦加成】。
#
# 边界:
#   · **城邦标识 → 中文名的映射未导出**(Leaders 表只有领袖,无城邦城市名)。
#     显示名优先用调用方传入的实时 name,回退到修正器 ID 还原的英文键。
#   · 少数效果的 ModifierType 语义未收录在翻译表里 —— 如实输出原始类型并
#     标注"语义未导出",绝不猜。
#   · PAPAL_PRIMACY 变体是「教皇至上」信条下的替代档位,非常态,已过滤。
# ===========================================================================

import json as _json
import re as _re
from functools import lru_cache as _lru_cache
from pathlib import Path as _Path

_GD_DIR = gamedata_path()

# ModifierType → (人话模板, 服务于哪条胜利路线)。未收录的类型如实报原名。
_EFFECT_TEMPLATES: dict[str, tuple[str, str]] = {
    "MODIFIER_PLAYER_CITIES_ADJUST_WONDER_PRODUCTION": (
        "所有城市奇观建造 +{amount}% 产能",
        "CULTURE",
    ),
    "MODIFIER_PLAYER_CITIES_ADJUST_CITY_YIELD_MODIFIER": (
        "所有城市 +{amount}% {yield}",
        "",
    ),
    "MODIFIER_PLAYER_CAPITAL_CITY_ADJUST_CITY_YIELD_CHANGE": (
        "首都 +{amount} {yield}",
        "",
    ),
    "MODIFIER_PLAYER_DISTRICTS_ADJUST_YIELD_CHANGE": (
        "对应区域每个 +{amount} {yield}",
        "",
    ),
    "MODIFIER_PLAYER_ADJUST_FREE_RESOURCE_IMPORT": (
        "每回合免费获得 {amount} 份 {extra}",
        "DOMINATION",
    ),
    "MODIFIER_PLAYER_DISTRICTS_ADJUST_TRADE_ROUTE_CAPACITY": (
        "+{amount} 条贸易路线容量",
        "",
    ),
    "MODIFIER_PLAYER_CITIES_ADJUST_ALL_PROJECTS_PRODUCTION": (
        "所有项目 +{amount}% 产能(含太空船项目)",
        "SCIENCE",
    ),
    "MODIFIER_PLAYER_UNITS_ADJUST_UNIT_ATTACK_EXPERIENCE_MODIFIER": (
        "单位攻击获得的经验 +{amount}%",
        "DOMINATION",
    ),
    "MODIFIER_PLAYER_DISTRICTS_ADJUST_EXTRA_REGIONAL_RANGE": (
        "区域效应辐射范围 +{amount} 格(工厂/娱乐中心覆盖更多城)",
        "",
    ),
    "MODIFIER_PLAYER_DISTRICTS_ADJUST_GREAT_PERSON_POINTS": (
        "对应区域 +{amount} 伟人点({extra})",
        "CULTURE",
    ),
    "MODIFIER_PLAYER_TREAT_CAPITAL_AS_HOLY_CITY": ("你的首都被视为圣城", "RELIGION"),
    "MODIFIER_PLAYER_ADOPT_ALLY_FOUNDED_RELIGIONS": ("采纳盟友创立的宗教", "RELIGION"),
    "MODIFIER_PLAYER_GRANT_RANDOM_TECHNOLOGY_BOOST_ON_NEW_ERA": (
        "每进入新时代获得 {amount} 个随机尤里卡",
        "SCIENCE",
    ),
    "MODIFIER_PLAYER_GRANT_RANDOM_CIVIC_BOOST_ON_NEW_ERA": (
        "每进入新时代获得 {amount} 个随机鼓舞",
        "CULTURE",
    ),
    "MODIFIER_PLAYER_ADJUST_NATURAL_WONDER_RELIC": (
        "发现自然奇观时获得圣物",
        "RELIGION",
    ),
    "MODIFIER_PLAYER_CITIES_ADJUST_GREATWORK_YIELD": (
        "对应巨作产出提升(缩放 {amount})",
        "CULTURE",
    ),
    "MODIFIER_PLAYER_GRANT_CITIES_FRESHWATER_HOUSING_BONUS": (
        "所有城市获得淡水级住房",
        "",
    ),
    "MODIFIER_PLAYER_CITIES_ENABLE_BUILDING_FAITH_PURCHASE": (
        "可用信仰购买对应区域的建筑",
        "RELIGION",
    ),
    "MODIFIER_PLAYER_CITIES_ADJUST_BUILDING_PURCHASE_COST": (
        "对应建筑购买价 -{amount}%",
        "",
    ),
    "MODIFIER_PLAYER_ADJUST_TRADE_ROUTE_YIELD_PER_POST_IN_FOREIGN_CITY": (
        "每个外国贸易站 +{amount} {yield}",
        "",
    ),
    "MODIFIER_PLAYER_CITIES_ADJUST_TRADE_ROUTE_YIELD_PER_DESTINATION_LUXURY_FOR_INTERNATIONAL": (
        "国际贸易路线按目的地奢侈种类 +{amount} {yield}",
        "",
    ),
    "MODIFIER_PLAYER_CITIES_ADJUST_CITY_STATE_TRADE_ROUTE_DISTRICT_YIELD": (
        "通往城邦的贸易路线按区域 +{amount} {yield}",
        "",
    ),
    "MODIFIER_PLAYER_OWNED_BONUS_RESOURCE_EXTRA_AMENITIES": (
        "每种奖励资源额外 +{amount} 宜居度",
        "",
    ),
    "MODIFIER_PLAYER_UNIT_GRANT_UNLIMITED_PROMOTION_CHOICES": (
        "{extra} 可任选晋升(不受随机限制)",
        "RELIGION",
    ),
    "MODIFIER_PLAYER_ADJUST_VALID_IMPROVEMENT": ("解锁独有改良设施 {extra}", "CULTURE"),
    "MODIFIER_PLAYER_UNITS_GRANT_ABILITY": ("单位获得特殊能力 {extra}", ""),
}

_YIELD_TEXT = {
    "YIELD_SCIENCE": "科技",
    "YIELD_CULTURE": "文化",
    "YIELD_GOLD": "金币",
    "YIELD_FAITH": "信仰",
    "YIELD_PRODUCTION": "产能",
    "YIELD_FOOD": "食物",
}

# 产出类型 → 它主要服务的胜利路线。模板本身是产出无关的(如"全城 +N% {yield}")
# 时,由实际 YieldType 决定路线归属 —— 日内瓦的 +15% 科技理应为科学路线加星。
_YIELD_VICTORY = {
    "YIELD_SCIENCE": "SCIENCE",
    "YIELD_CULTURE": "CULTURE",
    "YIELD_FAITH": "RELIGION",
    "YIELD_PRODUCTION": "DOMINATION",
}


def _gd(table: str) -> list[dict]:
    try:
        with (_GD_DIR / f"{table}.json").open(encoding="utf-8") as fh:
            data = _json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@_lru_cache(maxsize=1)
def _modifier_index() -> tuple[dict, dict]:
    mods = {r.get("ModifierId"): r.get("ModifierType", "") for r in _gd("Modifiers")}
    args: dict[str, list[tuple[str, str]]] = {}
    for r in _gd("ModifierArguments"):
        args.setdefault(r.get("ModifierId", ""), []).append(
            (r.get("Name", ""), r.get("Value", ""))
        )
    return mods, args


def _describe_effect(modifier_id: str) -> tuple[str, str]:
    """效果修正器 → (人话描述, 服务的胜利路线)。语义未收录时如实报原名。"""
    mods, args = _modifier_index()
    mtype = mods.get(modifier_id, "")
    kv = dict(args.get(modifier_id, []))
    amount = kv.get("Amount") or kv.get("ScalingFactor") or ""
    yield_txt = _YIELD_TEXT.get(kv.get("YieldType", ""), kv.get("YieldType", ""))
    extra = (
        kv.get("ResourceType")
        or kv.get("GreatPersonClassType")
        or kv.get("UnitType")
        or kv.get("ImprovementType")
        or kv.get("AbilityType")
        or kv.get("BuildingType")
        or kv.get("GreatWorkObjectType")
        or ""
    )
    tpl = _EFFECT_TEMPLATES.get(mtype)
    if not tpl:
        return (f"{mtype} —— 语义未导出,以游戏内说明为准", "")
    text, victory = tpl
    if not victory:
        # 产出无关的模板:路线归属由实际 YieldType 决定
        victory = _YIELD_VICTORY.get(kv.get("YieldType", ""), "")
    return (text.format(amount=amount, extra=extra, **{"yield": yield_txt}), victory)


@_lru_cache(maxsize=1)
def suzerain_bonus_table() -> dict[str, list[tuple[str, str]]]:
    """城邦英文键 → [(效果描述, 服务路线), ...]。全部从 Modifiers 反查。"""
    mods, args = _modifier_index()
    generic = {
        "SCIENTIFIC",
        "CULTURAL",
        "INDUSTRIAL",
        "MILITARISTIC",
        "RELIGIOUS",
        "TRADE",
        "GOLD",
        "PRODUCTION",
    }
    out: dict[str, list[tuple[str, str]]] = {}

    def _push(key: str, desc: str, victory: str) -> None:
        out.setdefault(key, [])
        if all(desc != d for d, _v in out[key]):
            out[key].append((desc, victory))

    for mid in mods:
        if not mid or not mid.startswith("MINOR_CIV_"):
            continue
        m = _re.match(r"MINOR_CIV_(.+?)_UNIQUE_INFLUENCE_", mid)
        if not m:
            continue
        key = m.group(1)
        if key in generic or "PAPAL_PRIMACY" in mid:
            continue
        target = dict(args.get(mid, [])).get("ModifierId")
        if not target:
            continue
        desc, victory = _describe_effect(target)
        _push(key, desc, victory)

    # 无 UNIQUE_INFLUENCE 包装的特例(如耶路撒冷)按前缀兜底
    for mid in mods:
        m = _re.match(r"MINOR_CIV_([A-Z_]+?)_(?:HOLY_CITY|RELIGION)_BONUS$", mid or "")
        if m and m.group(1) not in generic:
            desc, victory = _describe_effect(mid)
            _push(m.group(1), desc, victory)
    return out


def _match_city_state(name: str) -> str:
    """实时名字/标识 → 加成表的键。匹配不上返回空串。"""
    if not name:
        return ""
    probe = _re.sub(r"[^A-Z]", "", str(name).upper())
    if not probe:
        return ""
    for key in suzerain_bonus_table():
        if _re.sub(r"[^A-Z]", "", key) == probe:
            return key
    return ""


def get_suzerain_bonus_advice(
    suzerainties: list[dict], victory_type: str = "", max_items: int = 4
) -> list[str]:
    """已宗主城邦的加成 + 对胜利路线的价值。无宗主时返回空列表,不抛异常。

    suzerainties: [{"name"/"key", "type_name"(城邦类型,可选)}]
    """
    try:
        table = suzerain_bonus_table()
        if not suzerainties or not table:
            return []
        vt = (victory_type or "").upper()
        scored: list[tuple[int, str]] = []
        for cs in suzerainties:
            if not isinstance(cs, dict):
                continue
            raw_name = str(cs.get("name") or cs.get("key") or "")
            key = _match_city_state(cs.get("key") or "") or _match_city_state(raw_name)
            cs_type = str(cs.get("type_name") or cs.get("city_state_type") or "")
            type_txt = f"({cs_type})" if cs_type else ""
            if not key:
                if cs_type:
                    y = CITY_STATE_YIELD.get(cs_type, "产出")
                    scored.append(
                        (
                            0,
                            f"· {raw_name}{type_txt}:类型加成给{y};"
                            f"独有宗主加成未匹配到导出数据,"
                            f"以游戏内说明为准。",
                        )
                    )
                continue
            effects = table[key]
            texts = "、".join(d for d, _v in effects)
            hit = any(v == vt for _d, v in effects if v)
            value = 2 if hit else (1 if any(not v for _d, v in effects) else 0)
            line = f"· {raw_name}{type_txt}:{texts}"
            if hit:
                line += f" ★ 直接服务 {vt} 路线,优先保住宗主权。"
            elif value == 1:
                line += " —— 通用收益,任何路线都吃得到。"
            else:
                line += " —— 与当前路线不契合,使者可考虑挪去别处。"
            scored.append((value, line))
        if not scored:
            return []
        scored.sort(key=lambda p: -p[0])
        out = ["== 宗主加成 =="] + [t for _v, t in scored[:max_items]]
        if len(scored) > max_items:
            out.append(f"   (另有 {len(scored) - max_items} 个宗主城邦未列出)")
        out.append("   (加成从 Modifiers 反查;城邦中文名未导出,显示名用实时数据)")
        return out
    except Exception:
        return []
