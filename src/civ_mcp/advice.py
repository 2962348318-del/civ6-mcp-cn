"""统一建议接口 —— 任务 AG(优先级仲裁)与任务 AJ(统一风险语言)的共同地基。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物,不另建 .md)
═══════════════════════════════════════════════════════════════════════════

## 0. 这个文件解决什么问题

现状:14 个建议来源(13 个新模块 + policy/lategame 旧 API)各自往简报里塞
list[str]。战争评估说"撤退",交易建议说"卖丝绸",尤里卡说"先研究采矿"——
模型拿到的是一堆无法横向比较的碎片。计划书任务 AG/AJ/AN 要治这个,
但它们三个需要**同一套数据结构**才能协作:

    AG(priority_arbiter.py,待写) —— 消费 Advice.priority 做排序/截断/冲突分组
    AJ(risk_language.py,待写)   —— 消费 Advice.confidence 做统一风险渲染
    AN(叙事简报,待写)            —— 消费仲裁结果组装五段式叙事

本文件只定义**词汇表**(数据结构 + 兼容适配 + 参考实现),不做业务判断。
AG/AH 的冲突检测、采纳追踪等复杂逻辑落地时扩展参考实现,接口不变。

## 1. 核心结构:Advice

一条建议 = 一个可执行的动作 + 它的紧急度 + 它的可信度 + 不做的代价。

字段与设计理由(每个字段对应人类高手认知过程中的一个环节):

| 字段 | 类型 | 对应的认知环节 |
|---|---|---|
| module | str | 出处。高手知道"这个念头来自哪套本能"——防守本能还是贪财本能。仲裁器用它做单模块限行(防单模块刷屏,失联蛮族的教训)。 |
| priority | Priority | 前注意triage。高手扫一眼局面就分出"会死人的/别人要赢的/我的主线/顺手的"。这不是深思的产物,是排序的**前提**。 |
| action | str | 祈使句:现在做什么。高手脑子里是动作不是观察——"撤建造者"而非"建造者附近有威胁"。写不成祈使句的内容不配进简报。 |
| reason | str | 为什么是**现在**。紧急和重要的区分是时间性的。答不出"为什么是这回合"的建议自动降级。 |
| consequence | str | 不做的代价。高手按**后悔值**排序,不按收益排序。本字段承载 AJ 的数字化风险语言(见 §2)。 |
| confidence | Confidence | 证据等级。高手区分"游戏规则"和"我猜对手会这样"——前者敢梭哈,后者要留后手。 |
| alternative | str | 退路。AJ 规范:"每个建议都带如果不这么做还有什么选择"。冒险档建议没有退路 = 陷阱,渲染时会被点名。 |
| subject | str | 建议作用的对象("unit:1234"/"city:罗马"/"policy")。高手的注意力按对象组织。仲裁器靠它把"同一对象上互相矛盾的建议"放到一起,不需要 NLP。 |
| dedup_key | str | 跨回合身份。高手记得"这话我说过了"。AH 降噪(连续 3 回合未采纳→压制 5 回合)需要稳定身份;采纳状态本身是跨回合状态,归 world_model/AH 管,不在本结构里。 |
| detail | list[str] | 展开信息。渲染时最多取 2 行——深度按需,不许夹带小作文。 |

## 2. 风险语言(任务 AJ 的四档,数字区间为准)

    确定   >95%      规则必然。数值来自 gamedata 导出或引擎判定,不依赖对手。
    推荐   80-95%    实时数据算出的最优解,存在无法量化的因素(状态可能过时)。
    可选   50-80%    方向正确但变数大。必须在文本里写明变数是什么。
    冒险   <50%      高风险高回报。必须给 alternative,必须给数字化的失败代价。

渲染规则(和计划书 AJ 的 🟢🟡🟠🔴 圆点方案的差异,是本设计的主动决策):
  · **不给每行打置信度圆点标签**。理由:行首图标已被优先级占用(🔴⚠️💡),
    一行两套 emoji 词汇表 = 视觉噪音;且"标签盲"是真实现象——模型会跳过
    重复出现的标签。高手的做法是**只标记例外**:常态(确定/推荐)不说,
    "可选/冒险"必须在句子里带数字区间和依据缺口。
  · 禁用形容词风险语("极其危险""强烈建议")。能算的给区间
    ("预计损失 2-3 单位概率约 70%"),不能算的降档到可选/冒险并写明缺口。
    这条是**写作契约**,靠 review 执行,接口只提供载体(consequence 字段)。

## 3. 优先级五档(任务 AG)

    0 SURVIVAL 生存   本回合不处理就有不可逆损失(单位/平民/城市)。
    1 BLOCKING 阻断   某对手逼近胜利,不去阻止则我方一切努力作废。
    2 CORE     核心   锁定胜利路线上的主线推进(科技路径/太空链/宗教铺开)。
    3 OPTIMIZE 优化   让主线更快的确定性改进(公民/贸易/政策/砍树)。
    4 BONUS    锦上添花 机会性收益(奇观/伟人/城邦/交易)。

为什么模型不看标签也能排对序:**位置即标签**。渲染器保证排序后的前三行
就是前三件事(AN 的「本回合最重要的三件事」),生存/阻断行以 🔴 开头且
永不截断——重要性从版面结构里读出来,不靠模型解析"[P0]"之类的记号。

## 4. 截断规则(40 行预算)

    1. SURVIVAL / BLOCKING 永不截断(实战中这两档本来就少)。
    2. 单模块限行:每模块最多 6 行(SURVIVAL 豁免)——防单模块刷屏。
    3. 分层配额:CORE ≤ 12 行,OPTIMIZE ≤ 12 行,BONUS ≤ 6 行。
    4. 总量仍超 40:从 BONUS → OPTIMIZE → CORE 自下而上砍。
    5. 每个被砍的层留一行汇总:「(已省略 N 条XX建议:模块a·模块b)」——
       AN 的「省略的优化建议」段,被砍掉的信息保留可追索的线索。
    6. 层与层之间空一行(AG 明文要求,保证视觉分组)。
    7. 单条建议渲染上限 3 行(action 行 + 最多 2 行 detail)。

## 5. 迁移指南(已写好的模块怎么适配)

**结论:14 个现存来源一行代码都不用改。**

它们的 list[str] 输出已有隐性规约(图标前缀/缩进注解/「== X ==」段头/
「(另有 N 条)」尾注),`from_legacy()` 把这套规约机械翻译成 Advice 流:

    行首「== … ==」        → 丢弃(模块身份由注册表提供,段头由仲裁器重排)
    行首图标/「· 」/「[」   → 新建议;图标查注册表映射出 priority
    行首缩进或「(」「(」    → 附到上一条的 detail(模块的自我注解)
    其余顶行               → 按注册表缺省档位新建议

注册表(LEGACY_REGISTRY)给每个模块声明:⚠️ 行的档位、💡 行的档位、
🔴 行的档位(缺省 SURVIVAL,可覆盖——如 DVP 模块的 🔴 是路线内急务,
映射 CORE)。逐模块缺省档位见注册表本体,不在此重复。

新模块(危机管理/胜利阻断/战前评估等)**直接构造 Advice**,不走适配器;
何时用哪档写进各自的模块 docstring。旧模块若日后想要更精细的
consequence/alternative,再逐个迁移——迁移压力为零是本设计的硬约束。

## 6. 与 AN 叙事简报的接缝

## 7. 常驻上下文 vs 可重复建议(2026-08-03,校准局教训)

降噪(AH)的判据是"文本一字未变 = 局面没动 = 这条没被采纳"。这个判据对
**建议**成立,对**常驻上下文**不成立:科技链、政体链、胜利路线的关键指标
每回合本来就长一样 —— 它们不是"又提醒了一次",而是这一回合的坐标系。

T15 实测:8 条建议休眠后,简报里整个「主线」段消失了。原因就是主线全被
当成重复建议压掉。所以 `Advice.standing=True` 的条目**永不参与降噪**,
和 SURVIVAL/BLOCKING 同等豁免。哪些模块是常驻上下文,见 LEGACY_REGISTRY
的 standing 标记。

`arbitrate()` 接受三个叙事槽位:situation(当前局面,来自 world_model/AM)、
plan_a(如果顺利,来自胜利路线播书)、plan_b(如果出问题,来自
strategy_space 的 fallback——见 strategy_space.py)。输出即 AN 五段式:
局面 → 最重要的三件事 → 主线其余 → 如果顺利/出问题 → 省略汇总。
仲裁器不负责把三件事讲成因果链——那是 LLM 的工作;仲裁器负责保证
进入前三行的**是对的三件事**。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class Priority(IntEnum):
    """五档优先级。数值越小越紧急;排序直接用 int 比较。"""

    SURVIVAL = 0  # 生存:不可逆损失即将发生
    BLOCKING = 1  # 阻断:对手逼近胜利
    CORE = 2  # 核心:胜利路线主线
    OPTIMIZE = 3  # 优化:确定性改进
    BONUS = 4  # 锦上添花:机会性收益


class Confidence(Enum):
    """四档置信度(任务 AJ)。value = (中文名, 概率下界, 概率上界)。"""

    CERTAIN = ("确定", 0.95, 1.00)
    RECOMMENDED = ("推荐", 0.80, 0.95)
    OPTIONAL = ("可选", 0.50, 0.80)
    GAMBLE = ("冒险", 0.00, 0.50)

    @property
    def cn(self) -> str:
        return self.value[0]

    @property
    def band(self) -> tuple[float, float]:
        return self.value[1], self.value[2]

    @property
    def band_text(self) -> str:
        lo, hi = self.band
        if self is Confidence.CERTAIN:
            return ">95%"
        if self is Confidence.GAMBLE:
            return "<50%"
        return f"{lo * 100:.0f}-{hi * 100:.0f}%"


# 置信度排序辅助:同层内 确定 排在 冒险 前面
_CONF_RANK = {
    Confidence.CERTAIN: 0,
    Confidence.RECOMMENDED: 1,
    Confidence.OPTIONAL: 2,
    Confidence.GAMBLE: 3,
}


@dataclass(frozen=True)
class Advice:
    """一条建议。字段语义见模块 docstring §1。"""

    module: str
    priority: Priority
    action: str  # 祈使句:现在做什么
    reason: str = ""  # 为什么是现在
    consequence: str = ""  # 不做的代价(数字化,见 §2)
    confidence: Confidence = Confidence.RECOMMENDED
    alternative: str = ""  # 退路;冒险档缺失会被点名
    subject: str = ""  # 作用对象,如 "city:罗马"
    dedup_key: str = ""  # 跨回合身份;空 = 用 action 文本
    detail: tuple[str, ...] = ()  # 展开信息,渲染最多取 2 行
    standing: bool = False  # 常驻上下文,不参与降噪(见 §7)

    @property
    def identity(self) -> str:
        return self.dedup_key or f"{self.module}:{self.action}"

    def with_detail(self, extra: str) -> Advice:
        """追加一行 detail 并返回新对象(frozen dataclass 不能原地改)。"""
        return Advice(**{**self.__dict__, "detail": self.detail + (extra,)})


# ---------------------------------------------------------------------------
# 兼容适配:list[str] → list[Advice]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyProfile:
    """一个旧模块的翻译档案:各图标行映射到哪个优先级档位。"""

    warn: Priority  # ⚠️ / ⚡ / 💰 行
    hint: Priority  # 💡 / 📊 / ⏳ / 无图标行
    red: Priority = Priority.SURVIVAL  # 🔴 行(DVP 等模块可覆盖)
    standing: bool = False  # 该模块的输出是常驻上下文(见 §7)


# 14 个现存建议来源的档位注册表(迁移指南 §5 的机器可读版)。
# 档位判断依据:计划书任务 AG 的分层示例 + 各模块的业务语义。
LEGACY_REGISTRY: dict[str, LegacyProfile] = {
    # —— Batch 1 的 10 个模块 ——
    "citizen_optimizer": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    "trade_optimizer": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    "chop_overflow": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    "resource_trade": LegacyProfile(Priority.BONUS, Priority.BONUS),
    "event_window": LegacyProfile(Priority.CORE, Priority.OPTIMIZE),
    "governor_advisor": LegacyProfile(Priority.OPTIMIZE, Priority.BONUS),
    "city_state_advisor": LegacyProfile(Priority.BONUS, Priority.BONUS),
    "wonder_tier": LegacyProfile(Priority.BONUS, Priority.BONUS),
    "late_economy": LegacyProfile(Priority.CORE, Priority.OPTIMIZE),
    "spy_strategy": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    # —— lategame.py 的三个扩展 ——
    "great_person": LegacyProfile(Priority.BONUS, Priority.BONUS),
    "science_endgame": LegacyProfile(Priority.CORE, Priority.OPTIMIZE, standing=True),
    "dvp": LegacyProfile(
        Priority.CORE, Priority.CORE, red=Priority.CORE, standing=True
    ),
    # —— 旧 API(policy.check_policies / lategame.check_lategame 等
    #     的渲染输出;它们的 warn/hint 已经语义对齐)——
    "policy": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    "lategame": LegacyProfile(Priority.CORE, Priority.OPTIMIZE, standing=True),
    "pace": LegacyProfile(Priority.CORE, Priority.CORE, standing=True),
    # —— 2026-08-01 wiring pass 接入的源 ——
    # tech_path 是【常驻上下文】:科技链/市政链每回合都长这样,不是"又提醒了一次"。
    # T15 实测:8 条建议休眠后整个「主线」段消失 —— 因为主线全被当成重复建议压掉了。
    "tech_path": LegacyProfile(Priority.CORE, Priority.CORE, standing=True),
    "boost": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    # 危机预案:🔴 行(被宣战/忠诚崩溃)是生存级;⚠️ 行(平民阵亡复盘)同样
    "crisis": LegacyProfile(Priority.SURVIVAL, Priority.CORE),
    # 胜利阻断:🔴 行是"对手要赢了" —— 正是 BLOCKING 档的定义
    "victory_block": LegacyProfile(
        Priority.BLOCKING, Priority.BLOCKING, red=Priority.BLOCKING
    ),
    "diplomacy": LegacyProfile(Priority.CORE, Priority.CORE),
    "ranking": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    "district": LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE),
    "culture_endgame": LegacyProfile(Priority.CORE, Priority.OPTIMIZE, standing=True),
}

_DEFAULT_PROFILE = LegacyProfile(Priority.OPTIMIZE, Priority.OPTIMIZE)

# 行首图标分类。⚠️/⚡/💰 = warn 类;💡/📊/⏳/🔮/🎯 = hint 类;🔴 = red 类。
_WARN_ICONS = ("⚠️", "⚡", "💰")
_HINT_ICONS = ("💡", "📊", "⏳", "🔮", "🎯")
_ITEM_PREFIXES = ("· ", "[")  # 无图标但独立成条的行


def _is_annotation(line: str) -> bool:
    """缩进行/括号行 = 模块的自我注解,附到上一条的 detail。"""
    return line.startswith((" ", "　", "(", "(", ")", ")"))


def from_legacy(
    module: str,
    lines: list[str],
    confidence: Confidence = Confidence.RECOMMENDED,
) -> list[Advice]:
    """把现存模块的 list[str] 输出翻译成 Advice 流。

    翻译规约见模块 docstring §5。不抛异常:空输入返回空列表。
    多行建议(首行 + 缩进续行)会合并为一条 Advice(续行进 detail)。
    """
    profile = LEGACY_REGISTRY.get(module, _DEFAULT_PROFILE)
    out: list[Advice] = []
    pending: dict | None = None

    def flush():
        nonlocal pending
        if pending is not None:
            out.append(
                Advice(
                    module=module,
                    priority=pending["priority"],
                    action=pending["action"],
                    confidence=confidence,
                    detail=tuple(pending["detail"]),
                    standing=profile.standing,
                )
            )
            pending = None

    for raw in lines or []:
        line = raw.rstrip()
        if not line:
            continue
        stripped = line.strip()
        if stripped.startswith("=="):
            continue  # 段头丢弃(含「== X == 附注」变体),
            # 模块身份来自注册表
        if _is_annotation(line) and pending is not None:
            pending["detail"].append(stripped)
            continue

        # 判定新条目的档位
        if stripped.startswith("🔴"):
            prio = profile.red
            text = stripped[1:].strip()
        elif stripped.startswith(_WARN_ICONS):
            prio = profile.warn
            text = stripped.lstrip("".join(_WARN_ICONS)).strip()
        elif stripped.startswith(_HINT_ICONS):
            prio = profile.hint
            text = stripped.lstrip("".join(_HINT_ICONS)).strip()
        elif stripped.startswith(_ITEM_PREFIXES):
            prio = profile.hint
            text = stripped[2:].strip() if stripped.startswith("· ") else stripped
        elif _is_annotation(line):
            continue  # 孤儿注解,丢弃
        else:
            prio = profile.hint
            text = stripped
        flush()
        pending = {"priority": prio, "action": text, "detail": []}

    flush()
    return out


# ---------------------------------------------------------------------------
# 受益对象存在性校验(任务 P6,2026-08-03)
# ---------------------------------------------------------------------------
#
# 校准局两次实测的错误推荐,根因是同一个:**建议假设了一个不存在的受益对象**。
#
#     · 我方零骑兵时,连续推荐「演习」政策卡(内容是加骑乘单位产能)
#     · 使者恒为 0 却连推 60 回合「投使者给城邦」,而且从不说使者从哪来
#
# 两种失败形态要区别对待:
#
#     drop    受益对象不存在时建议**完全无效**(政策卡加成类)。留着是纯噪音,
#             而且会让人误以为系统在跟踪一个其实不存在的东西。
#     annotate 建议依然成立,但缺了「你现在没有,得先从哪来」这一句 ——
#             补上获取途径,不删。
#
# 判据来自**实际观测**(单位列表/资源余额),不是文本推断。观测缺失时一律放行:
# 不知道有没有,就不许假装知道没有。


@dataclass(frozen=True)
class BeneficiaryRule:
    """一条受益对象规则。

    keywords 命中建议文本 → 查 fact_key 的实际数量 → 为 0 时按 mode 处理。
    """

    fact_key: str
    keywords: tuple[str, ...]
    mode: str  # "drop" | "annotate"
    source_hint: str = ""  # annotate 模式下补充的获取途径


BENEFICIARY_RULES: tuple[BeneficiaryRule, ...] = (
    # 政策卡类:没有受益单位时加成为 0,建议完全无效
    BeneficiaryRule(
        "cavalry", ("演习", "骑乘单位产能", "重骑兵产能", "轻骑兵产能"), "drop"
    ),
    BeneficiaryRule("naval", ("海上贸易", "舰队产能", "海军产能"), "drop"),
    BeneficiaryRule("siege", ("攻城单位产能",), "drop"),
    # 消耗品类:建议成立,但必须说清楚从哪来
    BeneficiaryRule(
        "envoys",
        ("使者", "派使", "send_envoy"),
        "annotate",
        "你目前手上 0 个使者。使者来源:市政解锁(每解锁一个新时代的市政给 1)、"
        "城邦任务奖励、部分政策卡与建筑 —— 没有存量时这条要先解决来源",
    ),
    BeneficiaryRule(
        "spies",
        ("间谍", "spy_action"),
        "annotate",
        "你目前没有可用间谍。间谍容量来自外交科技线与部分建筑/政策",
    ),
    BeneficiaryRule(
        "religion_units",
        ("传教士", "使徒", "宗教单位"),
        "annotate",
        "你目前没有宗教单位。它们只能在有你自己宗教为多数信仰的城市购买",
    ),
    BeneficiaryRule(
        "great_works",
        ("巨作", "大作"),
        "annotate",
        "你目前没有巨作。巨作来自伟人(作家/艺术家/音乐家)与部分奇观",
    ),
)


def verify_beneficiaries(
    items: list[Advice], facts: dict[str, int] | None
) -> tuple[list[Advice], list[str]]:
    """按受益对象存在性过滤/标注建议。

    facts 是实际观测的数量表({"cavalry": 0, "envoys": 2, ...})。
    **key 缺席 = 没观测到 = 放行**,只有显式的 0 才触发规则。
    返回 (保留的建议, 被删原因说明)。永不抛异常。
    """
    if not facts:
        return list(items or []), []
    kept: list[Advice] = []
    dropped: list[str] = []
    for a in items or []:
        try:
            text = f"{a.action} {a.reason}"
            verdict = None
            for rule in BENEFICIARY_RULES:
                if rule.fact_key not in facts:
                    continue  # 没观测到 → 不判
                if facts.get(rule.fact_key, 0) > 0:
                    continue  # 受益对象存在 → 放行
                if not any(k in text for k in rule.keywords):
                    continue
                verdict = rule
                break
            if verdict is None:
                kept.append(a)
            elif verdict.mode == "drop":
                dropped.append(f"{a.action[:24]}(受益对象不存在:{verdict.fact_key}=0)")
            else:
                kept.append(a.with_detail(verdict.source_hint))
        except Exception:
            kept.append(a)
    return kept, dropped


# ---------------------------------------------------------------------------
# 参考实现:排序 / 截断 / 渲染(AG/AN 落地时在此基础上扩展,接口不变)
# ---------------------------------------------------------------------------

BRIEFING_MAX_LINES = 40
_TIER_QUOTA = {Priority.CORE: 12, Priority.OPTIMIZE: 12, Priority.BONUS: 6}
_PER_MODULE_CAP = 6  # 单模块行数上限(SURVIVAL 豁免)
_TIER_CN = {
    Priority.SURVIVAL: "生存",
    Priority.BLOCKING: "阻断",
    Priority.CORE: "主线",
    Priority.OPTIMIZE: "优化",
    Priority.BONUS: "机会",
}
_TIER_ICON = {
    Priority.SURVIVAL: "🔴",
    Priority.BLOCKING: "🔴",
    Priority.CORE: "⚠️",
    Priority.OPTIMIZE: "💡",
    Priority.BONUS: "💡",
}


def sort_advice(items: list[Advice]) -> list[Advice]:
    """稳定排序:先优先级,同级按置信度(确定在前),再同级 L3 策略层在前。

    末位规则(审查 2026-08-05 F2-1):CORE 12 行配额按排序顺序填充,而
    playbook/doctrine 在管线里最后追加 —— 同优先级同置信度时全靠插入序,
    策略层的主线建议(科技崩盘/别攻墙)被 legacy 模块吃光配额,整段只剩
    "已省略 N 条低优先级建议"。L3 只赢平局,不越置信度。插入序保底,可复现。
    """

    def _l3_rank(a: Advice) -> int:
        return 0 if str(a.module).startswith(("playbook.", "doctrine.")) else 1

    return sorted(
        items, key=lambda a: (a.priority, _CONF_RANK[a.confidence], _l3_rank(a))
    )


def render_item(a: Advice) -> list[str]:
    """单条建议 → 最多 3 行文本。风险语言规约(§2)在这里落地。"""
    text = a.action
    if a.reason:
        text += f" —— {a.reason}"
    if a.consequence:
        if not text.endswith(("。", "!", "?", ")", ")")):
            text += "。"
        text += a.consequence
    line = f"{_TIER_ICON[a.priority]} {text}"
    # 例外才标注:可选/冒险必须亮出数字区间;冒险无退路要点名(§2)
    if a.confidence is Confidence.OPTIONAL:
        line += f"(可选,把握 {a.confidence.band_text})"
    elif a.confidence is Confidence.GAMBLE:
        line += f"(冒险,把握 {a.confidence.band_text}"
        line += f";退路:{a.alternative})" if a.alternative else ";⚠️ 无退路方案——慎用)"
    out = [line]
    out.extend(f"   {d}" for d in a.detail[:2])
    return out


def arbitrate(
    items: list[Advice],
    situation: str = "",
    plan_a: str = "",
    plan_b: str = "",
    line_budget: int = BRIEFING_MAX_LINES,
) -> str:
    """参考实现:排序 → 配额 → 截断 → AN 五段式渲染。

    永不抛异常;没有任何内容时返回空串。冲突检测(同 subject 反向建议)
    与采纳追踪是 AG/AH 落地时的扩展点,在此函数内加,签名不变。
    """
    try:
        return _arbitrate_inner(items, situation, plan_a, plan_b, line_budget)
    except Exception:
        return ""


def _arbitrate_inner(items, situation, plan_a, plan_b, line_budget) -> str:
    ranked = sort_advice(list(items or []))
    if not ranked and not situation:
        return ""

    # 单模块限行(SURVIVAL 豁免)
    per_module: dict[str, int] = {}
    kept: list[Advice] = []
    dropped: list[Advice] = []
    for a in ranked:
        cost = len(render_item(a))
        if a.priority is not Priority.SURVIVAL:
            used = per_module.get(a.module, 0)
            if used + cost > _PER_MODULE_CAP:
                dropped.append(a)
                continue
            per_module[a.module] = used + cost
        kept.append(a)

    # 分层配额
    tiers: dict[Priority, list[list[str]]] = {p: [] for p in Priority}
    for a in kept:
        block = render_item(a)
        quota = _TIER_QUOTA.get(a.priority)
        if quota is not None:
            used = sum(len(b) for b in tiers[a.priority])
            if used + len(block) > quota:
                dropped.append(a)
                continue
        tiers[a.priority].append(block)

    # 总量截断:从 BONUS → OPTIMIZE → CORE 自下而上砍
    def total() -> int:
        return sum(len(b) for blocks in tiers.values() for b in blocks)

    for tier in (Priority.BONUS, Priority.OPTIMIZE, Priority.CORE):
        while total() > line_budget and tiers[tier]:
            tiers[tier].pop()  # 层内最末 = 排序最低
            dropped.append(None)  # 占位,汇总时按层统计

    # —— 渲染:AN 五段式 ——
    out: list[str] = []
    if situation:
        out.append(f"【当前局面】{situation}")
        out.append("")
    for tier in (Priority.SURVIVAL, Priority.BLOCKING):
        for block in tiers[tier]:
            out.extend(block)
    if tiers[Priority.SURVIVAL] or tiers[Priority.BLOCKING]:
        out.append("")
    for tier in (Priority.CORE, Priority.OPTIMIZE, Priority.BONUS):
        if not tiers[tier]:
            continue
        out.append(f"== {_TIER_CN[tier]} ==")
        for block in tiers[tier]:
            out.extend(block)
        out.append("")
    if plan_a:
        out.append(f"【如果顺利】{plan_a}")
    if plan_b:
        out.append(f"【如果出问题】{plan_b}")
    real_dropped = [d for d in dropped if d is not None]
    if real_dropped:
        mods = sorted({d.module for d in real_dropped})
        out.append(
            f"(已省略 {len(real_dropped)} 条低优先级建议:{'·'.join(mods[:5])}"
            f"{' 等' if len(mods) > 5 else ''} —— 需要时主动询问)"
        )
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)
