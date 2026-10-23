"""任务 C —— 战前评估 + 攻城战术模板 + 战后处理。

safety.py 管的是"别送死"(防御拦截);这里管的是"能不能打、怎么打"。
模型在 T52 那局从头到尾不知道自己打不打得动一座城 —— 这个模块给它
一个战前的确定性判断。

## 与所有其他模块的根本区别:一切数值用范围,不报精确值

文明 6 的战斗有 ±25% 伤害随机,再叠地形/夹击/支援修正,战斗公式不完全
可知。所以本模块的输出原则:
    · 回合数永远是区间("通常 2-4 回合"),不是单值
    · 伤害永远是定性+范围("微量"/"约 30-50"),不是公式拟合
    · 结论是四档置信度,不是胜率百分数 —— 差距 <5 战力视为五五开,
      ±25% 随机已内化在分档阈值里(宁可保守,不虚报必胜)

四档:
    🟢 可打    预期损失 ≤1 单位,通常 2-4 回合破城
    🟡 需准备  流程齐但要按部就班(削墙/站位/耗守军),通常 3-6 回合
    🟠 不推荐  缺关键兵种或战力差 ≥5 —— 打了预期损失 ≥3 单位
    🔴 会全灭  纯近战啃墙 / 战力差 ≥10 / 兵力不足 —— 别打

## 数据来源与边界(全部动态加载,不硬编码单位名)

    Units.json            98 单位:Combat/RangedCombat/Bombard/Range/PrereqTech
    TypeTags.json         兵种分类。⚠️ 陷阱:这张表同时给单位和能力打标签
                          (CLASS_SIEGE 下 20 个 ABILITY_ 和 5 个 UNIT_),
                          取单位必须按 Units 表过滤,否则分类全错。
    UnitAbilityModifiers  反骑兵 +10 是导出值(ANTI_CAVALRY_COMBAT_BONUS
                          Amount=10);其加成的适用条件在未导出的判定集里,
                          按能力名推断并标注。
    Buildings.json        城墙三级按 Name 含"城墙"动态发现:
                          远古(80/砖石工艺)→中世纪(225/城堡)→文艺复兴
                          (305/攻城战术)。**防御数值不在表里** —— 城防值
                          只能用实时查询的 defense_strength,查不到就写未知。
    CLASS_SIEGE_SETUP     攻城单位"需架设"标签(导出值)→ 战术注意事项。

    ❌ 攻城单位对城墙的伤害乘数未导出 —— 文案用"保守估计约 ×3(未导出)"。
    ❌ 近战对城墙的削减系数未导出 —— 定性表述"只能造成微量伤害"。
    ❌ 总督/驻军的忠诚数值未导出 —— 战后建议标注为经验值。

## game_state 输入契约(dict,字段与 lua/models 的 CityInfo/UnitInfo/
   safety.EnemySighting 对齐;全部可缺省,缺什么降级什么)

    target: {"name","population","defense_strength","wall_hp","wall_max_hp",
             "wall_building"(可选,BUILDING_WALLS 等)}
    my_units:   [{"type","x","y","hp"(0-100,可选)}...]
    enemy_units:[{"unit_type"或"type","cs"(可选,缺省查表),"x","y"}...]
    my_cities:  [{"x","y"}...]           # 算补给线
    my_techs:   set[str]                 # 替代方案里的解锁判断
    own_cities/enemy_cities(带 population) # post_war_advice 的忠诚计算
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from civ_mcp.hexgrid import hex_distance

_GAMEDATA = gamedata_path()

SUPPLY_WARN_DIST = 8  # 补给线超过 8 格就警告
CS_EVEN_BAND = 5  # 差距 <5 战力 = 五五开(±25% 随机内化)
CS_HOPELESS_GAP = 10  # 差距 ≥10 = 打不动

TIER_GREEN = "🟢 可打"
TIER_YELLOW = "🟡 需准备"
TIER_ORANGE = "🟠 不推荐"
TIER_RED = "🔴 会全灭"


def _rows(table: str) -> list[dict[str, str]]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@lru_cache(maxsize=1)
def _unit_stats() -> dict[str, dict]:
    out = {}
    for r in _rows("Units"):
        key = r.get("UnitType")
        if not key:
            continue

        def _i(col, r=r):
            try:
                return int(r.get(col) or 0)
            except (TypeError, ValueError):
                return 0

        out[key] = {
            "name": r.get("Name") or key,
            "cs": _i("Combat"),
            "rcs": _i("RangedCombat"),
            "bombard": _i("Bombard"),
            "range": _i("Range"),
            "moves": _i("BaseMoves"),
            "tech": r.get("PrereqTech") or "",
            "cost": _i("Cost"),
        }
    return out


@lru_cache(maxsize=1)
def _unit_tags() -> dict[str, frozenset[str]]:
    """UnitType → 标签集。⚠️ TypeTags 同时标注能力 —— 按 Units 表过滤。"""
    units = set(_unit_stats())
    out: dict[str, set[str]] = {}
    for r in _rows("TypeTags"):
        t = r.get("Type")
        if t in units:
            out.setdefault(t, set()).add(r.get("Tag") or "")
    return {k: frozenset(v) for k, v in out.items()}


def classify_unit(unit_type: str) -> str:
    """兵种分组:siege / ranged / melee / anti_cav / cavalry / other。"""
    tags = _unit_tags().get(unit_type, frozenset())
    if "CLASS_SIEGE" in tags:
        return "siege"
    if "CLASS_RANGED" in tags or "CLASS_RANGED_CAVALRY" in tags:
        return "ranged"
    if "CLASS_ANTI_CAVALRY" in tags:
        return "anti_cav"
    if (
        "CLASS_LIGHT_CAVALRY" in tags
        or "CLASS_HEAVY_CAVALRY" in tags
        or "CLASS_HEAVY_CHARIOT" in tags
    ):
        return "cavalry"
    if "CLASS_MELEE" in tags:
        return "melee"
    return "other"


def needs_setup(unit_type: str) -> bool:
    """攻城单位需架设才能射(CLASS_SIEGE_SETUP,导出标签)。"""
    return "CLASS_SIEGE_SETUP" in _unit_tags().get(unit_type, frozenset())


# 远程单位对城墙伤害减半,远程强度不够时基本磨不动。60 = 野战加农炮档:
# T335 校准局实测,弩手(40)21 回合磨不动文艺复兴墙,野战加农炮(60)可以。
WALL_BREAKER_MIN_RCS = 60


def unit_maintenance(unit_type: str) -> int:
    """单位每回合维护费(金)。Units.Maintenance,导出值;查不到返回 0。"""
    for r in _rows("Units"):
        if r.get("UnitType") == unit_type:
            try:
                return int(r.get("Maintenance") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


# 兵种克制。**只编码"谁克谁"这条类别规则,不编码加成数值** ——
# 具体加成(远古 +10,随时代变)没有导出,而游戏自己的战斗估算算得对
# (实测长矛对战车显示 CS35 而非 25)。所以这里只负责在【决定打之前】
# 提醒方向,数值仍以战斗估算为准。
#
# 2026-08-07 苏美尔局实机:assess_attack 只评估城市,单位对单位的克制
# 一个字都没有。用户点名的"军队编成/全前排全后排"就是这个缺口 ——
# 系统按单位【数量】算编成,不按【兵种职能与克制】算。
_COUNTERED_BY = {
    "cavalry": ("anti_cav", "反骑兵单位对骑兵有显著战力加成"),
    "light_cavalry": ("anti_cav", "反骑兵单位对骑兵有显著战力加成"),
}


def counter_warning(attacker_type: str, defender_type: str) -> str:
    """攻方是否撞在守方的克制面上。没有克制关系返回空串。"""
    atk = classify_unit(attacker_type)
    entry = _COUNTERED_BY.get(atk)
    if not entry:
        return ""
    counter_class, why = entry
    if classify_unit(defender_type) != counter_class:
        return ""
    return (
        f"⚠️ 兵种克制:你用【{atk}】打【{counter_class}】—— {why}。"
        f"换近战单位打,或先用远程磨血;数值以下面的战斗估算为准。"
    )


# ---------------------------------------------------------------------------
# 军队编成(2026-08-07,用户点名的三大战略缺陷之一:"全是前排全是后排")
#
# 系统一直只数【军力值】和【单位数量】,从不看兵种构成。实机后果:
#   · 苏美尔局 T116:4 个战车 + 0 个远程。战车打不掉一条 45 血的蛮族桨帆船
#     (跨域打不到),也磨不动任何城墙 —— 军力 138"看起来在涨",实际
#     这支部队什么都办不成。
#   · T362 罗马局反面:7 加农炮 + 6 弩手 + 只有 2 个近战 → 打不下任何城。
#
# 两头都是同一个病:纸面军力是标量,战斗力是向量。
#
# 判据全部来自 gamedata 标签(classify_unit),不写死单位名 ——
# 换文明、换时代、换 UU 都得成立。比例阈值是经验值,标注清楚。
# ---------------------------------------------------------------------------

# 一支能独立作战的部队至少要有的两条腿:
#   前排(melee/anti_cav/cavalry)—— 承伤、占城
#   后排(ranged/siege)         —— 输出、拆墙
# 低于这个比例就是"只有一条腿"。3 个单位以下不算部队,不报。
FRONTLINE_CLASSES = frozenset({"melee", "anti_cav", "cavalry"})
BACKLINE_CLASSES = frozenset({"ranged", "siege"})
COMPOSITION_MIN_UNITS = 3
COMPOSITION_MIN_SHARE = 0.25  # 经验值:任一条腿低于 25% 视为跛脚


def composition_gap(unit_types: list[str]) -> str:
    """军队编成体检。构成健康返回空串。

    只判【有没有这条腿】,不判该造多少个 —— 造几个是决策,
    缺一整个职能是事实。
    """
    types = [u for u in (unit_types or []) if u]
    if len(types) < COMPOSITION_MIN_UNITS:
        return ""
    buckets: dict[str, list[str]] = {}
    for u in types:
        buckets.setdefault(classify_unit(u), []).append(u)
    front = sum(len(v) for k, v in buckets.items() if k in FRONTLINE_CLASSES)
    back = sum(len(v) for k, v in buckets.items() if k in BACKLINE_CLASSES)
    total = front + back
    if total < COMPOSITION_MIN_UNITS:
        return ""  # 全是侦察/平民类,不是作战部队
    f_share, b_share = front / total, back / total
    if f_share >= COMPOSITION_MIN_SHARE and b_share >= COMPOSITION_MIN_SHARE:
        return ""
    if back == 0:
        return (
            f"⚠️ 军队编成:{total} 个战斗单位【全是前排】(近战/骑兵/反骑),0 个远程或攻城。"
            f"这支部队打不了城(近战对城墙只有微量伤害),也打不到水面单位。"
            f"缺的是【远程】—— 优先补到约 1/3。"
        )
    if front == 0:
        return (
            f"⚠️ 军队编成:{total} 个战斗单位【全是后排】(远程/攻城),0 个近战。"
            f"远程把城打到 0 血也占不了 —— 没有近战去收,只会夷平或原地僵持"
            f"(T362 实证:7 加农炮 + 6 弩手 + 2 近战 = 一座城都没打下)。"
            f"缺的是【近战/反骑】—— 它们同时是承伤面。"
        )
    lack = "远程/攻城" if b_share < COMPOSITION_MIN_SHARE else "近战/反骑/骑兵"
    return (
        f"⚠️ 军队编成失衡:前排 {front} / 后排 {back}"
        f"(前 {f_share:.0%} 后 {b_share:.0%})。缺的是【{lack}】。"
        f"纸面军力是标量,战斗力是向量 —— 少一条腿的部队,军力再高也办不成事。"
    )


def unit_base_cost(unit_type: str) -> int:
    """单位【基础】产能成本。Units.Cost,导出值;查不到返回 0。

    ⚠️ 只是基础值。开拓者的实际成本随已有城市数逐城上浮(游戏规则),
    所以拿它算 ETA 时必须注明"基础值,实际随城市数上浮" ——
    报一个偏乐观的 ETA 比不报更坏:agent 会照着它安排扩张节奏。
    """
    for r in _rows("Units"):
        if r.get("UnitType") == unit_type:
            try:
                return int(r.get("Cost") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


# 军事维护占毛收入的红线(T362 复盘的经济纪律)。超过这条线还买兵,
# 就是在给自己制造下一回合的破产。
MILITARY_UPKEEP_CEILING = 0.6


def can_break_walls(unit_type: str) -> bool:
    """该单位能否有效损伤城墙(宣战门禁 NO_SIEGE_VS_WALLS 用)。

    算数的:攻城系(CLASS_SIEGE,对墙全额伤害);海军远程
    (CLASS_NAVAL_RANGED,风云起对城防全额伤害 —— 舰炮拆沿海城是标准
    打法);轰炸机系(CLASS_AIR_BOMBER,Bombard 110 起步);攻城塔/攻城槌
    (让近战绕过/直接打墙);远程强度 ≥60 的陆军远程(减半后仍有磨墙力)。
    近战/骑兵对墙只有微量伤害,不算。
    """
    tags = _unit_tags().get(unit_type, frozenset())
    if tags & {"CLASS_SIEGE", "CLASS_NAVAL_RANGED", "CLASS_AIR_BOMBER"}:
        return True
    ut = (unit_type or "").upper()
    if "BATTERING_RAM" in ut or "SIEGE_TOWER" in ut:
        return True
    s = _unit_stats().get(unit_type)
    return bool(
        s and classify_unit(unit_type) == "ranged" and s["rcs"] >= WALL_BREAKER_MIN_RCS
    )


# ---------------------------------------------------------------------------
# 军队成色:时代与对空(2026-08-05 用户点名"作战不能只看纸面数值")
# T335 实证:军力 1137"全场第一",拆开是 13 个中世纪弩手 —— 纸面分数
# 按数量堆,完全看不见代差;阿拉伯一架战斗机(CS100)就压制了整个后方。
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _era_chronology() -> dict[str, int]:
    """ERA_* → 时代序号(远古=1 … 信息=8),Eras.json 动态加载。"""
    out: dict[str, int] = {}
    for r in _rows("Eras"):
        try:
            out[r.get("EraType") or ""] = int(r.get("ChronologyIndex") or 0)
        except (TypeError, ValueError):
            continue
    return out


@lru_cache(maxsize=1)
def _era_cn() -> dict[str, str]:
    """ERA_* → 本地化时代名。"""
    return {
        (r.get("EraType") or ""): (r.get("Name") or r.get("EraType") or "")
        for r in _rows("Eras")
    }


@lru_cache(maxsize=1)
def _tech_era() -> dict[str, str]:
    """TechnologyType → EraType。"""
    return {
        (r.get("TechnologyType") or ""): (r.get("EraType") or "")
        for r in _rows("Technologies")
        if r.get("TechnologyType")
    }


def unit_era(unit_type: str) -> tuple[int, str]:
    """单位所属时代 (序号, ERA_*)。无前置科技的开局单位算远古;查不到 (0, "")。"""
    s = _unit_stats().get(unit_type)
    if not s:
        return 0, ""
    tech = s.get("tech") or ""
    if not tech:
        return _era_chronology().get("ERA_ANCIENT", 1), "ERA_ANCIENT"
    era = _tech_era().get(tech, "")
    return _era_chronology().get(era, 0), era


def army_era_profile(unit_types: list[str]) -> dict | None:
    """军事单位的时代画像:{'median', 'oldest', 'total', 'lagging'}。

    lagging = (时代序号, 数量) 按数量降序 —— 报告"主力是什么时代的兵"。
    全部查不到时返回 None(缺数据静默,不硬凑)。
    """
    idxs: list[int] = []
    for ut in unit_types:
        i, _e = unit_era(ut)
        if i > 0:
            idxs.append(i)
    if not idxs:
        return None
    idxs.sort()
    counts: dict[int, int] = {}
    for i in idxs:
        counts[i] = counts.get(i, 0) + 1
    return {
        "median": idxs[len(idxs) // 2],
        "oldest": idxs[0],
        "total": len(idxs),
        "lagging": sorted(counts.items(), key=lambda kv: -kv[1]),
    }


def era_index(era_type: str) -> int:
    """ERA_* → 序号;未知 0。"""
    return _era_chronology().get((era_type or "").upper(), 0)


def era_cn(idx_or_type) -> str:
    """时代序号或 ERA_* → 本地化名;查不到原样返回。"""
    if isinstance(idx_or_type, int):
        for et, i in _era_chronology().items():
            if i == idx_or_type:
                return _era_cn().get(et, et)
        return f"时代#{idx_or_type}"
    return _era_cn().get(idx_or_type, str(idx_or_type))


# 能进城占领的兵种(机制硬约束,非经验值):远程/攻城/海军/空军/支援
# 都只能把城打到 0 血,**必须由地面近战系单位进城才算占领**。
# ⚠️ 别用 Units.CanCapture 判占领:那是"能否俘获平民",加农炮/轰炸机/护卫舰都 true。
# T335 实证:13 弩手 + 4 野战加农炮的军队,即使把城磨到 0 血也拿不下,
# 而 0 血城被远程再打一次是「夷平」不是「占领」(见 attack 的夷平警告)。
_CAPTURE_TAGS = frozenset(
    {
        "CLASS_MELEE",
        "CLASS_ANTI_CAVALRY",
        "CLASS_LIGHT_CAVALRY",
        "CLASS_HEAVY_CAVALRY",
        "CLASS_HEAVY_CHARIOT",
    }
)


def can_capture_city(unit_type: str) -> bool:
    """该单位能否进城占领。远程/攻城/海军/空军一律不能。"""
    return bool(_unit_tags().get(unit_type, frozenset()) & _CAPTURE_TAGS)


_AIR_TAGS = frozenset({"CLASS_AIR_FIGHTER", "CLASS_AIR_BOMBER", "CLASS_AIRCRAFT"})


def is_aircraft(unit_type: str) -> bool:
    return bool(_unit_tags().get(unit_type, frozenset()) & _AIR_TAGS)


def can_anti_air(unit_type: str) -> bool:
    """能打飞机:防空类(CLASS_ANTI_AIR,含战列舰/导弹巡洋舰)或战斗机(拦截)。"""
    tags = _unit_tags().get(unit_type, frozenset())
    return "CLASS_ANTI_AIR" in tags or "CLASS_AIR_FIGHTER" in tags


@lru_cache(maxsize=1)
def wall_chain() -> tuple[dict, ...]:
    """城墙三级,按 Cost 升序动态发现(Name 含"城墙")。防御值不在表里。"""
    walls = [
        {
            "type": r["BuildingType"],
            "name": r.get("Name") or r["BuildingType"],
            "cost": int(r.get("Cost") or 0),
            "tech": r.get("PrereqTech") or "",
        }
        for r in _rows("Buildings")
        if "城墙" in (r.get("Name") or "")
    ]
    return tuple(sorted(walls, key=lambda w: w["cost"]))


@lru_cache(maxsize=1)
def anti_cavalry_bonus() -> int:
    """反骑兵加成,从 UnitAbilityModifiers → ModifierArguments 反查。
    查不到返回 0(文案里就不提数值)。"""
    try:
        mids = {
            r.get("ModifierId")
            for r in _rows("UnitAbilityModifiers")
            if "ANTI_CAVALRY" in (r.get("UnitAbilityType") or "")
        }
        for r in _rows("ModifierArguments"):
            if r.get("ModifierId") in mids and r.get("Name") == "Amount":
                return int(r.get("Value") or 0)
    except Exception:
        pass
    return 0


def _dist(x1, y1, x2, y2) -> int:
    """Hex distance between two plots (Civ6 odd-r grid)."""
    return hex_distance(x1, y1, x2, y2)


# ---------------------------------------------------------------------------
# 兵力解析
# ---------------------------------------------------------------------------


@dataclass
class _Force:
    """一方兵力的汇总。"""

    groups: dict  # 分组 → [(unit_type, 有效战力)]
    best_melee_cs: int = 0
    best_cs: int = 0
    count: int = 0

    def n(self, group: str) -> int:
        return len(self.groups.get(group, ()))


def _effective_cs(unit_type: str) -> int:
    s = _unit_stats().get(unit_type)
    if not s:
        return 0
    return max(s["cs"], s["rcs"], s["bombard"])


def _parse_force(units: list) -> _Force:
    groups: dict[str, list] = {}
    best_melee = best = 0
    count = 0
    for u in units or []:
        if not isinstance(u, dict):
            continue
        ut = str(u.get("type") or u.get("unit_type") or "")
        group = classify_unit(ut)
        if group == "other":
            continue
        try:
            cs = int(u.get("cs") or 0) or _effective_cs(ut)
        except (TypeError, ValueError):
            cs = _effective_cs(ut)
        if cs <= 0:
            continue
        groups.setdefault(group, []).append((ut, cs))
        count += 1
        best = max(best, cs)
        if group in ("melee", "anti_cav", "cavalry"):
            best_melee = max(best_melee, cs)
    return _Force(groups=groups, best_melee_cs=best_melee, best_cs=best, count=count)


def _unit_cn(unit_type: str) -> str:
    s = _unit_stats().get(unit_type)
    return s["name"] if s else unit_type


def _force_brief(f: _Force) -> str:
    cn = {
        "melee": "近战",
        "ranged": "远程",
        "siege": "攻城",
        "cavalry": "骑兵",
        "anti_cav": "抗骑",
    }
    parts = []
    for g in ("melee", "anti_cav", "cavalry", "ranged", "siege"):
        items = f.groups.get(g)
        if items:
            top = max(cs for _, cs in items)
            parts.append(f"{cn[g]}×{len(items)}(至{top})")
    return " ".join(parts) if parts else "无战斗单位"


# ---------------------------------------------------------------------------
# 城防数学(2026-08-10 伦敦围城后新增)
# ---------------------------------------------------------------------------

CITY_HEAL_PER_TURN = 20  # 实测(罗马局 T176-191 伦敦):城市每回合回血约 20
_DMG_BASE = 24  # 与 lua/units.py 的战斗估算同源公式
CITY_SLOW_TURNS = 4  # 破城超过这么多回合就不配绿灯
CITY_GRIND_TURNS = 8  # 超过这么多回合是纯耗人


def city_hit_damage(attacker_cs: int, city_defense: int) -> int:
    """单次攻击对城市城防的预期伤害。

    公式与 lua/units.py 的战斗估算同源(24 × 10^(Δ/30)),截到 [0,100]。
    实测对照(罗马局 T177-191):火枪手 CS55 打伦敦城防 ~80 → Δ=-25 →
    本式给 4 伤;实机 5 次攻击(4 近战 + 1 炮击)总共只掉 17 血,
    约 3.4/次 —— 吻合。用基础战力算是【保守】的:夹击/支援/晋升
    能再加 5-15 有效战力,但那些加成不该成为"能不能打"的立论基础。
    """
    if attacker_cs <= 0 or city_defense <= 0:
        return 0
    dmg = _DMG_BASE * (10 ** ((attacker_cs - city_defense) / 30))
    return max(0, min(100, int(round(dmg))))


def city_assault_math(
    mine: _Force, city_defense: int | None, garrison_hp: int | None
) -> dict | None:
    """全军对城的每回合净伤 & 破城回合数。城防未知返回 None。

    每个战斗单位每回合出手一次;城市每回合回 CITY_HEAL_PER_TURN。
    净伤 ≤0 就是"数学上打不动"——伦敦就是这一档(4 次近战 1 次炮击
    只把 200 血打掉 17,还被回满)。
    """
    if not city_defense or city_defense <= 0:
        return None
    per_turn = 0
    for items in mine.groups.values():
        for _ut, cs in items:
            per_turn += city_hit_damage(cs, city_defense)
    net = per_turn - CITY_HEAL_PER_TURN
    hp = garrison_hp if (garrison_hp and garrison_hp > 0) else 200
    turns = None if net <= 0 else max(1, int(-(-hp // net)))
    return {
        "raw_per_turn": per_turn,
        "net_per_turn": net,
        "turns": turns,
        "garrison_hp": hp,
        "city_defense": city_defense,
    }


# ---------------------------------------------------------------------------
# 分档(核心判断,独立可测)
# ---------------------------------------------------------------------------


def assess_tier(
    mine: _Force,
    theirs: _Force,
    has_walls: bool,
    wall_broken: bool,
    supply_dist: int | None,
    city_defense: int | None = None,
    garrison_hp: int | None = None,
    land_reachable: bool | None = None,
) -> tuple[str, list[str]]:
    """四档判定 → (档位, 理由)。阈值已内化 ±25% 随机,保守取档。

    2026-08-10 伦敦围城教训:此前只比"我方近战 CS vs 守军【单位】CS",
    城市自己的城防值根本没进判定 —— 无墙 + 守军是弓箭手(CS15)就直接
    绿灯,而城防 80 的 11 人口首都对 CS55 全军是数学上打不动的。
    现在城防未知【不给绿灯】,城防已知则按净伤/回合定档。

    2026-08-11 缺陷105 —— 又是伦敦,同一座城,另一个漏判:陆路可达性。
    T307 工具对伦敦(60,51)给了 🟢「城防 73 下净伤 471/回合,约 1 回合破城」,
    end_turn 主线也把它列成"编成齐了,可以动手"的头号目标。实测 BFS 从我方
    单位所在陆块出发穷举 1603 格(=这块大陆的全部陆地),一个伦敦邻格都到不了
    —— 伦敦在海对面。补给距离用的是六边形直线距离,和"走得到"是两回事。
    火力数学再漂亮,部队站不到射程内就一发都打不出去。
    可达性是【前置条件】,不是加权项,所以排在所有战力计算之前。
    """
    reasons: list[str] = []
    gap = mine.best_melee_cs - theirs.best_cs
    math_ = city_assault_math(mine, city_defense, garrison_hp)

    # —— 红:陆军根本走不到(优先于一切火力数学) ——
    if land_reachable is False:
        return TIER_RED, [
            "陆路走不到:从我方陆军所在陆块做 BFS,穷举可达陆地也到不了这座城的"
            "任何相邻格 —— 它在另一块大陆/岛上,隔着水",
            "陆战单位下水即登船状态:战力归零,任何一击就死。这不是"
            "「多绕几回合」,是走不过去",
            "要打就得先有海运:运输/登陆(需对应科技),或换一个陆路连通的目标"
            "—— 用 get_pathing_estimate 逐个试候选城,能返回回合数的才算数",
        ]

    # —— 红:城防数学上啃不动(优先于一切"看起来能打") ——
    if math_ and math_["turns"] is None:
        return TIER_RED, [
            f"城防 {math_['city_defense']}:你全军一回合合计约 "
            f"{math_['raw_per_turn']} 伤,城市每回合回 {CITY_HEAL_PER_TURN} —— "
            f"净伤 ≤0,打到天荒地老也破不了(罗马局 T177-191 伦敦实证:"
            f"4 次近战 1 次炮击只掉 17 血,还被回满,三个火枪手打残)",
            "先升级兵种/带攻城单位把单击伤害拉过回血线,再来",
        ]

    # —— 红:根本打不动 ——
    if mine.count < 2:
        return TIER_RED, ["可用战斗单位不足 2 个——攻城最少要一削一占"]
    if has_walls and not wall_broken and mine.n("siege") == 0 and mine.n("ranged") == 0:
        return TIER_RED, [
            "有墙 + 你只有近战——近战对完好城墙只能造成微量伤害"
            "(削减系数未导出,机制如此),城防每回合反击,耗也把你耗死"
        ]
    if gap <= -CS_HOPELESS_GAP and theirs.count >= mine.count:
        return TIER_RED, [
            f"守军最强战力比你的最强近战高 {-gap}(≥{CS_HOPELESS_GAP})"
            f"且数量不落下风——±25% 随机救不了代差"
        ]

    # —— 橙:缺关键条件 ——
    if has_walls and not wall_broken and mine.n("siege") == 0 and mine.n("ranged") < 2:
        reasons.append("有墙但攻城单位为 0、远程不足 2——削墙效率撑不起围城")
    if gap <= -CS_EVEN_BAND:
        reasons.append(f"近战战力落后 {-gap}(≥{CS_EVEN_BAND})——换血必亏")
    if (
        supply_dist is not None
        and supply_dist > SUPPLY_WARN_DIST
        and theirs.count >= mine.count
    ):
        reasons.append(
            f"补给线 {supply_dist} 格(>{SUPPLY_WARN_DIST})且守军不弱——"
            f"伤了退不回来,援军也跟不上"
        )
    # —— 橙:城防能磨动但慢得离谱 ——
    if math_ and math_["turns"] and math_["turns"] > CITY_GRIND_TURNS:
        reasons.append(
            f"城防 {math_['city_defense']}:净伤 {math_['net_per_turn']}/回合,"
            f"破城要约 {math_['turns']} 回合——这么久守军援军早到了"
        )
    if reasons:
        return TIER_ORANGE, reasons

    # —— 绿:碾压条件全齐,且城防已知、破城够快 ——
    melee_bodies = mine.n("melee") + mine.n("anti_cav") + mine.n("cavalry")
    green_shape = (
        (not has_walls or wall_broken)
        and gap >= CS_EVEN_BAND
        and melee_bodies >= 2
        and mine.count > theirs.count
    )
    if green_shape and math_ and math_["turns"] and math_["turns"] <= CITY_SLOW_TURNS:
        return TIER_GREEN, [
            f"无墙(或墙已破)+ 近战战力领先 {gap} + 兵力占优;城防 "
            f"{math_['city_defense']} 下净伤 {math_['net_per_turn']}/回合,"
            f"约 {math_['turns']} 回合破城,预期损失 ≤1(±25% 随机,别贴脸站残血)"
        ]

    # —— 黄:能打,按流程来 ——
    why = []
    if green_shape and math_ is None:
        why.append(
            "兵力形态够绿灯,但【城防值读不到】(城不在视野/不可见)——"
            "城防是攻城的第一变量,不看城防的绿灯就是 T177 伦敦那种翻车,"
            "先把城照亮再评估"
        )
    elif green_shape and math_:
        why.append(
            f"城防 {math_['city_defense']}:净伤 {math_['net_per_turn']}/回合,"
            f"约 {math_['turns']} 回合破城——够打但不是速推,按围城流程走"
        )
    if has_walls and not wall_broken:
        why.append("有墙,需要先削墙(远程/攻城)再收尾")
    if abs(gap) < CS_EVEN_BAND:
        why.append(f"战力差 {gap:+d} 在 ±{CS_EVEN_BAND} 内,±25% 随机下是五五开的换血")
    if not why:
        why.append("兵力够但优势不足以速推,按围城流程走")
    why.append("通常 3-6 回合,期间守军援军可能到——盯外圈")
    return TIER_YELLOW, why


# ---------------------------------------------------------------------------
# 战前评估
# ---------------------------------------------------------------------------


def assess_attack(
    target_city_x: int, target_city_y: int, game_state: dict
) -> list[str]:
    """战前评估,≤12 行。信息不足时降级输出,不抛异常。"""
    try:
        return _assess_inner(
            int(target_city_x),
            int(target_city_y),
            game_state if isinstance(game_state, dict) else {},
        )
    except Exception:
        return []


def _wall_status(target: dict) -> tuple[bool, bool, str]:
    """(有墙?, 墙已破?, 描述)。等级已知报等级;只知 wall_max_hp 就说等级未知。"""
    wb = str(target.get("wall_building") or "")
    try:
        wall_max = int(target.get("wall_max_hp") or 0)
        wall_hp = int(target.get("wall_hp") or 0)
    except (TypeError, ValueError):
        wall_max = wall_hp = 0
    if wb:
        info = next((w for w in wall_chain() if w["type"] == wb), None)
        label = info["name"] if info else wb
    elif wall_max > 0:
        label = "有墙(等级未知,按至少远古城墙准备)"
    else:
        return False, False, "无墙"
    broken = wall_max > 0 and wall_hp <= wall_max * 0.25
    if broken:
        label += f",且已残(墙体 {wall_hp}/{wall_max})"
    elif wall_max > 0:
        label += f"(墙体 {wall_hp}/{wall_max})"
    return True, broken, label


def _assess_inner(x: int, y: int, gs: dict) -> list[str]:
    target = gs.get("target") if isinstance(gs.get("target"), dict) else {}
    mine = _parse_force(gs.get("my_units"))
    theirs = _parse_force(gs.get("enemy_units"))
    has_walls, broken, wall_label = _wall_status(target)

    supply = None
    for c in gs.get("my_cities") or []:
        try:
            d = _dist(x, y, c["x"], c["y"])
        except (KeyError, TypeError, ValueError):
            continue
        supply = d if supply is None else min(supply, d)

    def _int_or_none(key: str) -> int | None:
        try:
            v = int(target.get(key) or 0)
        except (TypeError, ValueError):
            return None
        return v or None

    defense = _int_or_none("defense_strength")
    garrison_hp = _int_or_none("garrison_hp")
    # None = 没测(不影响判定);False = 实测走不到,直接判死。
    reach = gs.get("land_reachable")
    land_reachable = reach if isinstance(reach, bool) else None
    tier, reasons = assess_tier(
        mine, theirs, has_walls, broken, supply, defense, garrison_hp, land_reachable
    )

    name = target.get("name") or f"({x},{y})"
    pop = target.get("population")
    out = [f"== 战前评估:{name} =="]
    head = f"目标:{wall_label}"
    if pop:
        head += f",人口 {pop}"
    if defense:
        head += f",城防值 {defense}"
        if garrison_hp:
            head += f",城血 {garrison_hp}/{target.get('garrison_max_hp') or 200}"
    else:
        head += ",城防值未知(城不在视野——先照亮它,城防是攻城第一变量)"
    out.append(head)
    out.append(
        f"守军:{_force_brief(theirs)}"
        if theirs.count
        else "守军:视野内无守军——可能藏在迷雾,别当空城"
    )
    out.append(f"我方:{_force_brief(mine)}")
    if supply is not None:
        tail = "——补给线过长,伤兵退不回去" if supply > SUPPLY_WARN_DIST else ""
        # 缺陷105:这是【直线】距离。伦敦离我方城市只有 3 格,陆路却是无穷远。
        # 不写清楚这一点,读的人(我)会把它当成"很近,好打"。
        out.append(f"补给:最近己方城市 {supply} 格(直线){tail}")
    if land_reachable is True:
        out.append("陆路:已实测连通(BFS 从我方陆军所在陆块可达城市相邻格)")
    out.append(f"判定:{tier}")
    for r in reasons[:3]:
        out.append(f"· {r}")

    if tier in (TIER_ORANGE, TIER_RED):
        out.extend(_alternatives(mine, gs, has_walls))
    bonus = anti_cavalry_bonus()
    if bonus and theirs.n("cavalry") and mine.n("anti_cav"):
        out.append(
            f"· 克制:你的抗骑单位对骑兵 +{bonus} 战力(导出值;适用条件的"
            f"判定集未导出,按能力名推断)——用它们顶住对面 {theirs.n('cavalry')} 骑。"
        )
    return out[:12]


def _alternatives(mine: _Force, gs: dict, has_walls: bool) -> list[str]:
    """🟠/🔴 时的替代方案。"""
    out: list[str] = []
    techs = gs.get("my_techs")
    techs = techs if isinstance(techs, (set, frozenset)) else set()
    if has_walls and mine.n("siege") == 0:
        siege_units = sorted(
            (u for u, tags in _unit_tags().items() if "CLASS_SIEGE" in tags),
            key=lambda u: _unit_stats()[u]["cost"],
        )
        for u in siege_units:
            s = _unit_stats()[u]
            if not s["tech"] or s["tech"] in techs:
                out.append(
                    f"替代:先造{s['name']}(科技已具备)再来——"
                    f"没有攻城单位的围城是给城防送经验。"
                )
                break
            out.append(f"替代:先研究 {s['tech']} 解锁{s['name']}——这是最近的攻城选项。")
            break
    if mine.n("ranged") >= 2 and not out:
        out.append("替代:先用远程在城外消耗守军(不打城),守军清完再评估围城。")
    if not out:
        out.append(
            "替代:这目标暂时超出能力——先拆最近的蛮族营地攒经验/升级,或换更软的目标。"
        )
    return out[:2]


# ---------------------------------------------------------------------------
# 攻城计划
# ---------------------------------------------------------------------------


def suggest_siege_plan(
    target_city_x: int, target_city_y: int, game_state: dict
) -> list[str]:
    """🟢/🟡 时给 3-5 步攻城计划;🟠/🔴 给一行引导。不抛异常。"""
    try:
        gs = game_state if isinstance(game_state, dict) else {}
        target = gs.get("target") if isinstance(gs.get("target"), dict) else {}
        mine = _parse_force(gs.get("my_units"))
        theirs = _parse_force(gs.get("enemy_units"))
        has_walls, broken, _ = _wall_status(target)
        supply = None
        for c in gs.get("my_cities") or []:
            try:
                d = _dist(int(target_city_x), int(target_city_y), c["x"], c["y"])
                supply = d if supply is None else min(supply, d)
            except (KeyError, TypeError, ValueError):
                continue
        try:
            cdef = int(target.get("defense_strength") or 0) or None
            ghp = int(target.get("garrison_hp") or 0) or None
        except (TypeError, ValueError):
            cdef = ghp = None
        tier, _r = assess_tier(mine, theirs, has_walls, broken, supply, cdef, ghp)
        if tier in (TIER_ORANGE, TIER_RED):
            return [
                f"当前判定 {tier} —— 不出攻城计划。先按战前评估的替代方案"
                f"补齐条件,免得计划变成阵亡名单。"
            ]
        return _plan_inner(mine, theirs, has_walls, broken)
    except Exception:
        return []


def _plan_inner(
    mine: _Force, theirs: _Force, has_walls: bool, broken: bool
) -> list[str]:
    out = ["== 攻城计划 =="]
    step = 1

    if theirs.count:
        rng = [
            (_unit_cn(u), _unit_stats().get(u, {}).get("range", 1))
            for u, _ in mine.groups.get("ranged", [])
        ]
        shooter = rng[0][0] if rng else "远程单位"
        out.append(
            f"第{step}步:{shooter}先清城外守军——站在射程边缘打完就撤,"
            f"城防每回合有一次远程反击,别在它射程里过夜。"
            f"单次预计削 20-40(±25% 随机)。"
        )
        step += 1

    if has_walls and not broken:
        siege = mine.groups.get("siege", [])
        if siege:
            s_name = _unit_cn(siege[0][0])
            setup = (
                "(需先架设:移动后当回合开不了炮,选好位再挪)"
                if needs_setup(siege[0][0])
                else ""
            )
            out.append(
                f"第{step}步:{s_name}轰墙{setup}——攻城单位对墙全额伤害"
                f"(对墙加成乘数未导出,保守按约 ×3 于普通单位估),"
                f"通常 2-3 轮告破;远程打墙只有半效,近战只有微量。"
            )
        else:
            out.append(
                f"第{step}步:远程集火削墙(无攻城单位,效率减半)——"
                f"墙量大时这一步会拖 3-5 回合,期间盯敌方援军。"
            )
        step += 1

    out.append(
        f"第{step}步:墙破(或无墙)后近战收尾——先杀最残血的守军,"
        f"清一个少一份反击火力;近战别贴墙罚站,站上去的那格会同时吃"
        f"城防反击+守军集火。"
    )
    step += 1
    out.append(
        f"第{step}步:城市血量清空后,用近战单位进城占领"
        f"(远程/攻城不能占城——机制常识,占领必须有近战进格)。"
    )
    out.append("占领后立刻跑 post_war_advice——忠诚度问题在占领当回合就要定调。")
    return out


# ---------------------------------------------------------------------------
# 战后处理
# ---------------------------------------------------------------------------


def post_war_advice(city_x: int, city_y: int, game_state: dict) -> list[str]:
    """占领后的忠诚度处理建议。复用 world_model.loyalty_pressure。不抛异常。"""
    try:
        from civ_mcp.world_model import loyalty_pressure

        gs = game_state if isinstance(game_state, dict) else {}
        own = [c for c in (gs.get("own_cities") or []) if isinstance(c, dict)]
        enemy = [c for c in (gs.get("enemy_cities") or []) if isinstance(c, dict)]
        net = loyalty_pressure(int(city_x), int(city_y), own, enemy)
    except Exception:
        return []

    out = [f"== 战后处理({city_x},{city_y}) =="]
    if net >= 2:
        out.append(
            f"忠诚:净 {net:+.1f}/回合,守得住——保留。驻军 1 个近战吃城防加成,"
            f"生产先补纪念碑稳忠诚。"
        )
        nearest = None
        for c in enemy:
            try:
                d = _dist(city_x, city_y, c["x"], c["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if nearest is None or d < nearest[0]:
                nearest = (d, c)
        if nearest:
            out.append(
                f"下一目标:敌方还有城在 {nearest[0]} 格外——趁兵力成型,"
                f"评估连推(先跑 assess_attack)。"
            )
    elif net > -8:
        out.append(
            f"忠诚:净 {net:+.1f}/回合,救得回但要立刻动手——"
            f"① 派总督(Victor 类防御型优先,总督忠诚数值未导出,标注为经验)"
            f"② 驻军 ③ 买纪念碑。三样一起上,只做一样大概率不够。"
        )
    else:
        out.append(
            f"忠诚:净 {net:+.1f}/回合——按公式约 {abs(100 / net):.0f} 回合叛变,"
            f"总督+驻军也填不平这个窟窿(阈值为经验值)。"
        )
        out.append(
            "推荐:夷平,或明知会丢、当几回合前进基地用——别投资一座注定还给敌人的城。"
        )
    out.append("(忠诚公式为计划书近似;总督/驻军数值未导出,以游戏内为准)")
    return out


# ===========================================================================
# 任务 X:掠夺经济(Pillage Economy)—— 追加,上方已有代码未动一行
#
# 有些高手宣战不为攻城,只为掠夺:一个轻骑兵每回合刮 1-2 块地,绕一圈净赚
# 几百产出然后撤。AI 宣战后只会直愣愣冲城,视野里的改良设施白白放着。
#
# 数值边界(本模块的诚实性关键):
#   · **每种改良设施的掠夺收益不在导出表里**。Improvements.json 只有
#     ImprovementType/Name/PrereqTech/PrereqCivic/BarbarianCamp/Goody/
#     Buildable 七列,没有掠夺产出列。⇒ 只做**分级排序**,不报具体数字。
#   · 分级依据(标注为 [经验]):产能类 > 特殊/资源类 > 食物类。
#   · 导出表能佐证掠夺机制存在且可被放大:RAID_DOUBLEPILLAGEIMPROVE /
#     TOTALWAR_DOUBLEPILLAGEIMPROVE(Amount=2,改良掠夺翻倍)、
#     SACK_DOUBLEPILLAGEDISTRICT(区域掠夺翻倍)、
#     MODIFIER_PLAYER_UNIT_ADJUST_ADVANCED_PILLAGING(掠夺不消耗全部移动力)。
#     这些卡名/修正器是导出值,可以放心提。
#   · 哪些单位掠夺后还能动:取决于晋升/能力(ADVANCED_PILLAGING),不是
#     兵种固有属性。⇒ 只按兵种给机动性排序,并提示以游戏内为准。
# ===========================================================================

# [经验] 掠夺价值分级。数值仅用于排序,不是游戏内收益。
_PILLAGE_TIERS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        3,
        "高",
        (
            "IMPROVEMENT_MINE",
            "IMPROVEMENT_QUARRY",
            "IMPROVEMENT_LUMBER_MILL",
            "IMPROVEMENT_OIL_WELL",
            "IMPROVEMENT_OFFSHORE_OIL_RIG",
        ),
    ),
    (
        2,
        "中",
        (
            "IMPROVEMENT_PLANTATION",
            "IMPROVEMENT_CAMP",
            "IMPROVEMENT_PASTURE",
            "IMPROVEMENT_FISHING_BOATS",
        ),
    ),
    (1, "低", ("IMPROVEMENT_FARM",)),
)
# 区域被掠夺的收益通常高于改良设施,且会瘫痪该区域的产出(双重打击)
DISTRICT_PILLAGE_TIER = 4

# 掠夺后仍保有机动性的兵种(按 war_planner 的分类;实际取决于晋升/能力)
_MOBILE_GROUPS = ("cavalry",)


@lru_cache(maxsize=1)
def _improvement_names() -> dict[str, str]:
    return {
        r["ImprovementType"]: r.get("Name") or r["ImprovementType"]
        for r in _rows("Improvements")
        if r.get("ImprovementType")
    }


@lru_cache(maxsize=1)
def _district_names() -> dict[str, str]:
    return {
        r["DistrictType"]: r.get("Name") or r["DistrictType"]
        for r in _rows("Districts")
        if r.get("DistrictType")
    }


def pillage_value(tile: dict) -> tuple[int, str, str]:
    """一格的掠夺价值 → (分级分, 档位中文, 目标名)。不可掠夺返回 (0,"","")。"""
    if not isinstance(tile, dict):
        return 0, "", ""
    if tile.get("is_pillaged"):
        return 0, "", ""  # 已经被刮过了
    district = str(tile.get("district") or "")
    if district:
        return (
            DISTRICT_PILLAGE_TIER,
            "极高",
            _district_names().get(district, district),
        )
    imp = str(tile.get("improvement") or "")
    if not imp:
        return 0, "", ""
    name = _improvement_names().get(imp, imp)
    for score, label, members in _PILLAGE_TIERS:
        if imp in members:
            return score, label, name
    return 1, "低", name  # 表里有但未分级的按低档处理


def get_pillage_advice(
    enemy_tiles: list[dict], cavalry_units: list[dict], max_items: int = 3
) -> list[str]:
    """战争期间的掠夺目标估价与路线。无可掠夺目标时返回空列表,不抛异常。

    enemy_tiles: [{"x","y","improvement","district","is_pillaged","owner_name"}]
    cavalry_units: [{"type","x","y"}] —— 通常是骑兵,其他兵种也接受
    """
    try:
        raiders = []
        for u in cavalry_units or []:
            if not isinstance(u, dict):
                continue
            ut = str(u.get("type") or u.get("unit_type") or "")
            group = classify_unit(ut)
            if group in ("other",):
                continue
            stats = _unit_stats().get(ut, {})
            raiders.append(
                {
                    "type": ut,
                    "cn": _unit_cn(ut),
                    "group": group,
                    "moves": stats.get("moves", 2),
                    "x": u.get("x"),
                    "y": u.get("y"),
                }
            )
        targets = []
        for t in enemy_tiles or []:
            score, label, name = pillage_value(t)
            if score > 0:
                targets.append((score, label, name, t))
        if not raiders or not targets:
            return []
        targets.sort(key=lambda p: -p[0])

        out = ["== 掠夺经济 =="]
        mobile = [r for r in raiders if r["group"] in _MOBILE_GROUPS]
        lead = (mobile or raiders)[0]
        for score, label, name, tile in targets[:max_items]:
            loc = f"({tile.get('x')},{tile.get('y')})"
            dist = ""
            try:
                d = hex_distance(
                    int(tile["x"]), int(tile["y"]), int(lead["x"]), int(lead["y"])
                )
                turns = max(1, -(-d // max(1, int(lead["moves"]))))
                dist = f",距 {lead['cn']} {d} 格(约 {turns} 回合到位)"
            except (KeyError, TypeError, ValueError):
                pass
            out.append(f"· {loc} {name}(掠夺价值 {label}){dist}")
        out.append(
            f"路线:{lead['cn']} 从最近的高价值目标开始,刮完就走,不要停在"
            f"敌城射程内过夜。"
        )
        if mobile:
            out.append(
                "   骑乘单位掠夺后通常还能移动(取决于晋升/能力,以游戏内为准)"
                "——打了就跑,别恋战。"
            )
        else:
            out.append(
                "   当前没有骑乘单位:步兵掠夺后一般会耗尽移动力,"
                "刮一格就要原地过一回合,风险高得多。"
            )
        out.append(
            "   放大手段(导出值):「突袭」/「全面战争」政策卡使改良设施掠夺"
            "翻倍,「洗劫」使区域掠夺翻倍——开抢前先看这几张卡在不在槽里。"
        )
        out.append("   (各设施的具体掠夺收益未导出,上面只做分级排序,不报数字)")
        return out
    except Exception:
        return []


# ===========================================================================
# 任务 AC:现代战争(追加,上方已有代码未动一行)
#
# assess_attack/suggest_siege_plan 规划的是古代-中世纪的攻城链:远程削墙、
# 近战收尾。到了工业时代以后战术完全不同 —— 空军先手、核武威慑、海空协同。
# 这一段补上后期的兵种账。
#
# 数据来源(全部动态发现,不硬编码单位名):
#   Units.json 的 Domain=DOMAIN_AIR   → 空军(战斗机/轰炸机及其喷气版)
#   Units.json 的 PromotionClass      → AIR_FIGHTER / AIR_BOMBER / NAVAL_CARRIER
#                                       / NAVAL_RAIDER 等分类
#   Units.json 的 StrategicResource   → 铝(空军)/石油(坦克航母)/铀(核潜艇)
#   Projects.json                     → 曼哈顿计划、常春藤行动、核装置/热核装置
#   Improvements.json                 → 导弹发射井(IMPROVEMENT_MISSILE_SILO)
#
# ⚠️ 两处本规则集没有的东西(计划书点名要做,但数据说不存在):
#   · **GDR(巨型死亡机器人)不在 RULESET_STANDARD**:Units 表里没有任何
#     GDR/GIANT_DEATH 条目 —— 它是 Gathering Storm 内容。不提供它的建议。
#   · 核弹的**爆炸威力/辐射范围/持续时间**均未导出(Projects 表只有 Cost/
#     PrereqTech)。所以只讲"有没有、怎么投、什么后果",不报伤害数字。
# ===========================================================================

MODERN_ERAS = (
    "ERA_INDUSTRIAL",
    "ERA_MODERN",
    "ERA_ATOMIC",
    "ERA_INFORMATION",
    "ERA_FUTURE",
)

# 计划书点名但本规则集不存在的单位(留作记录,并让检查函数据此保持沉默)
_ABSENT_UNIT_PATTERNS = ("GDR", "GIANT_DEATH", "ROBOT")


@lru_cache(maxsize=1)
def air_units() -> tuple[tuple[str, str, int, int], ...]:
    """空军单位 → (UnitType, 分类, 战力/轰炸值, 射程),按射程升序。

    分类取自 PromotionClass:AIR_FIGHTER=制空 / AIR_BOMBER=对地。
    """
    out = []
    for r in _rows("Units"):
        if r.get("Domain") != "DOMAIN_AIR":
            continue
        ut = r.get("UnitType")
        if not ut:
            continue
        cls = (r.get("PromotionClass") or "").replace("PROMOTION_CLASS_", "")
        try:
            power = max(int(r.get("Bombard") or 0), int(r.get("RangedCombat") or 0))
            rng = int(r.get("Range") or 0)
        except (TypeError, ValueError):
            continue
        out.append((ut, cls, power, rng))
    return tuple(sorted(out, key=lambda x: x[3]))


@lru_cache(maxsize=1)
def gdr_available() -> bool:
    """本规则集有没有 GDR。Units 表里搜不到 → False(原版就是没有)。"""
    return any(any(p in ut for p in _ABSENT_UNIT_PATTERNS) for ut in _unit_stats())


@lru_cache(maxsize=1)
def nuclear_projects() -> tuple[dict, ...]:
    """核相关项目 → (ProjectType, 中文名, Cost, PrereqTech),按 Cost 升序。"""
    out = [
        {
            "type": r["ProjectType"],
            "name": r.get("Name") or r["ProjectType"],
            "cost": int(r.get("Cost") or 0),
            "tech": r.get("PrereqTech") or "",
        }
        for r in _rows("Projects")
        if "NUCLEAR" in (r.get("ProjectType") or "")
        or "IVY" in (r.get("ProjectType") or "")
        or "MANHATTAN" in (r.get("ProjectType") or "")
    ]
    return tuple(sorted(out, key=lambda p: p["cost"]))


@lru_cache(maxsize=1)
def _strategic_gates() -> dict[str, str]:
    """UnitType → 它吃的战略资源(导出值)。没有的为空串。"""
    return {
        r["UnitType"]: (r.get("StrategicResource") or "")
        for r in _rows("Units")
        if r.get("UnitType")
    }


@lru_cache(maxsize=1)
def _ruleset() -> str:
    """本机规则集标识,用于诚实标注"这东西本局没有"。"""
    try:
        with (_GAMEDATA / "_all.json").open(encoding="utf-8") as fh:
            return json.load(fh).get("ruleset", "")
    except Exception:
        return ""


def modern_war_assess(game_state: dict, era: str = "", max_items: int = 6) -> list[str]:
    """任务 AC —— 现代战争能力评估。未进入工业时代返回空列表,不抛异常。

    game_state: {"my_units":[{"type"}...], "strategic_stock":{资源:数量},
                 "has_missile_silo":bool, "used_nuke_first":bool,
                 "target_city":str(可选)}
    """
    try:
        return _modern_inner(
            game_state if isinstance(game_state, dict) else {},
            str(era or ""),
            max_items,
        )
    except Exception:
        return []


def _modern_inner(gs: dict, era: str, max_items: int) -> list[str]:
    if era and era not in MODERN_ERAS:
        return []
    owned: dict[str, int] = {}
    for u in gs.get("my_units") or []:
        if not isinstance(u, dict):
            continue
        ut = str(u.get("type") or u.get("unit_type") or "")
        if ut:
            owned[ut] = owned.get(ut, 0) + 1
    if not owned and not era:
        return []

    stock = gs.get("strategic_stock") or {}

    def _have(pred) -> list[str]:
        return [ut for ut in owned if pred(ut)]

    air_map = {ut: (cls, power, rng) for ut, cls, power, rng in air_units()}
    my_fighters = [ut for ut in owned if air_map.get(ut, ("",))[0] == "AIR_FIGHTER"]
    my_bombers = [ut for ut in owned if air_map.get(ut, ("",))[0] == "AIR_BOMBER"]
    my_carriers = _have(lambda ut: (_unit_stats().get(ut) or {}) and "CARRIER" in ut)
    my_subs = _have(lambda ut: "SUBMARINE" in ut)

    out: list[str] = ["== 现代战争 =="]

    # —— 1. 空军战术链 ——
    if my_bombers or my_fighters:
        parts = []
        if my_fighters:
            f = my_fighters[0]
            parts.append(f"{_unit_cn(f)}(制空,射程 {air_map[f][2]})先夺制空权")
        if my_bombers:
            b = my_bombers[0]
            parts.append(
                f"{_unit_cn(b)}(轰炸 {air_map[b][1]},射程 {air_map[b][2]})削城防/砸守军"
            )
        out.append(
            f"✈ 空军链:{' → '.join(parts)} → 地面单位进城收割。"
            f"空军是后期唯一能在一回合内跨十几格投射火力的兵种,"
            f"攻城节奏由它定,不再由攻城车定。"
        )
        need_alu = {
            ut
            for ut in (my_fighters + my_bombers)
            if "ALUMINUM" in _strategic_gates().get(ut, "")
        }
        if need_alu:
            have = stock.get("RESOURCE_ALUMINUM", 0)
            if not have:
                out.append(
                    "   ⚠️ 空军吃铝,你的铝库存为 0 —— 飞机会因缺资源无法生产/维护。"
                )
    elif era in ("ERA_MODERN", "ERA_ATOMIC", "ERA_INFORMATION"):
        cand = [ut for ut, cls, _p, _r in air_units() if cls == "AIR_BOMBER"]
        if cand:
            tech = (_unit_stats().get(cand[0]) or {}).get("tech", "")
            out.append(
                f"✈ 你没有任何空军。{_unit_cn(cand[0])}需要 {tech} —— "
                f"到了这个时代还纯地面部队,等于用上个时代的战争打这个时代。"
            )

    # —— 2. 航母 ——
    if my_carriers and (my_fighters or my_bombers):
        out.append(
            f"🚢 有{_unit_cn(my_carriers[0])}+空军:舰载机把机场推到敌人门口,"
            f"跨洋作战不必先抢滩建机场。航母本身战力低,别让它单独顶前线。"
        )
    elif my_carriers:
        out.append(
            f"🚢 有{_unit_cn(my_carriers[0])}但没有舰载机 —— 空船一条,"
            f"先造飞机再谈投送。"
        )

    # —— 3. 核武 ——
    projects = nuclear_projects()
    if projects:
        enablers = [
            p for p in projects if "MANHATTAN" in p["type"] or "IVY" in p["type"]
        ]
        devices = [p for p in projects if "BUILD" in p["type"]]
        uranium = stock.get("RESOURCE_URANIUM", 0)
        gate = "、".join(f"{p['name']}({p['cost']} 锤,{p['tech']})" for p in enablers)
        dev = "、".join(f"{p['name']}({p['cost']} 锤)" for p in devices)
        out.append(f"☢ 核武链:先跑前置项目({gate}),之后才能造{dev}。铀库存 {uranium}。")
        silo = "有" if gs.get("has_missile_silo") else "无"
        out.append(
            f"   投掷方式三选一:轰炸机(会被战斗机拦截)/ 导弹发射井"
            f"(IMPROVEMENT_MISSILE_SILO,当前{silo})/ 核潜艇"
            f"{'(你有)' if my_subs else '(需 RESOURCE_URANIUM)'} —— "
            f"潜艇最隐蔽,发射井最稳。"
        )
        if not gs.get("used_nuke_first"):
            out.append(
                "   ⚠️ 外交后果:首个使用核武的文明会招致全世界的谴责与联合敌意。"
                "威力/辐射数值未导出,但这条政治代价是确定的 —— "
                "除非是终局一击,否则先掂量。"
            )

    # —— 4. GDR:本规则集没有,说清楚免得等 ——
    if not gdr_available() and era in ("ERA_INFORMATION", "ERA_FUTURE"):
        out.append(
            f"🤖 计划书提到的 GDR(巨型死亡机器人)在本规则集({_ruleset()})"
            f"【不存在】—— Units 表里没有任何相关条目,它是 Gathering Storm "
            f"内容。信息时代的地面主力是坦克/机械化步兵这条线,别等它。"
        )

    if len(out) <= 1:
        return []
    if len(out) > max_items + 1:
        omitted = len(out) - max_items - 1
        out = out[: max_items + 1]
        out.append(f"   (另有 {omitted} 条现代战争提醒已省略)")
    return out
