"""城市基建缺口审计。

2026-08-11 用户点名:「每个城市基础设施要健全,不能打完就什么都不管」。

这条要求背后是本局两次代价高昂的实证:

  · T246 维护费冲到毛收入的 116%(真赤字)。根因不是军队太多,而是 13 座城
    里多数没有商业中心/市场/银行 —— GNP 全场垫底。
  · T260 科研被刚果反超(122.8 vs 112)。根因同样不是城市数量(我 13 城对
    它 10 城),而是学院建筑残缺:拉文那 40 产能全场最高却只出 13 科研,
    因为它有学院有图书馆【没有大学】;另有五座城连学院区都没有。

两次的共同点是:缺口一直存在,而且简报里其实提过 —— 但只以零散的低优先级
提示出现(「住房满」「2 座城有工业区但没有工厂」「还没有任何发电厂」),
从来没有【按城汇总成一张表】。单条看都像可以押后的小事,合起来才是结构性
塌陷。攻下来的城尤其容易被遗忘:四座新占城长期只有一个纪念碑。

所以这里做的事很简单:把每座城对照一份基建清单,列出它缺什么。判断仍然
交给调用方 —— 前线小城确实可能该先造兵,但那应该是一个**看着缺口做的
决定**,而不是因为没人提起就默认跳过。
"""

from __future__ import annotations

# 一条基建链:(区域关键字, 该区域下按顺序该有的建筑, 缺了意味着什么)
# 区域为 None 表示市中心建筑,不需要任何区域。
_CHAINS: list[tuple[str | None, list[str], str]] = [
    (None, ["MONUMENT", "GRANARY", "WATER_MILL"], "基础产出"),
    ("CAMPUS", ["LIBRARY", "UNIVERSITY"], "科研"),
    ("COMMERCIAL_HUB", ["MARKET", "BANK"], "金币"),
    ("INDUSTRIAL_ZONE", ["WORKSHOP", "FACTORY"], "产能"),
]

# 人口低于这个值的城,只查基础产出链 —— 它们连工人都排不满,
# 要求它们建齐四条链只会把队列堵死。
SMALL_CITY_POP = 4

# Civ6 规则:每 3 点人口解锁 1 个区域槽位。审计不能要求一座城建它
# 【根本放不下】的区域 —— 否则给出的是执行不了的建议。
POP_PER_DISTRICT = 3

# 产能低于这个值时,催它建区域是本末倒置:区域动辄 300 回合,而且区域
# 【不能用金币买】,所以那 300 回合没有任何提前结束的可能。
#
# 2026-08-11 用户两次点名(「这299回合造商业中心补脑子有问题吗」
# 「那先补人口不该,你看看大神们是怎么做的一般」)。实机:我给产能 1 的
# 阿尔皮努姆设了 300 回合的商业中心就走了。正确打法是反过来 ——
# 产能是人口的结果不是原因:先把粮食地块改良出来(它有海港、周边两处
# FISH 和 WHEAT 全没改良,市民根本没有粮食格可站)、市民focus 调到食物、
# 用国内商路输血、缺的建筑直接掏钱买。人口上来产能自然有。
LOW_PRODUCTION = 5


def _has(items: list[str], key: str) -> bool:
    """区域/建筑名在不同来源里前缀不一,统一按子串匹配。"""
    return any(key in str(it).upper() for it in items or [])


def city_gaps(city) -> list[str]:
    """单座城的基建缺口,按重要性排序。空列表 = 建齐了。

    只报**这座城现在真能动手做**的事:区域槽位不够、或者产能低到区域要
    几百回合时,给的是「先补人口」而不是「建区域」。
    """
    pop = int(getattr(city, "population", 0) or 0)
    prod = float(getattr(city, "production", 0) or 0)
    districts = list(getattr(city, "districts", []) or [])
    buildings = list(getattr(city, "buildings", []) or [])
    # 在建的区域【占槽位但还不能盖建筑】。两件事要分开算,否则两头都错:
    # 2026-08-11 T270 实测,阿奎莱亚的学院还在施工,审计却报"CAMPUS 缺图书馆"
    # —— 图书馆那时根本建不了;同时槽位又被在建的算成已用,真正该建的漏报。
    building_now = list(getattr(city, "districts_building", []) or [])
    gaps: list[str] = []

    slots = pop // POP_PER_DISTRICT
    built = len(districts) + len(building_now)
    # 产能太低时区域是陷阱(几百回合且不能买),此时唯一该做的是长人口
    starved = prod < LOW_PRODUCTION

    for district, chain, why in _CHAINS:
        if district is None:
            missing = [b for b in chain if not _has(buildings, b)]
            if missing:
                gaps.append(f"缺{'/'.join(missing)}({why})")
            continue
        # 小城先把基础产出补齐,不催区域
        if pop < SMALL_CITY_POP:
            continue
        if not _has(districts, district):
            # 已经在盖了 —— 既不该催它再建一个,也不该报它"缺建筑":
            # 区域没封顶之前,里面的建筑一个都排不上。
            if _has(building_now, district):
                continue
            if starved:
                continue  # 下面统一给一条「先补人口」
            if built >= slots:
                continue  # 槽位用完了,建不下 —— 催了也没用
            gaps.append(f"无{district}({why}为零)")
            built += 1  # 这一个占掉一个槽,后面的区域这回合排不上
            continue
        missing = [b for b in chain if not _has(buildings, b)]
        if missing:
            gaps.append(f"{district}缺{'/'.join(missing)}({why}只发挥一半)")

    if starved and pop > 0:
        gaps.append(
            f"产能仅 {prod:.0f} —— 先补人口(改良粮食地块/市民focus食物/"
            "国内商路输血/缺的建筑直接买),别排区域:它要几百回合且不能买"
        )
    return gaps


def audit(cities: list) -> list[tuple[str, list[str]]]:
    """全帝国审计。返回 [(城市名, 缺口列表)],只含有缺口的城。"""
    out: list[tuple[str, list[str]]] = []
    for c in cities or []:
        gaps = city_gaps(c)
        if gaps:
            out.append((str(getattr(c, "name", "?")), gaps))
    return out


def narrate(cities: list, limit: int = 4) -> str:
    """审计结果 → 简报行。没有缺口时返回空串(不占版面)。"""
    rows = audit(cities)
    if not rows:
        return ""
    # 缺口多的城排前面 —— 它们离"能自己造东西"最远
    rows.sort(key=lambda r: -len(r[1]))
    head = "; ".join(f"{name}:{'、'.join(gaps)}" for name, gaps in rows[:limit])
    more = f" 等 {len(rows)} 座" if len(rows) > limit else ""
    return (
        f"🏗 基建缺口 {len(rows)} 座城{more}:{head}"
        " —— 打下来的城和老城都要建齐,缺口单看都像小事,"
        "攒起来就是 GNP 垫底和科研被反超(本局已各发生一次)。"
    )
