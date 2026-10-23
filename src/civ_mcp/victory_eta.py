"""胜利路线 ETA 对比 —— 回答"照这个速度,来得及赢吗"。

罗马局的教训:活到 T362,每一步都有里程碑("下一个里程碑缺 X 产能"),
每一步都在推进 —— 但整条路早就输了。系统从头到尾没有问过一句
**「来得及吗」**。

里程碑回答"下一步做什么",ETA 回答"这条路还赢不赢得了"。后者才是
换道决策的依据,而换道是整局最贵的决策之一(沉没成本会让人一路走到黑)。

本模块只做一件事:把【我方 ETA】与【对手 ETA】放在同一把尺上比,
然后给出"提速 / 换道 / 压制"三选一的判据。**不替 agent 做决定** ——
系统给判据,决策留给 agent(这是全项目一致的分工)。

## 诚实边界

对手 ETA 全部来自**线性外推**(两次采样之间的进度增速),标注
〔外推,非精确〕。三条自律:
  - 采样不足两次 → 不报 ETA,只报当前进度
  - 进度不增长(增速 ≤ 0)→ 不报 ETA(外推会得出负数或无穷大)
  - 外推超过 999 回合 → 视同"这条路他走不通",不报

宁可少报,也不要给一个精确到回合的假数字 —— agent 会照着它做换道决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 领先/落后多少回合才值得改变策略。小于它属于测量噪音。
MATERIAL_GAP = 25
# 外推上限:超过就认为这条路对方走不通
MAX_ETA = 999
# 采样保留上限(每 10 回合一次,够覆盖整局)
MAX_SAMPLES = 60


@dataclass
class VictoryETA:
    """一方在一条路线上的完成预计。"""

    who: str
    victory: str  # SCIENCE / DIPLOMATIC / CULTURE
    progress: float  # 0..1
    eta_turns: int | None  # None = 数据不足或不增长
    rate: float = 0.0  # 每回合进度增量(用于叙述)
    # 我方标记。**必须按 player_id 判,不能按名字判** ——
    # VictoryProgress.players 里包含我们自己,而我方在别处的显示名
    # ("你"/civ_name/leader_name)与这里的 name 不保证一致,
    # 按名字过滤会把自己当成"最快的对手"报出来。
    pid: int = -1

    @property
    def victory_cn(self) -> str:
        return {
            "SCIENCE": "科学",
            "DIPLOMATIC": "外交",
            "CULTURE": "文化",
            "RELIGIOUS": "宗教",
            "DOMINATION": "征服",
        }.get(self.victory, self.victory)


@dataclass
class ETAHistory:
    """胜利进度的跨回合采样。可序列化,与 PowerHistory 同构。"""

    records: list[dict] = field(default_factory=list)

    def record(self, turn: int, rows: list[dict]) -> None:
        """rows: [{"who":名字, "victory":类型, "progress":0..1}, ...]"""
        if turn <= 0 or not rows:
            return
        self.records = [r for r in self.records if r.get("turn") != turn]
        self.records.append({"turn": int(turn), "rows": list(rows)})
        self.records.sort(key=lambda r: r.get("turn", 0))
        if len(self.records) > MAX_SAMPLES:
            self.records = self.records[-MAX_SAMPLES:]

    def _first_and_last(self, who: str, victory: str):
        pts: list[tuple[int, float]] = []
        pid = -1
        for rec in self.records:
            for row in rec.get("rows") or []:
                if row.get("who") == who and row.get("victory") == victory:
                    pts.append((int(rec["turn"]), float(row.get("progress", 0.0))))
                    pid = int(row.get("pid", -1))
        return (pts[0], pts[-1], pid) if len(pts) >= 2 else None

    def eta(self, who: str, victory: str) -> VictoryETA | None:
        got = self._first_and_last(who, victory)
        if got is None:
            return None
        (t0, p0), (t1, p1), pid = got
        span = t1 - t0
        if span <= 0:
            return None
        rate = (p1 - p0) / span
        if p1 >= 1.0:
            return VictoryETA(who, victory, p1, 0, rate, pid)
        if rate <= 0:
            # 进度停滞 —— 报进度不报 ETA(外推会给出负数或无穷)
            return VictoryETA(who, victory, p1, None, rate, pid)
        turns = (1.0 - p1) / rate
        return VictoryETA(
            who,
            victory,
            p1,
            round(turns) if turns <= MAX_ETA else None,
            rate,
            pid,
        )


def rows_from_progress(vp) -> list[dict]:
    """从 lua.models.VictoryProgress 抽出可外推的进度行。

    只取有**明确分母**的三条:
      科学 = science_vp / science_vp_needed
      外交 = diplomatic_vp / 20(游戏规则的胜利线)
    文化的分母(对手的国内游客数)因人而异且非线性,不做 ETA ——
    强行给一个数字比不给更坏。
    """
    out: list[dict] = []
    for p in getattr(vp, "players", None) or []:
        who = str(getattr(p, "name", "") or "")
        if not who or who == "Unmet":
            continue
        # ⚠️ 不能写 `int(getattr(...) or -1)`:**玩家 0 是假值**,
        # `0 or -1` 得到 -1。而单人局我方恒为 player 0 —— 那样写会让
        # 自我识别永久失效,我方进度会被当成"最快的对手"报出来。
        _raw = getattr(p, "player_id", None)
        pid = int(_raw) if _raw is not None else -1
        need = int(getattr(p, "science_vp_needed", 0) or 0)
        got = int(getattr(p, "science_vp", 0) or 0)
        if need > 0:
            out.append(
                {
                    "who": who,
                    "pid": pid,
                    "victory": "SCIENCE",
                    "progress": min(1.0, got / need),
                }
            )
        dip = int(getattr(p, "diplomatic_vp", 0) or 0)
        if dip > 0:
            out.append(
                {
                    "who": who,
                    "pid": pid,
                    "victory": "DIPLOMATIC",
                    "progress": min(1.0, dip / 20.0),
                }
            )
    return out


def compare(
    my_eta: int | None,
    my_victory: str,
    rivals: list[VictoryETA],
    my_pid: int = -1,
) -> list[str]:
    """我方 ETA 与最快对手的对比。返回 0-2 行简报文本。

    my_eta 由调用方给(科学路线用 lategame 已经在算的产能剩余 ÷ 产能),
    本模块不重复实现那套里程碑成本 —— 一个数只能有一个权威源。
    """
    # 按 player_id 排除自己(见 VictoryETA.pid 的注释:按名字判会把
    # 自己报成"最快的对手")。拿不到 pid 时宁可全留 —— 多报一行
    # 比漏掉真正逼近胜利的对手安全。
    threats = [
        r for r in rivals if r.eta_turns is not None and (my_pid < 0 or r.pid != my_pid)
    ]
    threats.sort(key=lambda r: r.eta_turns or MAX_ETA)

    if my_eta is None:
        if not threats:
            return []
        first = threats[0]
        return [
            (
                f"【路线 ETA】你的完成时间估不出(缺速率数据);"
                f"最快的对手 {first.who} 约 {first.eta_turns} 回合后"
                f"达成{first.victory_cn}胜利〔外推,非精确〕"
                f"—— 先把自己这条路的速率量出来"
            )
        ]

    my_cn = {
        "SCIENCE": "科学",
        "CULTURE": "文化",
        "DIPLOMATIC": "外交",
        "RELIGIOUS": "宗教",
        "DOMINATION": "征服",
    }.get(my_victory, my_victory or "当前路线")

    if not threats:
        return [f"【路线 ETA】你的{my_cn}胜利约 {my_eta} 回合后达成;对手进度不足以外推"]

    first = threats[0]
    diff = my_eta - (first.eta_turns or MAX_ETA)
    head = (
        f"【路线 ETA】你的{my_cn}胜利约 {my_eta} 回合;"
        f"最快对手 {first.who}({first.victory_cn})约 {first.eta_turns} 回合"
        f"〔外推,非精确〕"
    )
    if diff > MATERIAL_GAP:
        return [
            head,
            (
                f"   ⚠️ 你晚 {diff} 回合 —— 照这个速度赢不了。三个选项:"
                f"①提速(缺口见分数归因)②换道(set_victory_goal)"
                f"③压制他(间谍破坏关键区域/外交孤立/军事打击)。"
                f"沉没成本不是留下的理由"
            ),
        ]
    if diff < -MATERIAL_GAP:
        return [head, f"   你领先 {-diff} 回合 —— 路线成立,守住速率别分心"]
    return [head, f"   差距 {abs(diff)} 回合,在噪音范围内 —— 谁先提速谁赢"]
