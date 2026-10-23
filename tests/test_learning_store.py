"""任务 AK 多局学习 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import debrief
from civ_mcp import learning_store as ls


def _log(
    turns=60,
    cities=1,
    exploration=0.03,
    camps=2,
    red_from=15,
    lost_at=(19, 27, 45, 46, 50),
):
    log = debrief.DebriefLog(
        game_id="rome_-180",
        civ="罗马",
        difficulty="DIFFICULTY_KING",
        victory_type="SCIENCE",
    )
    for t in range(1, turns + 1):
        log.record(
            debrief.TurnFact(
                turn=t,
                cities=cities,
                exploration=exploration,
                active_camps=camps if t > 10 else 0,
                threat_level="RED" if t >= red_from else "GREEN",
                units_lost=["UNIT_BUILDER"] if t in lost_at else [],
            )
        )
    return log


# --- 提取 ---


def test_extract_captures_identity_and_curves():
    rec = ls.extract_learning(_log(), result="T60 手动停止", map_type="PANGAEA")
    assert rec.game_id == "rome_-180" and rec.civ == "罗马"
    assert rec.result == "T60 手动停止" and rec.map_type == "PANGAEA"
    assert rec.total_turns == 60
    assert rec.units_lost == 5
    assert rec.turns_in_red == 46
    assert rec.camps_found == 2


def test_camps_destroyed_is_peak_minus_final():
    log = debrief.DebriefLog(game_id="g")
    for t, camps in ((1, 0), (10, 3), (20, 1)):
        log.record(debrief.TurnFact(turn=t, active_camps=camps))
    rec = ls.extract_learning(log)
    assert rec.camps_found == 3 and rec.camps_destroyed == 2


def test_top_issues_cover_the_t52_failure_pattern():
    """单城+低探索+高损失+长红警 —— 老失败局的四大症状都要被点名。"""
    rec = ls.extract_learning(_log())
    text = " ".join(rec.top_issues)
    assert len(rec.top_issues) == 3  # 硬上限 3 条
    assert "单城困局" in text
    assert "探索崩盘" in text or "高损失" in text


def test_t29_short_game_still_extracts_exploration_lesson():
    """T29/探索 2.5% 的真实失败局:局短也必须抽出探索停滞——那就是死因。"""
    rec = ls.extract_learning(
        _log(turns=29, red_from=15, lost_at=(29,)), result="T29 侦察兵阵亡停止"
    )
    assert any("探索停滞" in i for i in rec.top_issues)
    assert "探索停滞" in rec.lesson  # 且是第一教训,不被红警挤掉


def _growing_log(turns=384, end_pct=0.30):
    """探索一直在涨的大地图局(罗马 T384 的形状)。"""
    log = debrief.DebriefLog(game_id="rome_big", civ="罗马")
    for t in range(1, turns + 1):
        log.record(
            debrief.TurnFact(
                turn=t, cities=1 + t // 20, exploration=end_pct * t / turns
            )
        )
    return log


def test_big_map_low_percentage_is_not_a_collapse():
    """2026-08-12 用户实机指出:大地图上探索率天然低,曲线在涨就不是崩盘。

    罗马那局 2914 格(历史局 2.4 倍),T188 的 14.9% = 434 格绝对量并不少。
    旧的 <15% 硬阈值会把正常的大图探索误报成崩盘。
    """
    rec = ls.extract_learning(_growing_log(), result="T384 进行中")
    assert not any("探索" in i for i in rec.top_issues)


def test_stalled_exploration_still_caught_on_big_map():
    """反向:大地图上也可能真停滞 —— 前期探完就再没动过,照样要报。"""
    log = debrief.DebriefLog(game_id="stalled", civ="罗马")
    for t in range(1, 201):
        # 前 40 回合涨到 12%,之后彻底不动
        pct = min(0.12, 0.12 * t / 40)
        log.record(debrief.TurnFact(turn=t, cities=4, exploration=pct))
    rec = ls.extract_learning(log)
    assert any("探索停滞" in i for i in rec.top_issues)


def test_contradiction_issue_when_selfjudgement_fails():
    log = _log()
    log.add_judgement(28, "蛮族威胁已解除,转入发展期")
    rec = ls.extract_learning(log)
    assert any("自评失实" in i for i in rec.top_issues) or len(rec.top_issues) == 3


def test_lesson_is_one_actionable_line():
    rec = ls.extract_learning(_log(), result="T60 停止")
    assert rec.lesson.startswith("T60 停止:")
    assert "\n" not in rec.lesson


def test_healthy_game_extracts_no_issues():
    log = debrief.DebriefLog(game_id="ok", civ="德国")
    for t in range(1, 101):
        log.record(
            debrief.TurnFact(turn=t, cities=1 + t // 15, exploration=min(0.6, t / 120))
        )
    rec = ls.extract_learning(log, result="胜利(科技/T210)")
    assert rec.top_issues == []
    assert rec.lesson == "胜利(科技/T210)"


def test_extract_empty_and_garbage_safe():
    assert ls.extract_learning(debrief.DebriefLog()).game_id == ""
    assert ls.extract_learning(None).game_id == ""


def test_force_hint_differs_with_overrides():
    with_force = ls.extract_learning(_log(), overridden_intercepts=["T27 CIVILIAN"])
    without = ls.extract_learning(_log())
    assert any("force" in i for i in with_force.top_issues)
    assert any("拦截覆盖不到" in i for i in without.top_issues)


# --- 存取与淘汰 ---


def test_store_roundtrip(tmp_path):
    p = tmp_path / "经验.json"
    store = ls.LearningStore()
    assert store.add(ls.extract_learning(_log(), result="T60 停止"))
    assert store.add_insight("法典完成前不能排政策卡——原版规则集特性")
    store.save(p)
    back = ls.LearningStore.load(p)
    assert back.records[0].civ == "罗马"
    assert back.standing_insights == ["法典完成前不能排政策卡——原版规则集特性"]


def test_store_dedupes_by_game_id_and_prunes():
    store = ls.LearningStore()
    for i in range(15):
        store.add(ls.LearningRecord(game_id=f"g{i}", lesson=f"L{i}"))
    store.add(ls.LearningRecord(game_id="g14", lesson="覆盖"))
    assert len(store.records) == ls.KEEP_GAMES
    assert store.records[-1].lesson == "覆盖"
    assert not store.add(ls.LearningRecord())  # 空记录拒收
    assert not store.add_insight("")  # 空洞见拒收
    store.add_insight("X")
    assert not store.add_insight("X")  # 去重


def test_store_load_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    assert ls.LearningStore.load(p).records == []


# --- 注入渲染 ---


def test_briefing_renders_recent_three_newest_first():
    store = ls.LearningStore()
    for i in range(5):
        store.add(
            ls.LearningRecord(
                game_id=f"g{i}", civ=f"文明{i}", difficulty="王子", lesson=f"教训{i}"
            )
        )
    text = ls.render_lessons_briefing(store)
    assert text.startswith("== 往局教训 ==")
    assert "教训4" in text and "教训2" in text
    assert "教训1" not in text  # 只取最近 3 局
    assert text.index("教训4") < text.index("教训3")  # 最近的在前


def test_briefing_includes_standing_insights_and_caps_lines():
    store = ls.LearningStore()
    for i in range(3):
        store.add(ls.LearningRecord(game_id=f"g{i}", lesson=f"教训{i}"))
    for i in range(4):
        store.add_insight(f"规则{i}")
    text = ls.render_lessons_briefing(store)
    assert "[规则] 规则3" in text and "[规则] 规则2" in text
    assert "规则0" not in text  # 只取最后 2 条
    assert len(text.split("\n")) <= ls.BRIEFING_MAX_LINES


def test_briefing_empty_store_is_silent():
    assert ls.render_lessons_briefing(ls.LearningStore()) == ""


def test_debrief_facade_delegates():
    """debrief.extract_learning 是 AK 的门面 —— 委托给 learning_store。"""
    rec = debrief.extract_learning(_log(), result="T60 停止")
    assert isinstance(rec, ls.LearningRecord)
    assert rec.civ == "罗马"


# --- 闭环入口(2026-08-09 补):终局自动收割 + 手动复盘 ---


def _isolate_workspace(monkeypatch, tmp_path):
    """把学习库与复盘日志都指到 tmp —— 两者共用 CIV_MCP_WORKSPACE。"""
    monkeypatch.setenv("CIV_MCP_WORKSPACE", str(tmp_path))


def _write_debrief(civ="罗马", seed=-180):
    """把 _log() 落盘到 harvest 会去找的位置。"""
    jp = ls._debrief_path(civ, seed)
    log = _log()
    log.game_id = f"{civ}_{seed}"
    log.save(jp)
    return jp


def test_harvest_game_over_closes_the_loop(monkeypatch, tmp_path):
    """终局收割后,下局简报注入不再为空 —— 这就是此前断掉的那一段。"""
    _isolate_workspace(monkeypatch, tmp_path)
    _write_debrief()
    assert ls.harvest_game_over("罗马", -180, result="失败(美国以Culture取胜)")
    store = ls.default_store()
    assert len(store.records) == 1
    assert store.records[0].result == "失败(美国以Culture取胜)"
    assert "失败" in ls.render_lessons_briefing(store)


def test_harvest_without_debrief_file_returns_false(monkeypatch, tmp_path):
    _isolate_workspace(monkeypatch, tmp_path)
    assert not ls.harvest_game_over("没这局", 42)


def test_harvest_preserves_manual_lesson(monkeypatch, tmp_path):
    """手写教训优先:自动收割晚于 record_learning 触发时不能冲掉人写的话。"""
    _isolate_workspace(monkeypatch, tmp_path)
    _write_debrief()
    ls.record_manual_lesson("罗马", -180, lesson="文化线必须每 10 回合查一次")
    assert ls.harvest_game_over("罗马", -180, result="失败(文化)")
    store = ls.default_store()
    assert store.records[0].lesson == "文化线必须每 10 回合查一次"
    assert store.records[0].result == "失败(文化)"  # 事实字段仍被自动补全


def test_record_manual_lesson_with_insight(monkeypatch, tmp_path):
    _isolate_workspace(monkeypatch, tmp_path)
    _write_debrief()
    out = ls.record_manual_lesson(
        "罗马",
        -180,
        lesson="别再单城",
        standing_insight="原版规则集下法典完成前不能排政策卡",
    )
    assert "本局教训已入库" in out and "跨局规则已入库" in out
    store = ls.default_store()
    assert store.records[0].lesson == "别再单城"
    assert store.records[0].units_lost == 5  # 事实来自复盘日志,不用手报
    assert store.standing_insights == ["原版规则集下法典完成前不能排政策卡"]


def test_record_manual_lesson_survives_missing_debrief(monkeypatch, tmp_path):
    """复盘日志缺失时手写教训也不能丢 —— 降级为纯手写记录。"""
    _isolate_workspace(monkeypatch, tmp_path)
    out = ls.record_manual_lesson("希腊", 7, lesson="教训X", result="T100 停止")
    assert "本局教训已入库" in out
    rec = ls.default_store().records[0]
    assert rec.game_id == "希腊_7" and rec.lesson == "教训X"


def test_render_full_history_shows_everything(monkeypatch, tmp_path):
    _isolate_workspace(monkeypatch, tmp_path)
    store = ls.LearningStore()
    for i in range(5):
        store.add(ls.LearningRecord(game_id=f"g{i}", lesson=f"教训{i}", result="失败"))
    store.add_insight("规则A")
    text = ls.render_full_history(store)
    assert "教训0" in text and "教训4" in text  # 不受简报 3 局限制
    assert "规则A" in text


def test_harvest_abandoned_sweeps_orphans_but_not_current(monkeypatch, tmp_path):
    """弃局的复盘日志被收割为「未终局」;当前局排除。"""
    _isolate_workspace(monkeypatch, tmp_path)
    _write_debrief("罗马", -180)  # 已弃的旧局
    _write_debrief("希腊", 7)  # 当前局
    assert ls.harvest_abandoned(exclude_game_id="希腊_7") == 1
    store = ls.default_store()
    assert [r.game_id for r in store.records] == ["罗马_-180"]
    assert "未终局" in store.records[0].result


def test_harvest_abandoned_idempotent(monkeypatch, tmp_path):
    _isolate_workspace(monkeypatch, tmp_path)
    _write_debrief()
    assert ls.harvest_abandoned() == 1
    assert ls.harvest_abandoned() == 0  # 第二次扫不重复入库


def test_harvest_abandoned_tombstone_survives_pruning(monkeypatch, tmp_path):
    """被 KEEP_GAMES 淘汰的局不能被当孤儿重收 —— 墓碑必须比记录长寿。"""
    _isolate_workspace(monkeypatch, tmp_path)
    _write_debrief()
    assert ls.harvest_abandoned() == 1
    store = ls.default_store()
    for i in range(ls.KEEP_GAMES):  # 挤掉罗马那条记录
        store.add(ls.LearningRecord(game_id=f"新局{i}", lesson=f"L{i}"))
    store.save()
    assert all(r.game_id != "罗马_-180" for r in ls.default_store().records)
    assert ls.harvest_abandoned() == 0  # 墓碑仍在,不重收


def test_swept_ids_backfilled_from_old_format(tmp_path):
    """旧版存档无墓碑字段:已入库记录本身回填为墓碑。"""
    p = tmp_path / "旧.json"
    p.write_text(
        '{"records": [{"game_id": "老局_1"}], "standing_insights": []}',
        encoding="utf-8",
    )
    assert "老局_1" in ls.LearningStore.load(p).swept_ids


def test_render_full_history_empty(monkeypatch, tmp_path):
    _isolate_workspace(monkeypatch, tmp_path)
    assert "学习库为空" in ls.render_full_history(ls.LearningStore())
