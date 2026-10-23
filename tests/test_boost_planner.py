"""任务 K 尤里卡/鼓舞链规划 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import boost_planner as bp
from civ_mcp import tech_path as tp

_GS = {
    "builders": 1,
    "military_units": 2,
    "known_camps": 1,
    "cities": 2,
    "empire_population": 5,
    "max_city_population": 4,
    "improved_tiles": 1,
    "has_pantheon": False,
    "has_religion": False,
    "trade_capacity": 1,
    "active_trade_routes": 0,
    "land_units": 3,
    "unique_specialty_districts": 0,
    "coastal_settle_available": True,
}


# --- 数据加载 ---


def test_all_112_boosts_parse():
    boosts = bp.all_boosts()
    assert len(boosts) == 112
    for b in boosts:
        assert b["kind"] in ("tech", "civic")
        assert b["node"]


def test_every_boost_node_exists_in_its_tree():
    """boost 指向的科技/市政必须真的在对应的树里。"""
    for b in bp.all_boosts():
        if b["kind"] == "tech":
            assert b["node"] in tp._tech_nodes(), b["node"]
        else:
            assert b["node"] in tp._civic_nodes(), b["node"]


def test_boost_fraction_is_conservative_and_annotated():
    """Amount 列全空 —— 比例未导出,取保守 0.4,文案写 40-50%。"""
    assert bp.BOOST_FRACTION == 0.4
    assert bp.DISCOUNT == 0.6
    out = bp.get_boost_advice({"TECH_POTTERY"}, set(), _GS, {"CIVIC_CODE_OF_LAWS"}, 8)
    assert any("40-50%" in line for line in out)


# --- 三分类 ---


def _classify_all(gs):
    from collections import Counter

    c = Counter()
    for b in bp.all_boosts():
        st, _ = bp.classify_boost(b, set(), set(), gs)
        c[st] += 1
    return c


def test_classification_covers_all_rows():
    c = _classify_all(_GS)
    assert sum(c.values()) == 112
    assert c[bp.ST_NONE] == 6  # 6 个后期关键科技设计上无尤里卡
    assert c[bp.ST_REACHABLE] >= 10
    assert c[bp.ST_UNDECIDABLE] >= 80  # 目标物未导出是主因,诚实归 ❓


def test_banked_status_comes_from_completed_boosts():
    b = next(x for x in bp.all_boosts() if x["kind"] == "tech")
    st, note = bp.classify_boost(b, set(), {b["node"]}, _GS)
    assert st == bp.ST_BANKED
    assert "折扣" in note


def test_clear_camp_reachable_needs_army_and_known_camp():
    b = next(x for x in bp.all_boosts() if x["cls"] == "BOOST_TRIGGER_CLEAR_CAMP")
    assert bp.classify_boost(b, set(), set(), _GS)[0] == bp.ST_REACHABLE
    no_camp = dict(_GS, known_camps=0)
    st, note = bp.classify_boost(b, set(), set(), no_camp)
    assert st == bp.ST_UNDECIDABLE and "营地" in note


def test_meet_civ_is_undecidable_per_plan_example():
    """计划书原话:『遇见另一个文明——目前未遇,待定』。"""
    b = next(x for x in bp.all_boosts() if x["cls"] == "BOOST_TRIGGER_MEET_CIV")
    st, note = bp.classify_boost(b, set(), set(), _GS)
    assert st == bp.ST_UNDECIDABLE
    assert "探索" in note


def test_unexported_target_classes_are_honest():
    """HAVE_X_BUILDINGS 这类的目标建筑没被导出 —— 必须归 ❓ 并说明原因。"""
    b = next(x for x in bp.all_boosts() if x["cls"] == "BOOST_TRIGGER_HAVE_X_BUILDINGS")
    st, note = bp.classify_boost(b, set(), set(), _GS)
    assert st == bp.ST_UNDECIDABLE
    assert "未导出" in note


def test_missing_game_state_field_degrades_to_undecidable():
    b = next(
        x for x in bp.all_boosts() if x["cls"] == "BOOST_TRIGGER_NUM_IMPROVED_TILES"
    )
    st, note = bp.classify_boost(b, set(), set(), {})
    assert st == bp.ST_UNDECIDABLE
    assert "缺" in note


def test_pantheon_predicate():
    b = next(x for x in bp.all_boosts() if x["cls"] == "BOOST_TRIGGER_CREATE_PANTHEON")
    assert bp.classify_boost(b, set(), set(), _GS)[0] == bp.ST_REACHABLE
    assert bp.classify_boost(b, set(), set(), {})[0] == bp.ST_UNDECIDABLE


# --- 折扣表(与 tech_path 的协作) ---


def test_discounts_only_for_banked_and_reachable():
    d = bp.boost_discounts("civic", set(), set(), _GS)
    assert d and all(v == bp.DISCOUNT for v in d.values())
    # 全部无法判定时(空 game_state)只剩 banked
    d2 = bp.boost_discounts("tech", set(), {"TECH_IRON_WORKING"}, {})
    assert d2 == {"TECH_IRON_WORKING": bp.DISCOUNT}


def test_discount_excludes_researched():
    d = bp.boost_discounts("tech", {"TECH_IRON_WORKING"}, {"TECH_IRON_WORKING"}, {})
    assert "TECH_IRON_WORKING" not in d


def test_none_class_never_discounted():
    none_nodes = {b["node"] for b in bp.all_boosts() if b["cls"] == bp._NONE_CLASS}
    d = bp.boost_discounts("tech", set(), none_nodes, _GS)
    assert not none_nodes & set(d)


def test_tech_path_integration_applies_discount():
    """tech_path 的建议里出现 ⚡ 标记 = 折扣真的进了权重。"""
    out = tp.get_tech_advice(
        {"TECH_POTTERY", "TECH_ANIMAL_HUSBANDRY"}, 8, "DOMINATION", _GS, set()
    )
    assert any("⚡" in line for line in out)
    assert any("尤里卡已计入" in line for line in out)


# --- 建议输出 ---


def test_advice_prefers_near_targets_over_big_far_savings():
    """『单城人口 15』的后期市政省得再多,也不该排在眼前的科技前面。"""
    out = bp.get_boost_advice({"TECH_POTTERY"}, set(), _GS, {"CIVIC_CODE_OF_LAWS"}, 8)
    bullets = [l for l in out if l.startswith("· ")]
    assert bullets
    assert not any("城市化" in b for b in bullets)  # 闭包 8 步开外,被过滤


def test_advice_shows_savings_and_turns():
    out = bp.get_boost_advice({"TECH_POTTERY"}, set(), _GS, {"CIVIC_CODE_OF_LAWS"}, 8)
    assert any("省约" in line and "回合" in line for line in out)


def test_banked_section_lists_pending_harvest():
    out = bp.get_boost_advice(
        {"TECH_POTTERY"}, {"TECH_WRITING"}, _GS, {"CIVIC_CODE_OF_LAWS"}, 8
    )
    assert any("已触发待收割" in line and "写作" in line for line in out)


def test_undecidable_count_is_reported():
    out = bp.get_boost_advice({"TECH_POTTERY"}, set(), _GS, {"CIVIC_CODE_OF_LAWS"}, 8)
    assert any("无法判定" in line for line in out)


def test_linkage_line_uses_tech_path():
    out = bp.get_boost_advice({"TECH_POTTERY"}, set(), _GS, {"CIVIC_CODE_OF_LAWS"}, 8)
    link = [l for l in out if "衔接" in l]
    if link:  # 有衔接行时必须给前后对比
        assert "回合" in link[0]


def test_empty_inputs_are_silent_and_safe():
    assert bp.get_boost_advice(set(), set(), {}) == []
    assert bp.get_boost_advice(None, None, None) == []
    assert (
        bp.classify_boost(
            {"kind": "tech", "node": "TECH_BOGUS", "cls": "BOGUS", "num": 0},
            set(),
            set(),
            _GS,
        )[0]
        == bp.ST_UNDECIDABLE
    )


def test_advice_is_capped():
    out = bp.get_boost_advice(
        {"TECH_POTTERY"}, set(), _GS, {"CIVIC_CODE_OF_LAWS"}, 8, max_items=2
    )
    assert sum(1 for l in out if l.startswith("· ")) <= 2
