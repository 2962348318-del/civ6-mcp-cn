"""L1 局势测量层 —— 不问难度是什么,直接量我和对手的差距。

═══════════════════════════════════════════════════════════════════════════
 设计文档(本 docstring 即交付物)
═══════════════════════════════════════════════════════════════════════════

## 0. 为什么需要这一层

方案 v4 §0.1/§0.2 的两条硬约束:

    · 难度加成表在 docs/gamedata/ 里是空的(DifficultyBonuses / HandicapInfos
      都 "missing": true)。任何"神级 AI 有 +80% 产能"之类的数字都是猜的。
    · 难度本身会随机变化。同样是神级,先手位置好坏差别巨大;同样是国王,
      被三家夹击也可能崩。

结论:**判据必须是「我 vs 对手的实测差距」,不是难度字符串。**
params.py 的 `cities_by_t50` 那套按难度名硬编码的阈值,校准局已被证伪
(实测 T56 才 3 城、T100 只 5 城,却是全场军力第一并攻下一座首都)。
本模块提供的是**实测替代品**:每次调用现场量,不依赖任何经验表。

## 1. 数据来源:零新增 Lua 往返

方案 v4 原文写着"对手科技需新增 Lua 查询"——**这条是错的**。
`build_rival_snapshot_query()` 早就在查 techs/civics/sci/cul/mil/cities/pop,
只是此前只喂给了日记(diary power curves),从没有人拿它做战略判断。
本模块直接消费 `lua.models.RivalSnapshot`,不新增任何往返。

## 2. 五档(方案 v4 §L1.2)

| 档位 | 打法 |
|---|---|
| CRUSHED   | 求生:不扩张、抱大腿、卖资源换和平、苟到中期 |
| BEHIND    | 追赶:单点押注(一条路线全力),放弃全面发展 |
| EVEN      | 常规:按标准决策树打 |
| AHEAD     | 扩大:主动开战 / 抢奇观 |
| DOMINANT  | 收官:直取胜利条件 |

### 2.1 分档不是单轴的(方案 v4 未定义,这里补齐)

v4 §L1.2 的表只给了科技比的区间,没说"科技 1.2 但军力 0.3"算哪档。
本模块的规则,以及每条的实证依据:

    主轴 = 科技比(帝国引擎的总量代理)
    军力比 < 0.5  → 封顶到 BEHIND      理由:军力只有对手一半时,
                                       "领先"随时可能被一次宣战清零
    军力比 < 0.5 且 科技比 < 0.75 → CRUSHED
    军力比 > 1.5 且 主轴 ≥ EVEN → 升一档  ← 校准局实证:T110 科技落后
                                       (14 vs 巴西 36 的时代)但军力全场
                                       第一,正确决策是立刻宣战,并且
                                       确实攻下了一座首都。若只看科技比
                                       会判 BEHIND 并龟缩,是错的。

### 2.2 样本不足时不许分档(校准局教训)

校准局实测:系统全程推荐 Culture 70%,而真相是"三方游客都是 0、
差距仅 3"——统计假象。任何比值在**绝对量太小时都没有意义**。
所以有 `MIN_SIGNAL` 门槛:最强对手的该维度绝对值低于门槛时,
该维度的比值标记为不可用(而不是算出一个 0.33 然后当真)。

## 3. 不做的事

  · 不给难度加成猜数字(§0.1)
  · 不用城市数单独判成败(校准局证伪)
  · 不做"应该有 N 城"的硬编码;要比较就和**实测的对手中位数**比
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

# 比值有意义所需的最小绝对量。低于此值的维度不参与分档 —— 见 §2.2。
MIN_SIGNAL = {
    "techs": 4,  # 4 科技以前大家都在挖第一层,差 1 个不说明问题
    "civics": 3,  # 市政树更浅,门槛比科技低一档
    "military": 20,  # 一个远古兵约 20-40,低于此说明双方都还没军队
    "cities": 2,
    "pop": 8,
    "science": 5.0,
    "culture": 4.0,
}
# ⚠️ 这张表必须覆盖 _DIMS 的每一项。漏一项 = 该维度恒为"可用",
# 于是 T22 那种「我 1 个市政、对手 1 个市政 → 比值 1.00× → 它是你最强的一轴
# → 资源优先给市政」的荒谬建议就会出现(2026-08-03 目视检查实际抓到过)。
# 模块底部有一条断言守着这件事。

# 扩张速率的观察窗口(回合)
EXPANSION_WINDOW = 20


class PowerTier(IntEnum):
    """五档局势。数值越大越有利;可直接做 >= / <= 比较。"""

    CRUSHED = 0
    BEHIND = 1
    EVEN = 2
    AHEAD = 3
    DOMINANT = 4


TIER_CN = {
    PowerTier.CRUSHED: "被碾压",
    PowerTier.BEHIND: "落后",
    PowerTier.EVEN: "势均力敌",
    PowerTier.AHEAD: "领先",
    PowerTier.DOMINANT: "统治级",
}

TIER_DOCTRINE = {
    PowerTier.CRUSHED: "求生:停止扩张、抱大腿、卖资源换和平,目标是活到中期",
    PowerTier.BEHIND: "追赶:单点押注一条路线全力推,放弃全面发展",
    PowerTier.EVEN: "常规:按标准决策树打,保持各线均衡",
    PowerTier.AHEAD: "扩大优势:主动开战或抢奇观,把领先兑现成不可逆资产",
    PowerTier.DOMINANT: "收官:直取胜利条件,不再做通用发展",
}


@dataclass(frozen=True)
class Side:
    """一方的可比较产出。字段与 RivalSnapshot 对齐,方便直接喂。"""

    name: str
    player_id: int = -1
    cities: int = 0
    pop: int = 0
    techs: int = 0
    civics: int = 0
    science: float = 0.0
    culture: float = 0.0
    military: int = 0
    gold: float = 0.0
    score: int = 0

    @classmethod
    def from_rival(cls, r) -> Side:
        """从 lua.models.RivalSnapshot(鸭子类型)构造。"""
        return cls(
            name=getattr(r, "name", "?"),
            player_id=int(getattr(r, "id", -1) or -1),
            cities=int(getattr(r, "cities", 0) or 0),
            pop=int(getattr(r, "pop", 0) or 0),
            techs=int(getattr(r, "techs", 0) or 0),
            civics=int(getattr(r, "civics", 0) or 0),
            science=float(getattr(r, "sci", 0.0) or 0.0),
            culture=float(getattr(r, "cul", 0.0) or 0.0),
            military=int(getattr(r, "mil", 0) or 0),
            gold=float(getattr(r, "gold", 0.0) or 0.0),
            score=int(getattr(r, "score", 0) or 0),
        )

    def get(self, dim: str) -> float:
        return float(getattr(self, dim, 0) or 0)


@dataclass(frozen=True)
class Ratio:
    """一个维度上「我 / 该维度最强对手」的比值,带可用性标记。"""

    dim: str
    mine: float
    best: float
    best_name: str = ""
    usable: bool = True  # False = 绝对量太小,比值无意义(§2.2)

    @property
    def value(self) -> float:
        if self.best <= 0:
            return 1.0 if self.mine <= 0 else 2.0
        return self.mine / self.best

    def __str__(self) -> str:
        if not self.usable:
            return f"{self.dim} {self.mine:g} vs {self.best:g}(量太小,不作数)"
        return (
            f"{self.dim} {self.mine:g} vs {self.best_name or '最强'} "
            f"{self.best:g} = {self.value:.2f}×"
        )


@dataclass
class PowerSnapshot:
    """一次局势测量。所有数字都是实测,没有一个来自经验表。"""

    turn: int
    me: Side
    rivals: list[Side] = field(default_factory=list)
    ratios: dict[str, Ratio] = field(default_factory=dict)
    tier: PowerTier = PowerTier.EVEN
    reasons: list[str] = field(default_factory=list)
    # 游戏难度(DIFFICULTY_* 或已本地化的名字)。空 = 没采集到。
    # 2026-08-12 缺陷111:档位行只印【统治级】,那是"我 vs 对手"的实测比值,
    # 和难度设置是两回事。两个概念挨着,只显示其中一个,结果整局都在按
    # "神级"推理 —— 而实际难度是军阀(八档里第三档,AI 拿减益)。难度就在
    # get_game_overview 第一行,每回合都在眼前,却从没进过这条判断链。
    difficulty: str = ""
    # 对手近 EXPANSION_WINDOW 回合的铺城速度(城/回合),需要历史才有值
    rival_expansion_rate: float | None = None
    my_expansion_rate: float | None = None
    # 审查 2026-08-05 F2-7:军力比 >1.5 把【落后及以下】的主轴档抬了上来。
    # 这是"军力窗口在掩盖科技劣势"—— 档位可以升(该打就打),但生存纪律
    # (停止摊薄、资源收拢)必须照旧生效,否则 survival_priority 整棵树沉默。
    # T335 实测:科技 0.65×(落后) + 军力 1.57× → EVEN,"停止全面发展"
    # 一个字都没说,8 城齐造军事单位没人拦。
    military_masks_deficit: bool = False

    # -- 便捷读数 ---------------------------------------------------------
    # 全部走 _ratio():**不可用的维度一律返回 1.0(势均力敌)**,而不是
    # Ratio.value 那个在 best<=0 时给出的 2.0。
    #
    # 2026-08-06 实机事故:法国局 T4-T10,我方 1 勇士 + 1 侦察兵、对手军力
    # 未知(0),`military_ratio` 直接吐出 2.00×,playbook.war_timing 据此
    # 反复推荐"现在是开战性价比最高的窗口 —— 选一个接壤的弱邻居动手"。
    # 而同一份快照的 render() 同时在说"军力绝对量太小,比值不作数" ——
    # 同一个 snapshot 两个口径打架,这就是漏洞的指纹。
    #
    # 8-05 加的 `if best <= 0 < mine: usable = False` 只作用于 _classify
    # 与 _render_inner;这三个 property 绕过了它,下游决策树全部中招。
    # 返回 1.0 是刻意的:数据不可用时给中性值,既不会触发"开战窗口",
    # 也不会触发"封顶到落后档"。要精确区分"真的势均力敌"和"没数据",
    # 用 ratio_usable(dim) 或直接读 ratios[dim].usable。
    def _ratio(self, dim: str) -> float:
        r = self.ratios.get(dim)
        return r.value if r is not None and r.usable else 1.0

    def ratio_usable(self, dim: str) -> bool:
        """该维度的比值是否可信(绝对量够大、双方都有数据)。"""
        r = self.ratios.get(dim)
        return bool(r is not None and r.usable)

    @property
    def tech_ratio(self) -> float:
        return self._ratio("techs")

    @property
    def military_ratio(self) -> float:
        return self._ratio("military")

    @property
    def city_ratio(self) -> float:
        return self._ratio("cities")

    @property
    def top_rival(self) -> Side | None:
        """分数最高的对手 —— 叙事用的"头号对手",不是分档依据。"""
        return max(self.rivals, key=lambda s: s.score) if self.rivals else None

    @property
    def is_survival_mode(self) -> bool:
        # 军力升档掩盖主轴落后时,生存纪律照旧(审查 F2-7)——
        # 军力换城市的窗口期,恰恰是最不能摊薄资源的时候。
        return self.tier <= PowerTier.BEHIND or self.military_masks_deficit

    @property
    def strongest_axis(self) -> Ratio | None:
        """比值最高的可用维度 —— BEHIND 档「单点押注」押哪一点的答案。

        落后档下全面发展必输(对手每一项都比你快),唯一的出路是把
        资源全压在你唯一不落后的那条线上。这个属性回答"那条线是什么"。
        """
        usable = [r for r in self.ratios.values() if r.usable]
        return max(usable, key=lambda r: r.value) if usable else None

    def to_dict(self) -> dict:
        # my_score/median_score 是分数斜率哨兵(score_gap)的数据源:
        # 存进历史后,相邻两次测量(间隔 _POWER_INTERVAL)天然构成一个
        # 斜率窗口,不需要任何新增查询或独立存储。
        rival_scores = [s.score for s in self.rivals if s.score > 0]
        rival_scores.sort()
        median_score = rival_scores[len(rival_scores) // 2] if rival_scores else 0
        return {
            "turn": self.turn,
            "cities": self.me.cities,
            "techs": self.me.techs,
            "military": self.me.military,
            "tier": int(self.tier),
            "rival_cities": {s.player_id: s.cities for s in self.rivals},
            "my_score": self.me.score,
            "median_score": median_score,
            # 军力窗口倒计时(military_window_turns)的数据源。
            "rival_military_best": max((s.military for s in self.rivals), default=0),
        }


# ---------------------------------------------------------------------------
# 测量
# ---------------------------------------------------------------------------

_DIMS = ("techs", "civics", "science", "culture", "military", "cities", "pop")


def _best_in(rivals: list[Side], dim: str) -> tuple[float, str]:
    """该维度上最强的对手值与名字。空列表返回 (0, "")。

    刻意取【每维各自的最强者】而不是"综合最强的那一家":
    你是否落后于科技,取决于科技最快的那家,而不是分数最高的那家。
    宁可高估对手 —— 低估的代价是判错档然后按错的打法走整局。
    """
    if not rivals:
        return 0.0, ""
    top = max(rivals, key=lambda s: s.get(dim))
    return top.get(dim), top.name


def measure(
    turn: int,
    me: Side,
    rivals: list[Side],
    army_era_lag: int | None = None,
    difficulty: str = "",
) -> PowerSnapshot:
    """现场测量局势。不抛异常;没有对手时返回 EVEN 并写明原因。

    army_era_lag:我方军事单位中位时代落后游戏当前时代几个时代
    (war_planner.army_era_profile 提供;None = 无数据,不参与判断)。
    difficulty:游戏难度,只用于显示 —— 档位判定始终看实测比值,
    不看难度名(难度加成表在导出数据里是空的,见模块头注)。
    """
    snap = PowerSnapshot(turn=turn, me=me, rivals=list(rivals or []))
    snap.difficulty = difficulty or ""
    for dim in _DIMS:
        best, best_name = _best_in(snap.rivals, dim)
        floor = MIN_SIGNAL.get(dim)
        usable = floor is None or max(best, me.get(dim)) >= floor
        # 2026-08-03 实机抓到:我方人口没有任何来源,于是恒为 0,
        # 分档里显示「人口 0.00×」—— 一个凭空捏造的劣势。
        # 我方为 0 而对手明显不为 0,几乎一定是**我方该维度缺传感器**,
        # 不是真的一个人都没有。缺数据要显形,不能伪装成极端劣势。
        if me.get(dim) <= 0 < best:
            usable = False
        # 镜像防护(审查 F2-9):对手全为 0 而我方有值 —— 几乎一定是对手
        # 该维度缺传感器,不是真的优势。放行会捏造一个 2.00× 假优势,
        # 直接坐上 strongest_axis 变成"就押它"。
        if best <= 0 < me.get(dim):
            usable = False
        snap.ratios[dim] = Ratio(
            dim=dim,
            mine=me.get(dim),
            best=best,
            best_name=best_name,
            usable=usable,
        )
    snap.tier, snap.reasons, snap.military_masks_deficit = _classify(snap, army_era_lag)
    return snap


def _tier_from_ratio(r: float) -> PowerTier:
    """主轴区间(方案 v4 §L1.2 的表)。"""
    if r < 0.5:
        return PowerTier.CRUSHED
    if r < 0.75:
        return PowerTier.BEHIND
    if r < 1.1:
        return PowerTier.EVEN
    if r < 1.5:
        return PowerTier.AHEAD
    return PowerTier.DOMINANT


def _classify(
    snap: PowerSnapshot, army_era_lag: int | None = None
) -> tuple[PowerTier, list[str], bool]:
    """分档 + 人话理由 + 军力是否在掩盖主轴落后。规则见 docstring §2.1。"""
    if not snap.rivals:
        return (
            PowerTier.EVEN,
            ["还没遇到任何对手 —— 无法测量差距,按势均力敌处理"],
            False,
        )

    tech = snap.ratios.get("techs")
    mil = snap.ratios.get("military")
    reasons: list[str] = []

    if tech is None or not tech.usable:
        # 开局前十几回合的常态:大家都只有两三个科技,比值是噪音
        reasons.append(
            f"科技绝对量太小({tech.mine:g} vs {tech.best:g}),"
            f"比值不作数 —— 开局阶段按势均力敌处理,等 {MIN_SIGNAL['techs']} 科技后再判"
            if tech
            else "科技数据缺失"
        )
        base = PowerTier.EVEN
    else:
        base = _tier_from_ratio(tech.value)
        reasons.append(f"主轴科技比 {tech.value:.2f}×({tech})")

    tier = base
    masks_deficit = False
    if mil is not None and mil.usable:
        mr = mil.value
        if mr < 0.5:
            if tech is not None and tech.usable and tech.value < 0.75:
                tier = PowerTier.CRUSHED
                reasons.append(f"军力比 {mr:.2f}× 且科技也落后 —— 双轴同时崩,判被碾压")
            elif tier > PowerTier.BEHIND:
                tier = PowerTier.BEHIND
                reasons.append(
                    f"军力比仅 {mr:.2f}×,封顶到落后档 —— "
                    f"军力只有对手一半时,任何领先都可能被一次宣战清零"
                )
            else:
                reasons.append(f"军力比 {mr:.2f}×({mil})")
        elif mr > 1.5 and tier < PowerTier.DOMINANT:
            if army_era_lag is not None and army_era_lag >= 2:
                # 科技是第一生产力(2026-08-05 用户实战总结):主力落后
                # ≥2 个时代的军队,数量堆出来的比值兑现不了 —— 不升档。
                # T110 的升档实证是【当代兵】的军力优势(勇士/弓箭手打
                # 远古-古典战争);T335 的 13 个中世纪弩手在现代战场连墙
                # 都磨不动,同一个 1.5× 阈值,两种完全不同的真实价值。
                reasons.append(
                    f"军力比 {mr:.2f}× 但主力落后当前时代 {army_era_lag} 个时代"
                    f" —— 纸面数量堆出来的分,不升档。科技是第一生产力:"
                    f"先补代差(upgrade_unit / 追科技),再谈把军力兑现成城市"
                )
            else:
                # 校准局 T110 实证:科技 14 vs 36(比 0.39,主轴判被碾压)但
                # 军力 148 vs 65。此时正确决策不是"求生龟缩",而是立刻宣战
                # ——并且确实攻下了一座首都。所以军力优势的升档【不限主轴
                # 档位】:军力是把科技劣势换成城市的唯一机制,而它有保质期。
                # 但基础档在【落后及以下】时,升档只改变"该不该打",不豁免
                # 生存纪律 —— masks_deficit 让 is_survival_mode 照旧生效
                # (审查 F2-7:T335 就是升档吞掉了"停止摊薄"的整棵决策树)。
                masks_deficit = base <= PowerTier.BEHIND
                tier = PowerTier(int(tier) + 1)
                reasons.append(
                    f"军力比 {mr:.2f}× —— 升一档。"
                    + (
                        "军力是你唯一的领先项,且有保质期:对手科技一上来就归零。"
                        "现在不把它兑现成城市,就永远兑现不了"
                        if tech is not None and tech.usable and tech.value < 0.75
                        else "军力优势是有保质期的资产,该兑现成城市"
                    )
                )
        else:
            reasons.append(f"军力比 {mr:.2f}×({mil})")

    return tier, reasons, masks_deficit


# ---------------------------------------------------------------------------
# 历史与扩张速率
# ---------------------------------------------------------------------------


class PowerHistory:
    """跨回合的测量历史。只留分档必需的最小状态,可序列化进 world_model。"""

    def __init__(self, records: list[dict] | None = None) -> None:
        self.records: list[dict] = list(records or [])

    def record(self, snap: PowerSnapshot) -> None:
        if not isinstance(snap, PowerSnapshot):
            return
        row = snap.to_dict()
        # 同回合重复测量只留最后一次
        self.records = [r for r in self.records if r.get("turn") != row["turn"]]
        self.records.append(row)
        self.records.sort(key=lambda r: r.get("turn", 0))
        if len(self.records) > 200:
            self.records = self.records[-200:]

    def _at_or_before(self, turn: int) -> dict | None:
        older = [r for r in self.records if r.get("turn", 0) <= turn]
        return older[-1] if older else None

    def expansion_rates(
        self, snap: PowerSnapshot, window: int = EXPANSION_WINDOW
    ) -> tuple[float | None, float | None]:
        """(我方, 对手合计) 近 window 回合的铺城速度(城/回合)。

        没有足够历史时返回 (None, None) —— 宁可不报,也不拿 1 个采样点编趋势。
        """
        past = self._at_or_before(snap.turn - window)
        if past is None or past.get("turn") == snap.turn:
            return None, None
        span = snap.turn - int(past.get("turn", 0))
        if span <= 0:
            return None, None
        mine = (snap.me.cities - int(past.get("cities", 0))) / span
        old_rivals = past.get("rival_cities") or {}
        now_total = sum(s.cities for s in snap.rivals)
        # 键在 JSON 往返后会变成 str,两种都认
        old_total = sum(int(v) for v in old_rivals.values())
        theirs = (now_total - old_total) / span if old_rivals else None
        return round(mine, 3), (round(theirs, 3) if theirs is not None else None)

    def military_window_turns(
        self, snap: PowerSnapshot, floor: float = 1.3
    ) -> int | None:
        """军力比跌到 floor 还剩几回合。无历史/在涨/已跌破 → None。

        两局同一个错(罗马 T335 的 1.57× 拖 15 回合、法国局 T55-62 的
        1.63× 拖到 T64 掉回 1.26×):军力优势会随对手科技上升自动归零,
        而系统只报过当前比值,从没算过**还剩几回合**。没有倒计时,
        "窗口会关"就只是一句正确的废话。

        线性外推,故意保守:只用最近两次测量,不做拟合 —— 对手军力
        增速本来就不匀速,拟合出来的精度是假的。

        回看点取【最近一条更早的记录】,不是固定 EXPANSION_WINDOW(20)
        之前那条:测量间隔是 _POWER_INTERVAL(10),要求 20 回合前的记录
        会让倒计时在整整两个测量周期内都算不出来 —— 而窗口本身可能
        只有十几回合。与 score_gap._slopes 同一取法。
        """
        earlier = [r for r in self.records if int(r.get("turn", 0)) < snap.turn]
        if not earlier:
            return None
        past = earlier[-1]
        span = snap.turn - int(past.get("turn", 0))
        if span <= 0:
            return None
        old_mine = float(past.get("military", 0) or 0)
        old_best = float(past.get("rival_military_best", 0) or 0)
        now_mine = float(snap.me.military or 0)
        now_best = max((float(s.military or 0) for s in snap.rivals), default=0.0)
        if min(old_mine, old_best, now_mine, now_best) <= 0:
            return None
        now_ratio = now_mine / now_best
        if now_ratio <= floor:
            return 0
        # 双方各自的增速,按当前速度往前推到比值等于 floor
        d_mine = (now_mine - old_mine) / span
        d_best = (now_best - old_best) / span
        for t in range(1, 61):
            m = now_mine + d_mine * t
            b = now_best + d_best * t
            if b <= 0:
                return None
            if m / b <= floor:
                return t
        return None  # 60 回合内不会关 —— 那就不算"窗口"了

    def trend(self, snap: PowerSnapshot, window: int = EXPANSION_WINDOW) -> str:
        """档位走势的一句话。无历史返回空串。"""
        past = self._at_or_before(snap.turn - window)
        if past is None:
            return ""
        old = PowerTier(int(past.get("tier", int(PowerTier.EVEN))))
        if old == snap.tier:
            return f"近 {snap.turn - int(past['turn'])} 回合档位未变({TIER_CN[old]})"
        arrow = "↑" if snap.tier > old else "↓"
        return (
            f"档位 {TIER_CN[old]} {arrow} {TIER_CN[snap.tier]}"
            f"(T{past['turn']} → T{snap.turn})"
        )

    def to_list(self) -> list[dict]:
        return list(self.records)


def attach_history(
    snap: PowerSnapshot, history: PowerHistory, window: int = EXPANSION_WINDOW
) -> PowerSnapshot:
    """把扩张速率填进快照并把本次测量记入历史。返回同一个快照对象。"""
    try:
        mine, theirs = history.expansion_rates(snap, window)
        snap.my_expansion_rate = mine
        snap.rival_expansion_rate = theirs
        history.record(snap)
    except Exception:
        pass
    return snap


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


_DIFFICULTY_CN: dict[str, str] = {
    "DIFFICULTY_SETTLER": "开拓者(1/8)",
    "DIFFICULTY_CHIEFTAIN": "首领(2/8)",
    "DIFFICULTY_WARLORD": "军阀(3/8)",
    "DIFFICULTY_PRINCE": "王子(4/8)",
    "DIFFICULTY_KING": "国王(5/8)",
    "DIFFICULTY_EMPEROR": "君主(6/8)",
    "DIFFICULTY_IMMORTAL": "不朽(7/8)",
    "DIFFICULTY_DEITY": "神级(8/8)",
}


def difficulty_label(raw: str) -> str:
    """把难度渲染成带档位序号的形式。

    序号是关键:光写"军阀"看不出它在八档里靠哪头,而 AI 在王子以下拿减益、
    君主以上拿加成 —— 这条界线决定了该保守还是该主动兑现优势。
    已经本地化的名字(实机 get_game_overview 直接给「军阀」)原样透出。
    """
    key = (raw or "").strip()
    if not key:
        return ""
    return _DIFFICULTY_CN.get(key.upper(), key)


def headline(snap: PowerSnapshot) -> str:
    """一行局势判断,供 advice.arbitrate(situation=...) 的【当前局面】段。"""
    tail = TIER_DOCTRINE.get(snap.tier, "")
    if snap.military_masks_deficit:
        # 一致性(审查 F2-7 + 实机 T335):档位行不能一边说"保持均衡"、
        # 下一行又说"军力是唯一领先项赶紧兑现"。升档掩盖落后时明说。
        tail = (
            "军力升档在掩盖主轴落后 —— 纪律按【落后】档执行:停止摊薄、"
            "资源收拢到主线,同时把军力窗口兑现成城市"
        )
    if snap.tier is PowerTier.BEHIND:
        axis = snap.strongest_axis
        if axis is not None and axis.value >= 1.0:
            tail += f";你唯一不落后的是【{_DIM_CN.get(axis.dim, axis.dim)}】({axis.value:.2f}×),就押它"
    diff = difficulty_label(snap.difficulty)
    diff_str = f" ‖ 难度 {diff}" if diff else ""
    return f"T{snap.turn} 实测档位【{TIER_CN[snap.tier]}】{diff_str} —— {tail}"


def render(snap: PowerSnapshot, history: PowerHistory | None = None) -> str:
    """完整的局势测量段。没有对手数据时返回简短说明,不返回空串。"""
    try:
        return _render_inner(snap, history)
    except Exception:
        return ""


def _render_inner(snap: PowerSnapshot, history: PowerHistory | None) -> str:
    doctrine_line = TIER_DOCTRINE.get(snap.tier, "")
    if snap.military_masks_deficit:
        doctrine_line = (
            "军力升档在掩盖主轴落后 —— 纪律按【落后】档执行:"
            "停止摊薄、资源收拢到主线,同时把军力窗口兑现成城市"
        )
    lines = [
        f"== 局势测量(实测,非难度经验值)== T{snap.turn}",
        f"档位:【{TIER_CN[snap.tier]}】{doctrine_line}",
    ]
    for r in snap.reasons[:3]:
        lines.append(f"  · {r}")

    usable = [
        snap.ratios[d] for d in _DIMS if d in snap.ratios and snap.ratios[d].usable
    ]
    if usable:
        cells = []
        for r in usable:
            mark = "" if 0.9 <= r.value <= 1.1 else ("↑" if r.value > 1.1 else "↓")
            cells.append(f"{_DIM_CN.get(r.dim, r.dim)} {r.value:.2f}×{mark}")
        lines.append("各维比值(我/该维最强对手):" + " ‖ ".join(cells))

    # 两种「不作数」要分开说:量太小 vs 我方压根没数据。
    # 后者是**传感器缺口**,必须点名 —— 藏起来就会变成下一个 1e。
    too_small, no_data = [], []
    for d in _DIMS:
        r = snap.ratios.get(d)
        if r is None or r.usable:
            continue
        (no_data if r.mine <= 0 < r.best else too_small).append(_DIM_CN.get(d, d))
    if too_small:
        lines.append(
            f"(以下维度绝对量太小,比值不作数:{'、'.join(too_small)} —— "
            f"这正是校准局「三方游客都是 0 却报文化领先 70%」的成因)"
        )
    if no_data:
        lines.append(
            f"⚠️ 以下维度**我方无数据**(对手有值,我方为 0),已排除出分档:"
            f"{'、'.join(no_data)} —— 这是传感器缺口,不是真的劣势,需要修"
        )

    top = snap.top_rival
    if top:
        lines.append(
            f"头号对手:{top.name} 分{top.score} 城{top.cities} "
            f"科技{top.techs} 军力{top.military}"
        )

    if snap.my_expansion_rate is not None:
        mine = snap.my_expansion_rate * EXPANSION_WINDOW
        seg = f"近 {EXPANSION_WINDOW} 回合铺城:我 +{mine:.1f}"
        if snap.rival_expansion_rate is not None:
            theirs = snap.rival_expansion_rate * EXPANSION_WINDOW
            seg += f",对手合计 +{theirs:.1f}"
            if theirs > mine * 2 and theirs >= 2:
                seg += " ⚠️ 对手扩张是你的两倍以上,差距会随时间复利放大"
        lines.append(seg)

    if history is not None:
        t = history.trend(snap)
        if t:
            lines.append(t)

    return "\n".join(lines)


_DIM_CN = {
    "techs": "科技",
    "civics": "市政",
    "science": "科研",
    "culture": "文化",
    "military": "军力",
    "cities": "城市",
    "pop": "人口",
}

# 供其它模块(doctrine 等)复用的维度中文名
DIM_CN = _DIM_CN

# 导入期自检:每个参与分档的维度都必须有最小信号门槛与中文名。
# 加新维度时忘了配门槛,会在这里立刻炸掉,而不是在某一局里悄悄给出
# 一条"押注一个双方都是 0 的维度"的建议。
assert set(_DIMS) <= set(MIN_SIGNAL), (
    f"这些维度缺 MIN_SIGNAL 门槛:{sorted(set(_DIMS) - set(MIN_SIGNAL))}"
)
assert set(_DIMS) <= set(_DIM_CN), (
    f"这些维度缺中文名:{sorted(set(_DIMS) - set(_DIM_CN))}"
)
