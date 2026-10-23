"""任务 AK —— 多局学习:让下一局记得上一局怎么死的。

AI 打 10 局文明 6,每局犯同样的错 —— 因为每局都是第一次玩。debrief.py
已经能在局末产出教训(lessons()),但那份教训躺在上一局的复盘文件里,
下一局的模型根本不会去读。本模块补上闭环的最后一段:

    局末:extract_learning(debrief_log) → LearningRecord → LearningStore.add()
    开局:render_lessons_briefing() → "== 往局教训 ==" 注入 T1-T3 简报

## 数据结构设计理由

LearningRecord 不存"完整复盘"(那是 debrief 的事),只存**下局开局时
值得占三行简报的浓缩物**:结果一句话 + 最致命问题 ≤3 条 + 一行教训。
存多了没人读 —— 这个表的读者是开局时的 LLM,它的注意力预算就几行。

top_issues 的提取阈值刻意与 debrief.lessons() 同源(单城困局/探索崩盘/
高损失/长期红警/自评失实)—— 不是复制逻辑,是同一套判据的**跨局压缩版**:
lessons() 面向本局复盘(讲清前因后果),top_issues 面向下局注入(一句话)。

## 边界诚实声明

· camps_found/destroyed 是**从事实曲线推断的下界**:TurnFact 只记录了
  active_camps(当下未拆数),没有累计发现/拆除计数 —— 用峰值和终值近似,
  低估可能,高估不会。
· overridden_intercepts / unused_modules 由调用方传入(它们的事实来源是
  InterceptLog 与简报引擎,extract 时未必在手)—— 缺省为空,不编。
· 存储在进程外(JSON 落盘,与世界模型同一工作区),跨局天然生效;
  standing_insights(如"法典完成前不能排政策卡")是跨局恒真的规则性教训,
  单独一层,不随局淘汰。

## 集成点(供 wiring 使用,本模块不自己动别的文件)

    局末(game_over_watchdog / 手动复盘):
        rec = learning_store.extract_learning(debrief_log, result="…")
        learning_store.default_store().add(rec)
    开局(end_turn 的叙事引擎在 T≤3 时已接入 render_lessons_briefing)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 渲染时只取最近 N 局 —— 更老的教训要么已内化成 standing insight,要么过时
RENDER_RECENT_GAMES = 3
# 落盘保留的局数上限(淘汰按时间序)
KEEP_GAMES = 10
# 简报段的行数硬上限(往局教训不配挤占开局评估的版面)
BRIEFING_MAX_LINES = 7


def storage_path() -> Path:
    """与 world_model.storage_path 同一套工作区逻辑,子目录「学习」。"""
    ws = os.environ.get("CIV_MCP_WORKSPACE")
    base = Path(ws) if ws else Path.home() / ".civ6-mcp"
    return base / "学习" / "经验.json"


@dataclass
class LearningRecord:
    """一局的浓缩教训。字段说明见模块 docstring。"""

    game_id: str = ""
    civ: str = ""
    difficulty: str = ""
    map_type: str = ""
    result: str = ""  # "失败(T29 侦察兵阵亡)" / "胜利(科技/T210)"
    victory_type: str = ""
    top_issues: list[str] = field(default_factory=list)  # ≤3 条
    overridden_intercepts: list[str] = field(default_factory=list)
    unused_modules: list[str] = field(default_factory=list)
    exploration: float = 0.0
    camps_found: int = 0  # 曲线峰值 —— 推断下界,见 docstring
    camps_destroyed: int = 0  # 峰值 - 终值 —— 同上
    units_lost: int = 0
    turns_in_red: int = 0
    total_turns: int = 0
    lesson: str = ""  # 一行:这局最该被记住的一件事


# ---------------------------------------------------------------------------
# 自动提取
# ---------------------------------------------------------------------------


def extract_learning(
    debrief_log,
    result: str = "",
    map_type: str = "",
    overridden_intercepts: list[str] | None = None,
    unused_modules: list[str] | None = None,
) -> LearningRecord:
    """从 DebriefLog(鸭子类型)提取 LearningRecord。事实不足时字段留空,不编。

    不抛异常:空日志返回空记录(game_id 为空 = 调用方可据此不入库)。
    """
    try:
        return _extract_inner(
            debrief_log,
            result,
            map_type,
            list(overridden_intercepts or []),
            list(unused_modules or []),
        )
    except Exception:
        return LearningRecord()


def _extract_inner(log, result, map_type, overridden, unused) -> LearningRecord:
    facts = list(getattr(log, "facts", None) or [])
    rec = LearningRecord(
        game_id=str(getattr(log, "game_id", "") or ""),
        civ=str(getattr(log, "civ", "") or ""),
        difficulty=str(getattr(log, "difficulty", "") or ""),
        map_type=map_type,
        result=result,
        victory_type=str(getattr(log, "victory_type", "") or ""),
        overridden_intercepts=overridden,
        unused_modules=unused,
    )
    if not facts:
        return rec

    last = facts[-1]
    rec.total_turns = int(getattr(last, "turn", 0) or 0)
    rec.exploration = float(getattr(last, "exploration", 0.0) or 0.0)
    rec.units_lost = sum(len(getattr(f, "units_lost", ()) or ()) for f in facts)
    rec.turns_in_red = sum(1 for f in facts if getattr(f, "threat_level", "") == "RED")
    peak_camps = max((getattr(f, "active_camps", 0) or 0) for f in facts)
    rec.camps_found = peak_camps
    rec.camps_destroyed = max(0, peak_camps - (getattr(last, "active_camps", 0) or 0))

    # —— top_issues:与 debrief.lessons() 同源的判据,压成一句话/条 ——
    issues: list[str] = []
    cities_last = getattr(last, "cities", 0) or 0
    if rec.total_turns >= 50 and cities_last <= 1:
        issues.append(
            f"单城困局:T{rec.total_turns} 仍只有 {cities_last} 座城——"
            f"扩张从未启动,下局 T30 前必须出开拓者"
        )
    # 探索判据(2026-08-12 重写,用户实机指出):此前用绝对百分比阈值
    # (<15%)判「探索崩盘」—— 而 exploration 是 revealed/total,分母随地图
    # 大小变。罗马那局地图 2914 格(历史局的 2.4 倍),T188 的 14.9% 换算成
    # 绝对量 434 格,比小图同期只多不少,却会被判成崩盘。
    #
    # 「探索崩盘」的本质是**探索停止了**(侦察单位死光/被忘在角落),不是
    # 「探明的比例少」—— 大图上你既探不完也不需要探完。改判据为停滞检测:
    # 局末探索率低 **且** 后段曲线几乎不涨,才算崩。一直在涨 = 地图大而已。
    if rec.exploration < 0.35 and rec.total_turns > 20:
        tail = [f for f in facts if getattr(f, "turn", 0) >= rec.total_turns * 0.7]
        if len(tail) >= 2:
            growth = float(getattr(tail[-1], "exploration", 0.0) or 0.0) - float(
                getattr(tail[0], "exploration", 0.0) or 0.0
            )
        else:
            growth = 1.0  # 样本不足,不下断言(宁可漏报)
        # 后 30% 回合里探索涨幅 <2 个百分点 = 探图确实停了
        if growth < 0.02:
            issues.append(
                f"探索停滞:局末 {rec.exploration:.1%},且后 30% 回合几乎零增长——"
                f"探图中断后从未恢复,下局侦察单位的存活优先级提到最高"
            )
    if rec.units_lost >= 5:
        force_hint = (
            "(有 force 记录,先复核这些绕过是否正当)"
            if overridden
            else "(无 force 记录——损失发生在拦截覆盖不到的场景,值得补规则)"
        )
        issues.append(f"高损失:共阵亡 {rec.units_lost} 个单位{force_hint}")
    if rec.total_turns > 0 and rec.turns_in_red > rec.total_turns * 0.4:
        issues.append(
            f"长期红警:{rec.turns_in_red}/{rec.total_turns} 回合处于红色——"
            f"根源(营地/敌国)一直没被拔掉,只是在补窟窿"
        )
    try:
        cons = log.contradictions()
    except Exception:
        cons = []
    if cons:
        issues.append(
            f"自评失实 {len(cons)} 次:模型的「感觉安全」被事实推翻——"
            f"判断必须以查询数据为准,不以印象为准"
        )
    rec.top_issues = issues[:3]

    # —— lesson:结果 + 第一致命问题,拼成一句可操作的话 ——
    if rec.top_issues:
        rec.lesson = f"{result + ':' if result else ''}{rec.top_issues[0]}"
    elif result:
        rec.lesson = result
    return rec


# ---------------------------------------------------------------------------
# 落盘存取
# ---------------------------------------------------------------------------


@dataclass
class LearningStore:
    """全部往局记录 + 跨局恒真的规则性教训。"""

    records: list[LearningRecord] = field(default_factory=list)
    standing_insights: list[str] = field(default_factory=list)
    # 墓碑:凡入过库的 game_id。没有它,被 KEEP_GAMES 淘汰的局会被
    # harvest_abandoned 当孤儿反复重收,把新记录挤掉。
    swept_ids: list[str] = field(default_factory=list)

    def add(self, rec: LearningRecord) -> bool:
        """入库(同 game_id 覆盖,防重复复盘写两份)。空记录拒收。"""
        if not isinstance(rec, LearningRecord) or not rec.game_id:
            return False
        self.records = [r for r in self.records if r.game_id != rec.game_id]
        self.records.append(rec)
        self.records = self.records[-KEEP_GAMES:]
        if rec.game_id not in self.swept_ids:
            self.swept_ids.append(rec.game_id)
            self.swept_ids = self.swept_ids[-200:]
        return True

    def add_insight(self, text: str) -> bool:
        """规则性教训(如「法典完成前不能排政策卡——原版规则集特性」)。去重。"""
        text = (text or "").strip()
        if not text or text in self.standing_insights:
            return False
        self.standing_insights.append(text)
        return True

    def recent(self, n: int = RENDER_RECENT_GAMES) -> list[LearningRecord]:
        return self.records[-n:][::-1]  # 最近的在前

    # -- 持久化 --------------------------------------------------------------

    def save(self, path: Path | None = None) -> None:
        p = path or storage_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {
                        "records": [asdict(r) for r in self.records],
                        "standing_insights": self.standing_insights,
                        "swept_ids": self.swept_ids,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 落盘失败不阻断对局

    @classmethod
    def load(cls, path: Path | None = None) -> "LearningStore":
        p = path or storage_path()
        store = cls()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return store
        for raw in d.get("records", []):
            try:
                store.records.append(LearningRecord(**raw))
            except TypeError:
                continue  # 字段演化后的旧记录,跳过
        store.standing_insights = [
            s for s in d.get("standing_insights", []) if isinstance(s, str)
        ]
        store.swept_ids = [s for s in d.get("swept_ids", []) if isinstance(s, str)]
        # 旧版文件无墓碑字段:已入库的记录本身就是墓碑
        for r in store.records:
            if r.game_id and r.game_id not in store.swept_ids:
                store.swept_ids.append(r.game_id)
        return store


def default_store() -> LearningStore:
    return LearningStore.load()


# ---------------------------------------------------------------------------
# 闭环入口(2026-08-09 补):此前 extract_learning→add() 无任何调用方,
# 教训永远不入库,开局注入的「往局教训」恒为空 —— 整条跨局记忆是断的。
# ---------------------------------------------------------------------------


def _debrief_dir() -> Path:
    """复盘日志目录 —— 与 end_turn._record_debrief 同一套路径逻辑。"""
    from civ_mcp import world_model

    return world_model.storage_path("_", 0).parent.parent / "复盘"


def _debrief_path(civ: str, seed: int) -> Path:
    return _debrief_dir() / f"{civ}_{seed}.json"


def harvest_game_over(
    civ: str, seed: int, result: str = "", map_type: str = ""
) -> bool:
    """终局自动收割:磁盘复盘日志 → LearningRecord → 入库落盘。

    幂等(add 按 game_id 覆盖),可在 check_game_over 每次命中时安全重调。
    已有记录里 LLM 手写的 lesson 优先于自动提取 —— 自动收割在终局检测
    循环里可能晚于手动记录触发,不能把人写的一句话冲掉。
    """
    try:
        from civ_mcp import debrief

        jp = _debrief_path(civ, seed)
        if not jp.exists():
            return False
        log_obj = debrief.DebriefLog.load(jp)
        rec = extract_learning(log_obj, result=result, map_type=map_type)
        if not rec.game_id:
            return False
        store = default_store()
        prev = next((r for r in store.records if r.game_id == rec.game_id), None)
        if prev is not None and prev.lesson and prev.lesson != rec.lesson:
            rec.lesson = prev.lesson  # 手写教训优先
        if not store.add(rec):
            return False
        store.save()
        return True
    except Exception:
        return False


def harvest_abandoned(exclude_game_id: str = "") -> int:
    """孤儿复盘日志扫描:落了盘却从未入库的局,按弃局收割。返回收割数。

    check_game_over 只覆盖打到正式终局的局,而校准局大多中途弃局/换局
    结束 —— 那些局的复盘日志躺在磁盘上,教训一样丢。本函数在
    get_game_identity 每次确定新身份时跑一遍:当前局排除(它还活着),
    其余不在库里的都按「未终局」收割。按文件 mtime 升序入库,
    KEEP_GAMES 淘汰时最近的局存活。
    """
    try:
        from civ_mcp import debrief

        d = _debrief_dir()
        if not d.is_dir():
            return 0
        store = default_store()
        known = {r.game_id for r in store.records} | set(store.swept_ids)
        harvested = 0
        for jp in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime):
            gid = jp.stem
            if gid == exclude_game_id or gid in known:
                continue
            rec = extract_learning(
                debrief.DebriefLog.load(jp), result="未终局(中途弃局/换局)"
            )
            if rec.game_id and store.add(rec):
                harvested += 1
        if harvested:
            store.save()
        return harvested
    except Exception:
        return 0


def record_manual_lesson(
    civ: str,
    seed: int,
    lesson: str,
    result: str = "",
    map_type: str = "",
    standing_insight: str = "",
) -> str:
    """LLM 局末手动复盘入口:自动提取打底,手写 lesson 覆盖其上。

    返回给模型看的确认文本(写了什么、存在哪),失败时如实说明。
    """
    from civ_mcp import debrief

    store = default_store()
    wrote: list[str] = []

    jp = _debrief_path(civ, seed)
    if jp.exists():
        log_obj = debrief.DebriefLog.load(jp)
        rec = extract_learning(log_obj, result=result, map_type=map_type)
        # 复盘日志损坏/为空时 extract 返回空记录 —— 手写教训不能因此丢失
        if not rec.game_id:
            rec.game_id, rec.civ, rec.result = f"{civ}_{seed}", civ, result
    else:
        rec = LearningRecord(game_id=f"{civ}_{seed}", civ=civ, result=result)
    if lesson.strip():
        rec.lesson = lesson.strip()
    if rec.game_id and store.add(rec):
        wrote.append(f"本局教训已入库:{rec.lesson or rec.result or '(仅事实字段)'}")

    if standing_insight.strip():
        if store.add_insight(standing_insight.strip()):
            wrote.append(f"跨局规则已入库:{standing_insight.strip()}")
        else:
            wrote.append("跨局规则已存在,未重复入库")

    if not wrote:
        return "没有可入库的内容 —— lesson 与 standing_insight 都为空。"
    store.save()
    wrote.append(f"(存储:{storage_path()};下局 T1-T3 简报会自动注入)")
    return "\n".join(wrote)


def render_full_history(store: LearningStore | None = None) -> str:
    """完整往局记录视图 —— 供 get_learnings 工具用,不受简报 7 行预算限制。"""
    st = store if store is not None else default_store()
    if not st.records and not st.standing_insights:
        return "学习库为空 —— 还没有任何一局的教训入库。"
    lines: list[str] = ["== 往局学习库 =="]
    for r in st.records[::-1]:
        who = "/".join(x for x in (r.civ, r.difficulty, r.map_type) if x)
        head = f"· {r.game_id or '?'}" + (f"({who})" if who else "")
        lines.append(f"{head} — {r.result or '结果未记录'},共 {r.total_turns} 回合")
        if r.lesson:
            lines.append(f"  教训:{r.lesson}")
        for iss in r.top_issues:
            lines.append(f"  问题:{iss}")
    if st.standing_insights:
        lines.append("-- 跨局恒真规则 --")
        lines.extend(f"· {s}" for s in st.standing_insights)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 开局注入段
# ---------------------------------------------------------------------------


def render_lessons_briefing(store: LearningStore | None = None) -> str:
    """「== 往局教训 ==」段。没有可注入的教训返回空串,不抛异常。

    只取最近 3 局 + 至多 2 条规则性教训,总行数 ≤7 —— 教训是调味料,
    不是开局评估的替代品。
    """
    try:
        st = store if store is not None else default_store()
        lines: list[str] = []
        for r in st.recent():
            who = "/".join(x for x in (r.civ, r.difficulty) if x)
            head = f"· 上局({who})" if who else "· 上局"
            body = r.lesson or r.result or (r.top_issues[0] if r.top_issues else "")
            if not body:
                continue
            lines.append(f"{head}:{body}")
        for s in st.standing_insights[-2:]:
            lines.append(f"· [规则] {s}")
        if not lines:
            return ""
        return "== 往局教训 ==\n" + "\n".join(lines[: BRIEFING_MAX_LINES - 1])
    except Exception:
        return ""
