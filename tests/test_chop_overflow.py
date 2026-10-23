"""任务 U 砍树溢出 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import chop_overflow as ch


def _ctx(**kw):
    base = dict(
        city_name="罗马",
        producing="BUILDING_PYRAMIDS",
        producing_cn="金字塔",
        production_remaining=90,
        production_per_turn=10,
        sites=[],
        bonuses=[],
    )
    base.update(kw)
    return ch.ChopContext(**base)


def _site(**kw):
    base = dict(
        x=14,
        y=34,
        feature="FEATURE_FOREST",
        builder_id=1,
        builder_charges=2,
        chop_yield=120,
    )
    base.update(kw)
    return ch.ChopSite(**base)


# --- 数据校验:地貌名必须真的在 Features.json 里 ---


def test_choppable_features_validated_against_gamedata():
    assert "FEATURE_FOREST" in ch.CHOPPABLE_FEATURES
    assert "FEATURE_JUNGLE" in ch.CHOPPABLE_FEATURES
    assert ch.CHOPPABLE_FEATURES["FEATURE_FOREST"] == "森林"


def test_natural_wonders_are_not_choppable():
    assert "FEATURE_EVEREST" not in ch.CHOPPABLE_FEATURES
    assert not ch.ChopSite(0, 0, "FEATURE_EVEREST", builder_charges=1).is_choppable


# --- 加成算术 ---


def test_effective_chop_with_no_bonus():
    assert ch.effective_chop(120, []) == 120


def test_effective_chop_stacks_percentages():
    """马格努斯 +50% 与政策卡 +30% 叠加是加法,不是乘法。"""
    assert ch.effective_chop(120, [("马格努斯", 50)]) == 180
    assert ch.effective_chop(120, [("马格努斯", 50), ("政策卡", 30)]) == 216


# --- 建议输出 ---


def test_chop_that_completes_the_item_reports_spare():
    out = ch.get_chop_advice(_ctx(sites=[_site()]))
    text = "\n".join(out)
    assert "直接完成" in text
    assert "金字塔" in text
    assert "余 30 锤" in text


def test_partial_chop_reports_progress_not_completion():
    out = ch.get_chop_advice(_ctx(production_remaining=400, sites=[_site()]))
    text = "\n".join(out)
    assert "直接完成" not in text
    assert "进度 +30%" in text


def test_bonus_source_is_named_in_output():
    out = ch.get_chop_advice(_ctx(sites=[_site()], bonuses=[("马格努斯·地面清理", 50)]))
    assert any("马格努斯" in line and "+50%" in line for line in out)


def test_unknown_chop_yield_states_mechanic_without_numbers():
    """砍树产能不在游戏库里 —— 未知时必须说清楚,不许编数字。"""
    out = ch.get_chop_advice(_ctx(sites=[_site(chop_yield=None)]))
    site_line = next(line for line in out if "(14,34)" in line)
    assert "游戏库里没有这张表" in site_line
    assert "锤" not in site_line  # 不许报任何具体产能
    assert any("不编造数字" in line for line in out)


def test_ordinary_unit_production_does_not_trigger_advice():
    """当前生产勇士时砍树是浪费建造者次数,不该提醒。"""
    assert (
        ch.get_chop_advice(
            _ctx(producing="UNIT_WARRIOR", producing_cn="勇士", sites=[_site()])
        )
        == []
    )


def test_settler_is_high_value():
    out = ch.get_chop_advice(
        _ctx(producing="UNIT_SETTLER", producing_cn="开拓者", sites=[_site()])
    )
    assert out


def test_builder_without_charges_is_ignored():
    assert ch.get_chop_advice(_ctx(sites=[_site(builder_charges=0)])) == []


def test_no_sites_is_silent():
    assert ch.get_chop_advice(_ctx()) == []


def test_no_production_is_silent():
    assert ch.get_chop_advice(_ctx(producing="", sites=[_site()])) == []


def test_extra_sites_are_summarised():
    sites = [_site(x=i) for i in range(5)]
    out = ch.get_chop_advice(_ctx(sites=sites), max_sites=2)
    assert any("另有 3 个" in line for line in out)


def test_zero_production_per_turn_does_not_divide_by_zero():
    out = ch.get_chop_advice(_ctx(production_per_turn=0, sites=[_site()]))
    assert out
