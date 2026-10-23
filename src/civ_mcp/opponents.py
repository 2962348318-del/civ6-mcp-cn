"""对手推演 —— "不知道对面会干什么,就假设自己是对面"。

用户 2026-07-31 提出的想法。在文明6里这个特别可行,因为对手不是黑盒:
    · 蛮族是确定性规则机(营地出兵、抓平民、掠夺改良),行为完全可算
    · 主要文明有【议程】系统 —— 议程就是对手的策略函数,而且 get_diplomacy
      已经在查了(CivInfo.agendas),只是从来没人拿它做推演

推演分两层,可信度差很多,必须分开说:
    1. 能力推演(准):它下回合能打到哪 = 移动力 + 射程。算出来的上界,不用猜。
       已实现在 safety.py 的 EnemySighting.reach。
    2. 意图推演(有把握但要标注):议程 → 它在意什么 → 它会因为什么恨你/打你。
       这一层给的是【倾向】,不是保证,输出里必须写清楚。

宁可高估对手 —— 低估的代价是死单位,高估的代价只是多造一个兵。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 议程 → 行为预测
# ---------------------------------------------------------------------------
# 数据来源:docs/gamedata/Agendas.md(43 条)、HistoricalAgendas.md(20 条)。
# 每条给三样:它在意什么 / 你做什么会触怒它 / 它大概率会怎么打。
# ⚠️ 议程的具体好感度数值未逐条查证,这里给的是【方向】不是【数值】。


@dataclass(frozen=True)
class AgendaProfile:
    cares: str  # 它看重什么
    angers: str  # 什么行为会让它敌视你
    predicts: str  # 由此推断它会怎么行动
    war_risk: str  # "高" / "中" / "低" — 该议程被触怒后开战的倾向


AGENDA_PROFILES: dict[str, AgendaProfile] = {
    # --- 军事/扩张倾向:被触怒后最可能真的打你 ---
    "AGENDA_STANDING_ARMY": AgendaProfile(
        "常备军规模", "你的军力弱于它", "军力落后会被视为软柿子,可能主动宣战", "高"
    ),
    "AGENDA_BIG_STICK_POLICY": AgendaProfile(
        "同大陆无他国殖民", "你在它所在大陆扩张", "在其大陆铺城会被强烈敌视", "高"
    ),
    "AGENDA_SUN_NEVER_SETS": AgendaProfile(
        "跨洲殖民", "你在它本土大陆之外的地方比它强", "会争抢海外殖民点", "中"
    ),
    "AGENDA_IRON_CROWN": AgendaProfile(
        "征服城邦", "你是城邦宗主国", "会攻击城邦,也会攻击城邦的宗主", "高"
    ),
    "AGENDA_LAST_VIKING_KING": AgendaProfile(
        "海军与沿海掠夺", "你没有海军", "会用海军骚扰沿海城市", "高"
    ),
    "AGENDA_TLATOANI": AgendaProfile(
        "奢侈品资源", "你占有它想要的奢侈品", "会为资源开战", "高"
    ),
    "AGENDA_WITH_YOUR_SHIELD_OR_ON_IT": AgendaProfile(
        "只打仗不媾和", "你与人媾和/避战", "极度好战,几乎不接受和平", "高"
    ),
    "AGENDA_BUSHIDO": AgendaProfile(
        "军事与信仰并重", "你军力或信仰低下", "会挑软柿子", "中"
    ),
    "AGENDA_PARANOID": AgendaProfile(
        "邻国军力", "你在它边境附近驻军", "边境驻军会引发它先发制人", "高"
    ),
    "AGENDA_AYYUBID_DYNASTY": AgendaProfile(
        "圣地与宗教", "你在它城市附近传教/建圣地", "宗教冲突可能升级为战争", "中"
    ),
    "AGENDA_BARBARIAN_LOVER": AgendaProfile(
        "蛮族存续", "你拆蛮族营地", "拆营地会降低它的好感(但这不该阻止你拆)", "低"
    ),
    # --- 发展倾向:主要影响竞速,不太主动开战 ---
    "AGENDA_TECHNOPHILE": AgendaProfile(
        "科技领先", "你科技领先它", "会全力抢科技,科技胜利的直接竞争者", "低"
    ),
    "AGENDA_DARWINIST": AgendaProfile(
        "科技与自然奇观", "你科技落后", "科技竞速对手", "低"
    ),
    "AGENDA_WESTERNIZER": AgendaProfile(
        "科技与文化双高", "你两项都落后", "全面发展型对手", "低"
    ),
    "AGENDA_CULTURED": AgendaProfile(
        "文化产出", "你文化落后", "文化胜利的直接竞争者", "低"
    ),
    "AGENDA_WONDER_OBSESSED": AgendaProfile(
        "建造奇观", "你抢它想要的奇观", "**会激进抢奇观** — 奇观竞速极危险", "中"
    ),
    "AGENDA_WONDER_ADVOCATE": AgendaProfile(
        "建造奇观", "你奇观比它少", "奇观竞速对手", "低"
    ),
    "AGENDA_GREAT_PERSON_OBSESSED": AgendaProfile(
        "招募伟人", "你抢它想要的伟人", "会用信仰/金币抢伟人", "低"
    ),
    "AGENDA_GREAT_PERSON_ADVOCATE": AgendaProfile(
        "招募伟人", "你伟人比它少", "伟人竞争者", "低"
    ),
    "AGENDA_POPULOUS": AgendaProfile(
        "城市人口", "你城市人口少", "会优先发展人口", "低"
    ),
    "AGENDA_MONEY_GRUBBER": AgendaProfile(
        "金币储备", "你比它有钱", "会抢贸易路线", "低"
    ),
    "AGENDA_INDUSTRIALIST": AgendaProfile(
        "产能与工业区", "你工业落后", "产能竞争者", "低"
    ),
    "AGENDA_DEVOUT": AgendaProfile(
        "信仰产出", "你信仰低/传播别的宗教", "宗教胜利竞争者", "中"
    ),
    "AGENDA_ENTHUSIASTIC_DISCIPLE": AgendaProfile(
        "接受它的宗教", "你城市信别的宗教", "会派传教士压你", "中"
    ),
    "AGENDA_COUNTER_REFORMER": AgendaProfile(
        "宗教纯洁", "你向它传教", "会用宗教单位反击", "中"
    ),
    "AGENDA_EXPLORER": AgendaProfile(
        "探索地图", "你探索落后", "会大量派侦察单位", "低"
    ),
    "AGENDA_CITY_STATE_PROTECTOR": AgendaProfile(
        "保护城邦", "你攻击城邦", "攻击城邦会招来它", "中"
    ),
    "AGENDA_CITY_STATE_ALLY": AgendaProfile(
        "城邦使者", "你抢它的城邦宗主权", "会砸使者抢城邦", "低"
    ),
    "AGENDA_DELIAN_LEAGUE": AgendaProfile(
        "城邦宗主权", "你抢城邦", "城邦争夺对手", "中"
    ),
    "AGENDA_ALLY_OF_ENKIDU": AgendaProfile(
        "共同作战", "你与它邻近却不结盟", "倾向拉你结盟,拒绝会降好感", "低"
    ),
    "AGENDA_PEACEKEEPER": AgendaProfile(
        "和平", "你宣战/占领城市", "极厌恶侵略者,但自己不主动开战", "低"
    ),
    "AGENDA_BACKSTAB_AVERSE": AgendaProfile(
        "守信", "你毁约/背刺", "一旦被背刺会长期敌视", "中"
    ),
    "AGENDA_CIVILIZED": AgendaProfile("不与人开战", "你频繁开战", "厌恶好战者", "低"),
    "AGENDA_FUN_LOVING": AgendaProfile("宜居度", "你城市宜居度低", "发展型", "低"),
    "AGENDA_ENVIRONMENTALIST": AgendaProfile("环境", "你砍伐/污染", "发展型", "低"),
    "AGENDA_NATURALIST": AgendaProfile(
        "自然奇观周边", "你占据自然奇观", "会争抢自然奇观地块", "低"
    ),
    "AGENDA_EXPLOITATIVE": AgendaProfile(
        "开发战略资源", "你不开发资源", "资源竞争者", "低"
    ),
    "AGENDA_IDEOLOGUE": AgendaProfile("同政体", "你政体与它不同", "意识形态对立", "中"),
    "AGENDA_AIRPOWER": AgendaProfile("空军规模", "你空军落后", "后期空中压制", "中"),
    "AGENDA_NUKE_LOVER": AgendaProfile("核武", "你没核武", "后期核威慑", "高"),
    "AGENDA_QUEEN_OF_NILE": AgendaProfile(
        "军力强大的邻居", "你军力弱", "喜欢强者,弱者会被轻视", "中"
    ),
    "AGENDA_BLACK_QUEEN": AgendaProfile(
        "间谍活动", "你没有间谍/被它渗透", "会大量用间谍", "低"
    ),
    "AGENDA_OPTIMUS_PRINCEPS": AgendaProfile(
        "城市数量与领土", "你城市少", "扩张型,会抢地", "中"
    ),
}

# 蛮族不在议程系统里,但它的行为规则是已知且确定的
BARBARIAN_DOCTRINE = [
    "营地会周期性出兵,不拆就不会停",
    "优先攻击【无护卫的平民】—— 开拓者/建造者是它的首选目标",
    "会掠夺无防护的改良设施和贸易路线",
    "侦察兵发现你的城市后会跑回营地报信,之后该营地持续针对你",
    "不会接受外交,只能靠武力解决",
]


@dataclass
class OpponentRead:
    """对一个对手的推演结果。"""

    player_id: int
    name: str
    leader: str
    at_war: bool = False
    relationship: str = ""
    their_military: int = 0
    my_military: int = 0
    agendas: list[str] = field(default_factory=list)
    profiles: list[AgendaProfile] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)  # 我方正在触怒它的行为
    war_risk: str = "低"

    @property
    def military_ratio(self) -> float:
        if self.my_military <= 0:
            return 99.0
        return self.their_military / self.my_military


def read_opponent(civ, my_military: int, my_cities: int = 0) -> OpponentRead:
    """把一个 CivInfo 转成推演结果。civ 为鸭子类型(lua.models.CivInfo)。"""
    agenda_names = []
    profiles = []
    for a in getattr(civ, "agendas", None) or []:
        nm = getattr(a, "name", "") or ""
        agenda_names.append(nm)
        # AgendaInfo.name 是本地化名,需按中文名反查;这里两边都试
        for key, prof in AGENDA_PROFILES.items():
            if nm and (nm in key or key.endswith(nm.upper().replace(" ", "_"))):
                profiles.append(prof)
                break

    read = OpponentRead(
        player_id=getattr(civ, "player_id", -1),
        name=getattr(civ, "civ_name", "?"),
        leader=getattr(civ, "leader_name", "?"),
        at_war=bool(getattr(civ, "is_at_war", False)),
        relationship=getattr(civ, "diplomatic_state", ""),
        their_military=int(getattr(civ, "military_strength", 0) or 0),
        my_military=my_military,
        agendas=agenda_names,
        profiles=profiles,
    )

    # 触怒判定:能用数字算的才写,不能算的不猜
    if read.military_ratio >= 2.0:
        read.triggers.append(
            f"它的军力约为你的 {read.military_ratio:.1f} 倍 —— "
            f"军力悬殊本身就会招来主动宣战"
        )
    risks = [p.war_risk for p in profiles]
    if "高" in risks:
        read.war_risk = "高"
    elif "中" in risks:
        read.war_risk = "中"
    if read.military_ratio >= 2.0 and read.war_risk == "低":
        read.war_risk = "中"
    return read


def render_opponents(reads: list[OpponentRead], has_barbarians: bool = False) -> str:
    """渲染成简报段。没有内容返回空串。"""
    lines: list[str] = []

    for r in sorted(reads, key=lambda r: {"高": 0, "中": 1, "低": 2}[r.war_risk])[:4]:
        icon = "⚔️" if r.at_war else ("⚠️" if r.war_risk == "高" else "·")
        head = f"{icon} {r.name}({r.leader})"
        if r.at_war:
            head += " 【交战中】"
        if r.their_military and r.my_military:
            head += f" 军力 {r.their_military} vs 你 {r.my_military}"
            if r.military_ratio >= 1.5:
                head += f" ← 它是你的 {r.military_ratio:.1f} 倍"
        lines.append(head)
        for p in r.profiles[:2]:
            lines.append(f"    议程「{p.cares}」→ {p.predicts}")
        for t in r.triggers[:2]:
            lines.append(f"    ⚠️ {t}")

    if has_barbarians:
        lines.append("· 蛮族(行为确定,不需要猜):")
        lines.append(f"    {BARBARIAN_DOCTRINE[1]}")

    if not lines:
        return ""
    header = "== 对手推演 ==(意图为倾向判断,不是保证;能力推演见安全拦截)"
    return header + "\n" + "\n".join(lines)


# ===========================================================================
# 任务 D:军力排名 + 行为日志 + 宣战风险(追加,上方已有代码未动一行)
#
# 议程是性格,行为是动作 —— 从行为推意图比从议程推更准。这里补上
# read_opponent 缺的两块:横向军力排位(你在食物链的哪一层)和纵向
# 行为记录(他最近 20 回合对你做了什么)。
# ===========================================================================

BEHAVIOR_WINDOW = 20  # 行为日志的记忆窗口(回合)

# 行为类别 → 是否计入敌意信号。类别由调用方标注;未知类别不计入敌意。
HOSTILE_KINDS = frozenset(
    {
        "military_move",  # 向我方向移动军事单位
        "border_settle",  # 贴我边境铺城
        "demand",  # 勒索/索要
        "denounce",  # 谴责
        "troop_buildup",  # 军力异常增长
    }
)


@dataclass
class BehaviorEvent:
    """一条对手行为记录。action 写人话,带回合与坐标最有用。"""

    player_id: int
    action: str
    turn: int
    kind: str = ""  # HOSTILE_KINDS 之一,或空(中性记录)


class BehaviorLog:
    """近 BEHAVIOR_WINDOW 回合的对手行为。超窗自动淘汰。"""

    def __init__(self) -> None:
        self.events: list[BehaviorEvent] = []

    def add(self, player_id: int, action: str, turn: int, kind: str = "") -> None:
        if not action:
            return
        try:
            self.events.append(
                BehaviorEvent(int(player_id), str(action), int(turn), str(kind))
            )
        except (TypeError, ValueError):
            return

    def recent(
        self, current_turn: int, player_id: int | None = None
    ) -> list[BehaviorEvent]:
        cutoff = current_turn - BEHAVIOR_WINDOW
        return [
            e
            for e in self.events
            if e.turn > cutoff and (player_id is None or e.player_id == player_id)
        ]

    def hostile_count(self, current_turn: int, player_id: int) -> int:
        return sum(
            1 for e in self.recent(current_turn, player_id) if e.kind in HOSTILE_KINDS
        )

    def render(
        self, current_turn: int, names: dict[int, str] | None = None, max_lines: int = 4
    ) -> str:
        events = self.recent(current_turn)
        if not events:
            return ""
        names = names or {}
        lines = ["对手行为记录(近 20 回合):"]
        for e in events[-max_lines:]:
            who = names.get(e.player_id, f"玩家{e.player_id}")
            lines.append(f"· {who}:T{e.turn} {e.action}")
        return "\n".join(lines)


# 模块级默认日志(每局由 reset_behavior_log 清空;测试也用它隔离)
_BEHAVIOR_LOG = BehaviorLog()


def reset_behavior_log() -> None:
    global _BEHAVIOR_LOG
    _BEHAVIOR_LOG = BehaviorLog()


def add_behavior_log(player_id: int, action: str, turn: int, kind: str = "") -> None:
    """记录一条对手行为。kind 见 HOSTILE_KINDS,中性记录留空。不抛异常。"""
    _BEHAVIOR_LOG.add(player_id, action, turn, kind)


def get_military_ranking(
    civs: list,
    my_military: int,
    my_name: str = "你",
    neighbor_ids: tuple[int, ...] = (),
) -> list[str]:
    """全部已知文明的军力排位 + 你的位置。无已知文明返回空列表。

    civs 为鸭子类型(CivInfo/dict 均可:civ_name + military_strength)。
    (计划书的 get_military_ranking() 无参 —— 本模块无实时查询能力,
    数据必须由调用方传入,签名据此调整。)
    """
    try:
        rows: list[tuple[str, int, bool]] = [(my_name, int(my_military or 0), False)]
        for c in civs or []:
            if isinstance(c, dict):
                name = str(c.get("civ_name") or c.get("name") or "?")
                mil = c.get("military_strength")
                pid = c.get("player_id", -1)
            else:
                name = getattr(c, "civ_name", "?")
                mil = getattr(c, "military_strength", 0)
                pid = getattr(c, "player_id", -1)
            try:
                mil = int(mil or 0)
            except (TypeError, ValueError):
                mil = 0
            rows.append((name, mil, pid in neighbor_ids))
        if len(rows) <= 1:
            return []
        rows.sort(key=lambda r: -r[1])
        listing = " ".join(
            f"{i}.{name}({mil})" for i, (name, mil, _n) in enumerate(rows, 1)
        )
        my_pos = next(i for i, r in enumerate(rows, 1) if r[0] == my_name)
        n = len(rows)
        out = [f"军力排名:{listing}", f"你在 {n} 个已知文明中排第 {my_pos}。"]
        stronger_neighbors = [
            name
            for (name, _m, is_n), i in zip(rows, range(1, n + 1))
            if is_n and i < my_pos
        ]
        if stronger_neighbors:
            out.append(
                f"⚠️ 排你前面的 {('、'.join(stronger_neighbors))} 是你的邻居"
                f"——军力差距对邻居才是现实威胁,隔洋的第一名打不到你。"
            )
        elif my_pos > (n + 1) // 2:
            out.append("你在下半区——弱者的边境需要更多驻军或更好的外交。")
        return out
    except Exception:
        return []


def assess_war_risk(
    neighbor,
    my_military: int,
    current_turn: int = 0,
    log: BehaviorLog | None = None,
) -> tuple[str, list[str]]:
    """综合 军力比 + 议程 + 近期行为 → 宣战风险(高/中/低)与理由。

    neighbor 为鸭子类型(CivInfo/dict)。行为日志缺省用模块级日志。
    不抛异常;信息不足时按已知部分判,理由里写明缺口。
    """
    try:
        read = read_opponent(_CivView(neighbor), my_military)
        reasons: list[str] = list(read.triggers)
        level_rank = {"低": 0, "中": 1, "高": 2}
        level = read.war_risk
        for p in read.profiles[:2]:
            if p.war_risk != "低":
                reasons.append(f"议程「{p.cares}」({p.war_risk}风险):{p.predicts}")

        use_log = log if log is not None else _BEHAVIOR_LOG
        pid = read.player_id
        hostile = use_log.hostile_count(current_turn, pid) if current_turn else 0
        if hostile >= 2:
            reasons.append(
                f"近 {BEHAVIOR_WINDOW} 回合有 {hostile} 条敌意行为记录"
                f"——行为比议程诚实,他已经在动手准备了"
            )
            level = "高"
        elif hostile == 1 and level_rank[level] < 1:
            level = "中"
            reasons.append("近期有 1 条敌意行为记录——开始留意他的边境动向")

        if not reasons:
            reasons.append("军力比正常、议程未触怒、无敌意行为——维持现状即可")
        return level, reasons
    except Exception:
        return "低", ["评估异常,按低风险处理(信息缺口本身也是情报)"]


class _CivView:
    """把 dict 邻居适配成 read_opponent 期望的鸭子类型。CivInfo 原样透传。"""

    def __init__(self, src) -> None:
        self._src = src if isinstance(src, dict) else None
        self._obj = None if isinstance(src, dict) else src

    def __getattr__(self, name):
        if self._obj is not None:
            return getattr(self._obj, name, None)
        src = self._src or {}
        if name == "agendas":
            out = []
            for a in src.get("agendas") or []:
                nm = a.get("name") if isinstance(a, dict) else a
                out.append(type("A", (), {"name": nm or ""})())
            return out
        return src.get(name)
