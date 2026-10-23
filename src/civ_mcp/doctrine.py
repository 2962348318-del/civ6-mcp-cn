"""L3 策略引擎 —— 决策树 11/12/13,神级必需的三棵。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物)
═══════════════════════════════════════════════════════════════════════════

## 0. 和已有 42 个模块的分工

已有模块回答「这一格该干什么」(建造者去哪、政策换哪张、贸易路线开哪条)。
本模块回答**「这一局该怎么打」**,而且只在已有模块答不了的三个问题上出手:

    决策树 11 生存优先级   —— 劣势档下,**覆盖**其他所有建议
    决策树 12 议程管理     —— 神级下比军力更便宜的止战手段
    决策树 13 不对称交战   —— 用 AI 的战术弱点抵消它的产出优势

## 1. 门控:v3 的老毛病是「条件 → 动作」

方案 v4 §L3.2:v3 的决策树是无条件的,而校准局证伪了其中三条
(蛮族追杀、清营优先、按难度名硬编码城市数)。v4 的形式是

    条件 AND L1档位允许 AND 资源余力足够 → 动作

所以本模块的每棵树都先看 `PowerSnapshot.tier`(L1 实测档位),
拿不到档位时**降级为只报事实、不下指令** —— 坏传感器 + 决策树
= 自信的错误决策,比只提醒更糟。

## 2. 决策树 11 为什么要「覆盖」而不是「追加」

劣势档下最贵的不是做错事,是**同时做五件对的事**。校准局 T110 的
局面(科技 0.39×)如果还在按常规档推奇观/区域/使者,产能会被摊薄到
每条线都不成立。所以 11 号树命中时输出 SURVIVAL 档建议 ——
`advice.arbitrate()` 保证 SURVIVAL/BLOCKING 永不截断且排在最前,
天然实现"覆盖"。

## 3. 决策树 12 的数据基础(以及顺手修掉的一个缺陷)

`opponents.AGENDA_PROFILES` 有 43 条议程画像,但 `read_opponent()` 拿
`AgendaInfo.name` 去和 `AGENDA_*` 标识做子串匹配 —— 而 `name` 是
**本地化名**(中文),这个匹配基本永远失败。本模块改从
`docs/gamedata/Agendas.json` 建「本地化名 → AgendaType」索引,
外加 `HistoricalAgendas.json` 的「领袖 → 历史议程」直查,
两条路都走通才认。

## 4. 决策树 13 只说能算的

「AI 战术差,能吃掉 1.5-2 倍兵力」这个量级**没有实测支撑**
(方案 v4 §5 已诚实标注)。所以本树不给倍数,只给三条可判定的规则:
有城墙且无攻城单位 → 不打;残血 → 轮换;没集结 → 不发起。
这三条都能从 `CityInfo.wall_hp` / `UnitInfo.health` / 单位坐标直接算。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from civ_mcp.resources import gamedata_path

import json
from dataclasses import dataclass, field
from pathlib import Path

from civ_mcp import civ_kit
from civ_mcp.advice import Advice, Confidence, Priority
from civ_mcp.civ_kit import CivKit
from civ_mcp.opponents import AGENDA_PROFILES, AgendaProfile
from civ_mcp.power_balance import DIM_CN, TIER_CN, PowerSnapshot, PowerTier

_GAMEDATA = gamedata_path()

# 屯兵判定:离我方城市这么近的敌军算「压境」
SIEGE_RADIUS = 5
# 几个单位算「屯兵」而不是路过
MASSING_UNITS = 3
# 单位轮换血线(与 discipline.LOW_HP_PCT 同一条线,保持口径一致)
ROTATE_HP_PCT = 60

_HOSTILE_STATES = ("UNFRIENDLY", "HOSTILE", "DENOUNCED", "WAR")


# ---------------------------------------------------------------------------
# 议程索引(修掉本地化名匹配不上的缺陷,见 docstring §3)
# ---------------------------------------------------------------------------

_agenda_by_name: dict[str, str] | None = None
_agenda_by_leader: dict[str, str] | None = None


def _load(table: str) -> list[dict]:
    try:
        with (_GAMEDATA / f"{table}.json").open(encoding="utf-8") as fh:
            rows = json.load(fh).get("rows")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def agenda_type_from_name(localized: str) -> str:
    """本地化议程名 → AGENDA_* 标识。查不到返回空串。"""
    global _agenda_by_name
    if _agenda_by_name is None:
        _agenda_by_name = {
            (r.get("Name") or "").strip(): r.get("AgendaType") or ""
            for r in _load("Agendas")
            if r.get("Name")
        }
    return _agenda_by_name.get((localized or "").strip(), "")


def historical_agenda(leader_type: str) -> str:
    """领袖标识 → 它的历史议程。查不到返回空串。"""
    global _agenda_by_leader
    if _agenda_by_leader is None:
        _agenda_by_leader = {
            r.get("LeaderType") or "": r.get("AgendaType") or ""
            for r in _load("HistoricalAgendas")
            if r.get("LeaderType")
        }
    return _agenda_by_leader.get((leader_type or "").upper(), "")


def resolve_agendas(civ, leader_type: str = "") -> list[str]:
    """把一个 CivInfo 的议程解析成 AGENDA_* 标识列表。

    三条路都试:本地化名反查 → 标识本身就是 AGENDA_* → 领袖历史议程。
    隐藏议程(名字是 "???")解析不出来,如实略过 —— 不猜。
    """
    out: list[str] = []
    for a in getattr(civ, "agendas", None) or []:
        raw = (getattr(a, "name", "") or "").strip()
        if not raw or raw.startswith("?"):
            continue
        if raw.upper().startswith("AGENDA_"):
            out.append(raw.upper())
            continue
        t = agenda_type_from_name(raw)
        if t:
            out.append(t)
    hist = historical_agenda(leader_type)
    if hist and hist not in out:
        out.append(hist)
    return out


# ---------------------------------------------------------------------------
# 议程 → 最便宜的解法
# ---------------------------------------------------------------------------
# 只覆盖 AGENDA_PROFILES 里 war_risk 为「高/中」的条目 —— 低风险议程即使
# 触怒了也不会真打你,给建议只是噪音。文本是【行为处方】,不含数值承诺:
# "满足议程能买到多少回合和平"没有实测标定(方案 v4 §5 第 3 条),
# 所以这里只说做什么,不说能换来多久。

AGENDA_REMEDY: dict[str, str] = {
    "AGENDA_STANDING_ARMY": "把军力练到不低于他 —— 这条议程只认军力数字,外交手段无效",
    "AGENDA_BIG_STICK_POLICY": "停止在他所在大陆继续铺城;已铺的城加驻军,他会针对最新一座",
    "AGENDA_IRON_CROWN": "别做城邦宗主,或做好被他连城邦一起打的准备",
    "AGENDA_LAST_VIKING_KING": "造 1-2 艘船 —— 他鄙视没有海军的邻居,有船本身就是止战",
    "AGENDA_TLATOANI": "把他缺的奢侈品**主动交易给他**,这是最便宜的一条",
    "AGENDA_WITH_YOUR_SHIELD_OR_ON_IT": "无解:他厌恶一切媾和者。按必然开战准备边境驻军",
    "AGENDA_PARANOID": "把边境附近的部队后撤一格 —— 驻军位置本身就是他的开战理由",
    "AGENDA_AYYUBID_DYNASTY": "停止向他的城市传教;自己建圣地不触怒他,传教才会",
    "AGENDA_QUEEN_OF_NILE": "军力上去他就转好感;军力弱时任何礼物都换不来尊重",
    "AGENDA_NUKE_LOVER": "后期务必留核威慑或防空;她只认这一项",
    "AGENDA_BUSHIDO": "军力和信仰任一项拉起来即可,他挑的是两项都低的软柿子",
    "AGENDA_WONDER_OBSESSED": "别和他抢同一个奇观 —— 抢输了赔产能,抢赢了赔一个邻居",
    "AGENDA_DEVOUT": "别在他的城市传播你的宗教;自己发展信仰无妨",
    "AGENDA_ENTHUSIASTIC_DISCIPLE": "接受他的宗教在你城市传播,成本几乎为零",
    "AGENDA_COUNTER_REFORMER": "停止向他传教",
    "AGENDA_CITY_STATE_PROTECTOR": "不要攻击城邦;要打就先确认他没在场",
    "AGENDA_DELIAN_LEAGUE": "别和他抢同一个城邦的宗主权,换一个投使者",
    "AGENDA_BACKSTAB_AVERSE": "不要毁约 —— 他的敌意是永久性的,比其他议程贵得多",
    "AGENDA_IDEOLOGUE": "换成和他同一政体,或接受长期冷淡",
    "AGENDA_AIRPOWER": "后期补空军;前中期这条不生效",
}


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


@dataclass
class DoctrineInput:
    """策略层各树的共同输入(doctrine 11-13 与 playbook 1-10 共用)。

    全部字段可缺 —— 缺什么就少出哪条建议。**没有一个字段有编造的默认值**:
    缺数据时对应的树静默,而不是拿默认值算出一条看起来很确定的错建议。
    """

    turn: int = 0
    power: PowerSnapshot | None = None
    kit: CivKit | None = None
    civs: list = field(default_factory=list)  # CivInfo 鸭子类型
    threats: list = field(default_factory=list)  # ThreatInfo 鸭子类型
    my_units: list = field(default_factory=list)  # UnitInfo 鸭子类型
    my_cities: list = field(default_factory=list)  # CityInfo 鸭子类型
    my_military: int = 0
    leader_types: dict = field(default_factory=dict)  # player_id -> LEADER_*
    # —— playbook(决策树 1-10)追加 ——
    era: str = ""
    victory_type: str = ""
    # 威胁等级 RED/YELLOW/GREEN(2026-08-06)。此前 L3 策略层只有原始
    # threats 列表、拿不到已仲裁的等级,于是写不出任何"红警下让步"的
    # 分支 —— 实机后果:RED 期间一边硬拦平民生产(game_state 直接返回
    # Error: RED_ALERT_CIVILIAN),一边照旧建议"首都转产开拓者",
    # 两条互相矛盾的话同时出现在一份简报里。
    threat_level: str = ""
    gold: float = 0.0
    production: int = 0
    researched: set = field(default_factory=set)  # 已完成科技标识
    civics_done: set = field(default_factory=set)
    boosted: set = field(default_factory=set)  # 尤里卡/鼓舞可用的项
    camps: list = field(default_factory=list)  # (x, y, 发现回合)
    exploration_pct: float = 0.0
    difficulty: str = ""  # 仅用于叙述,不做阈值
    # 跨回合测量历史(power_balance.PowerHistory)。war_campaign 用它算
    # 军力窗口倒计时 —— 缺了就只报当前比值,不报"还剩几回合"。
    power_history: object | None = None

    @property
    def tier(self) -> PowerTier | None:
        return self.power.tier if self.power is not None else None


def _is_military(unit) -> bool:
    cs = int(getattr(unit, "combat_strength", 0) or 0)
    rs = int(getattr(unit, "ranged_strength", 0) or 0)
    return cs > 0 or rs > 0


def _hp_pct(unit) -> float:
    mx = int(getattr(unit, "max_health", 0) or 0)
    if mx <= 0:
        return 100.0
    return 100.0 * int(getattr(unit, "health", 0) or 0) / mx


def _promotion_class(unit) -> str:
    return civ_kit.unit_row(getattr(unit, "unit_type", "") or "").get(
        "PromotionClass", ""
    )


# ---------------------------------------------------------------------------
# 决策树 11:生存优先级
# ---------------------------------------------------------------------------


def survival_priority(inp: DoctrineInput) -> list[Advice]:
    """劣势档下覆盖其他所有建议。非劣势档返回空列表。

    门控:必须有 L1 档位且档位 ≤ BEHIND。没有档位数据时**不出手** ——
    这棵树的建议会挤掉其他所有建议,凭猜测触发的代价太大。
    """
    out: list[Advice] = []
    power = inp.power
    if power is None or not power.is_survival_mode:
        return out
    crushed = power.tier is PowerTier.CRUSHED
    axis = power.strongest_axis
    axis_detail = (
        (
            f"唯一不落后的维度是【{DIM_CN.get(axis.dim, axis.dim)}】"
            f"({axis.value:.2f}×),资源优先给它",
        )
        if axis is not None and axis.value >= 1.0
        else ()
    )

    out.append(
        Advice(
            module="doctrine.survival",
            priority=Priority.SURVIVAL if crushed else Priority.CORE,
            action=(
                "把本回合的目标压缩成一条:活下去。"
                if crushed
                else "停止全面发展,把资源集中到一条线上。"
            ),
            reason=f"L1 实测档位【{TIER_CN[power.tier]}】(科技比 {power.tech_ratio:.2f}×)",
            consequence=(
                "劣势档下同时推五条线,每条都推不成 —— 校准局的产能就是这么被摊薄的"
            ),
            confidence=Confidence.RECOMMENDED,
            dedup_key="doctrine:survival:mode",
            detail=axis_detail,
        )
    )

    # —— 分支 1:有 AI 在近处屯兵 ——
    massing = _massing_by_owner(inp)
    for owner, (count, dist) in sorted(massing.items(), key=lambda kv: kv[1][1]):
        if count < MASSING_UNITS:
            continue
        out.append(
            Advice(
                module="doctrine.survival",
                priority=Priority.SURVIVAL,
                action=f"停掉发展项,全部产能转防御 —— {owner} 已在 {dist} 格内屯了 {count} 个单位",
                reason="屯兵是开战的前置动作,不是巡逻",
                consequence=(
                    "劣势档下丢一座城基本不可逆:人口、区域、奇观一起没,"
                    "而重建的产能你没有"
                ),
                confidence=Confidence.RECOMMENDED,
                subject=f"player:{owner}",
                dedup_key=f"doctrine:survival:massing:{owner}",
                detail=(
                    "优先级:城墙 > 近战驻军 > 远程。城墙是唯一能让弱势方吃到"
                    "防御倍率的建筑",
                    "同时准备外交:屯兵期间的和平/开边境成本远低于开战后",
                ),
            )
        )

    # —— 分支 2:被两家以上同时敌视 ——
    hostiles = _hostile_civs(inp)
    if len(hostiles) >= 2:
        weakest = min(
            hostiles, key=lambda c: int(getattr(c, "military_strength", 0) or 0)
        )
        out.append(
            Advice(
                module="doctrine.survival",
                priority=Priority.SURVIVAL if crushed else Priority.CORE,
                action=(
                    f"立刻向 {getattr(weakest, 'civ_name', '较弱的一方')} 缓和"
                    f"(送礼/开放边境/宣友),避免两线"
                ),
                reason=f"当前有 {len(hostiles)} 家同时处于敌视或交战状态",
                consequence="两线作战在劣势档下没有解;先摘掉一条线比加兵便宜得多",
                confidence=Confidence.RECOMMENDED,
                alternative="若对方拒绝,退而求其次:把兵力集中到威胁更大的一侧,弱侧只留城墙",
                subject="diplomacy",
                dedup_key="doctrine:survival:twofront",
            )
        )

    # —— 分支 3:科技比 <0.5,砍掉一切非必需 ——
    tech = power.ratios.get("techs")
    if tech is not None and tech.usable and tech.value < 0.5:
        out.append(
            Advice(
                module="doctrine.survival",
                priority=Priority.CORE,
                action="放弃奇观与「区域建全」,科技只走军事与增长必需线",
                reason=f"科技比 {tech.value:.2f}×,按对手的节奏你抢不到任何奇观",
                consequence=("抢奇观失败会把已投入产能折成金币,是劣势档最贵的一种失败"),
                confidence=Confidence.RECOMMENDED,
                alternative="确实有极高性价比的奇观时,先确认没有对手已经在建(看奇观提示)",
                dedup_key="doctrine:survival:no_wonders",
            )
        )
    return out


def _massing_by_owner(inp: DoctrineInput) -> dict[str, tuple[int, int]]:
    """{对手名: (SIEGE_RADIUS 内单位数, 最近距离)}。蛮族单列不混入。"""
    out: dict[str, tuple[int, int]] = {}
    for t in inp.threats or []:
        try:
            dist = int(getattr(t, "distance", 99) or 99)
            if dist > SIEGE_RADIUS:
                continue
            owner = getattr(t, "owner_name", "") or "Barbarian"
            if getattr(t, "is_city_state", False):
                continue
            count, nearest = out.get(owner, (0, 99))
            out[owner] = (count + 1, min(nearest, dist))
        except Exception:
            continue
    return out


def _hostile_civs(inp: DoctrineInput) -> list:
    out = []
    for c in inp.civs or []:
        if not getattr(c, "has_met", True):
            continue
        state = (getattr(c, "diplomatic_state", "") or "").upper()
        if getattr(c, "is_at_war", False) or any(h in state for h in _HOSTILE_STATES):
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 决策树 12:AI 议程管理
# ---------------------------------------------------------------------------


def agenda_management(inp: DoctrineInput) -> list[Advice]:
    """每个邻居一行:他为什么可能打你 + 最便宜的解法。

    这棵树**不受档位门控** —— 议程管理在任何档位都是净收益,
    而且它是劣势档下唯一比造兵便宜的止战手段。
    """
    out: list[Advice] = []
    for civ in inp.civs or []:
        if not getattr(civ, "has_met", True):
            continue
        if getattr(civ, "is_at_war", False):
            continue  # 已经开打了,议程管理来不及了
        pid = getattr(civ, "player_id", -1)
        leader_type = (inp.leader_types or {}).get(pid, "")
        types = resolve_agendas(civ, leader_type)
        if not types:
            continue
        name = getattr(civ, "civ_name", None) or f"玩家{pid}"
        for atype in types:
            prof: AgendaProfile | None = AGENDA_PROFILES.get(atype)
            if prof is None or prof.war_risk == "低":
                continue
            remedy = AGENDA_REMEDY.get(atype)
            if not remedy:
                remedy = f"避免触发条件:{prof.angers}"
            out.append(
                Advice(
                    module="doctrine.agenda",
                    priority=Priority.CORE
                    if prof.war_risk == "高"
                    else Priority.OPTIMIZE,
                    action=f"{name}:{remedy}",
                    reason=f"议程「{prof.cares}」({prof.war_risk}开战风险)—— {prof.predicts}",
                    consequence=(
                        # 2026-08-12 缺陷111:这里原本写死"神级下 AI 的产出优势你
                        # 追不上"。本局实测难度是【军阀】(DIFFICULTY_WARLORD,
                        # 八档里的第三档),AI 拿的是减益不是加成 —— 而这句话让我
                        # 在推理里凭空引入了"神级 AI 会有反应"这个前提,据此把统治
                        # 路线的可行性压低了一整档。议程是规则驱动这一点在任何难度
                        # 都成立,它不需要靠难度来立论。难度就在 get_game_overview
                        # 第一行(「Turn 355 | 罗马 (图拉真) | Score: 651 | 军阀」),
                        # 每回合都在眼前,而这句硬编码文案盖过了它。
                        "AI 的开战判定是**规则驱动**的:满足议程比造兵便宜一个数量级"
                        if prof.war_risk == "高"
                        else ""
                    ),
                    confidence=Confidence.OPTIONAL,
                    alternative=f"不做也行,代价是持续掉好感({prof.angers})",
                    subject=f"player:{pid}",
                    dedup_key=f"doctrine:agenda:{pid}:{atype}",
                )
            )
    return out


# ---------------------------------------------------------------------------
# 决策树 13:不对称交战
# ---------------------------------------------------------------------------

_SIEGE_CLASS = "PROMOTION_CLASS_SIEGE"
_RANGED_CLASS = "PROMOTION_CLASS_RANGED"


def asymmetric_engagement(inp: DoctrineInput) -> list[Advice]:
    """只在交战中出手。三条都能从数据直接算,不给没有实测支撑的倍数。"""
    out: list[Advice] = []
    at_war = [c for c in inp.civs or [] if getattr(c, "is_at_war", False)]
    if not at_war:
        return out

    from civ_mcp import war_planner

    mil = [u for u in inp.my_units or [] if _is_military(u)]
    # 破墙判定统一走 war_planner.can_break_walls(审查 2026-08-05 F2-2):
    # 只认 PROMOTION_CLASS_SIEGE 会把野战加农炮(RANGED 类攻城主力)判成
    # "没有攻城单位",在 T335 后期变成假阳性禁攻令。
    breakers = [
        u
        for u in mil
        if war_planner.can_break_walls(str(getattr(u, "unit_type", "") or ""))
    ]
    ranged = [u for u in mil if _promotion_class(u) == _RANGED_CLASS]

    # —— 规则 1:有城墙且我无破墙单位 → 不打这座城 ——
    walled = []
    for civ in at_war:
        for vc in getattr(civ, "visible_cities", None) or []:
            if getattr(vc, "has_walls", False):
                walled.append((getattr(civ, "civ_name", "?"), vc))
    if walled and not breakers:
        names = "、".join(f"{c}的{getattr(v, 'name', '?')}" for c, v in walled[:3])
        out.append(
            Advice(
                module="doctrine.engage",
                priority=Priority.CORE,
                action=f"不要进攻 {names} —— 有城墙而你没有破墙单位",
                reason=(
                    "城墙让近战伤害被大幅削减,低强度远程也磨不动"
                    "(弩手 21 回合零进展是 T335 实测),而城市每回合反击并自行回血"
                ),
                consequence="每一轮都在赔单位,而城血几乎不掉",
                confidence=Confidence.RECOMMENDED,
                alternative=(
                    f"改打野战:掠夺改良与贸易路线逼他出城,"
                    f"{'用现有 %d 个远程单位集火出城的部队' % len(ranged) if ranged else '先补破墙单位(攻城系/野战加农炮档远程/海军远程/轰炸机)'}"
                ),
                dedup_key="doctrine:engage:walls",
                detail=(
                    "攻城的正确顺序:破墙单位先把城墙磨掉,近战最后一击进城",
                    "ZOC:进入敌方控制区会终止移动,所以近战必须在开打的前一回合就位",
                ),
            )
        )

    # —— 规则 1b:零占领单位 —— 磨到 0 血也拿不下城(2026-08-05)——
    # 机制硬约束:远程/攻城/海军/空军只能把城打到 0 血,必须近战系进城
    # 才算占领;0 血城被远程再打一次是【夷平】。T335 实证:13 弩手 +
    # 4 野战加农炮的编成,攻城流程走完了也拿不到城。
    # 外部高手共识同向:纯远程军队推不动城,近战是必需品(且夹击/支援
    # 加成只有近战吃得到)。
    if at_war and not any(
        war_planner.can_capture_city(str(getattr(u, "unit_type", "") or ""))
        for u in mil
    ):
        out.append(
            Advice(
                module="doctrine.engage",
                priority=Priority.CORE,
                action="补近战系单位 —— 你现在的编成打到 0 血也占不下任何城",
                reason=(
                    "只有地面近战系(近战/反骑兵/骑兵)能进城占领;"
                    "远程与攻城单位把城打到 0 血后再补一击是【夷平】不是占领"
                ),
                consequence="没有近战就等于整场战争只能掠夺,城池一座都拿不到",
                confidence=Confidence.CERTAIN,
                alternative=(
                    "手上有金就直接 purchase_item 买一个当代近战单位跟着攻城队走"
                ),
                dedup_key="doctrine:engage:no_capture_unit",
                detail=(
                    "标准编成:破墙单位磨墙 → 远程消耗守军 → 近战最后一击进城",
                    "近战还带来夹击(+2/相邻友军)与支援加成,纯远程军队两样都吃不到",
                ),
            )
        )

    # —— 规则 2:残血单位强制轮换 ——
    hurt = [u for u in mil if _hp_pct(u) < ROTATE_HP_PCT]
    if hurt:
        listing = "、".join(
            f"{getattr(u, 'name', '?')}({_hp_pct(u):.0f}%)" for u in hurt[:4]
        )
        out.append(
            Advice(
                module="doctrine.engage",
                priority=Priority.CORE,
                action=f"把 {listing} 撤出接触线回血,换满血单位顶上",
                reason=f"低于 {ROTATE_HP_PCT}% 的单位再吃一次反击就可能阵亡",
                consequence=(
                    "校准局 T108 实证:51 血的军团继续攻城,阵亡 —— "
                    "损失的不只是这个兵,是整条战线的推进能力"
                ),
                confidence=Confidence.CERTAIN,
                alternative="没有替补时:全线暂停一回合,原地 fortify 集体回血,再一起压上",
                dedup_key="doctrine:engage:rotate",
            )
        )

    # —— 规则 3:集结再发起 ——
    healthy = [u for u in mil if _hp_pct(u) >= ROTATE_HP_PCT]
    if len(mil) >= 2 and len(healthy) >= 2 and walled:
        out.append(
            Advice(
                module="doctrine.engage",
                priority=Priority.OPTIMIZE,
                action="发起攻城前,让全部参战单位在同一回合抵达发起线",
                reason="分批进场 = 守军逐个吃掉;第一个单位挨完全部反击,第二个面对的还是满城防",
                consequence="校准局 T69 单团先行,白送一个单位",
                confidence=Confidence.RECOMMENDED,
                dedup_key="doctrine:engage:stage",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def evaluate(inp: DoctrineInput) -> list[Advice]:
    """跑三棵树。任何一棵抛异常都不影响其余 —— 策略层不许拖垮简报。"""
    out: list[Advice] = []
    for tree in (survival_priority, agenda_management, asymmetric_engagement):
        try:
            out.extend(tree(inp))
        except Exception:
            continue
    return out
