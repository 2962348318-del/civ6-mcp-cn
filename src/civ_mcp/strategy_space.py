"""策略空间模板库(任务 AI)—— schema 定义、数据自检与查询接口。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物,不另建 .md)
═══════════════════════════════════════════════════════════════════════════

## 0. 定位

这是**知识库**,不是决策器。开局评估器(任务 AM)是运行时推理引擎,它从
这里拉取「文明原理 + 地图/邻居/难度适应规则」,结合本局的实际地形资源,
自己推理出路线。所以本库存的不是结论("走统治"),而是**带推导链的原理**
("军团 40 对同期 35 是代差 ⇒ 3-4 个就能推平一个邻居 ⇒ 科技答案是炼铁术
优先" )——LLM 读完应当能自己接着往下推,而不是背答案。

数据文件在 `data/strategy/*.json`,一个文明一个文件。本模块负责:
    1. 定义 schema(见 §1)并把 JSON 映射成 dataclass;
    2. **数据自检**(见 §3)——每条硬主张都带 gamedata 引用,加载时逐条核对;
    3. 查询接口 `get_strategy(文明, 地图, 邻居议程, 难度)` → 组装好的推理材料。

## 1. Schema(v1)

顶层字段(JSON 对象,一个文明一份):

| 字段 | 类型 | 说明 |
|---|---|---|
| schema | int | 版本号,当前 1。结构变更时递增,加载器按版本兼容。 |
| civ / leader | str | CIVILIZATION_* / LEADER_* 标识,对 Leaders 表可查。 |
| civ_cn / leader_cn | str | 中文名,查询接口的别名键。 |
| identity | str | 一句话:这个文明赢在什么。评估报告的第一行素材。 |
| verified_facts | list[Fact] | 硬主张清单,**每条带 source 引用**(§3),加载时逐条核对。 |
| principles | list[Principle] | fact(能力事实)→ derivation(推导链)→ confidence。知识库的核心:不是"做什么"而是"为什么,以及怎么从这里往下推"。 |
| map_plans | dict[地图类型→MapPlan] | 键对齐 params.py 的 _MAP_PARAMS(PANGAEA/CONTINENTS/ARCHIPELAGO/...),外加 DEFAULT(地图未知/未收录时的兜底)。 |
| neighbor_plays | list[NeighborPlay] | 按**议程原型**组织的应对。matches_agendas 引用 HistoricalAgendas 表的真实议程标识——与 opponents.py 的议程画像同一词汇表,查询时拿实际邻居的议程做交集。matches 为空 = 局面型条目(非议程触发),按议程查询时跳过,供 AM 全量读取。 |
| difficulty_rules | list[DifficultyRule] | min_difficulty 引用 Difficulties 表标识,按 params.DIFFICULTY_ORDER 做序比较:玩家难度 ≥ min 则生效。 |
| fallbacks | list[Fallback] | trigger(什么坏了)→ plan(转哪条路)。这是 AN 叙事简报「如果出问题」段的直接素材,也是 advice.arbitrate(plan_b=...) 的数据来源。 |
| needs_ingame_check | list[str] | 诚实清单:导出数据回答不了、必须实机确认的问题。 |

设计理由(对应人类高手的认知结构):
  · **principle 而非 build order**——高手迁移到新地图时靠的是"我知道这个
    文明为什么强",不是背了 53 套开局序。derivation 字段强制作者写出
    从数据到打法的推导,LLM 拿到的是可续推的链条。
  · **map/neighbor/difficulty 三个正交轴**——同一文明换图换邻居换难度,
    变的不是原理,是原理的**兑现率**(军团在群岛兑现不了)。所以适应规则
    各自独立成轴,查询时按需组合,不做 3×N 的笛卡尔积模板。
  · **fallback 是一等公民**——高手从来不只有一个计划。"没抢到大预言家
    怎么办"和主计划同格存储,评估器随取随用。

## 2. 置信度

与 advice.Confidence 同一套四档词汇(确定/推荐/可选/冒险,任务 AJ 的
统一风险语言):数值区间由档位隐含,不重复存储。旧文档里的 [经验] 标注
≈ 本词汇表的「可选」档(方向对、数值粗、未经本机校准)。

## 3. source 引用语法与数据自检

verified_facts 的每条 source 是一个可机验的断言,四种形式:

    "Units:UNIT_ROMAN_LEGION:Combat=40"        行字段断言(值可为空串,
                                                如 ...:StrategicResource= )
    "Modifiers:TRAIT_FREE_ROADS_TO_CAPITAL"    存在性断言
    "Modifiers:OLIGARCHY_MELEE_BUFF:Amount=4"  修正器参数断言(字段不在
                                                Modifiers 列中时自动改查
                                                ModifierArguments)
    "District_Adjacencies:DISTRICT_LAVRA:YieldChangeId=Mountain_Faith1"
                                                多行表断言(任一行匹配即过)

`verify_all()` 逐条核对,返回不一致清单;`get_strategy()` 每次组装时
顺带自检并在尾行声明结果——**数据陈旧会自己暴露**(比如换了规则集重导
gamedata 后,对不上的主张立即显形),这是被"凭印象编地形表"打脸后的
制度化防线。

### attribution:direct 与 inferred(本 schema 最重要的诚实机制)

导出的 40 张表里**没有 TraitModifiers/LeaderTraits 连接表**——Modifiers
表里躺着 TRAIT_INCREASED_TUNDRA_FAITH 这样的修正器,但"它属于俄罗斯"
这件事在导出数据里查不到,只能按效果特征推断(冻土+1信仰,全游戏只有
Mother Russia 有这个描述)。于是每条 fact 标注归属方式:

    direct   —— 表行本身就写明归属(Units 的军团行/HistoricalAgendas 的
                议程行/名字带 BARBAROSSA 的修正器)。自检通过即为真。
    inferred —— 修正器**存在**可机验,**归属**是推断。渲染时标〔归属推断〕,
                并在 needs_ingame_check 里留对应的实机确认项。

## 4. 覆盖范围与扩充指南

计划书要求首批 10 文明;当前 3 个样例(罗马/俄罗斯/德国)。
⚠️ 计划书点名的朝鲜(善德女王)**不在本机规则集**——Leaders 表 20 个主要
领袖是原版阵容,无 SEONDEOK,无澳大利亚;顺带:计划书 neighbor 示例里的
"朝鲜/澳大利亚是科技邻居"在本机也不可能出现。已换德国(巴巴罗萨):
同为科技强势位,且汉萨/区域槽的数据可验证性最高。

加第 N 个文明的流程:
    1. 从 Leaders/HistoricalAgendas/UnitReplaces/Units/Districts 里拉该文明
       的全部 direct 事实,写进 verified_facts;
    2. 在 Modifiers 里按效果特征找 TRAIT_* 修正器,标 inferred;
    3. principles 只写「从上述事实能推导出来的」,推不出的进
       needs_ingame_check——**宁缺毋编**;
    4. 跑 `verify_all()` 确认 0 不一致再提交。

## 5. 三个样例的对照设计(为什么选这三个)

    罗马   —— 窗口型:优势是一段会关闭的时间窗,换图会换胜利路线
    俄罗斯 —— 引擎型:优势是不挑地图的折扣引擎,换图不换路线
    德国   —— 结构型:优势是区域槽/半价区域的结构差,难度加成稀释不掉
三种原理形态覆盖了策略空间的主要象限,后续文明大多是三者的变体或混合。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path, strategy_path

import json
from dataclasses import dataclass, field
from pathlib import Path

from civ_mcp.params import DIFFICULTY_ORDER

_ROOT = Path(__file__).resolve().parents[2]
_GAMEDATA = gamedata_path()
_STRATEGY_DIR = strategy_path()

SCHEMA_VERSION = 1

# 各表的行键列(source 引用解析用)
_ROW_KEYS: dict[str, str] = {
    "Units": "UnitType",
    "UnitReplaces": "CivUniqueUnitType",
    "Districts": "DistrictType",
    "District_Adjacencies": "DistrictType",
    "Adjacency_YieldChanges": "ID",
    "Buildings": "BuildingType",
    "Modifiers": "ModifierId",
    "HistoricalAgendas": "LeaderType",
    "Leaders": "LeaderType",
    "Technologies": "TechnologyType",
    "Governments": "GovernmentType",
    "Resources": "ResourceType",
}

_VALID_CONFIDENCE = ("确定", "推荐", "可选", "冒险")


# ---------------------------------------------------------------------------
# 数据结构(镜像 §1 的 schema)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    claim: str
    source: str  # §3 的引用语法
    attribution: str = "direct"  # "direct" | "inferred"


@dataclass(frozen=True)
class Principle:
    fact: str
    derivation: str
    confidence: str = "推荐"


@dataclass(frozen=True)
class MapPlan:
    victory: str
    confidence: str
    reasoning: str
    opening: tuple[str, ...] = ()


@dataclass(frozen=True)
class NeighborPlay:
    archetype: str
    matches_agendas: tuple[str, ...]
    play: str


@dataclass(frozen=True)
class DifficultyRule:
    min_difficulty: str
    rule: str
    confidence: str = "可选"


@dataclass(frozen=True)
class Fallback:
    trigger: str
    plan: str


@dataclass(frozen=True)
class CivStrategy:
    civ: str
    leader: str
    civ_cn: str
    leader_cn: str
    identity: str
    verified_facts: tuple[Fact, ...] = ()
    principles: tuple[Principle, ...] = ()
    map_plans: dict[str, MapPlan] = field(default_factory=dict)
    neighbor_plays: tuple[NeighborPlay, ...] = ()
    difficulty_rules: tuple[DifficultyRule, ...] = ()
    fallbacks: tuple[Fallback, ...] = ()
    needs_ingame_check: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# gamedata 访问(带缓存)
# ---------------------------------------------------------------------------

_table_cache: dict[str, list[dict[str, str]]] = {}


def _rows(table: str) -> list[dict[str, str]]:
    if table in _table_cache:
        return _table_cache[table]
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("rows")
        out = rows if isinstance(rows, list) else []
    except Exception:
        out = []
    _table_cache[table] = out
    return out


def verify_fact(source: str) -> str | None:
    """核对一条 source 断言。返回 None = 通过;str = 不一致的人话描述。

    语法见模块 docstring §3。任何解析/IO 异常都归为不一致(数据缺失
    也是一种"对不上"),不抛异常。
    """
    try:
        parts = source.split(":", 2)
        if len(parts) < 2:
            return f"引用格式错误:{source}"
        table, key = parts[0], parts[1]
        key_col = _ROW_KEYS.get(table)
        if not key_col:
            return f"未注册的表:{table}"
        rows = [r for r in _rows(table) if r.get(key_col) == key]
        if not rows:
            return f"{table} 里没有 {key}"
        if len(parts) == 2:
            return None  # 存在性断言通过
        f_expr = parts[2]
        if "=" not in f_expr:
            return f"字段断言缺少 '=':{source}"
        fname, expected = f_expr.split("=", 1)
        # 字段在表列里 → 任一行匹配即过(多行表如 District_Adjacencies)
        if any(fname in r for r in rows):
            if any(r.get(fname, "") == expected for r in rows):
                return None
            got = sorted({r.get(fname, "") for r in rows})
            return f"{table}:{key} 的 {fname} 实为 {got},主张 {expected!r}"
        # Modifiers 的参数断言 → 改查 ModifierArguments
        if table == "Modifiers":
            args = [
                r
                for r in _rows("ModifierArguments")
                if r.get("ModifierId") == key and r.get("Name") == fname
            ]
            if not args:
                return f"ModifierArguments 里 {key} 没有参数 {fname}"
            if any(r.get("Value", "") == expected for r in args):
                return None
            got = sorted({r.get("Value", "") for r in args})
            return f"{key} 的参数 {fname} 实为 {got},主张 {expected!r}"
        return f"{table} 没有字段 {fname}"
    except Exception as exc:  # 兜底:自检不许炸
        return f"自检异常 {source}:{exc}"


# ---------------------------------------------------------------------------
# 加载与自检
# ---------------------------------------------------------------------------

_strategies: dict[str, CivStrategy] | None = None


def _parse(doc: dict) -> CivStrategy | None:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_VERSION:
        return None
    try:
        return CivStrategy(
            civ=doc["civ"],
            leader=doc["leader"],
            civ_cn=doc.get("civ_cn", doc["civ"]),
            leader_cn=doc.get("leader_cn", doc["leader"]),
            identity=doc.get("identity", ""),
            verified_facts=tuple(
                Fact(f["claim"], f["source"], f.get("attribution", "direct"))
                for f in doc.get("verified_facts", [])
            ),
            principles=tuple(
                Principle(p["fact"], p["derivation"], p.get("confidence", "推荐"))
                for p in doc.get("principles", [])
            ),
            map_plans={
                k.upper(): MapPlan(
                    v.get("victory", ""),
                    v.get("confidence", "可选"),
                    v.get("reasoning", ""),
                    tuple(v.get("opening", [])),
                )
                for k, v in doc.get("map_plans", {}).items()
            },
            neighbor_plays=tuple(
                NeighborPlay(
                    n["archetype"], tuple(n.get("matches_agendas", [])), n["play"]
                )
                for n in doc.get("neighbor_plays", [])
            ),
            difficulty_rules=tuple(
                DifficultyRule(
                    d["min_difficulty"], d["rule"], d.get("confidence", "可选")
                )
                for d in doc.get("difficulty_rules", [])
            ),
            fallbacks=tuple(
                Fallback(f["trigger"], f["plan"]) for f in doc.get("fallbacks", [])
            ),
            needs_ingame_check=tuple(doc.get("needs_ingame_check", [])),
        )
    except (KeyError, TypeError):
        return None


def load_all(force: bool = False) -> dict[str, CivStrategy]:
    """加载 data/strategy/ 下全部策略文件。坏文件静默跳过,不抛异常。"""
    global _strategies
    if _strategies is not None and not force:
        return _strategies
    out: dict[str, CivStrategy] = {}
    try:
        files = sorted(_STRATEGY_DIR.glob("*.json"))
    except Exception:
        files = []
    for path in files:
        try:
            with path.open(encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        strat = _parse(doc)
        if strat:
            out[strat.civ] = strat
    _strategies = out
    return out


def list_civs() -> list[str]:
    """已收录的文明(标识 + 中文名),供评估器枚举。"""
    return [f"{s.civ}({s.civ_cn}/{s.leader_cn})" for s in load_all().values()]


def verify_all() -> list[str]:
    """全库数据自检。返回不一致清单(空 = 全部通过)。"""
    problems: list[str] = []
    for strat in load_all().values():
        for fact in strat.verified_facts:
            issue = verify_fact(fact.source)
            if issue:
                problems.append(f"[{strat.civ_cn}] {fact.claim} ← {issue}")
    return problems


def _find(civ_or_leader: str) -> CivStrategy | None:
    if not civ_or_leader:
        return None
    key = civ_or_leader.strip()
    for strat in load_all().values():
        if key.upper() in (strat.civ, strat.leader):
            return strat
        if key in (strat.civ_cn, strat.leader_cn):
            return strat
    return None


def _difficulty_at_least(player: str, minimum: str) -> bool:
    try:
        return DIFFICULTY_ORDER.index(player.upper()) >= DIFFICULTY_ORDER.index(
            minimum.upper()
        )
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------


def get_strategy(
    civ_or_leader: str,
    map_type: str = "",
    neighbor_agendas: tuple[str, ...] | list[str] = (),
    difficulty: str = "",
) -> str:
    """按(文明, 地图, 邻居议程, 难度)组装推理材料。

    输出是给开局评估器(任务 AM)的**推理材料**,不是简报本体——简报的
    行数预算由 advice.arbitrate() 管,评估器负责把材料压缩成结论。
    未收录的文明返回空串;任何异常返回空串,不抛。
    """
    try:
        return _get_strategy_inner(
            civ_or_leader, map_type, tuple(neighbor_agendas or ()), difficulty
        )
    except Exception:
        return ""


def _get_strategy_inner(civ_or_leader, map_type, neighbor_agendas, difficulty) -> str:
    strat = _find(civ_or_leader)
    if strat is None:
        return ""

    out: list[str] = []
    head = f"【{strat.civ_cn}·{strat.leader_cn}】"
    if map_type:
        head += f" 地图:{map_type.upper()}"
    if difficulty:
        head += f" 难度:{difficulty.upper()}"
    out.append(head)
    out.append(f"定位:{strat.identity}")

    if strat.principles:
        out.append("原理(fact → 推导):")
        for p in strat.principles:
            tag = "" if p.confidence in ("确定", "推荐") else f"〔{p.confidence}〕"
            out.append(f"· {p.fact}{tag} ⇒ {p.derivation}")

    plan = strat.map_plans.get((map_type or "").upper()) or strat.map_plans.get(
        "DEFAULT"
    )
    if plan:
        out.append(f"本图路线:{plan.victory}({plan.confidence})—— {plan.reasoning}")
        for step in plan.opening:
            out.append(f"  ○ {step}")

    if neighbor_agendas:
        hits = [
            np
            for np in strat.neighbor_plays
            if np.matches_agendas and set(np.matches_agendas) & set(neighbor_agendas)
        ]
        if hits:
            out.append("邻居应对(按实际议程命中):")
            for np in hits:
                out.append(f"· [{np.archetype}] {np.play}")

    if difficulty:
        rules = [
            r
            for r in strat.difficulty_rules
            if _difficulty_at_least(difficulty, r.min_difficulty)
        ]
        for r in rules:
            tag = "" if r.confidence in ("确定", "推荐") else f"〔{r.confidence}〕"
            out.append(f"难度规则{tag}:{r.rule}")

    if strat.fallbacks:
        out.append("如果出问题(fallback,供简报「如果出问题」段):")
        for fb in strat.fallbacks:
            out.append(f"· {fb.trigger} → {fb.plan}")

    # 数据自检尾行:陈旧数据自己暴露
    issues = [issue for f in strat.verified_facts if (issue := verify_fact(f.source))]
    inferred = sum(1 for f in strat.verified_facts if f.attribution == "inferred")
    tail = (
        f"数据自检:{len(strat.verified_facts) - len(issues)}/"
        f"{len(strat.verified_facts)} 条主张与导出表一致"
    )
    if issues:
        tail += f";⚠️ 不一致:{' ‖ '.join(issues[:3])}"
    if inferred:
        tail += f";其中 {inferred} 条为归属推断(修正器存在,归属需实机确认)"
    if strat.needs_ingame_check:
        tail += f";待游戏内确认 {len(strat.needs_ingame_check)} 项"
    out.append(tail)
    return "\n".join(out)
