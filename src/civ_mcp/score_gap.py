"""分数差距的归因与斜率 —— 把「你排第 6」变成可执行信号。

2026-08-06 用户观察:「它打每次得分都差别人好多」。两局复盘的病根一致
(罗马 T362 分数垫底弃局、法国 T64 分数 39 vs 82):Civ 6 的总分 ≈ 帝国
规模各项存量(科技数、市政数、人口、城市/区域/建筑、奇观、伟人)的
时间积分。而系统对分数完全沉默 —— overview 里有总分和排名,agent 每回合
看得到「你排第 6」,但没人告诉它差距**来自哪一项**、是在**收窄还是扩大**、
该**做什么**。「分数低」于是只是一种焦虑,不是一个信号。

三条设计决定:

1. **存量差不报警,斜率差才报警。** 国王及以上难度的 AI 开局自带额外
   开拓者与单位,前几十回合存量落后是结构性的 —— 高手打这个难度同样
   落后,中盘反超。把结构性落后当失败去追,会做出坏决策(比如提前打
   没准备的仗)。斜率连续落后才是真信号:那说明差距在扩大。
2. **归因只给排序,不给分值。** 计分权重是游戏常数,但拿不到真值
   (分项接口待探测),硬编码一套猜的权重比不给更糟 —— 按相对差排序
   已经足以回答「差在哪」。
3. **军队不产分。** 分数公式里没有军力项;打不下城的战争在记分板上是
   纯支出。这句要在斜率落后时明说,因为「打一仗把分抢回来」是最诱人
   的错误结论(罗马局 T350 的原型)。

数据零新增:归因用 PowerSnapshot 已有的 me/rivals(每 _POWER_INTERVAL
回合实测),斜率用 PowerHistory.records 新增的 my_score/median_score 字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 斜率窗口(回合)。与 end_turn._POWER_INTERVAL 对齐:历史里相邻两条记录
# 正好构成一个窗口,不需要插值。
SLOPE_WINDOW = 10
# 连续落后窗口数达到该值 → 从"陈述"升级为"警报"
LAG_ALERT = 2
# 此前的存量落后视为结构性(AI 高难开局加成),只陈述不催动作
EARLY_TURN = 50

# 归因维度:只用直接计分的存量项。science/culture 是流量(techs/civics 的
# 导数),不直接计分,不进归因 —— 它们的落后会通过 techs/civics 显形。
_DIMS: tuple[tuple[str, str], ...] = (
    ("techs", "科技数"),
    ("civics", "市政数"),
    ("pop", "人口"),
    ("cities", "城市数"),
)


@dataclass(frozen=True)
class ScoreGap:
    """一次分数差距测量。数字全部实测,缺数据的字段为 None/空。"""

    turn: int
    my_score: int
    median: int
    leader: int
    leader_name: str
    # (中文名, 我方值, 对手中位值),仅落后项,按 我/中位 升序(最落后在前)
    shortfalls: list[tuple[str, float, float]] = field(default_factory=list)
    slope_mine: float | None = None  # 分/回合
    slope_median: float | None = None
    lag_windows: int = 0  # 连续斜率落后的窗口数(含当前)


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


def build(snap, history=None) -> ScoreGap | None:
    """从局势快照(+可选历史)构造分数差距报告。

    数据不足时返回 None(诚实静默):没有对手分数、或我方分数读不到,
    任何一条归因都会是编的。
    """
    rivals = [r for r in getattr(snap, "rivals", []) if int(getattr(r, "score", 0)) > 0]
    my_score = int(getattr(getattr(snap, "me", None), "score", 0) or 0)
    if not rivals or my_score <= 0:
        return None

    median = int(_median([float(r.score) for r in rivals]))
    leader = max(rivals, key=lambda r: r.score)

    shortfalls: list[tuple[str, float, float]] = []
    for dim, cn in _DIMS:
        mine = float(getattr(snap.me, dim, 0) or 0)
        med = _median([float(getattr(r, dim, 0) or 0) for r in rivals])
        if med > 0 and mine < med:
            shortfalls.append((cn, mine, med))
    # 最落后的维度排最前 —— 排序本身就是归因(设计决定 2)
    shortfalls.sort(key=lambda t: t[1] / t[2])

    slope_mine, slope_median, lag = _slopes(history)

    return ScoreGap(
        turn=int(getattr(snap, "turn", 0) or 0),
        my_score=my_score,
        median=median,
        leader=int(leader.score),
        leader_name=str(getattr(leader, "name", "?")),
        shortfalls=shortfalls,
        slope_mine=slope_mine,
        slope_median=slope_median,
        lag_windows=lag,
    )


def _slopes(history) -> tuple[float | None, float | None, int]:
    """(我方斜率, 对手中位斜率, 连续落后窗口数)。

    宁可 (None, None, 0) 也不拿一个采样点编趋势 —— 与
    PowerHistory.expansion_rates 同一条纪律。旧版本的历史记录没有
    my_score 字段,一律当缺数据跳过,不猜。
    """
    records = list(getattr(history, "records", []) or [])
    usable = [
        r
        for r in records
        if int(r.get("my_score", 0) or 0) > 0 and int(r.get("median_score", 0) or 0) > 0
    ]
    if len(usable) < 2:
        return None, None, 0

    last, prev = usable[-1], usable[-2]
    dt = int(last["turn"]) - int(prev["turn"])
    if dt <= 0:
        return None, None, 0
    slope_mine = (int(last["my_score"]) - int(prev["my_score"])) / dt
    slope_median = (int(last["median_score"]) - int(prev["median_score"])) / dt

    # 从最新往回数连续落后的窗口;一旦某个窗口不落后就停
    lag = 0
    for newer, older in zip(reversed(usable), list(reversed(usable))[1:]):
        w = int(newer["turn"]) - int(older["turn"])
        if w <= 0:
            break
        d_mine = (int(newer["my_score"]) - int(older["my_score"])) / w
        d_med = (int(newer["median_score"]) - int(older["median_score"])) / w
        if d_mine < d_med:
            lag += 1
        else:
            break
    return slope_mine, slope_median, lag


def render(gap: ScoreGap | None) -> list[str]:
    """简报行(0-3 行)。领先时一行确认,落后时归因 + 斜率判断。"""
    if gap is None:
        return []

    if gap.my_score >= gap.median:
        line = f"【分数】{gap.my_score},高于对手中位 {gap.median}"
        if gap.leader > gap.my_score:
            line += f"(第一名 {gap.leader_name} {gap.leader})"
        return [line]

    out: list[str] = []
    head = f"【分数】{gap.my_score} vs 对手中位 {gap.median}(差 {gap.median - gap.my_score})"
    if gap.shortfalls:
        parts = [f"{cn} {mine:g}vs{med:g}" for cn, mine, med in gap.shortfalls[:3]]
        head += "|缺口最大:" + "、".join(parts) + "〔按相对差排序,分数=规模的积分〕"
    out.append(head)

    if gap.slope_mine is None or gap.slope_median is None:
        if gap.turn < EARLY_TURN:
            out.append(
                "   开局存量落后是结构性的(高难度 AI 带开局加成),"
                "盯斜率不盯存量 —— 下个测量窗口起看追赶速度"
            )
        return out

    if gap.lag_windows >= LAG_ALERT:
        out.append(
            f"   ⚠️ 分数斜率连续 {gap.lag_windows} 个窗口落后"
            f"(你 +{gap.slope_mine:.1f}/t vs 中位 +{gap.slope_median:.1f}/t)"
            f" —— 差距在扩大。军队不产分:把产能转回城市/人口/科技这些计分项,"
            f"缺口第一位是{gap.shortfalls[0][0] if gap.shortfalls else '规模'}"
        )
    elif gap.slope_mine >= gap.slope_median:
        out.append(
            f"   斜率 +{gap.slope_mine:.1f}/t vs 中位 +{gap.slope_median:.1f}/t,"
            f"在收窄 —— 路线正确,存量差是历史,不用为它改策略"
        )
    else:
        out.append(
            f"   斜率 +{gap.slope_mine:.1f}/t vs 中位 +{gap.slope_median:.1f}/t,"
            f"本窗口略落后(连续 {gap.lag_windows} 窗口)—— 先观察,连续 "
            f"{LAG_ALERT} 个窗口落后才需要动策略"
        )
    return out
