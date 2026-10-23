"""任务 A —— 区域长期布局规划 + 城市职能分工。

纯计算,不需要 AI 判断:数一数城市周围的山、河、海岸、资源,就知道这座城
该干什么。规则全部来自导出表:

    Districts.json               21 区域:Cost / PrereqTech / RequiresPopulation
                                 (RequiresPopulation=true 即"专业区域",
                                  占区域槽位;水渠/浴场/宇航中心不占)
    District_Adjacencies.json    区域 → 相邻规则挂载(45 条)
    Adjacency_YieldChanges.json  规则本体:什么地形/地貌/资源给多少产出
    Buildings.json               各区域的建筑链(IsWonder=false 且有
                                  PrereqDistrict 的),按 Cost 排序即建设时序

两个诚实标注:

  · **区域槽位公式是 [机制常量]**:人口 1 → 1 槽,之后每 +3 人口 +1 槽
    (上限 = 1 + (人口-1)//3)。导出表只有 RequiresPopulation 布尔列,
    没有这条公式——它来自计划书,按机制常识处理,实机可验。
  · **相邻分是"最优落位的上限估算"**:把整个可工作圈的山/林/资源都数进去,
    再按六邻域封顶。实际落位一格只有 6 个邻格,真实值 ≤ 估算值。
    文案里写"上限",不冒充精确。

标准区域 vs 文明独有区域:同一产出组里 Cost 最高的是标准款(独有款半价,
27 vs 54,已在 strategy_space 验证)。推荐一律用标准款名字——玩家若是
对应文明,游戏会自动替换成独有款,建议不会失效;反过来推荐独有款给
造不出的文明,建议就是错的。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from civ_mcp.hexgrid import hex_distance

_GAMEDATA = gamedata_path()


# 缺陷107(2026-08-11 罗马局 T315-T317)
# ---------------------------------------------------------------------------
# CityInfo.districts 的元素【自带坐标后缀】:"DISTRICT_INDUSTRIAL_ZONE@66,47"
# —— Lua 侧 distLocs 把 DistrictType、未完工标记 "*" 和 "@x,y" 拼成一个串
# (见 lua/cities.py)。于是 `"DISTRICT_INDUSTRIAL_ZONE" in city.districts`
# 这种精确成员判断【永远返回 False】。
#
# 两处中招,而且第二处更贵:
#   ① get_area_effect_advice 的 has_iz —— 罗马和奥斯蒂亚明明各有工业区+
#      工厂+发电厂,简报连着两回合说"目前群内还没有工业区,这是后期产能的
#      地基",劝我去建一个已经存在的东西。
#   ② end_turn 的 spaceport_cities —— 科学胜利收尾模块靠它判断"太空港建了
#      没"。这个恒为空元组,意味着整条科学收尾链的核心输入一直是空的。
#      T311 刚锁定科学路线,再过几回合就要建宇航中心 —— 这个缺陷是在它
#      造成损失【之前】被抓到的。
#
# 和缺陷 99(建筑前缀 BUILDING_)、101(奢侈品份数后缀 xN)是同一族:同一个
# 值在两处用两种写法表示,只在一处做了归一。区别是这次的归一从来没做过。
def district_types(districts) -> set[str]:
    """把 CityInfo.districts 归一成纯 DistrictType 集合。

    剥掉 "@x,y" 坐标后缀和未完工标记 "*"。传 None/非序列返回空集合。
    """
    out: set[str] = set()
    for raw in districts or ():
        name = str(raw or "").split("@", 1)[0].replace("*", "").strip()
        if name:
            out.add(name)
    return out


def has_district(districts, *wanted: str) -> bool:
    """城市已建成的区域里是否有 wanted 中的任意一种。

    永远用它代替 `"DISTRICT_X" in city.districts` —— 后者因为坐标后缀恒假。
    """
    if not wanted:
        return False
    return bool(district_types(districts) & set(wanted))


# [机制常量] 区域槽位:人口 1 → 1 槽,每 +3 人口 +1 槽。见模块 docstring。
def district_slot_limit(population: int) -> int:
    if population <= 0:
        return 0
    return 1 + (population - 1) // 3


# 胜利路线 → 产出权重(基准 1.0)
_VICTORY_WEIGHTS: dict[str, dict[str, float]] = {
    "SCIENCE": {"YIELD_SCIENCE": 2.0, "YIELD_PRODUCTION": 1.4},
    "CULTURE": {"YIELD_CULTURE": 2.0, "YIELD_FAITH": 1.2},
    "RELIGION": {"YIELD_FAITH": 2.0, "YIELD_CULTURE": 1.2},
    "DOMINATION": {"YIELD_PRODUCTION": 2.0, "YIELD_GOLD": 1.3},
}

_YIELD_CN = {
    "YIELD_SCIENCE": "科技",
    "YIELD_FAITH": "信仰",
    "YIELD_GOLD": "金币",
    "YIELD_PRODUCTION": "产能",
    "YIELD_CULTURE": "文化",
}
_SPECIALIZATION_CN = {
    "YIELD_SCIENCE": "学院城",
    "YIELD_FAITH": "圣地城",
    "YIELD_GOLD": "商业城",
    "YIELD_PRODUCTION": "工业城",
    "YIELD_CULTURE": "剧院城",
}
# 相邻分低于这个值就不配叫专精 —— 报"均衡城"
MIN_SPECIALIZATION_SCORE = 2.0


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@lru_cache(maxsize=1)
def _districts() -> dict[str, dict[str, str]]:
    return {r["DistrictType"]: r for r in _rows("Districts") if r.get("DistrictType")}


@lru_cache(maxsize=1)
def _adjacency_rules() -> dict[str, list[dict[str, str]]]:
    """DistrictType → 它挂载的相邻规则(已 join Adjacency_YieldChanges)。"""
    rules = {r.get("ID"): r for r in _rows("Adjacency_YieldChanges")}
    out: dict[str, list[dict[str, str]]] = {}
    for link in _rows("District_Adjacencies"):
        rule = rules.get(link.get("YieldChangeId"))
        d = link.get("DistrictType")
        if d and rule:
            out.setdefault(d, []).append(rule)
    return out


@lru_cache(maxsize=1)
def _district_primary_yield() -> dict[str, str]:
    """区域 → 它相邻规则的主产出(多数票)。无规则的区域不在表里。"""
    out = {}
    for d, rules in _adjacency_rules().items():
        counts: dict[str, int] = {}
        for r in rules:
            y = r.get("YieldType", "")
            if y:
                counts[y] = counts.get(y, 0) + 1
        if counts:
            out[d] = max(counts, key=lambda y: (counts[y], y))
    return out


@lru_cache(maxsize=1)
def standard_district_for_yield() -> dict[str, str]:
    """产出 → 标准款区域(同产出组里 Cost 最高的;独有款半价,见 docstring)。"""
    best: dict[str, tuple[int, str]] = {}
    for d, y in _district_primary_yield().items():
        try:
            cost = int(_districts().get(d, {}).get("Cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        if y not in best or cost > best[y][0]:
            best[y] = (cost, d)
    return {y: d for y, (_c, d) in best.items()}


def district_cn(dtype: str) -> str:
    return (_districts().get(dtype) or {}).get("Name") or dtype


def is_specialty(dtype: str) -> bool:
    """占区域槽位的专业区域(RequiresPopulation=true,导出值)。"""
    return (_districts().get(dtype) or {}).get("RequiresPopulation") == "true"


@lru_cache(maxsize=32)  # 21 个区域全装得下;maxsize=1 会互相踢缓存
def building_chain(dtype: str) -> tuple[tuple[int, str], ...]:
    """区域的建筑链,(Cost, 中文名) 升序。同价位多选一的只保留最先一个并计数。"""
    raw = sorted(
        (int(r.get("Cost") or 0), r.get("Name") or "")
        for r in _rows("Buildings")
        if r.get("PrereqDistrict") == dtype and r.get("IsWonder") != "true"
    )
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(raw):
        cost = raw[i][0]
        tier = [name for c, name in raw if c == cost]
        # 同价位多选一(如大学/伊斯兰学校):独有性不在导出表里,并列展示不单推
        if len(tier) > 1:
            label = "/".join(tier[:2]) + ("等" if len(tier) > 2 else "") + "(同级择一)"
        else:
            label = tier[0]
        out.append((cost, label))
        i += len(tier)
    return tuple(out)


# ---------------------------------------------------------------------------
# 相邻潜力(最优落位的上限估算)
# ---------------------------------------------------------------------------


def _count_tiles(tiles: list[dict], pred) -> int:
    n = 0
    for t in tiles:
        if isinstance(t, dict) and pred(t):
            n += 1
    return n


def adjacency_potential(dtype: str, tiles: list[dict]) -> tuple[float, list[str]]:
    """某区域在这批地块上的相邻分上限 → (分, 依据)。

    只计算能从地块判定的规则(地形/地貌/河流/资源);区域相邻、自然奇观等
    来源列未导出或依赖未来建设的规则跳过。单一来源按六邻域封顶。
    """
    score = 0.0
    why: list[str] = []
    for rule in _adjacency_rules().get(dtype, ()):
        try:
            change = float(rule.get("YieldChange") or 0)
            per = max(1, int(rule.get("TilesRequired") or 1))
        except (TypeError, ValueError):
            continue
        terrain = rule.get("AdjacentTerrain") or ""
        feature = rule.get("AdjacentFeature") or ""
        if terrain:
            n = min(6, _count_tiles(tiles, lambda t, terrain=terrain: t.get("terrain") == terrain))
            if n:
                score += n // per * change
                why.append(f"{n} 格{terrain.split('_')[-1].lower()}")
        elif feature:
            n = min(6, _count_tiles(tiles, lambda t, feature=feature: t.get("feature") == feature))
            if n // per:
                score += n // per * change
        elif rule.get("AdjacentRiver") == "true":
            if _count_tiles(tiles, lambda t: t.get("is_river")):
                score += change
                why.append("沿河")
        elif rule.get("AdjacentResource") == "true":
            n = min(6, _count_tiles(tiles, lambda t: t.get("resource")))
            if n:
                score += n * change
                why.append(f"{n} 处资源")
        # AdjacentDistrict / OtherDistrictAdjacent / 自然奇观列未导出 → 跳过
    return score, why


# ---------------------------------------------------------------------------
# 城市职能分工
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Specialization:
    yield_type: str  # "" = 均衡城
    cn: str  # 学院城 / 工业城 / … / 均衡城
    district: str  # 对应的标准款区域("" = 无)
    score: float  # 加权相邻分(上限估算)
    reasons: tuple[str, ...]


def suggest_specialization(
    city_tiles: list[dict], victory_type: str = ""
) -> Specialization:
    """按地形算这座城最适合的职能。纯计算,不抛异常。

    city_tiles:该城可工作圈的地块(与 TileInfo 对齐的 dict)。
    """
    try:
        weights = _VICTORY_WEIGHTS.get((victory_type or "").upper(), {})
        best: Specialization | None = None
        for y, dtype in standard_district_for_yield().items():
            if y not in _YIELD_CN:
                continue
            raw, why = adjacency_potential(dtype, city_tiles or [])
            weighted = raw * weights.get(y, 1.0)
            cand = Specialization(y, _SPECIALIZATION_CN[y], dtype, weighted, tuple(why))
            if best is None or cand.score > best.score:
                best = cand
        if best is None or best.score < MIN_SPECIALIZATION_SCORE:
            return Specialization(
                "",
                "均衡城",
                "",
                best.score if best else 0.0,
                ("周边没有突出的相邻加成地形——产能/粮食基础设施优先,不追专精",),
            )
        return best
    except Exception:
        return Specialization("", "均衡城", "", 0.0, ())


# ---------------------------------------------------------------------------
# 区域建设建议
# ---------------------------------------------------------------------------


def get_district_advice(city: dict, game_state: dict | None = None) -> list[str]:
    """一座城的区域建设时序 + 放置注意。无可建议时返回空列表,不抛异常。

    city: {"name", "population", "districts": [已建 DistrictType], "tiles": [...]}
    game_state: {"victory_type", "techs": set[str](可选,过滤未解锁)}
    """
    try:
        return _advice_inner(city or {}, game_state or {})
    except Exception:
        return []


def _advice_inner(city: dict, gs: dict) -> list[str]:
    tiles = city.get("tiles") or []
    built = [d for d in (city.get("districts") or []) if isinstance(d, str)]
    try:
        pop = int(city.get("population") or 0)
    except (TypeError, ValueError):
        pop = 0
    if pop <= 0 and not built:
        return []
    name = city.get("name") or "该城"
    victory = str(gs.get("victory_type") or "")
    techs = gs.get("techs")

    out = [f"== 区域规划({name}) =="]

    # 1. 槽位账
    limit = district_slot_limit(pop)
    used = sum(1 for d in built if is_specialty(d))
    if used >= limit:
        out.append(
            f"⚠️ 专业区域 {used}/{limit} 槽已满(人口 {pop})——"
            f"再 +{3 - (pop - 1) % 3} 人口解锁下一槽,先涨人口再规划新区域。"
        )
    else:
        out.append(
            f"专业区域 {used}/{limit} 槽(人口 {pop}),还能再建 {limit - used} 个。"
        )

    # 2. 下一个区域:职能驱动
    spec = suggest_specialization(tiles, victory)
    if spec.district and spec.district not in built and used < limit:
        row = _districts().get(spec.district, {})
        pre = row.get("PrereqTech") or row.get("PrereqCivic") or ""
        gate = ""
        if pre and isinstance(techs, (set, frozenset)) and pre not in techs:
            gate = f"(需先研究 {pre})"
        why = f",依据:{('、'.join(spec.reasons))}" if spec.reasons else ""
        out.append(
            f"💡 职能判定:{spec.cn}(相邻上限约 +{spec.score:.0f}{why})。"
            f"下一个区域 → {district_cn(spec.district)}{gate}。"
        )
        # 3. 同地形竞争提示(数据驱动:哪些区域抢同一种相邻来源)
        rivals = _terrain_rivals(spec.district)
        if rivals:
            out.append(
                f"   落位注意:{('、'.join(district_cn(r) for r in rivals[:2]))}"
                f"和它吃同一类相邻地形——最好的那格先给主线区域,不要互相稀释。"
            )
    elif spec.yield_type == "" and used < limit:
        out.append(
            f"💡 {spec.reasons[0]}" if spec.reasons else "💡 无突出地形,按需建设。"
        )

    # 4. 已建区域的建筑链
    for d in built:
        chain = building_chain(d)
        if not chain:
            continue
        seq = "→".join(f"{n}({c})" for c, n in chain[:3])
        out.append(f"· {district_cn(d)}建筑链:{seq}——前两级性价比最高,别停在空区域。")
        break  # 一条就够,不刷屏

    out.append("   (相邻分为最优落位的上限估算;槽位公式为机制常量,实机可验)")
    return out


@lru_cache(maxsize=8)
def _terrain_rivals(dtype: str) -> tuple[str, ...]:
    """和 dtype 抢同一种地形相邻的其他专业区域(数据驱动的互斥提示)。"""
    mine = {
        r.get("AdjacentTerrain")
        for r in _adjacency_rules().get(dtype, ())
        if r.get("AdjacentTerrain")
    }
    if not mine:
        return ()
    out = []
    std = set(standard_district_for_yield().values())
    for other, rules in _adjacency_rules().items():
        if other == dtype or other not in std:
            continue
        theirs = {r.get("AdjacentTerrain") for r in rules if r.get("AdjacentTerrain")}
        if mine & theirs:
            out.append(other)
    return tuple(sorted(out))


# ===========================================================================
# 任务 S:后期产能叠加(区域效应)—— 追加,上方已有代码未动一行
#
# 工厂/发电厂给【辐射范围内所有城市】加产能,娱乐中心给宜居度。三座城
# 挤在一个工业区周围,等于每座城白捡一份产出 —— 人类高手会刻意让城市群
# 围绕工业区布局,AI 不知道这回事。这是纯几何计算。
#
# ⚠️ 数值边界:
#   · 工厂 +3 / 发电厂 +4 的**具体数值不在导出表里**。Buildings.json 只有
#     Cost/PrereqTech/PrereqDistrict;辐射产出列(Building_YieldChanges 的
#     区域效应部分)未被导出脚本带出。数值引自计划书,标注为未导出。
#   · 辐射半径 6 格是 [机制常量](同样未导出)。间接佐证:导出表里存在
#     MINOR_CIV_TORONTO_REGIONAL_RANGE_BONUS(Amount=3,区域辐射范围 +3)
#     和 GREATPERSON_EXTRA_REGIONAL_BUILDING_ENTERTAINMENT —— 说明"辐射
#     范围"这个机制确实存在且可被修改,只是基数没导出。
# ===========================================================================

REGIONAL_RANGE = 6  # [机制常量] 区域效应辐射半径
MIN_CLUSTER = 2  # 少于 2 座城共享就不值得特意布局

# [计划书值,未导出] 辐射建筑 → (中文名, 每城收益描述)
_AREA_BUILDINGS = (
    ("BUILDING_FACTORY", "产能", "+3 产能/城"),
    ("BUILDING_POWER_PLANT", "产能", "再 +4 产能/城"),
)


def _hex_dist(a: dict, b: dict) -> int:
    try:
        return hex_distance(int(a["x"]), int(a["y"]), int(b["x"]), int(b["y"]))
    except (KeyError, TypeError, ValueError):
        return 99


def find_area_clusters(cities: list[dict]) -> list[tuple[dict, list[dict]]]:
    """以每座城为候选枢纽,找辐射范围内的城市群。按覆盖数降序,已去重。"""
    valid = [
        c
        for c in (cities or [])
        if isinstance(c, dict) and c.get("x") is not None and c.get("y") is not None
    ]
    clusters: list[tuple[dict, list[dict]]] = []
    for hub in valid:
        covered = [c for c in valid if _hex_dist(hub, c) <= REGIONAL_RANGE]
        if len(covered) >= MIN_CLUSTER:
            clusters.append((hub, covered))
    clusters.sort(key=lambda p: -len(p[1]))
    # 去重:覆盖集合相同的只留第一个(枢纽可互换时不重复报)
    seen: set[frozenset] = set()
    out = []
    for hub, covered in clusters:
        key = frozenset(
            str(c.get("name") or f"{c.get('x')},{c.get('y')}") for c in covered
        )
        if key in seen:
            continue
        seen.add(key)
        out.append((hub, covered))
    return out


def get_area_effect_advice(cities: list[dict], max_items: int = 2) -> list[str]:
    """区域效应共享建议。没有可共享的城市群时返回空列表,不抛异常。

    cities: [{"name","x","y","districts":[已建 DistrictType]}]
    """
    try:
        clusters = find_area_clusters(cities)
        if not clusters:
            return []
        out = ["== 区域效应共享 =="]
        for hub, covered in clusters[:max_items]:
            names = "、".join(
                str(c.get("name") or f"({c.get('x')},{c.get('y')})") for c in covered
            )
            hub_name = str(hub.get("name") or f"({hub.get('x')},{hub.get('y')})")
            has_iz = any(
                has_district(
                    c.get("districts"), "DISTRICT_INDUSTRIAL_ZONE", "DISTRICT_HANSA"
                )
                for c in covered
            )
            n = len(covered)
            line = (
                f"· {names} 共 {n} 座城互相在 {REGIONAL_RANGE} 格内 —— "
                f"在 {hub_name} 建工业区+工厂,这 {n} 座城【每座】都吃到加成。"
            )
            if has_iz:
                line += "该城市群已有工业区,补工厂即可激活辐射。"
            else:
                line += "目前群内还没有工业区——这是后期产能的地基。"
            out.append(line)
            out.append(
                f"   收益量级:工厂 +3 产能/城、发电厂再 +4/城(计划书值,"
                f"未导出)→ {n} 座城合计约 +{3 * n}~+{7 * n} 产能/回合。"
            )
            out.append(
                f"   娱乐中心同理:辐射范围内每城 +1 宜居度(数值未导出),"
                f"{n} 座城共享一个娱乐中心比各建各的省 {n - 1} 个区域槽。"
            )
        if len(clusters) > max_items:
            out.append(f"   (另有 {len(clusters) - max_items} 组可共享的城市群)")
        return out
    except Exception:
        return []


# ===========================================================================
# 空区域检测(2026-08-07,用户点出"科技是不是太慢了"之后查出来的)
#
# 实机数据(苏美尔局 T169):
#   乌鲁克  学院+图书馆+大学  → 科研 18
#   西巴尔  学院,里面空的     → 科研 7
#   埃利都  学院,里面空的     → 科研 6
# 科研 48/回合 vs 美国 82,差距的一大半就在这两个空壳上。
#
# 区域是【容器】,建筑才是【产出】。花 54 锤把学院盖起来然后不放图书馆,
# 等于把锤子埋在地里。而全仓此前【没有任何一处】检查已建区域里的建筑
# 是否跟上 —— district_planner 里那句"别停在空区域"是在【建议建区域】
# 时说的,对已经建好的空壳一个字都没有。
#
# 判据全部来自 gamedata 的 Buildings.PrereqDistrict + PrereqTech/PrereqCivic,
# 不写死建筑名 —— 换版本、换规则集都得成立。只报【当前科技/市政已经解锁】
# 的建筑,没解锁的不算欠账。
# ===========================================================================


@lru_cache(maxsize=1)
def _district_buildings() -> dict[str, tuple[tuple[str, str, str], ...]]:
    """区域 -> ((建筑, 前置科技, 前置市政), ...)。按 Cost 升序(先便宜的)。"""
    out: dict[str, list[tuple[int, str, str, str]]] = {}
    for r in _rows("Buildings"):
        dist = str(r.get("PrereqDistrict") or "")
        btype = str(r.get("BuildingType") or "")
        if not dist or not btype:
            continue
        if str(r.get("IsWonder") or "").lower() in ("true", "1"):
            continue
        try:
            cost = int(r.get("Cost") or 0)
        except (TypeError, ValueError):
            cost = 0
        out.setdefault(dist, []).append(
            (
                cost,
                btype,
                str(r.get("PrereqTech") or ""),
                str(r.get("PrereqCivic") or ""),
            )
        )
    return {d: tuple((b, t, c) for _, b, t, c in sorted(v)) for d, v in out.items()}


# 区域对各胜利路线的权重。**不能"看到空区域就催你补"** ——
# 2026-08-07 用户点出:"文化宗教呢但是不能一起都建要看你想怎么胜利"。
# 无差别催建会把人推去补圣地建筑,而这局宗教路线在 T130 就已经关闭
# (4 个宗教位全满,再也出不了大预言家)。那不是提醒,是噪音加误导。
#
# 权重只分三档:2=这条路线的核心产出,1=有用但不是重点,0=对这条路线无关。
# 0 的不进建议 —— 锤子有限,补一个用不上的建筑就是少一个能用的。
_VICTORY_DISTRICT_WEIGHT: dict[str, dict[str, int]] = {
    # 统治拼的是代差和后勤:科技(更强的兵)、产能(造得出来)、金币(买得起/升得动)
    "DOMINATION": {
        "DISTRICT_CAMPUS": 2,
        "DISTRICT_INDUSTRIAL_ZONE": 2,
        "DISTRICT_ENCAMPMENT": 2,
        "DISTRICT_COMMERCIAL_HUB": 2,
        "DISTRICT_HARBOR": 1,
        "DISTRICT_THEATER": 1,  # 只为解锁军事市政/政策卡,不为旅游
        "DISTRICT_HOLY_SITE": 0,
        "DISTRICT_ENTERTAINMENT_COMPLEX": 1,
    },
    "SCIENCE": {
        "DISTRICT_CAMPUS": 2,
        "DISTRICT_INDUSTRIAL_ZONE": 2,
        "DISTRICT_COMMERCIAL_HUB": 1,
        "DISTRICT_THEATER": 1,
        "DISTRICT_HOLY_SITE": 0,
        "DISTRICT_ENCAMPMENT": 0,
        "DISTRICT_HARBOR": 1,
        "DISTRICT_ENTERTAINMENT_COMPLEX": 1,
    },
    "CULTURE": {
        "DISTRICT_THEATER": 2,
        "DISTRICT_ENTERTAINMENT_COMPLEX": 2,
        "DISTRICT_CAMPUS": 1,
        "DISTRICT_COMMERCIAL_HUB": 1,
        "DISTRICT_INDUSTRIAL_ZONE": 1,
        "DISTRICT_HOLY_SITE": 1,
        "DISTRICT_ENCAMPMENT": 0,
        "DISTRICT_HARBOR": 1,
    },
    "RELIGION": {
        "DISTRICT_HOLY_SITE": 2,
        "DISTRICT_CAMPUS": 1,
        "DISTRICT_COMMERCIAL_HUB": 1,
        "DISTRICT_INDUSTRIAL_ZONE": 1,
        "DISTRICT_THEATER": 1,
        "DISTRICT_ENCAMPMENT": 0,
        "DISTRICT_HARBOR": 1,
        "DISTRICT_ENTERTAINMENT_COMPLEX": 1,
    },
}


def district_weight(dtype: str, victory_type: str) -> int:
    """该区域对这条胜利路线的权重。未锁定路线时一律返回 1(不做取舍)。"""
    if not victory_type:
        return 1
    table = _VICTORY_DISTRICT_WEIGHT.get(victory_type.upper())
    if not table:
        return 1
    return table.get(dtype, 1)


def empty_district_advice(
    cities: list[dict],
    researched_techs: set[str] | None = None,
    completed_civics: set[str] | None = None,
    victory_type: str = "",
    max_items: int = 3,
) -> list[str]:
    """已建区域里缺哪些【已解锁且对当前胜利路线有用】的建筑。

    没有欠账返回空列表。cities 每项需要 name / districts / buildings。
    districts 允许 "DISTRICT_CAMPUS@59,29" 这种带坐标的形式。

    ⚠️ 权重为 0 的区域不进建议 —— 锤子有限,补一个用不上的建筑
    就是少一个能用的。未锁定胜利路线时不做取舍,全部按同权重报。
    """
    techs = researched_techs or set()
    civics = completed_civics or set()
    table = _district_buildings()
    if not table:
        return []
    rows: list[tuple[int, int, str]] = []
    try:
        for c in cities or []:
            name = str(c.get("name") or "?")
            # 两边前缀必须先统一再比:city 的 buildings 列表里前缀已被剥掉
            # ("WORKSHOP"),而 chain 里是全名("BUILDING_WORKSHOP")。
            # 2026-08-11 T284 实锤:旧代码直接拿全名去 in 一个短名集合,
            # 于是【每一栋已建的建筑都被判成缺失】—— 罗马和奥斯蒂亚明明
            # 都有工坊和工厂,简报却报"INDUSTRIAL_ZONE 是空的,缺 WORKSHOP、
            # FACTORY"。作者在【输出】那行写了 replace("BUILDING_", ""),
            # 说明知道两边格式不同,却没在【比较】那行也统一。
            # 这条一直在错,只是长期被"低优先级建议已省略"盖住,直到
            # SCIENCE 路线把工业区权重提到【核心】才浮上来。
            have = {
                str(b).split("@")[0].replace("BUILDING_", "")
                for b in (c.get("buildings") or [])
            }
            for d in c.get("districts") or []:
                dtype = str(d).split("@")[0]
                chain = table.get(dtype)
                if not chain:
                    continue
                w = district_weight(dtype, victory_type)
                if w <= 0:
                    continue  # 这条路线用不上,不占建议位
                missing = []
                for btype, ptech, pcivic in chain:
                    short = btype.replace("BUILDING_", "")
                    if short in have:
                        continue
                    # 前置没到 = 不是欠账,是还没轮到
                    if ptech and ptech not in techs:
                        continue
                    if pcivic and pcivic not in civics:
                        continue
                    missing.append(short)
                if missing:
                    tag = "【核心】" if w >= 2 else ""
                    rows.append(
                        (
                            w,
                            len(missing),
                            f"{tag}{name} 的 {dtype.replace('DISTRICT_', '')} 是空的 —— "
                            f"缺 {'、'.join(missing)}(都已解锁)",
                        )
                    )
    except Exception:
        return []
    if not rows:
        return []
    rows.sort(key=lambda t: (-t[0], -t[1]))
    head = "有区域盖好了却空着 —— 区域是容器,建筑才是产出。"
    if victory_type:
        head += f"下面只列对 {victory_type} 路线有用的(用不上的区域已过滤):"
    else:
        head += "花几十锤把区域立起来然后不放建筑,等于把锤子埋在地里:"
    out = [head]
    out.extend("   · " + t for _, _, t in rows[:max_items])
    if len(rows) > max_items:
        out.append(f"   (另有 {len(rows) - max_items} 个空区域)")
    return out
