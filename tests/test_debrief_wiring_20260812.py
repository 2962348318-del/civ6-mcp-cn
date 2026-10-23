"""复盘事实层接线 —— 2026-08-12 罗马 T384 实机缺陷的回归测试。

复盘报「损失单位 0」而那局打了三场战争:lost_units 由 diff_units 在同一
函数体里算出来了,却从没传进 _record_debrief。这是同一条链上第三次
「定义了没接线」,而下游不只是复盘表 —— learning_store 的「高损失」判据
与 debrief.contradictions() 的自评校验都读这个字段。
"""

from __future__ import annotations

from types import SimpleNamespace

from civ_mcp import debrief, end_turn


def _fakes(monkeypatch, tmp_path):
    monkeypatch.setenv("CIV_MCP_WORKSPACE", str(tmp_path))
    gs = SimpleNamespace(_game_identity=("罗马", -1392428359))
    wm = SimpleNamespace(
        difficulty="DIFFICULTY_WARLORD",
        victory_type="SCIENCE",
        exploration_pct=0.30,
        threat_level="RED",
        active_camps=lambda: [],
    )
    scan = SimpleNamespace(
        cities=20,
        military=24,
        production=497,
        science=368,
        culture=210,
        techs=["T"] * 68,
        civics=["C"] * 46,
    )
    return gs, wm, scan


def _load_log(tmp_path):
    from civ_mcp import world_model

    base = world_model.storage_path("罗马", -1392428359).parent.parent / "复盘"
    return debrief.DebriefLog.load(base / "罗马_-1392428359.json")


def test_lost_units_reach_the_debrief_log(monkeypatch, tmp_path):
    """核心缺陷:阵亡单位必须落进 TurnFact.units_lost。"""
    gs, wm, scan = _fakes(monkeypatch, tmp_path)
    end_turn._record_debrief(
        gs, wm, scan, 384, lost_units=[("UNIT_ROMAN_LEGION", 85, 45)]
    )
    log = _load_log(tmp_path)
    assert log.facts[-1].units_lost == ["UNIT_ROMAN_LEGION"]


def test_no_losses_records_empty_not_crash(monkeypatch, tmp_path):
    gs, wm, scan = _fakes(monkeypatch, tmp_path)
    end_turn._record_debrief(gs, wm, scan, 100)  # 缺省 None
    assert _load_log(tmp_path).facts[-1].units_lost == []


def test_culture_and_civics_also_wired(monkeypatch, tmp_path):
    """同批漏填的两个字段 —— TurnFact 定义了就该有来源。"""
    gs, wm, scan = _fakes(monkeypatch, tmp_path)
    end_turn._record_debrief(gs, wm, scan, 384)
    fact = _load_log(tmp_path).facts[-1]
    assert fact.culture == 210
    assert fact.civics_done == 46
    assert fact.techs_done == 68


def test_far_camps_revealed_late_are_not_a_lesson():
    """罗马 T384:卫星揭图让 active_camps 从 1 跳到 17,而它们距首都 66 格。

    「17 个营地未拆」是指标语义漂移造成的误报 —— 够不着的营地不耗你产能。
    """
    log = debrief.DebriefLog(game_id="rome")
    for t in range(1, 361):
        # 探索同步增长,免得探索教训混进断言
        log.record(
            debrief.TurnFact(
                turn=t, active_camps=1, camps_near=0, exploration=0.3 * t / 365
            )
        )
    log.record(
        debrief.TurnFact(turn=365, active_camps=17, camps_near=0, exploration=0.3)
    )
    assert not any("未拆除" in x for x in log.lessons())


def test_near_camps_still_produce_the_lesson():
    """反向:够得着的营地一直没拆,教训照报。"""
    log = debrief.DebriefLog(game_id="lazy")
    for t in range(1, 101):
        log.record(
            debrief.TurnFact(
                turn=t, active_camps=4, camps_near=3, exploration=0.5 * t / 100
            )
        )
    assert any("3 个营地" in x for x in log.lessons())


def test_legacy_log_without_distance_degrades_honestly():
    """老日志无 camps_near(-1):如实声明数据缺口,不冒充精确。"""
    log = debrief.DebriefLog(game_id="old")
    for t in range(1, 101):
        log.record(
            debrief.TurnFact(turn=t, active_camps=17, exploration=0.5 * t / 100)
        )  # camps_near 缺省 -1
    text = " ".join(log.lessons())
    assert "旧日志无距离" in text


def test_big_map_exploration_not_flagged_by_lessons():
    """探索判据同样改为停滞检测 —— 大地图低比例但在涨,不该报。"""
    log = debrief.DebriefLog(game_id="big")
    for t in range(1, 385):
        log.record(debrief.TurnFact(turn=t, exploration=0.30 * t / 384))
    assert not any("探索" in x for x in log.lessons())


def test_losses_feed_the_learning_store_judgement(monkeypatch, tmp_path):
    """端到端:复盘里的损失必须能被跨局学习的「高损失」判据看见。

    这是缺陷真正的代价 —— 事实层为空时,这条教训永远不会出现在下局简报里。
    """
    from civ_mcp import learning_store

    gs, wm, scan = _fakes(monkeypatch, tmp_path)
    for turn in range(1, 7):
        end_turn._record_debrief(
            gs, wm, scan, turn, lost_units=[("UNIT_ROMAN_LEGION", 85, 45)]
        )
    rec = learning_store.extract_learning(_load_log(tmp_path))
    assert rec.units_lost == 6
    assert any("高损失" in i for i in rec.top_issues)
