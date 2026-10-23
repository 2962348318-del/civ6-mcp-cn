"""政策卡自动检查 —— 治"政策什么都不会灵活调整,什么都要我提醒"。

用户原话。问题不是模型不会排卡,是【没人在它该换卡的那一刻提醒它】——
政策卡的最佳配置随局势每回合都在变,而模型不会主动每回合去查 get_policies。

所以这里做的是:服务端每回合跑一遍确定性检查,把"现在这张卡该换"直接
写进简报。规则全部可判定,不含主观判断。

卡名和槽位来自 docs/gamedata/Policies.md(103 张)与 Government_SlotCounts.md。
⚠️ 卡的具体数值效果(+50% 之类)未逐条查证,所以文案只说"该插哪张",
不承诺具体加成幅度 —— 写错的数字比不写更糟。
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 情境 → 推荐政策卡
# ---------------------------------------------------------------------------
# 每条:(卡标识, 中文名, 槽位, 触发条件描述, 为什么)


@dataclass(frozen=True)
class Card:
    policy: str
    name: str
    slot: str
    why: str


# 生产开拓者时:殖民 +50% 开拓者产能
CARD_COLONIZATION = Card(
    "POLICY_COLONIZATION",
    "殖民",
    "SLOT_ECONOMIC",
    "正在生产开拓者 —— 这张卡直接加快开拓者产能",
)
# 生产建造者时:服役
CARD_ILKUM = Card(
    "POLICY_ILKUM", "服役", "SLOT_ECONOMIC", "正在生产建造者 —— 这张卡加快建造者产能"
)
# 生产近战/远程单位时:斯巴达教育
CARD_AGOGE = Card(
    "POLICY_AGOGE",
    "斯巴达教育",
    "SLOT_MILITARY",
    "正在生产古代近战/远程单位 —— 这张卡加快其产能",
)
# 受攻击/城防吃紧:棱堡
CARD_BASTIONS = Card(
    "POLICY_BASTIONS",
    "棱堡",
    "SLOT_MILITARY",
    "城市受威胁 —— 这张卡提升城防与城市远程攻击",
)
# 蛮族威胁:纪律(对蛮族战斗加成)
CARD_DISCIPLINE = Card(
    "POLICY_DISCIPLINE",
    "纪律",
    "SLOT_MILITARY",
    "蛮族威胁中 —— 这张卡专门加成对蛮族作战",
)
# 探索不足:调查(侦察单位视野/移动)
CARD_SURVEY = Card(
    "POLICY_SURVEY", "调查", "SLOT_MILITARY", "探索严重不足 —— 这张卡加快侦察"
)
# 走科学:自然哲学(学院加成)
CARD_NATURAL_PHILOSOPHY = Card(
    "POLICY_NATURAL_PHILOSOPHY",
    "自然哲学",
    "SLOT_ECONOMIC",
    "锁定科学胜利 —— 这张卡放大学院产出",
)
# 走文化:文学传统 / 后续文化卡
CARD_LITERARY = Card(
    "POLICY_LITERARY_TRADITION",
    "文学传统",
    "SLOT_GREAT_PERSON",
    "锁定文化胜利 —— 加快文化类伟人点数",
)
# 走宗教:经文
CARD_SCRIPTURE = Card(
    "POLICY_SCRIPTURE", "经文", "SLOT_ECONOMIC", "锁定宗教胜利 —— 放大圣地信仰产出"
)
# 走统治:演习(机动单位) / 经验
CARD_MANEUVER = Card(
    "POLICY_MANEUVER", "演习", "SLOT_MILITARY", "锁定统治胜利 —— 加快骑乘单位产能"
)
# 城市多:城市规划(产能)
CARD_URBAN_PLANNING = Card(
    "POLICY_URBAN_PLANNING",
    "城市规划",
    "SLOT_ECONOMIC",
    "通用产能卡,前期没有更好选择时优先插它",
)
# 建区域:强迫劳役
CARD_CORVEE = Card(
    "POLICY_CORVEE", "强迫劳役", "SLOT_ECONOMIC", "正在建造区域 —— 这张卡加快区域产能"
)


@dataclass
class PolicyAdvice:
    """一条政策建议。severity: warn(该换) / hint(可以更好)。"""

    severity: str
    text: str


@dataclass
class PolicyContext:
    """检查所需的事实,全部由服务端查询提供。"""

    slots: dict[str, list[str]] = None  # 槽位类型 -> 当前插的卡(空槽为 "")
    available: set[str] = None  # 当前可用(已解锁)的卡
    producing: list[str] = None  # 各城正在生产的项目名
    threat_level: str = "GREEN"
    victory_type: str = ""
    exploration: float = 1.0
    government_changed: bool = False
    free_change_window: bool = False  # 本回合换卡免费
    government: str = ""  # 当前政体 GovernmentType
    completed_civics: set[str] = None  # 已完成市政(判断哪些政体已解锁)

    def __post_init__(self):
        if self.slots is None:
            self.slots = {}
        if self.available is None:
            self.available = set()
        if self.producing is None:
            self.producing = []
        if self.completed_civics is None:
            self.completed_civics = set()

    def empty_slots(self) -> list[str]:
        out = []
        for slot_type, cards in self.slots.items():
            for c in cards:
                if not c:
                    out.append(slot_type)
        return out

    def has_card(self, policy: str) -> bool:
        return any(policy in cards for cards in self.slots.values())

    def slot_has_room(self, slot_type: str) -> bool:
        """该槽位有空位,或者装的是可被替换的通用卡。"""
        cards = self.slots.get(slot_type, [])
        return any(not c for c in cards)


def _suggest(
    ctx: PolicyContext, card: Card, severity: str = "warn", why: str | None = None
) -> PolicyAdvice | None:
    """如果这张卡可用、还没插、且有对应空槽,就给建议。

    why 覆盖卡面上的默认理由。2026-08-08 T191 实机:「演习」有两条触发路径
    (正在造骑乘单位 / 锁定统治胜利),理由文本却只有一份写死的
    "锁定统治胜利 —— 加快骑乘单位产能"。当时胜利路线明明是 T190 刚锁的
    SCIENCE,简报却因为京都在造重型战车喊出"锁定统治胜利" —— 刚改完道就
    收到一条把你往回拽的假信号。触发条件是什么,理由就必须说什么。
    """
    if card.policy not in ctx.available:
        return None
    if ctx.has_card(card.policy):
        return None
    room = ctx.slot_has_room(card.slot)
    tail = "" if room else f"(需换掉 {card.slot} 里现有的卡)"
    return PolicyAdvice(
        severity,
        f"插上「{card.name}」{tail} — {why or card.why}",
    )


def _slot_type_of(policy_type: str) -> str:
    """这张卡该插哪类槽。gamedata Policies.GovernmentSlotType,查不到返回空串。"""
    for r in _gd_rows("Policies"):
        if r.get("PolicyType") == policy_type:
            return str(r.get("GovernmentSlotType") or "")
    return ""


def _has_card_for_slot(ctx: PolicyContext, slot_type: str) -> bool:
    """这个空槽现在有卡能插吗。

    通用槽(SLOT_WILDCARD)接受任何卡,所以只要手里有卡就算填得上;
    其余槽要卡的 GovernmentSlotType 对得上。查不到 gamedata 时
    **按"填得上"处理** —— 数据缺失该 fail-open,不能因为查不到就静默
    一条真实的空槽提醒。
    """
    if not ctx.available:
        return False
    if slot_type == "SLOT_WILDCARD":
        return True
    for p in ctx.available:
        st = _slot_type_of(p)
        if not st or st == slot_type:
            return True
    return False


def _gov_slots() -> dict[str, list[str]]:
    """政体 -> 槽位清单(展开 NumSlots)。数据源 Government_SlotCounts。"""
    out: dict[str, list[str]] = {}
    for r in _gd_rows("Government_SlotCounts"):
        gov = str(r.get("GovernmentType") or "")
        slot = str(r.get("GovernmentSlotType") or "")
        if not gov or not slot:
            continue
        try:
            n = int(r.get("NumSlots") or 0)
        except (TypeError, ValueError):
            n = 0
        out.setdefault(gov, []).extend([slot] * n)
    return out


def government_upgrade_advice(ctx: PolicyContext) -> PolicyAdvice | None:
    """已经解锁了槽位更多的政体,却还挂着旧政体 —— 纯亏,而且是复利亏。

    2026-08-07 苏美尔国王局实机:政治哲学早就研完(防御战术都能选了),
    我却在 **T116** 还挂着酋邦(2 槽)。换成古典共和当场变 4 槽 ——
    整整 100 回合每回合少吃两张卡的加成。

    为什么之前没发现:tech_path 里那条「下一个政体」算的是【还没研的】
    市政目标(酋邦时它指向王权神授,42 回合外),它假定你已经用上了
    当前能用的最好政体。没有任何一处检查「手上这个是不是最好的」。
    这与 #3「政策卡从不复审」是同一个病:系统只会推进,不会回头看。

    判据只用槽位总数 —— 政体特性(奇观加成/战斗经验/伟人点)是决策,
    槽位数是事实。少一个槽 = 少一整张卡,任何特性都换不回来。
    """
    cur = ctx.government
    if not cur:
        return None
    table = _gov_slots()
    cur_slots = table.get(cur)
    if not cur_slots:
        return None  # gamedata 缺失 → 静默,不瞎报
    unlocked: list[tuple[int, str, list[str]]] = []
    for r in _gd_rows("Governments"):
        gov = str(r.get("GovernmentType") or "")
        if not gov or gov == cur:
            continue
        prereq = str(r.get("PrereqCivic") or "")
        # PrereqCivic 为空 = 起始政体;否则要求该市政已完成
        if prereq and prereq not in ctx.completed_civics:
            continue
        slots = table.get(gov) or []
        if len(slots) > len(cur_slots):
            unlocked.append((len(slots), gov, slots))
    if not unlocked:
        return None
    unlocked.sort(key=lambda t: -t[0])
    best_n = unlocked[0][0]
    gain = best_n - len(cur_slots)

    def _fmt(slots: list[str]) -> str:
        from collections import Counter

        c = Counter(s.replace("SLOT_", "") for s in slots)
        return "+".join(f"{k}×{v}" for k, v in sorted(c.items()))

    opts = "; ".join(f"{g}({_fmt(s)})" for _, g, s in unlocked[:3])
    return PolicyAdvice(
        "warn",
        f"政体还挂着 {cur}({len(cur_slots)} 槽),但已解锁 {best_n} 槽的政体 —— "
        f"每回合白扔 {gain} 张卡的加成。用 change_government 换:{opts}。"
        f"(首次升级免无政府状态;换完槽位会变,必须重排一遍政策卡)",
    )


def check_policies(ctx: PolicyContext) -> list[PolicyAdvice]:
    """跑一遍全部确定性检查,返回建议列表(已按重要性排序)。"""
    out: list[PolicyAdvice] = []
    seen: set[str] = set()

    def add(a: PolicyAdvice | None):
        if a and a.text not in seen:
            seen.add(a.text)
            out.append(a)

    prod = " ".join(ctx.producing).upper()

    # --- 最高优先:政体本身就落后。补空槽之前先把槽位数拉满 ---
    # 顺序有意为之:换政体会重排全部槽位,先补卡再换 = 白补一次。
    add(government_upgrade_advice(ctx))

    # --- 次高优先:空槽。空着就是纯亏 ---
    # ⚠️ 但只报【填得上】的空槽(2026-08-07 实机):苏美尔局 T5,法典还没研完,
    # 一张卡都没解锁,简报却在喊"2 个空政策槽,用 set_policies 补上"——
    # 一条看起来可执行、实际做不了的建议。这比纯噪音更伤:agent 会真的去调
    # set_policies,失败后开始怀疑整个建议层。
    fillable = [s for s in ctx.empty_slots() if _has_card_for_slot(ctx, s)]
    if fillable:
        add(
            PolicyAdvice(
                "warn",
                f"有 {len(fillable)} 个空政策槽({', '.join(sorted(set(fillable)))})"
                f" —— 空着等于白扔加成,用 set_policies 补上",
            )
        )

    # --- 换政体后必须重排 ---
    if ctx.government_changed:
        add(PolicyAdvice("warn", "刚换过政体,槽位配置已变 —— 必须重新排一遍政策卡"))

    # --- 免费换卡窗口 ---
    if ctx.free_change_window:
        add(PolicyAdvice("hint", "本回合换卡免费(刚完成市政),要调整就趁现在"))

    # --- 威胁相关 ---
    if ctx.threat_level == "RED":
        add(_suggest(ctx, CARD_DISCIPLINE))
        add(_suggest(ctx, CARD_BASTIONS))

    # --- 生产匹配 ---
    if "SETTLER" in prod:
        add(_suggest(ctx, CARD_COLONIZATION))
    if "BUILDER" in prod:
        add(_suggest(ctx, CARD_ILKUM))
    if "DISTRICT" in prod:
        add(_suggest(ctx, CARD_CORVEE))
    if any(
        k in prod for k in ("WARRIOR", "SLINGER", "ARCHER", "SPEARMAN", "SWORDSMAN")
    ):
        add(_suggest(ctx, CARD_AGOGE))
    if any(k in prod for k in ("HORSEMAN", "HEAVY_CHARIOT", "KNIGHT")):
        # 生产触发 —— 理由必须说生产,不能沿用卡面的"锁定统治胜利"
        add(
            _suggest(
                ctx, CARD_MANEUVER, why="正在生产骑乘单位 —— 这张卡加快骑乘单位产能"
            )
        )

    # --- 探索不足 ---
    if ctx.exploration < 0.10:
        add(_suggest(ctx, CARD_SURVEY, "hint"))

    # --- 胜利路线匹配 ---
    vt = ctx.victory_type
    if vt == "SCIENCE":
        add(_suggest(ctx, CARD_NATURAL_PHILOSOPHY))
    elif vt == "CULTURE":
        add(_suggest(ctx, CARD_LITERARY))
    elif vt == "RELIGION":
        add(_suggest(ctx, CARD_SCRIPTURE))
    elif vt == "DOMINATION":
        add(_suggest(ctx, CARD_MANEUVER))

    # --- 兜底:有空经济槽又没别的建议时,城市规划 ---
    if not out and ctx.slot_has_room("SLOT_ECONOMIC"):
        add(_suggest(ctx, CARD_URBAN_PLANNING, "hint"))

    # warn 排在 hint 前面
    out.sort(key=lambda a: 0 if a.severity == "warn" else 1)
    return out


def render_policy_advice(advice: list[PolicyAdvice], max_items: int = 3) -> str:
    """渲染成简报段。没有建议返回空串。"""
    if not advice:
        return ""
    lines = []
    for a in advice[:max_items]:
        icon = "⚠️" if a.severity == "warn" else "💡"
        lines.append(f"{icon} {a.text}")
    more = len(advice) - max_items
    if more > 0:
        lines.append(f"   (还有 {more} 条,用 get_policies 查看全部)")
    return "== 政策卡 ==\n" + "\n".join(lines)


# ===========================================================================
# 任务 V:政策卡微操(追加,上方已有代码未动一行)
#
# 典型场景:插「职业军队」→ 当回合升完所有单位 → 下回合换回。人类高手
# 用这一招省几百金。机制上的关键是**换卡窗口**:刚完成市政时换卡免费,
# 其余时候要花金 —— 所以这一招在免费窗口里是纯赚,窗口外要算账。
#
# 数据来源(动态查表,不硬编码卡名):
#   Policies.json         POLICY_PROFESSIONAL_ARMY / 职业军队 /
#                         PrereqCivic=CIVIC_MERCENARIES / SLOT_MILITARY
#   ModifierArguments     PROFESSIONAL_ARMY_UNITUPGRADEDISCOUNT Amount=50
#                         → 升级费用减 50%(导出值,不是记忆)
# ===========================================================================

import json as _json
from functools import lru_cache as _lru_cache
from pathlib import Path as _Path

_GAMEDATA_DIR = gamedata_path()

MICRO_MIN_UNITS = 2  # 少于 2 个单位可升级就不值得为它换卡


def _gd_rows(table: str) -> list[dict]:
    try:
        with (_GAMEDATA_DIR / f"{table}.json").open(encoding="utf-8") as fh:
            data = _json.load(fh)
        rows = data.get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@_lru_cache(maxsize=1)
def upgrade_discount_card() -> dict | None:
    """升级折扣卡 → {policy, name, civic, slot, discount}。查不到返回 None。

    识别方式:在 ModifierArguments 里找升级折扣修正器,反查它属于哪张卡。
    """
    try:
        # 1) 谁是"调整单位升级折扣"的修正器
        mod_ids = {
            r.get("ModifierId")
            for r in _gd_rows("Modifiers")
            if r.get("ModifierType")
            == "MODIFIER_PLAYER_ADJUST_UNIT_UPGRADE_DISCOUNT_PERCENT"
        }
        # 2) 哪张政策卡挂了它
        by_policy = {
            r.get("PolicyType"): r.get("ModifierId")
            for r in _gd_rows("PolicyModifiers")
            if r.get("ModifierId") in mod_ids
        }
        if not by_policy:
            return None
        policy_type, mid = next(iter(by_policy.items()))
        discount = 0
        for r in _gd_rows("ModifierArguments"):
            if r.get("ModifierId") == mid and r.get("Name") == "Amount":
                try:
                    discount = int(r.get("Value") or 0)
                except (TypeError, ValueError):
                    discount = 0
                break
        row = next(
            (r for r in _gd_rows("Policies") if r.get("PolicyType") == policy_type), {}
        )
        return {
            "policy": policy_type,
            "name": row.get("Name") or policy_type,
            "civic": row.get("PrereqCivic") or "",
            "slot": row.get("GovernmentSlotType") or "",
            "discount": discount,
        }
    except Exception:
        return None


def get_policy_micro_advice(
    upgradable_units: int,
    total_upgrade_cost: int,
    free_swap_available: bool,
    card_unlocked: bool = True,
    card_already_slotted: bool = False,
) -> list[str]:
    """政策卡微操窗口提醒。不值得操作时返回空列表,不抛异常。"""
    try:
        card = upgrade_discount_card()
        if card is None:
            return []
        try:
            units = int(upgradable_units)
            cost = int(total_upgrade_cost)
        except (TypeError, ValueError):
            return []
        if units < MICRO_MIN_UNITS or cost <= 0:
            return []
        if card_already_slotted:
            return [
                f"💡 {units} 个单位可升级,「{card['name']}」已在槽位上 —— "
                f"直接 upgrade_unit 逐个升级,别浪费这张卡占着的回合。"
            ]
        if not card_unlocked:
            return [
                f"💡 {units} 个单位可升级(总计 {cost} 金)。"
                f"「{card['name']}」({card['civic']} 解锁)能砍掉 "
                f"{card['discount']}% —— 还没解锁,先记着这个窗口。"
            ]

        saved = cost * card["discount"] // 100
        out = [
            f"💡 政策卡微操:{units} 个单位可升级,总费用 {cost} 金 —— "
            f"插「{card['name']}」({card['slot']})后只需 {cost - saved} 金,"
            f"省 {saved} 金(折扣 {card['discount']}% 为导出值)。"
        ]
        if free_swap_available:
            out.append(
                f"   本回合换卡免费(刚完成市政)—— 操作:set_policies 插"
                f"「{card['name']}」→ upgrade_unit 逐个升级 → 下回合换回原卡。"
                f"零成本,纯赚 {saved} 金。"
            )
        else:
            out.append(
                f"   ⚠️ 当前不在免费换卡窗口:换卡要花金。先算账 —— "
                f"省的 {saved} 金要能盖住换卡成本才划算,"
                f"否则等下个市政完成再操作。"
            )
        return out
    except Exception:
        return []
