"""任务 P —— 奇观优先级分级。

不是所有奇观都值得抢。抢错一个奇观 = 一座城十几到几十回合的产能白扔
(被抢走只退金币,回合数不退)。人类高手知道哪些是必抢、哪些是诱饵;
AI 看到"可建奇观"就按顺序选。

**分级的依据不是记忆,是效果本身。** 每条 S/A 档的理由都是从
`Modifiers` + `ModifierArguments` 两张导出表里查到的实际效果:

    金字塔      PYRAMID_ADJUST_BUILDER_CHARGES  Amount=1  → 全部建造者 +1 次使用
    大图书馆    ...GRANT_ALL_TECHNOLOGY_BOOST_BY_ERA ANCIENT→CLASSICAL
    牛津大学    OXFORD_ADDSCIENCEYIELD 20% + GRANT_RANDOM_TECHNOLOGY Amount=2
    鲁尔山谷    ...ADJUST_CITY_YIELD_MODIFIER PRODUCTION 20% + 每格 +1 产能
    大本钟      BIG_BEN_DOUBLE_GOLD Amount=100(国库翻倍)+ 1 个经济槽
    巨石阵      STONEHENGE_GRANT_PROPHET Amount=1(直接送一个大预言家)
    空中花园    HANGING_GARDEN_ADDGROWTH Amount=15(全城 +15% 成长)

两条被数据纠正的地方,记在这里免得后人再犯:
    · 计划书把**巨石阵**列为诱饵。数据不同意:它直接送一个大预言家,
      而"抢不到大预言家 = 宗教路线直接断"。⇒ 宗教路线下它是 S 档。
      非宗教路线下它才是诱饵。
    · 计划书点名的**基尔瓦基斯瓦尼**和**摩索拉斯王陵墓**在
      RULESET_STANDARD 的 Buildings 表里【不存在】(资料片内容)。
      本模块只认导出表里真的有的奇观,查不到的一律丢弃。

分级本身(S/A/B/诱饵)是编辑判断,不是导出值 —— 这一点必须承认。
但每条判断背后的效果都可查证。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass
from pathlib import Path

_GAMEDATA = gamedata_path()

TIER_ORDER = ("S", "A", "B", "诱饵")


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _load_wonders() -> dict[str, dict[str, str]]:
    """Buildings.json 里 IsWonder=true 的行。这是"这个奇观存不存在"的唯一判据。"""
    return {
        r["BuildingType"]: r
        for r in _rows("Buildings")
        if r.get("BuildingType") and (r.get("IsWonder") or "").lower() == "true"
    }


WONDERS: dict[str, dict[str, str]] = _load_wonders()


@dataclass(frozen=True)
class TierEntry:
    """一个奇观的分级。tiers: 胜利路线 -> 档位;default 是其它路线的档位。"""

    effect: str  # 已查证的效果;空串 = 导出表里查不到
    default: str
    tiers: dict[str, str]


# 键必须在 Buildings.json 里存在,否则在 _validated() 里被丢弃。
_RAW_TIERS: dict[str, TierEntry] = {
    # ---- 通用高价值 ----
    "BUILDING_PYRAMIDS": TierEntry(
        "全部建造者 +1 次使用次数,并直接送 1 个建造者", "A", {}
    ),
    "BUILDING_BIG_BEN": TierEntry("国库金币翻倍(+100%),另加 1 个经济政策槽", "A", {}),
    "BUILDING_FORBIDDEN_CITY": TierEntry(
        "+1 个通用(万能)政策槽", "A", {"CULTURE": "S"}
    ),
    # ---- 科学 ----
    "BUILDING_GREAT_LIBRARY": TierEntry(
        "直接获得远古+古典时代全部科技的尤里卡", "B", {"SCIENCE": "S"}
    ),
    "BUILDING_OXFORD_UNIVERSITY": TierEntry(
        "该城 +20% 科技,并直接送 2 个随机科技", "B", {"SCIENCE": "S"}
    ),
    "BUILDING_RUHR_VALLEY": TierEntry(
        "该城 +20% 产能,城内每格 +1 产能", "A", {"SCIENCE": "S", "DOMINATION": "S"}
    ),
    # ---- 文化 ----
    "BUILDING_BOLSHOI_THEATRE": TierEntry("直接送 2 个随机市政", "B", {"CULTURE": "S"}),
    "BUILDING_EIFFEL_TOWER": TierEntry(
        "全境所有城市 +2 魅力(国家公园/海滨度假区的前提)", "B", {"CULTURE": "S"}
    ),
    "BUILDING_CRISTO_REDENTOR": TierEntry(
        "海滨度假区旅游业绩 ×2,且宗教旅游永远按满额计算", "B", {"CULTURE": "S"}
    ),
    "BUILDING_BROADWAY": TierEntry(
        "该城 +20% 文化,并送 1 个原子能时代随机市政灵感", "B", {"CULTURE": "A"}
    ),
    "BUILDING_HERMITAGE": TierEntry("", "B", {"CULTURE": "A"}),
    "BUILDING_SYDNEY_OPERA_HOUSE": TierEntry("", "B", {"CULTURE": "A"}),
    "BUILDING_ESTADIO_DO_MARACANA": TierEntry("", "B", {"CULTURE": "A"}),
    "BUILDING_ORACLE": TierEntry(
        "该城所有区域 +2 各类伟人点", "B", {"CULTURE": "A", "RELIGION": "A"}
    ),
    # ---- 宗教 ----
    "BUILDING_STONEHENGE": TierEntry(
        "直接送 1 个大预言家 —— 大预言家被抢走等于宗教路线直接断",
        "诱饵",
        {"RELIGION": "S"},
    ),
    "BUILDING_HAGIA_SOPHIA": TierEntry(
        "全部宗教单位 +1 次传教次数", "诱饵", {"RELIGION": "S"}
    ),
    "BUILDING_MAHABODHI_TEMPLE": TierEntry(
        "直接送 2 个使徒", "诱饵", {"RELIGION": "A"}
    ),
    "BUILDING_MONT_ST_MICHEL": TierEntry(
        "全部宗教单位获得殉道者晋升", "诱饵", {"RELIGION": "A"}
    ),
    # ---- 统治 ----
    "BUILDING_TERRACOTTA_ARMY": TierEntry(
        "全部现役单位免费升一级,考古学家可进入外国领土",
        "B",
        {"DOMINATION": "A", "CULTURE": "A"},
    ),
    "BUILDING_ALHAMBRA": TierEntry("+1 个军事政策槽", "B", {"DOMINATION": "A"}),
    "BUILDING_VENETIAN_ARSENAL": TierEntry(
        "建造海军单位时额外免费获得一个同样的单位", "B", {"DOMINATION": "A"}
    ),
    # ---- 经济 / 条件型 ----
    "BUILDING_COLOSSUS": TierEntry(
        "+1 条贸易路线容量,并直接送 1 个商人(需沿海城市)", "B", {}
    ),
    "BUILDING_GREAT_ZIMBABWE": TierEntry(
        "+1 条贸易路线容量,贸易路线按城内奖励资源数额外产金", "B", {}
    ),
    "BUILDING_PETRA": TierEntry(
        "城内沙漠格 +2 食物 +2 金币 +1 产能 —— 没有沙漠就是废的", "B", {}
    ),
    "BUILDING_CHICHEN_ITZA": TierEntry(
        "城内雨林格 +2 文化 +1 产能 —— 没有雨林就是废的", "B", {"CULTURE": "A"}
    ),
    "BUILDING_HUEY_TEOCALLI": TierEntry(
        "城内湖泊格 +1 食物 +1 产能,湖泊提供宜居度 —— 没有湖就是废的", "B", {}
    ),
    "BUILDING_POTALA_PALACE": TierEntry("+1 个外交政策槽", "B", {}),
    "BUILDING_GREAT_LIGHTHOUSE": TierEntry(
        "全部海军单位 +1 移动力 —— 内陆局基本无用", "诱饵", {}
    ),
    # ---- 诱饵 ----
    "BUILDING_HANGING_GARDENS": TierEntry(
        "全部城市 +15% 成长速度 —— 早期基数太小,折算下来每城约每十几回合多 1 人口",
        "诱饵",
        {},
    ),
    "BUILDING_COLOSSEUM": TierEntry("", "诱饵", {"CULTURE": "B"}),
}


def _validated() -> dict[str, TierEntry]:
    """只保留 Buildings.json 里真的存在的奇观。查不到的直接丢弃,不猜。"""
    if not WONDERS:
        return {}
    return {k: v for k, v in _RAW_TIERS.items() if k in WONDERS}


WONDER_TIERS: dict[str, TierEntry] = _validated()

# 计划书点名、但本规则集里不存在的奇观(资料片内容)。留作记录。
UNAVAILABLE_IN_RULESET = tuple(sorted(set(_RAW_TIERS) - set(WONDERS)))


def wonder_cn(building_type: str) -> str:
    row = WONDERS.get(building_type)
    return (row or {}).get("Name") or building_type


def wonder_cost(building_type: str) -> int:
    row = WONDERS.get(building_type)
    try:
        return int((row or {}).get("Cost") or 0)
    except (TypeError, ValueError):
        return 0


def tier_of(building_type: str, victory_type: str = "") -> str:
    """某个奇观在某条路线下的档位。未分级的奇观返回 "" —— 不硬凑。"""
    entry = WONDER_TIERS.get(building_type)
    if not entry:
        return ""
    return entry.tiers.get((victory_type or "").upper(), entry.default)


def get_wonder_advice(
    available_wonders: list[str],
    victory_type: str = "",
    production_per_turn: int = 0,
    max_items: int = 4,
) -> list[str]:
    """给一批"当前可建的奇观"分级排序。没有可评的奇观时返回空列表。

    available_wonders: BuildingType 列表,来自 get_city_production 的可选项。
    """
    if not available_wonders or not WONDER_TIERS:
        return []

    graded: list[tuple[int, str, str]] = []
    for wonder in available_wonders:
        tier = tier_of(wonder, victory_type)
        if not tier:
            continue
        graded.append((TIER_ORDER.index(tier), tier, wonder))
    if not graded:
        return []
    graded.sort(key=lambda g: (g[0], wonder_cost(g[2])))

    out = ["== 奇观分级 =="]
    for _, tier, wonder in graded[:max_items]:
        entry = WONDER_TIERS[wonder]
        cost = wonder_cost(wonder)
        line = f"[{tier}] {wonder_cn(wonder)}({cost} 锤)"
        if production_per_turn > 0 and cost > 0:
            line += f",约 {cost / production_per_turn:.0f} 回合"
        if entry.effect:
            line += f" —— {entry.effect}"
        else:
            line += " —— 具体效果不在导出表里,建议在游戏内确认后再决定"
        if tier == "诱饵":
            line += "。除非闲着没事,否则别抢。"
        out.append(line)

    if len(graded) > max_items:
        out.append(f"   (另有 {len(graded) - max_items} 个可建奇观未列出)")
    out.append(
        "   奇观被抢走只退金币,不退回合 —— 和议程为「抢奇观」的对手正面竞速前,"
        "先掂量产能差距。"
    )
    return out
