"""任务 P 奇观优先级分级 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import wonder_tier as wt


# --- 数据校验:奇观是否存在,只认 Buildings.json ---


def test_wonders_loaded_from_buildings_table():
    assert wt.WONDERS
    assert "BUILDING_PYRAMIDS" in wt.WONDERS
    assert wt.wonder_cn("BUILDING_PYRAMIDS") == "金字塔"
    assert wt.wonder_cost("BUILDING_PYRAMIDS") == 220


def test_non_wonder_buildings_are_excluded():
    assert "BUILDING_MONUMENT" not in wt.WONDERS
    assert "BUILDING_LIBRARY" not in wt.WONDERS


def test_every_graded_wonder_actually_exists_in_this_ruleset():
    """分级表里不能出现游戏里没有的奇观 —— 会让模型去造一个不存在的东西。"""
    missing = [w for w in wt.WONDER_TIERS if w not in wt.WONDERS]
    assert missing == []


def test_expansion_only_wonders_are_dropped_not_faked():
    """计划书点名的基尔瓦基斯瓦尼/摩索拉斯王陵墓不在原版规则集里。"""
    assert "BUILDING_KILWA_KISIWANI" not in wt.WONDER_TIERS
    assert "BUILDING_MAUSOLEUM_AT_HALICARNASSUS" not in wt.WONDER_TIERS


def test_unknown_wonder_gets_no_tier():
    assert wt.tier_of("BUILDING_BOGUS") == ""
    assert wt.tier_of("BUILDING_BOGUS", "SCIENCE") == ""


# --- 分级本身 ---


def test_tiers_are_from_the_declared_set():
    for entry in wt.WONDER_TIERS.values():
        assert entry.default in wt.TIER_ORDER
        for tier in entry.tiers.values():
            assert tier in wt.TIER_ORDER


def test_science_wonders_are_s_tier_on_science_route():
    assert wt.tier_of("BUILDING_GREAT_LIBRARY", "SCIENCE") == "S"
    assert wt.tier_of("BUILDING_OXFORD_UNIVERSITY", "SCIENCE") == "S"
    assert wt.tier_of("BUILDING_RUHR_VALLEY", "SCIENCE") == "S"


def test_same_wonder_is_route_dependent():
    """大图书馆对统治路线不是 S —— 分级必须随路线变,否则等于没分。"""
    assert wt.tier_of("BUILDING_GREAT_LIBRARY", "SCIENCE") == "S"
    assert wt.tier_of("BUILDING_GREAT_LIBRARY", "DOMINATION") == "B"


def test_stonehenge_is_s_for_religion_data_overrides_the_plan():
    """计划书把巨石阵列为诱饵;数据显示它直接送大预言家 ——
    而抢不到大预言家等于宗教路线直接断。宗教路线下必须是 S。"""
    assert wt.tier_of("BUILDING_STONEHENGE", "RELIGION") == "S"
    assert wt.tier_of("BUILDING_STONEHENGE", "SCIENCE") == "诱饵"
    assert "大预言家" in wt.WONDER_TIERS["BUILDING_STONEHENGE"].effect


def test_hanging_gardens_stays_a_trap():
    assert wt.tier_of("BUILDING_HANGING_GARDENS", "SCIENCE") == "诱饵"
    assert wt.tier_of("BUILDING_HANGING_GARDENS", "CULTURE") == "诱饵"


def test_pyramids_is_a_tier_on_every_route():
    for vt in ("SCIENCE", "CULTURE", "RELIGION", "DOMINATION", ""):
        assert wt.tier_of("BUILDING_PYRAMIDS", vt) == "A"


# --- 输出 ---


def test_advice_sorts_s_tier_first():
    out = wt.get_wonder_advice(
        ["BUILDING_HANGING_GARDENS", "BUILDING_GREAT_LIBRARY"], "SCIENCE"
    )
    assert "[S]" in out[1]
    assert "大图书馆" in out[1]


def test_trap_wonders_are_told_not_to_build():
    out = wt.get_wonder_advice(["BUILDING_HANGING_GARDENS"], "SCIENCE")
    assert any("别抢" in line for line in out)


def test_effect_is_stated_when_verified():
    out = wt.get_wonder_advice(["BUILDING_PYRAMIDS"])
    assert any("建造者" in line for line in out)


def test_unverified_effect_is_labelled_not_invented():
    """效果查不到就说查不到,不能编一句听起来合理的。"""
    assert wt.WONDER_TIERS["BUILDING_HERMITAGE"].effect == ""
    out = wt.get_wonder_advice(["BUILDING_HERMITAGE"], "CULTURE")
    assert any("不在导出表里" in line for line in out)


def test_turn_estimate_uses_real_cost():
    out = wt.get_wonder_advice(["BUILDING_PYRAMIDS"], production_per_turn=22)
    assert any("10 回合" in line for line in out)


def test_zero_production_does_not_divide_by_zero():
    assert wt.get_wonder_advice(["BUILDING_PYRAMIDS"], production_per_turn=0)


def test_empty_input_is_silent():
    assert wt.get_wonder_advice([]) == []
    assert wt.get_wonder_advice(["BUILDING_BOGUS"]) == []


def test_advice_is_capped():
    out = wt.get_wonder_advice(list(wt.WONDER_TIERS)[:10], "SCIENCE", max_items=3)
    assert any("未列出" in line for line in out)
