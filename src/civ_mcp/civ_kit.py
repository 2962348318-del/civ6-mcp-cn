"""L2 文明套件推导层 —— 随机文明下,套件从 gamedata 现算,不查手写模板。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物)
═══════════════════════════════════════════════════════════════════════════

## 0. 为什么不能用手写模板做主路径

53 个领袖(本机规则集 20 个),`data/strategy/` 只有 3 份手写模板,
而且**已知含事实错误**:罗马那份写着"军团需要铁",与 `Units.json` 相反
(`UNIT_ROMAN_LEGION` 的 `StrategicResource` 是空串,`UNIT_SWORDSMAN`
才是 `RESOURCE_IRON`)。这条错误误导了整个校准局。

方案 v4 §0.3 的结论:**手写模板降级为可选覆盖层,主路径必须运行时推导。**
本模块就是那条主路径。`strategy_space.py` 继续存在,提供推不出来的
判断性内容(原理/预案),但不再是"这个文明有什么"的答案来源。

## 1. 归属问题(本模块最诚实的一段)

导出的 40 张表里 **没有** 文明→UU 的连接表:
`UnitReplaces` 只说 `UNIT_ROMAN_LEGION` 替换 `UNIT_SWORDSMAN`,
不说它属于罗马。归属在引擎里靠 `CivilizationTraits` × `Units.TraitType`,
这两列都没进导出。

所以本模块有两条获取路径,优先级明确:

    runtime  ——(权威)世界扫描 Lua 已经在发 `UU|` 行,走的正是
                CivilizationTraits × UnitReplaces。world_model.my_unique_units
                就是这份数据。有它就用它。
    inferred ——(兜底)按单位标识里的民族形容词前缀匹配文明标识。
                只在没有实机数据时用(离线分析/测试),渲染时标〔归属推断〕。

判断性内容一概不推断:UU 的战力/成本/前置科技/资源门槛全部来自
`Units.json`,是硬事实;"这个 UU 强不强"由公式算,不由人写。

## 2. UU 窗口(统治路线的命门,方案 v4 §L2.2)

校准局教训:罗马军团 CS40 对同期 CS35 是代差,**但系统直到 T46 都没提过
军团**,窗口白白错过一半。所以窗口必须 T1 就算出来。

窗口的三个端点,全部可从 gamedata 机械导出:

    开启 = UU 的 PrereqTech 所在时代
    关闭 = 第一个【通用】单位的战力追平/超过该 UU 的时代
           (不是"UU 被淘汰",而是"对手不需要特殊条件就能造出同级货")
    代差 = UU 战力 − 同期最强通用单位战力

"同期最强通用单位"刻意排除所有 UU(它们是别家的特权,不是普遍可得的
对照组)。这样算出来的代差回答的是:**我现在能造的兵,比任何一个
普通对手能造的都强多少。**

## 3. 路线倾向(leans)

UU 只能回答军事。区域/建筑类特色同样没有离线归属数据,所以本模块的
`ability_tags` 与 `unique_infra` 走和 UU 相同的 runtime-first 路线:
调用方(世界扫描)给什么就用什么,没有就诚实留空,**不猜**。

## 4. 明确不做

  · ❌ 自动生成 43 份文明策略模板 —— 手写的那份都藏着事实错误,
       用浅分类器批量生成只会把错误规模化(方案 v4 §L2.3)
  · ❌ 凭记忆写任何游戏数值 —— 本模块一个字面数字都没有,全部查表
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_GAMEDATA = gamedata_path()

# 民族形容词与文明标识对不上的少数几个(纯词形问题,不含任何判断)。
# 只在没有实机 UU 数据时的兜底匹配里用到 —— 见 docstring §1。
_CIV_ADJECTIVE = {
    "FRANCE": "FRENCH",
    "GREECE": "GREEK",
    "SPAIN": "SPANISH",
    "ENGLAND": "ENGLISH",
    "GERMANY": "GERMAN",
    "JAPAN": "JAPANESE",
    "NORWAY": "NORWEGIAN",
    "EGYPT": "EGYPTIAN",
    "ARABIA": "ARABIAN",
    "AMERICA": "AMERICAN",
    "BRAZIL": "BRAZILIAN",
    "RUSSIA": "RUSSIAN",
    "ROME": "ROMAN",
    "INDIA": "INDIAN",
    "CHINA": "CHINESE",
    "SCYTHIA": "SCYTHIAN",
    "SUMERIA": "SUMERIAN",
}

# 区域 → 它服务的路线。用于把「独特区域」翻译成路线倾向。
_DISTRICT_LEAN = {
    "CAMPUS": "science",
    "THEATER": "culture",
    "HOLY_SITE": "faith",
    "HARBOR": "naval",
    "COMMERCIAL_HUB": "gold",
    "INDUSTRIAL_ZONE": "production",
    "ENCAMPMENT": "military",
    "AQUEDUCT": "growth",
    "ENTERTAINMENT": "growth",
    "GOVERNMENT": "gold",
}


# ---------------------------------------------------------------------------
# gamedata 访问(带缓存,坏数据静默降级)
# ---------------------------------------------------------------------------

_cache: dict[str, list[dict[str, str]]] = {}


def _rows(table: str) -> list[dict[str, str]]:
    if table in _cache:
        return _cache[table]
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        out = data.get("rows") if isinstance(data.get("rows"), list) else []
    except Exception:
        out = []
    _cache[table] = out
    return out


def _num(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


_units_by_type: dict[str, dict] | None = None


def _units() -> dict[str, dict]:
    global _units_by_type
    if _units_by_type is None:
        _units_by_type = {r["UnitType"]: r for r in _rows("Units") if r.get("UnitType")}
    return _units_by_type


_upgrade_target_cache: dict[str, str] | None = None


def _upgrade_targets() -> dict[str, str]:
    """UnitType → 升级目标 UnitType(UnitUpgrades 表)。

    timing push 的机制基础:研究出升级目标的前置科技那一刻,
    原单位就不能再建造了 —— 窗口是被自己的科技进度关掉的。
    """
    global _upgrade_target_cache
    if _upgrade_target_cache is None:
        _upgrade_target_cache = {
            r["Unit"]: r.get("UpgradeUnit") or ""
            for r in _rows("UnitUpgrades")
            if r.get("Unit")
        }
    return _upgrade_target_cache


_era_index_cache: dict[str, int] | None = None


def _era_indices() -> dict[str, int]:
    global _era_index_cache
    if _era_index_cache is None:
        _era_index_cache = {
            r["EraType"]: _num(r, "ChronologyIndex")
            for r in _rows("Eras")
            if r.get("EraType")
        }
    return _era_index_cache


_tech_era_cache: dict[str, tuple[str, int]] | None = None


def _tech_eras() -> dict[str, tuple[str, int]]:
    """科技/市政 → (时代标识, 年代序号)。未知前置返回时代 0。"""
    global _tech_era_cache
    if _tech_era_cache is None:
        eras = _era_indices()
        out: dict[str, tuple[str, int]] = {}
        for table, key in (("Technologies", "TechnologyType"), ("Civics", "CivicType")):
            for r in _rows(table):
                t = r.get(key)
                if t:
                    era = r.get("EraType") or ""
                    out[t] = (era, eras.get(era, 0))
        _tech_era_cache = out
    return _tech_era_cache


def _era_of(prereq: str) -> tuple[str, int]:
    if not prereq:
        return "ERA_ANCIENT", _era_indices().get("ERA_ANCIENT", 1)
    return _tech_eras().get(prereq, ("", 0))


def _era_cn(era_type: str) -> str:
    for r in _rows("Eras"):
        if r.get("EraType") == era_type:
            return r.get("Name") or era_type
    return era_type or "?"


_all_uu_types: set[str] | None = None


def _uu_types() -> set[str]:
    """`UnitReplaces` 里登记的特色单位(替换型)。"""
    global _all_uu_types
    if _all_uu_types is None:
        _all_uu_types = {
            r["CivUniqueUnitType"]
            for r in _rows("UnitReplaces")
            if r.get("CivUniqueUnitType")
        }
    return _all_uu_types


# 既不属于任何文明、普通对手也拿不到的特殊单位。
# UNIT_WARRIOR_MONK 是信仰购买的特殊单位(需要特定信条),CS40 却没有前置科技
# —— 把它算进"同期最强通用单位"会让远古时代的对照基线凭空拔到 40,
# 所有远古 UU 的代差都会被算成 0。这是实测踩到的坑,不是预防性排除。
_NON_GENERIC_EXACT = frozenset({"UNIT_WARRIOR_MONK"})

_civ_tokens_cache: set[str] | None = None


def _civ_tokens() -> set[str]:
    """全部民族前缀词。用来识别「这是某家的特色单位」。

    主来源是 UnitReplaces 里 UU 标识的首段(ROMAN/GREEK/KONGO/…),
    再并上 _CIV_ADJECTIVE 的值 —— 后者覆盖 **不替换任何单位** 的独立 UU
    (苏美尔战车、斯基泰弓骑手、印度战象、中国虎蹲炮),它们不在
    UnitReplaces 里,却同样不是通用单位。
    """
    global _civ_tokens_cache
    if _civ_tokens_cache is None:
        toks = set(_CIV_ADJECTIVE.values()) | set(_CIV_ADJECTIVE.keys())
        for t in _uu_types():
            stem = t.replace("UNIT_", "")
            if "_" in stem:
                toks.add(stem.split("_", 1)[0])
        _civ_tokens_cache = toks
    return _civ_tokens_cache


def is_generic_unit(unit_type: str) -> bool:
    """这个单位是否是**任何对手都能造**的通用单位。

    通用性是代差计算的分母:只有通用单位才能代表"普通对手的兵长什么样"。
    蛮族专属单位同样排除 —— 它们不构成文明间的战力基线。
    """
    if not unit_type or unit_type in _NON_GENERIC_EXACT:
        return False
    stem = unit_type.replace("UNIT_", "")
    if stem.startswith("BARBARIAN_"):
        return False
    if unit_type in _uu_types():
        return False
    head = stem.split("_", 1)[0]
    return head not in _civ_tokens()


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniqueUnit:
    """一个特色单位的硬事实 + 从表里算出的对照结论。"""

    unit_type: str
    name: str
    replaces: str
    replaces_name: str
    combat: int
    ranged: int
    cost: int
    moves: int
    prereq: str
    era: str
    era_index: int
    domain: str
    promotion_class: str
    strategic_resource: str
    base_combat: int = 0
    base_cost: int = 0
    base_resource: str = ""
    attribution: str = "runtime"  # "runtime"(实机权威) / "inferred"(前缀推断)

    @property
    def power(self) -> int:
        """有效战力:远程单位看远程值,近战看近战值。"""
        return self.ranged if self.ranged > self.combat else self.combat

    @property
    def base_power(self) -> int:
        return self.base_combat

    @property
    def resource_free(self) -> bool:
        """UU 不吃战略资源而它替换的单位吃 —— 这是最容易被忽略的优势。

        校准局实证:手写模板把这一条写反了(说"军团需要铁"),
        导致整局都在找铁,而军团根本不需要。
        """
        return not self.strategic_resource and bool(self.base_resource)


@dataclass(frozen=True)
class UUWindow:
    """UU 的战力窗口。三个端点全部机械导出,见 docstring §2。"""

    uu: UniqueUnit
    peer_type: str  # 同期最强通用单位
    peer_name: str
    peer_power: int
    # 同期最强的**无战略资源门槛**通用单位。对手缺铁/缺马时,它才是
    # 战场上真正会出现的对照组 —— 校准局的军团正是靠这一条打出代差。
    open_peer_name: str = ""
    open_peer_power: int = 0
    counter_type: str = ""  # 第一个追平/超过它的通用单位
    counter_name: str = ""
    counter_era: str = ""
    counter_era_index: int = 0
    counter_prereq: str = ""
    # —— 可建造窗口(2026-08-05,外部高手攻略的 timing push 框架)——
    # 上面几个字段回答"它比同期强多少、优势能撑多久";这两个回答
    # **"我还能造多久"** —— 完全不同的一件事,而且是自己关掉的:
    # 一旦研究出升级目标的科技,前一时代的单位就再也造不出来了
    # (UnitUpgrades + Units.PrereqTech,机制可验证)。
    # 所以窗口关闭可倒计时、可预测,甚至可以【主动暂缓研究】来延长。
    # T335 罗马实证:军团窗口 = 铁器时代 → 学徒制,整个窗口期没打过仗,
    # 等到 T314 想认真开战时主力已经是弩手,文明身份等于没用过。
    upgrade_type: str = ""  # UnitUpgrades 里的升级目标
    upgrade_name: str = ""
    upgrade_prereq: str = ""  # 研究出它 = 窗口关闭(不可逆)

    @property
    def buildable_window_known(self) -> bool:
        """升级链数据齐全时才谈可建造窗口 —— 缺数据就不猜。"""
        return bool(self.uu.prereq and self.upgrade_prereq)

    @property
    def edge(self) -> int:
        return self.uu.power - self.peer_power

    @property
    def open_edge(self) -> int:
        """对手没有战略资源时的代差。"""
        return self.uu.power - self.open_peer_power

    @property
    def edge_ratio(self) -> float:
        return self.uu.power / self.peer_power if self.peer_power else 1.0

    @property
    def era_span(self) -> int:
        """窗口跨几个时代。0 = 一开门就有人追平(窗口价值极低)。"""
        if self.counter_era_index <= 0:
            return 9  # 全游戏没有通用单位追平 —— 窗口不关
        return max(0, self.counter_era_index - self.uu.era_index)

    @property
    def value(self) -> float:
        """窗口价值 = 代差比 × 窗口长度。用于路线权重,不是绝对分。"""
        return round((self.edge_ratio - 1.0) * max(1, self.era_span), 3)


@dataclass
class CivKit:
    """一个文明的能力向量。缺的字段就是缺 —— 不猜。"""

    civ: str = ""
    leader: str = ""
    uu: list[UniqueUnit] = field(default_factory=list)
    windows: list[UUWindow] = field(default_factory=list)
    unique_districts: list[str] = field(default_factory=list)
    unique_buildings: list[str] = field(default_factory=list)
    unique_improvements: list[str] = field(default_factory=list)
    ability_tags: set[str] = field(default_factory=set)
    leans: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def best_window(self) -> UUWindow | None:
        return max(self.windows, key=lambda w: w.value) if self.windows else None

    @property
    def military_window(self) -> float:
        w = self.best_window
        return w.value if w else 0.0

    def lean(self, key: str) -> float:
        return self.leans.get(key, 0.0)


# ---------------------------------------------------------------------------
# 推导
# ---------------------------------------------------------------------------


def _mk_unique_unit(
    uu_type: str, base_type: str, attribution: str
) -> UniqueUnit | None:
    units = _units()
    row = units.get(uu_type)
    if not row:
        return None
    base = units.get(base_type, {})
    prereq = row.get("PrereqTech") or row.get("PrereqCivic") or ""
    era, era_idx = _era_of(prereq)
    return UniqueUnit(
        unit_type=uu_type,
        name=row.get("Name") or uu_type,
        replaces=base_type,
        replaces_name=base.get("Name") or base_type,
        combat=_num(row, "Combat"),
        ranged=_ranged(row),
        cost=_num(row, "Cost"),
        moves=_num(row, "BaseMoves"),
        prereq=prereq,
        era=era,
        era_index=era_idx,
        domain=row.get("Domain") or "",
        promotion_class=row.get("PromotionClass") or "",
        strategic_resource=row.get("StrategicResource") or "",
        base_combat=_num(base, "Combat"),
        base_cost=_num(base, "Cost"),
        base_resource=base.get("StrategicResource") or "",
        attribution=attribution,
    )


def unit_row(unit_type: str) -> dict:
    """按标识取 Units 表原始行。查不到返回空字典 —— 调用方用 .get 兜底。"""
    return _units().get(unit_type, {})


def infer_unique_units(civ_type: str) -> dict[str, str]:
    """兜底:按民族形容词前缀猜本文明的 UU。有实机数据时不该走这里。

    返回 {UU标识: 被替换单位标识}。猜不出返回空字典 —— 不硬凑。
    """
    token = (civ_type or "").upper().replace("CIVILIZATION_", "").strip()
    if not token:
        return {}
    adj = _CIV_ADJECTIVE.get(token, token)

    def mine(unit_type: str) -> bool:
        stem = unit_type.replace("UNIT_", "")
        return stem.startswith(adj + "_") or stem.startswith(token + "_")

    out: dict[str, str] = {}
    for r in _rows("UnitReplaces"):
        uu = r.get("CivUniqueUnitType") or ""
        base = r.get("ReplacesUnitType") or ""
        if uu and base and mine(uu):
            out[uu] = base
    # 不替换任何单位的独立 UU(斯基泰弓骑手/苏美尔战车/印度战象/中国虎蹲炮)
    # 不在 UnitReplaces 里,只能从 Units 表按前缀捞。它们同样是特色单位,
    # 漏掉就等于告诉调用方"这个文明没有特色单位"——比不报还糟。
    for unit_type in _units():
        if (
            unit_type not in out
            and mine(unit_type)
            and _num(_units()[unit_type], "Combat") + _ranged(_units()[unit_type]) > 0
        ):
            out[unit_type] = ""
    return out


def _ranged(row: dict) -> int:
    """单位的远程战力。攻城类走 Bombard 列,普通远程走 RangedCombat 列。

    2026-08-11 T291 实测:UNIT_ARTILLERY 是 Combat=60 / RangedCombat=0 /
    Bombard=80,而 UNIT_FIELD_CANNON 是 RangedCombat=60 / Bombard=0。只读
    RangedCombat 会把整条攻城线(投石机/攻城炮/大炮)的主武器读成 0 ——
    它们会被当成纯近战单位,而攻城单位恰恰是啃城墙唯一高效的手段。
    """
    return max(_num(row, "RangedCombat"), _num(row, "Bombard"))


def _generic_military_units() -> list[dict]:
    """全部【通用】军事单位(排除各家特色单位与蛮族专属),按时代序号排好。"""
    out = []
    for row in _units().values():
        t = row.get("UnitType") or ""
        if not is_generic_unit(t):
            continue
        if _num(row, "Combat") <= 0 and _ranged(row) <= 0:
            continue
        prereq = row.get("PrereqTech") or row.get("PrereqCivic") or ""
        _era, idx = _era_of(prereq)
        out.append({**row, "_era_index": idx})
    out.sort(key=lambda r: (r["_era_index"], _num(r, "Cost")))
    return out


def compute_window(uu: UniqueUnit) -> UUWindow:
    """算出 UU 的战力窗口。规则见 docstring §2。"""
    # 对照组的选取顺序:同兵种 → 同领域 → 全部。
    # 同兵种优先是刻意的:武士(近战 48)拿去和骑士(重骑 50)比会算出负代差,
    # 但"我不造武士就造披甲战士(45)"才是真实的替代关系。跨兵种的最强单位
    # 回答的是"战场上最猛的是谁",不是"这个 UU 给我带来了什么"。
    generics = _generic_military_units()
    pool = [
        r
        for r in generics
        if (r.get("Domain") or "") == uu.domain
        and (r.get("PromotionClass") or "") == uu.promotion_class
    ]
    if not pool:
        pool = [r for r in generics if (r.get("Domain") or "") == uu.domain]
    if not pool:
        pool = generics

    def power(r: dict) -> int:
        c, rg = _num(r, "Combat"), _ranged(r)
        return rg if rg > c else c

    # 同期(时代序号 ≤ UU 时代)最强的通用单位 —— 代差的对照组
    same_era = [r for r in pool if r["_era_index"] <= uu.era_index]
    peer = max(same_era, key=power) if same_era else None
    open_pool = [r for r in same_era if not (r.get("StrategicResource") or "")]
    open_peer = max(open_pool, key=power) if open_pool else None

    # 第一个追平/超过它的通用单位(必须晚于 UU 自己的时代)
    later = [r for r in pool if r["_era_index"] > uu.era_index and power(r) >= uu.power]
    counter = min(later, key=lambda r: (r["_era_index"], -power(r))) if later else None
    c_prereq = (
        (counter.get("PrereqTech") or counter.get("PrereqCivic") or "")
        if counter
        else ""
    )
    c_era, c_idx = _era_of(c_prereq) if counter else ("", 0)

    # 可建造窗口的关门点:UnitUpgrades 的升级目标 + 它的前置科技。
    # 查不到就留空 —— buildable_window_known 会据此静默。
    up_type = _upgrade_targets().get(uu.unit_type, "")
    up_row = _units().get(up_type) or {}

    return UUWindow(
        uu=uu,
        peer_type=(peer or {}).get("UnitType", ""),
        peer_name=(peer or {}).get("Name", ""),
        peer_power=power(peer) if peer else 0,
        open_peer_name=(open_peer or {}).get("Name", ""),
        open_peer_power=power(open_peer) if open_peer else 0,
        counter_type=(counter or {}).get("UnitType", ""),
        counter_name=(counter or {}).get("Name", ""),
        counter_era=c_era,
        counter_era_index=c_idx,
        counter_prereq=c_prereq,
        upgrade_type=up_type,
        upgrade_name=up_row.get("Name", "") or up_type,
        upgrade_prereq=up_row.get("PrereqTech") or "",
    )


def derive(
    civ_type: str = "",
    leader: str = "",
    unique_units: dict[str, str] | None = None,
    unique_districts: list[str] | None = None,
    unique_buildings: list[str] | None = None,
    unique_improvements: list[str] | None = None,
) -> CivKit:
    """现场推导本文明的能力向量。

    `unique_units` 是实机权威数据(world_model.my_unique_units,
    格式 {UU标识: 被替换单位标识})。传空时退回前缀推断并标注。
    永不抛异常 —— 推导失败返回空套件,调用方照常出简报。
    """
    kit = CivKit(civ=civ_type or "", leader=leader or "")
    try:
        pairs = dict(unique_units or {})
        attribution = "runtime"
        if not pairs:
            pairs = infer_unique_units(civ_type)
            attribution = "inferred"
            if pairs:
                kit.notes.append(
                    "UU 归属为【前缀推断】(导出数据里没有文明→UU 连接表);"
                    "实机世界扫描一旦跑过就会用权威数据覆盖"
                )
        for uu_type, base in pairs.items():
            u = _mk_unique_unit(uu_type, base, attribution)
            if u:
                kit.uu.append(u)
                kit.windows.append(compute_window(u))
        kit.unique_districts = list(unique_districts or [])
        kit.unique_buildings = list(unique_buildings or [])
        kit.unique_improvements = list(unique_improvements or [])
        kit.leans = _leans(kit)
        if not kit.uu and not kit.unique_districts:
            kit.notes.append(
                "没有查到本文明的特色单位/区域 —— 要么该文明的特色是"
                "领袖能力(导出表里没有归属列),要么世界扫描还没跑过。"
                "此时不要假设「这个文明没特色」,按通用打法起手并留意实机提示"
            )
    except Exception:
        pass
    return kit


def _leans(kit: CivKit) -> dict[str, float]:
    """路线倾向 0-1。只从**有数据**的维度算,没数据的维度不出现在字典里。"""
    out: dict[str, float] = {}
    w = kit.best_window
    if w is not None:
        # 军事倾向 = 窗口价值归一化,截断在 [0,1]。
        # 负窗口(UU 比同期通用兵还弱)不是"负倾向",只是"没有军事优势"。
        out["military"] = round(max(0.0, min(1.0, w.value * 2.0)), 3)
    naval = [u for u in kit.uu if u.domain == "DOMAIN_SEA"]
    if naval:
        out["naval"] = round(min(1.0, 0.4 + 0.3 * len(naval)), 3)
    for d in kit.unique_districts:
        stem = d.replace("DISTRICT_", "").upper()
        for key, lean in _DISTRICT_LEAN.items():
            if stem.startswith(key):
                out[lean] = max(out.get(lean, 0.0), 0.8)
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_window(w: UUWindow, researched: set[str] | None = None) -> list[str]:
    """一个 UU 窗口的人话描述。researched 传已研科技集合时会标"已解锁"。"""
    u = w.uu
    researched = researched or set()
    unlocked = (not u.prereq) or u.prereq in researched
    status = "✅ 已解锁" if unlocked else f"⏳ 需 {u.prereq.replace('TECH_', '')}"
    # 独立 UU(不替换任何单位,如战象/虎蹲炮)没有 replaces —— 别渲染成「替换,」
    repl = (
        f"替换{u.replaces_name}," if u.replaces_name else "独立单位(不替换任何通用兵),"
    )
    lines = [
        f"【{u.name}】{status} —— {repl}"
        f"战力 {u.power} vs 同期最强通用 {w.peer_name or '?'} {w.peer_power}"
        f"({'+' if w.edge >= 0 else ''}{w.edge})"
    ]
    if u.resource_free:
        extra = ""
        if w.open_peer_power and w.open_edge > w.edge:
            extra = (
                f";对手若也缺该资源,他能造的最强通用兵是 "
                f"{w.open_peer_name} {w.open_peer_power},你的代差实际是 "
                f"+{w.open_edge} 而不是 {'+' if w.edge >= 0 else ''}{w.edge}"
            )
        lines.append(
            f"  ⭐ 它**不需要战略资源**,而 {u.replaces_name} 需要 "
            f"{u.base_resource.replace('RESOURCE_', '')} —— "
            f"没有该资源时你依然能造出比对手更强的兵{extra}。这条最容易被漏掉"
        )
    elif u.strategic_resource:
        lines.append(
            f"  资源门槛:{u.strategic_resource.replace('RESOURCE_', '')}"
            f"(没有就造不出来,开局要先确认领土内有没有)"
        )
    if u.cost and u.base_cost:
        diff = u.cost - u.base_cost
        if diff:
            lines.append(
                f"  成本 {u.cost}(比 {u.replaces_name} {'贵' if diff > 0 else '便宜'}"
                f" {abs(diff)})"
            )
    if w.counter_type:
        lines.append(
            f"  窗口:{_era_cn(u.era)} 开启 → {_era_cn(w.counter_era)} 关闭"
            f"(对手研出 {w.counter_prereq.replace('TECH_', '')} 造 {w.counter_name}"
            f" 就追平),跨 {w.era_span} 个时代,窗口价值 {w.value}"
        )
    else:
        lines.append(
            f"  窗口:{_era_cn(u.era)} 开启,全游戏没有通用单位追平它的战力"
            f"(窗口价值 {w.value})"
        )
    return lines


# 窗口价值分档。刻意留出中间档:校准局证明「UU 代差不大但军力比 2.28×」
# 时开战依然是对的 —— **是否开战由 L1 军力比决定,UU 窗口只决定什么时候
# 开战性价比最高**。把这两件事混在一起是 v3 决策树的老毛病。
_WINDOW_STRONG = 0.30
_WINDOW_MARGINAL = 0.10


def _window_verdict(w: UUWindow) -> str:
    if w.value >= _WINDOW_STRONG:
        return (
            "**代差型**优势:窗口内主动开战的收益远高于发展,"
            "窗口关闭后同样的兵就只是普通兵了"
        )
    if w.value >= _WINDOW_MARGINAL:
        return (
            "边际优势:单靠它不足以成为开战理由。"
            "是否开战看 L1 军力比,这个窗口只告诉你**什么时候打性价比最高**"
        )
    return "不构成军事代差:把它当同期单位用即可,不要为了兑现它而调整科技路线或提前开战"


# 可建造窗口的四个状态(timing push 框架,2026-08-05)
WINDOW_LOCKED = "locked"  # 前置科技还没到 —— 窗口未开
WINDOW_OPEN = "open"  # 能造了,关门科技还没研究 —— 黄金期
WINDOW_CLOSING = "closing"  # 关门科技已可研究/正在研究 —— 最后通牒
WINDOW_CLOSED = "closed"  # 关门科技已完成 —— 永久失去,不可逆


def window_state(
    w: UUWindow,
    researched: set[str] | None,
    researching: str = "",
    available: set[str] | None = None,
) -> str:
    """UU 可建造窗口的当前状态。数据不全返回空串(调用方据此静默)。

    判据全部是机制事实,没有一个经验数字:
      · 开门 = UU 的前置科技已完成
      · 关门 = 升级目标的前置科技已完成(此后永远造不出 UU)
      · 即将关门 = 关门科技正在研究,或已经可研究
    """
    if not w.buildable_window_known:
        return ""
    done = researched or set()
    if w.upgrade_prereq in done:
        return WINDOW_CLOSED
    if w.uu.prereq not in done:
        return WINDOW_LOCKED
    if researching == w.upgrade_prereq or w.upgrade_prereq in (available or set()):
        return WINDOW_CLOSING
    return WINDOW_OPEN


def render(kit: CivKit, researched: set[str] | None = None) -> str:
    """完整套件段。没有任何内容时返回空串。"""
    try:
        return _render_inner(kit, researched)
    except Exception:
        return ""


def _render_inner(kit: CivKit, researched: set[str] | None) -> str:
    lines: list[str] = []
    head = "== 文明套件(运行时从 gamedata 推导,非手写模板)=="
    inferred = any(u.attribution == "inferred" for u in kit.uu)
    if inferred:
        head += "〔归属推断〕"
    lines.append(head)

    if kit.windows:
        for w in sorted(kit.windows, key=lambda w: -w.value):
            lines.extend(render_window(w, researched))
        best = kit.best_window
        if best is not None:
            lines.append(f"⇒ 军事窗口价值 {best.value} —— {_window_verdict(best)}")
    if kit.unique_districts or kit.unique_buildings or kit.unique_improvements:
        for label, items in (
            ("独特区域", kit.unique_districts),
            ("独特建筑", kit.unique_buildings),
            ("独特改良", kit.unique_improvements),
        ):
            if items:
                lines.append(f"{label}:{'、'.join(items)}")
    if kit.leans:
        lines.append(
            "路线倾向:"
            + " ‖ ".join(
                f"{k} {v:.2f}"
                for k, v in sorted(kit.leans.items(), key=lambda kv: -kv[1])
            )
        )
    for n in kit.notes:
        lines.append(f"({n})")
    return "\n".join(lines) if len(lines) > 1 else ""
