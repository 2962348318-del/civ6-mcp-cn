"""任务 AD 后期经济系统 —— 单元测试。"""

from __future__ import annotations

import json
from civ_mcp.resources import gamedata_path

from civ_mcp import late_economy as le

_GAMEDATA = gamedata_path()


def _ctx(**kw):
    base = dict(turn=180, era="ERA_MODERN", victory_type="SCIENCE")
    base.update(kw)
    return le.LateEconomyContext(**base)


# --- 数据来源:全部从导出表反查,不是记忆 ---


def test_big_ben_exists_and_costs_what_the_table_says():
    assert le.BIG_BEN in le.BUILDINGS
    assert le.BUILDINGS[le.BIG_BEN]["Cost"] == "1450"
    assert le.BUILDINGS[le.BIG_BEN]["PrereqTech"] == "TECH_ECONOMICS"
    assert le.building_cn(le.BIG_BEN) == "大本钟"


def test_big_ben_doubling_is_backed_by_a_real_modifier():
    """+100% 国库这个数字必须在 ModifierArguments 里查得到。"""
    tables = json.loads((_GAMEDATA / "_all.json").read_text(encoding="utf-8"))["tables"]
    args = [
        r
        for r in tables["ModifierArguments"]["rows"]
        if r["ModifierId"] == "BIG_BEN_DOUBLE_GOLD"
    ]
    assert any(r["Name"] == "Amount" and r["Value"] == "100" for r in args)


def test_every_late_policy_exists_in_this_ruleset():
    assert le.LATE_POLICIES
    for card in le.LATE_POLICIES:
        assert card.policy in le.POLICIES


def test_democracy_slot_counts_come_from_the_export():
    slots = le.government_slots("GOVERNMENT_DEMOCRACY")
    assert slots == {
        "SLOT_MILITARY": 1,
        "SLOT_ECONOMIC": 3,
        "SLOT_DIPLOMATIC": 2,
        "SLOT_WILDCARD": 2,
    }


def test_ally_trade_bonus_belongs_to_the_policy_card_not_the_government():
    """计划书把它记成政体加成 —— 实际是 POLICY_ARSENAL_OF_DEMOCRACY。
    归属搞错会导致"换了政体却没插卡",收益为零。"""
    card = next(
        c for c in le.LATE_POLICIES if c.policy == "POLICY_ARSENAL_OF_DEMOCRACY"
    )
    assert "盟友" in card.effect
    assert le.POLICIES["POLICY_ARSENAL_OF_DEMOCRACY"]["PrereqCivic"] == "CIVIC_SUFFRAGE"


# --- 大本钟囤金窗口 ---


def test_big_ben_hoard_window_states_the_doubled_amount():
    out = le.get_late_economy_advice(_ctx(gold=2450, big_ben_turns_left=8))
    line = next(x for x in out if "大本钟" in x)
    assert "4900" in line
    assert "停止一切非必要购买" in line


def test_big_ben_silent_when_far_off():
    assert le.get_late_economy_advice(_ctx(gold=2450, big_ben_turns_left=40)) == []


def test_big_ben_silent_when_already_built():
    assert (
        le.get_late_economy_advice(
            _ctx(gold=2450, big_ben_turns_left=5, has_big_ben=True)
        )
        == []
    )


def test_big_ben_silent_when_not_being_built():
    assert le.get_late_economy_advice(_ctx(gold=2450)) == []


# --- 后期政策卡 ---


def test_science_route_gets_rationalism():
    out = le.get_late_economy_advice(
        _ctx(available_policies=("POLICY_RATIONALISM",), empty_slots=1)
    )
    assert any("理性主义" in line and "+100%" in line for line in out)


def test_science_only_card_hidden_on_domination_route():
    out = le.get_late_economy_advice(
        _ctx(
            victory_type="DOMINATION",
            available_policies=("POLICY_RATIONALISM",),
            empty_slots=1,
        )
    )
    assert out == []


def test_conditional_card_is_hidden_when_condition_fails():
    """没有国际贸易路线时插电子商务是白插。"""
    assert (
        le.get_late_economy_advice(
            _ctx(available_policies=("POLICY_ECOMMERCE",), international_routes=0)
        )
        == []
    )


def test_conditional_card_shows_the_computed_payoff():
    out = le.get_late_economy_advice(
        _ctx(
            available_policies=("POLICY_INTERNATIONAL_SPACE_AGENCY",),
            suzerain_count=3,
            empty_slots=1,
        )
    )
    assert any("+15% 科技" in line for line in out)


def test_already_slotted_card_is_not_suggested():
    assert (
        le.get_late_economy_advice(
            _ctx(
                available_policies=("POLICY_RATIONALISM",),
                slotted_policies=("POLICY_RATIONALISM",),
            )
        )
        == []
    )


def test_no_empty_slot_says_swap_is_needed():
    out = le.get_late_economy_advice(
        _ctx(available_policies=("POLICY_RATIONALISM",), empty_slots=0)
    )
    assert any("需换掉现有的卡" in line for line in out)


# --- 政体槽位对比 ---


def test_government_switch_compares_slots_from_real_data():
    out = le.get_late_economy_advice(
        _ctx(
            current_government="GOVERNMENT_COMMUNISM",
            unlocked_governments=("GOVERNMENT_DEMOCRACY",),
        )
    )
    line = next(x for x in out if "GOVERNMENT_DEMOCRACY" in x)
    assert "SLOT_DIPLOMATIC" in line and "SLOT_WILDCARD" in line
    assert "少" in line  # 民主的军事槽比共产少


def test_unknown_government_is_ignored():
    assert (
        le.get_late_economy_advice(_ctx(unlocked_governments=("GOVERNMENT_BOGUS",)))
        == []
    )


# --- 产能链 ---


def test_industrial_zone_without_factory_is_flagged():
    out = le.get_late_economy_advice(
        _ctx(city_count=6, cities_with_industrial_zone=4, cities_with_factory=1)
    )
    assert any("没有工厂" in line for line in out)


def test_no_industrial_zone_at_all_is_flagged():
    out = le.get_late_economy_advice(_ctx(city_count=5))
    assert any("一个工业区都没有" in line for line in out)


def test_production_chain_quiet_in_early_eras():
    assert le.get_late_economy_advice(_ctx(era="ERA_ANCIENT", city_count=5)) == []


def test_gathering_storm_power_grid_is_not_claimed():
    """RULESET_STANDARD 没有发电量/电力缺口系统 —— 不许提。"""
    out = le.get_late_economy_advice(
        _ctx(city_count=6, cities_with_industrial_zone=4, cities_with_factory=4)
    )
    text = "\n".join(out)
    assert "电力缺口" not in text and "发电量" not in text
    assert "发电厂" in text


def test_output_is_capped():
    out = le.get_late_economy_advice(
        _ctx(
            gold=1000,
            big_ben_turns_left=5,
            city_count=6,
            cities_with_industrial_zone=4,
            cities_with_factory=1,
            available_policies=(
                "POLICY_RATIONALISM",
                "POLICY_FIVE_YEAR_PLAN",
                "POLICY_THIRD_ALTERNATIVE",
            ),
            empty_slots=1,
            current_government="GOVERNMENT_COMMUNISM",
            unlocked_governments=("GOVERNMENT_DEMOCRACY",),
        ),
        max_items=3,
    )
    assert any("已省略" in line for line in out)


def test_quiet_turn_produces_nothing():
    assert le.get_late_economy_advice(_ctx(era="ERA_MEDIEVAL")) == []
