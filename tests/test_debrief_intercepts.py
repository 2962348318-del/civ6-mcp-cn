"""任务 H 反事实复盘(debrief 追加部分)—— 单元测试。"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import debrief


def _verdict(
    blocked=True,
    code="CIVILIAN_UNESCORTED",
    message="蛮族 CS20 骑手在 (12,34) 可打到该格",
):
    return NS(blocked=blocked, code=code, message=message)


def test_existing_debrief_api_untouched():
    for name in ("TurnFact", "Judgement", "DebriefLog"):
        assert hasattr(debrief, name)
    log = debrief.DebriefLog()
    log.record(debrief.TurnFact(turn=1))
    assert log.summary()  # 旧行为原样


def test_record_intercept_from_verdict():
    log = debrief.InterceptLog(game_id="g")
    debrief.record_intercept(log, 27, _verdict(), "UNIT_BUILDER", (12, 34))
    assert len(log.records) == 1
    r = log.records[0]
    assert r.turn == 27 and r.code == "CIVILIAN_UNESCORTED"
    assert r.consequence == "蛮族 CS20 骑手在 (12,34) 可打到该格"
    assert r.location == (12, 34) and not r.force_used


def test_passing_verdict_not_recorded():
    log = debrief.InterceptLog()
    debrief.record_intercept(log, 5, _verdict(blocked=False))
    assert log.records == []


def test_forced_bypass_recorded_even_if_released():
    """force=True 时判定被绕过(blocked 最终为 False)也要记 —— 这正是要审计的。"""
    log = debrief.InterceptLog()
    debrief.record_intercept(log, 27, _verdict(blocked=False), force_used=True)
    assert len(log.records) == 1 and log.records[0].force_used


def test_summary_counts_and_estimates():
    log = debrief.InterceptLog()
    for t in (5, 9, 11, 14, 20, 22, 30):
        debrief.record_intercept(log, t, _verdict())
    for t in (12, 18):
        debrief.record_intercept(log, t, _verdict(code="RANGED_ALREADY_MOVED"))
    text = log.render_summary()
    assert "触发 7 次" in text and "触发 2 次" in text
    assert "合计:约 9 次损失被挡下" in text
    assert "保守口径估算" in text  # 反事实必须标注为估算


def test_high_force_rate_flags_suspect_rule():
    log = debrief.InterceptLog()
    debrief.record_intercept(log, 5, _verdict())
    for t in (7, 9, 11):
        debrief.record_intercept(log, t, _verdict(blocked=False), force_used=True)
    text = log.render_summary()
    assert "force 绕过 3/4 次" in text
    assert "可能误报" in text


def test_low_force_rate_not_flagged():
    log = debrief.InterceptLog()
    for t in (5, 7, 9):
        debrief.record_intercept(log, t, _verdict())
    debrief.record_intercept(log, 11, _verdict(blocked=False), force_used=True)
    assert log.suspects() == []


def test_summary_empty_when_no_records():
    assert debrief.InterceptLog().render_summary() == ""


def test_intercept_roundtrip(tmp_path):
    log = debrief.InterceptLog(game_id="rome_-180")
    debrief.record_intercept(log, 27, _verdict(), "UNIT_BUILDER", (12, 34))
    p = tmp_path / "g.intercepts.json"
    log.save(p)
    back = debrief.InterceptLog.load(p)
    assert back.game_id == "rome_-180"
    assert back.records[0].location == (12, 34)


def test_load_corrupt_and_garbage_safe(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    assert debrief.InterceptLog.load(p).records == []
    log = debrief.InterceptLog()
    debrief.record_intercept(log, "x", None)  # 垃圾输入不炸不记
    assert log.records == []
