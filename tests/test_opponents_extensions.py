"""任务 D 军力排名 + 行为日志 + 宣战风险(opponents 追加部分)—— 单元测试。"""

from __future__ import annotations

import pytest

from civ_mcp import opponents as op


@pytest.fixture(autouse=True)
def _clean_log():
    op.reset_behavior_log()
    yield
    op.reset_behavior_log()


# --- 不回归:原有 API 一个不少 ---


def test_existing_api_untouched():
    for name in (
        "AgendaProfile",
        "AGENDA_PROFILES",
        "OpponentRead",
        "read_opponent",
        "render_opponents",
        "BARBARIAN_DOCTRINE",
    ):
        assert hasattr(op, name)


# --- 行为日志 ---


def test_behavior_log_records_and_windows():
    op.add_behavior_log(1, "在(9,33)边境铺城", 42, "border_settle")
    op.add_behavior_log(1, "向我方向移动 3 个军事单位", 45, "military_move")
    op.add_behavior_log(2, "送来使者", 50)
    recent = op._BEHAVIOR_LOG.recent(current_turn=50)
    assert len(recent) == 3
    # T63 时窗口下限 = 43:T42 淘汰,T45/T50 保留
    assert len(op._BEHAVIOR_LOG.recent(current_turn=63)) == 2
    # T70 时全部出窗
    assert len(op._BEHAVIOR_LOG.recent(current_turn=71)) == 0


def test_behavior_log_filters_by_player():
    op.add_behavior_log(1, "铺城", 10, "border_settle")
    op.add_behavior_log(2, "铺城", 10, "border_settle")
    assert len(op._BEHAVIOR_LOG.recent(12, player_id=1)) == 1


def test_hostile_count_only_counts_hostile_kinds():
    op.add_behavior_log(1, "谴责你", 10, "denounce")
    op.add_behavior_log(1, "送礼", 11)  # 中性
    op.add_behavior_log(1, "军力增长 30%", 12, "troop_buildup")
    assert op._BEHAVIOR_LOG.hostile_count(15, 1) == 2


def test_behavior_log_render_uses_names():
    op.add_behavior_log(1, "T42 在你边境附近铺城", 42, "border_settle")
    text = op._BEHAVIOR_LOG.render(45, names={1: "英国(Victoria)"})
    assert "英国(Victoria)" in text and "铺城" in text


def test_behavior_log_garbage_is_dropped():
    op.add_behavior_log("x", "动作", "y")
    op.add_behavior_log(1, "", 5)
    assert op._BEHAVIOR_LOG.events == []


# --- 军力排名 ---


def _civ(name, mil, pid=1):
    return {"civ_name": name, "military_strength": mil, "player_id": pid}


def test_ranking_sorted_and_positions_me():
    out = op.get_military_ranking(
        [_civ("蒙古", 320, 1), _civ("英国", 240, 2), _civ("埃及", 150, 3)],
        my_military=180,
    )
    assert "1.蒙古(320)" in out[0]
    assert "2.英国(240)" in out[0]
    assert "排第 3" in out[1]


def test_ranking_warns_about_stronger_neighbors():
    out = op.get_military_ranking(
        [_civ("蒙古", 320, 1), _civ("英国", 240, 2)],
        my_military=180,
        neighbor_ids=(1, 2),
    )
    assert any("邻居" in l and "蒙古" in l for l in out)


def test_ranking_distant_top_dog_not_flagged_as_neighbor():
    out = op.get_military_ranking(
        [_civ("蒙古", 320, 1)], my_military=180, neighbor_ids=()
    )
    assert not any("是你的邻居" in l for l in out)


def test_ranking_bottom_half_notice():
    out = op.get_military_ranking(
        [_civ("A", 300, 1), _civ("B", 250, 2), _civ("C", 200, 3)], my_military=50
    )
    assert any("下半区" in l for l in out)


def test_ranking_empty_and_garbage_safe():
    assert op.get_military_ranking([], 100) == []
    assert op.get_military_ranking(None, 100) == []
    out = op.get_military_ranking([{"civ_name": "X", "military_strength": "垃圾"}], 10)
    assert out  # 脏军力按 0 处理,不炸


# --- 宣战风险 ---


def _neighbor(mil=100, agendas=()):
    return {
        "player_id": 7,
        "civ_name": "阿兹特克",
        "leader_name": "蒙特祖玛",
        "military_strength": mil,
        "agendas": [{"name": a} for a in agendas],
    }


def test_war_risk_military_gap_alone():
    level, reasons = op.assess_war_risk(_neighbor(mil=250), my_military=50)
    assert level in ("中", "高")
    assert any("军力" in r for r in reasons)


def test_war_risk_hostile_behavior_escalates_to_high():
    op.add_behavior_log(7, "边境铺城", 40, "border_settle")
    op.add_behavior_log(7, "调兵向我", 43, "military_move")
    level, reasons = op.assess_war_risk(
        _neighbor(mil=100), my_military=100, current_turn=45
    )
    assert level == "高"
    assert any("敌意行为" in r for r in reasons)


def test_war_risk_single_hostile_event_raises_to_medium():
    op.add_behavior_log(7, "谴责", 44, "denounce")
    level, _ = op.assess_war_risk(_neighbor(mil=100), my_military=100, current_turn=45)
    assert level == "中"


def test_war_risk_agenda_reason_included():
    level, reasons = op.assess_war_risk(
        _neighbor(mil=100, agendas=("AGENDA_TLATOANI",)), my_military=100
    )
    assert level == "高"  # TLATOANI 议程画像即高风险
    assert any("议程" in r for r in reasons)


def test_war_risk_quiet_neighbor_is_low():
    level, reasons = op.assess_war_risk(_neighbor(mil=90), my_military=100)
    assert level == "低"
    assert reasons  # 理由说明为什么低


def test_war_risk_garbage_never_raises():
    level, reasons = op.assess_war_risk(None, my_military=0)
    assert level in ("高", "中", "低") and reasons
