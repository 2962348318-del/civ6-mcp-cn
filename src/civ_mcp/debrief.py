"""L4 复盘自动化 —— 把"这局为什么输/赢"从人工整理变成自动产出。

起因:用户手工整理了那份 52 回合复盘(8 个死亡、0 营地清除、产出曲线),
才让整个诊断有了实证基础。但手工整理一次要很久,而且下一局还得再来一遍。

这一层做两件事:
    1. 每回合把【事实】自动记进工作区(服务端写,模型改不了)
    2. 局末汇总成复盘文档,并提炼可跨局复用的教训

关键设计 —— 事实/判断/校验三段式(方案 §5):
    模型总结自己的状态 = 给自己发合格证。复盘里 T28 那句"蛮族威胁已解除"
    就是这么来的,而且如果写进文件,下一个十回合的模型读到它会接着错。
    所以事实由服务端写,判断由模型写,两者并排摆着,冲突时自动打脸。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 复盘口径的「够得着的营地」半径。比 world_model.CAMP_NEAR_CAPITAL(6,
# 「再不拆就要死单位」的告警线)宽一档:教训问的是「整局有没有被营地
# 持续拖累」,不是「此刻危不危险」。
DEBRIEF_CAMP_RADIUS = 12


@dataclass
class TurnFact:
    """一个回合的客观事实。全部来自服务端查询,模型无法修改。"""

    turn: int
    cities: int = 0
    military: int = 0
    exploration: float = 0.0
    active_camps: int = 0
    # 首都 DEBRIEF_CAMP_RADIUS 格内的活跃营地。2026-08-12 罗马 T384 实机:
    # 卫星揭图那一回合 active_camps 从 1 跳到 17,复盘据此教训「17 个营地
    # 未拆」—— 而那些营地大多是揭图当回合才看见、距首都 66 格,产能根本
    # 耗不到我身上。指标在信息量突变时语义漂移(从「我知道的营地」变成
    # 「全图营地」),判据必须看得见距离才不会误报。-1 = 老日志无此数据。
    camps_near: int = -1
    threat_level: str = "GREEN"
    units_lost: list[str] = field(default_factory=list)  # 本回合损失的单位类型
    gold: int = 0
    science: int = 0
    culture: int = 0
    production: int = 0
    techs_done: int = 0
    civics_done: int = 0


@dataclass
class Judgement:
    """模型自己写的判断。会被事实校验。"""

    turn: int
    text: str


@dataclass
class DebriefLog:
    """一局的完整记录。"""

    game_id: str = ""
    civ: str = ""
    difficulty: str = ""
    victory_type: str = ""
    facts: list[TurnFact] = field(default_factory=list)
    judgements: list[Judgement] = field(default_factory=list)

    def record(self, fact: TurnFact) -> None:
        # 同回合重复写入时覆盖(end_turn 可能被重试)
        self.facts = [f for f in self.facts if f.turn != fact.turn]
        self.facts.append(fact)
        self.facts.sort(key=lambda f: f.turn)

    def add_judgement(self, turn: int, text: str) -> None:
        self.judgements.append(Judgement(turn, text))

    # -- 校验:用事实打判断的脸 -------------------------------------------

    def contradictions(self, window: int = 10) -> list[str]:
        """找出与事实冲突的模型判断。

        只做几条高置信度的检查 —— 宁可漏报,不能误报:
        误报会让模型不信任这个机制,那就白做了。
        """
        out: list[str] = []
        if not self.facts:
            return out

        for j in self.judgements:
            t = j.text
            # "威胁解除/全清" vs 营地仍在
            if any(k in t for k in ("威胁已解除", "蛮族全清", "威胁解除", "已清空")):
                after = [f for f in self.facts if j.turn <= f.turn <= j.turn + window]
                still = [f for f in after if f.active_camps > 0]
                if still:
                    out.append(
                        f"T{j.turn} 判断「{t[:24]}」与事实冲突:"
                        f"T{still[-1].turn} 时仍有 {still[-1].active_camps} 个已知营地未拆除"
                    )
                relapse = [f for f in after if f.threat_level == "RED"]
                if relapse:
                    out.append(
                        f"T{j.turn} 判断「{t[:24]}」与事实冲突:"
                        f"T{relapse[0].turn} 威胁等级重新变红"
                    )
            # "进入发展期" vs 之后仍在损失单位
            if any(k in t for k in ("转入发展", "进入发展期", "开始种田")):
                after = [f for f in self.facts if j.turn < f.turn <= j.turn + window]
                lost = sum(len(f.units_lost) for f in after)
                if lost >= 2:
                    out.append(
                        f"T{j.turn} 判断「{t[:24]}」与事实冲突:"
                        f"之后 {window} 回合又损失了 {lost} 个单位"
                    )
        return out

    # -- 汇总 ---------------------------------------------------------------

    def summary(self) -> dict:
        if not self.facts:
            return {}
        first, last = self.facts[0], self.facts[-1]
        all_lost: list[str] = []
        for f in self.facts:
            all_lost.extend(f.units_lost)
        red_turns = [f.turn for f in self.facts if f.threat_level == "RED"]
        return {
            "回合数": f"{first.turn} → {last.turn}",
            "城市数": f"{first.cities} → {last.cities}",
            "损失单位": len(all_lost),
            "损失明细": all_lost,
            "最终探索度": f"{last.exploration:.1%}",
            "未拆营地": last.active_camps,
            "其中首都附近": (last.camps_near if last.camps_near >= 0 else "—"),
            "红色警报回合数": len(red_turns),
            "红色占比": f"{len(red_turns) / max(1, len(self.facts)):.0%}",
        }

    def _exploration_stalled(self) -> bool:
        """后 30% 回合里探索涨幅 <2 个百分点 = 探图确实停了。

        与 learning_store 的同名判据同源(那边是跨局压缩版)。样本不足时
        返回 False —— 宁可漏报,不误报。
        """
        if len(self.facts) < 4:
            return False
        cutoff = self.facts[-1].turn * 0.7
        tail = [f for f in self.facts if f.turn >= cutoff]
        if len(tail) < 2:
            return False
        return (tail[-1].exploration - tail[0].exploration) < 0.02

    def lessons(self) -> list[str]:
        """从事实里提炼可跨局复用的教训。只在证据足够时给。"""
        out: list[str] = []
        s = self.summary()
        if not s:
            return out
        last = self.facts[-1]
        total_lost = s["损失单位"]

        # 探索:低比例本身不是问题(大地图上天然低),探图**停了**才是。
        # 2026-08-12 用户实机指出:罗马那局 2914 格图,14.9% 的绝对量
        # 比小图同期还多 —— 阈值判据在大地图上系统性误报。
        if last.exploration < 0.35 and self._exploration_stalled():
            out.append(
                f"探索停滞:局末 {last.exploration:.1%},且后段几乎零增长 —— "
                f"探图中断会连锁导致「找不到营地→拆不了→无限被咬」。"
                f"下局早期必须保证探图不中断。"
            )
        # 营地:只算够得着的那些(见 TurnFact.camps_near 的实机说明)。
        # 老日志无此字段(-1)时降级为如实声明,不冒充精确。
        if last.camps_near > 0:
            out.append(
                f"局末仍有 {last.camps_near} 个营地在首都 "
                f"{DEBRIEF_CAMP_RADIUS} 格内未拆除 —— "
                f"营地不拆,出兵不停,产能会一直耗在补兵上。"
            )
        elif last.camps_near < 0 and last.active_camps > 0:
            out.append(
                f"局末有 {last.active_camps} 个已知营地未拆除(旧日志无距离"
                f"数据,其中可能含刚探明的远方营地 —— 只有够得着的才真的耗你)。"
            )
        if total_lost >= 5:
            out.append(
                f"共损失 {total_lost} 个单位。检查其中平民占比 —— "
                f"平民必须同格护卫,这条已在工具层拦截,若仍有损失说明用了 force。"
            )
        red_ratio = len([f for f in self.facts if f.threat_level == "RED"]) / max(
            1, len(self.facts)
        )
        if red_ratio > 0.4:
            out.append(
                f"{red_ratio:.0%} 的回合处于红色警报 —— 长期高威胁说明"
                f"根源问题(营地/敌国)一直没被解决,只是在补窟窿。"
            )
        if last.cities <= 1 and last.turn >= 50:
            out.append(
                f"T{last.turn} 仍只有 {last.cities} 座城 —— "
                f"城市数是所有产出的乘数,单城几乎不可能赢。"
            )
        return out

    # -- 渲染与持久化 --------------------------------------------------------

    def to_markdown(self) -> str:
        s = self.summary()
        lines = [
            f"# 对局复盘 · {self.civ or '未知文明'}",
            "",
            f"> 难度:{self.difficulty or '未记录'} ‖ "
            f"胜利路线:{self.victory_type or '未锁定'}",
            "",
            "## 概况",
            "",
            "| 项 | 值 |",
            "|---|---|",
        ]
        for k, v in s.items():
            if k == "损失明细":
                continue
            lines.append(f"| {k} | {v} |")

        if s.get("损失明细"):
            lines += ["", "## 单位损失", ""]
            for f in self.facts:
                for u in f.units_lost:
                    lines.append(f"- T{f.turn} {u}")

        con = self.contradictions()
        if con:
            lines += [
                "",
                "## ⚠️ 判断与事实冲突",
                "",
                "> 这些是模型当时的判断被后续事实推翻的地方 —— "
                "自我评估最容易出错的位置。",
                "",
            ]
            for c in con:
                lines.append(f"- {c}")

        les = self.lessons()
        if les:
            lines += ["", "## 教训(可带入下一局)", ""]
            for l in les:
                lines.append(f"- {l}")

        if self.facts:
            lines += [
                "",
                "## 曲线",
                "",
                "| 回合 | 城市 | 军队 | 探索 | 营地 | 威胁 | 产能 | 科技 |",
                "|---|---|---|---|---|---|---|---|",
            ]
            step = max(1, len(self.facts) // 20)
            for f in self.facts[::step]:
                lines.append(
                    f"| {f.turn} | {f.cities} | {f.military} | "
                    f"{f.exploration:.1%} | {f.active_camps} | "
                    f"{f.threat_level} | {f.production} | {f.science} |"
                )
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "game_id": self.game_id,
                "civ": self.civ,
                "difficulty": self.difficulty,
                "victory_type": self.victory_type,
                "facts": [asdict(f) for f in self.facts],
                "judgements": [asdict(j) for j in self.judgements],
            },
            ensure_ascii=False,
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> DebriefLog:
        try:
            d = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return cls()
        log = cls(
            game_id=d.get("game_id", ""),
            civ=d.get("civ", ""),
            difficulty=d.get("difficulty", ""),
            victory_type=d.get("victory_type", ""),
        )
        for f in d.get("facts", []):
            log.facts.append(TurnFact(**f))
        for j in d.get("judgements", []):
            log.judgements.append(Judgement(**j))
        return log

    def save(self, json_path: Path, md_path: Path | None = None) -> None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(self.to_json(), encoding="utf-8")
        if md_path:
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(self.to_markdown(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> DebriefLog:
        if not path.exists():
            return cls()
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except OSError:
            return cls()


# ===========================================================================
# 任务 H:反事实复盘(追加,上方已有 class 与函数签名一概未动)
#
# 改造方案 §8.3:每次拦截触发时记下「如果没拦会怎样」,局末给出
# 「本局拦截避免了 N 次损失」的量化收益 —— 也能反过来发现哪些拦截是误报。
#
# 设计边界:
#   · **不改 safety.py**。Verdict 已携带 code 与 message(message 里本来就
#     写着推演依据,如「蛮族 CS20 骑手在 (12,34) 可打到该格」)—— 反事实
#     就是这句话,不需要 safety 层多说一个字。记录动作由调用方
#     (game_state.py 的拦截返回处)接线,拦截层保持纯判定。
#   · 「避免 1 次拦截 ≈ 避免 1 个单位死亡」是**估算口径**不是事实——
#     没拦的世界线没人见过。文案里写"约",高 force 率反向标注为疑似误报。
#   · 独立落盘(与 DebriefLog 并排的 *.intercepts.json),不动 DebriefLog
#     的序列化格式 —— 旧复盘文件零迁移。
# ===========================================================================

# force 率超过这个值的拦截码,判定为"疑似误报,需人工复核阈值"
FORCE_SUSPECT_RATIO = 0.5

# 拦截码 → (人话名, 每次拦截避免了什么)
_INTERCEPT_MEANING: dict[str, tuple[str, str]] = {
    "CIVILIAN_UNESCORTED": ("平民裸奔拦截", "约 1 个平民单位死亡"),
    "ESCORT_TOO_WEAK": ("护卫不足拦截", "1 次「有护卫但挡不住」的虚假安全感"),
    "RANGED_ALREADY_MOVED": ("远程顺序拦截", "1 次白扔的攻击回合"),
    "UNIT_STALE": ("单位 ID 过期拦截", "1 次打错单位的静默事故"),
    "RED_NO_CIVILIAN_PRODUCTION": ("红警产平民拦截", "1 个注定被抓的平民"),
}


@dataclass
class InterceptRecord:
    """一次 L0 拦截的反事实记录。"""

    turn: int
    code: str  # Verdict.code
    unit_type: str = ""
    location: tuple = ()  # 目标格 (x, y);未知为空
    consequence: str = ""  # 如果没拦会怎样 —— 取自 Verdict.message
    force_used: bool = False  # True = 模型用 force 绕过了这次拦截


@dataclass
class InterceptLog:
    """一局的全部拦截记录。与 DebriefLog 并排落盘,互不纠缠。"""

    game_id: str = ""
    records: list[InterceptRecord] = field(default_factory=list)

    def record(self, rec: InterceptRecord) -> None:
        if isinstance(rec, InterceptRecord) and rec.code:
            self.records.append(rec)

    # -- 汇总 ----------------------------------------------------------------

    def by_code(self) -> dict[str, tuple[int, int]]:
        """code → (触发次数, 其中被 force 绕过次数)。"""
        out: dict[str, list[int]] = {}
        for r in self.records:
            slot = out.setdefault(r.code, [0, 0])
            slot[0] += 1
            if r.force_used:
                slot[1] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}

    def suspects(self) -> list[str]:
        """force 率过半的拦截码 —— 模型持续不信任它,该复核阈值而不是怪模型。"""
        out = []
        for code, (n, forced) in self.by_code().items():
            if n >= 2 and forced / n > FORCE_SUSPECT_RATIO:
                out.append(
                    f"⚠️ {code} 被 force 绕过 {forced}/{n} 次——"
                    f"这条规则可能误报,需复核阈值(也可能模型在冒不该冒的险,"
                    f"对照事实曲线里 force 之后有没有损失)"
                )
        return out

    def render_summary(self) -> str:
        """「== 拦截统计 ==」段。没有记录返回空串。"""
        if not self.records:
            return ""
        lines = ["== 拦截统计 =="]
        held_total = 0
        for code, (n, forced) in sorted(self.by_code().items()):
            cn, avoided = _INTERCEPT_MEANING.get(code, (code, "1 次潜在损失"))
            held = n - forced
            held_total += held
            line = f"· {cn}({code}):触发 {n} 次"
            if held:
                line += f" → 避免了{avoided.replace('1 ', f'{held} ' if held > 1 else '1 ')}"
            if forced:
                line += f"(其中 {forced} 次被 force 绕过)"
            lines.append(line)
        lines.append(
            f"· 合计:约 {held_total} 次损失被挡下(按每次拦截避免一次损失的"
            f"保守口径估算——反事实无法实证,只能对照 force 后的实际损失)"
        )
        lines.extend(self.suspects())
        return "\n".join(lines)

    # -- 持久化(与 DebriefLog 并排,独立文件)------------------------------

    def save(self, json_path: Path) -> None:
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps(
                    {
                        "game_id": self.game_id,
                        "records": [asdict(r) for r in self.records],
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path) -> "InterceptLog":
        log = cls()
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return log
        log.game_id = d.get("game_id", "")
        for raw in d.get("records", []):
            try:
                raw["location"] = tuple(raw.get("location") or ())
                log.records.append(InterceptRecord(**raw))
            except TypeError:
                continue
        return log


def record_intercept(
    log: InterceptLog,
    turn: int,
    verdict,
    unit_type: str = "",
    location: tuple = (),
    force_used: bool = False,
) -> None:
    """从 safety.Verdict(鸭子类型)登记一次拦截。放行的 Verdict 不记录。

    接线位置:game_state.py 调用 safety.check_* 拿到 blocked Verdict 之后
    (含 force=True 时先跑一次判定再放行的路径)。不在 safety 层内做 ——
    拦截层保持纯判定,记录是调用方的事。
    """
    try:
        if not getattr(verdict, "blocked", False) and not force_used:
            return
        log.record(
            InterceptRecord(
                turn=int(turn),
                code=str(getattr(verdict, "code", "") or ""),
                unit_type=str(unit_type or ""),
                location=tuple(location or ()),
                consequence=str(getattr(verdict, "message", "") or ""),
                force_used=bool(force_used),
            )
        )
    except Exception:
        return


def extract_learning(debrief_log: "DebriefLog", **kwargs):
    """任务 AK 的入口代理:委托 learning_store.extract_learning。

    放在这里是为了让「局末复盘」的调用方只面对 debrief 一个门面;
    真正的提取逻辑与 LearningRecord 同住 learning_store.py(内聚)。
    延迟导入避免环形依赖。
    """
    from civ_mcp import learning_store

    return learning_store.extract_learning(debrief_log, **kwargs)
