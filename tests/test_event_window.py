"""任务 F 关键时间窗口提醒 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import event_window as ew


def _ctx(**kw):
    return ew.EventContext(**kw)


# --- 数据来自 gamedata,不是记忆 ---


def test_gamedata_tables_loaded():
    assert ew.UNITS and ew.UPGRADES and ew.TECHS and ew.GOVERNMENTS


def test_upgrade_chain_comes_from_unitupgrades_table():
    assert ew.UPGRADES["UNIT_WARRIOR"] == "UNIT_SWORDSMAN"
    assert ew.UPGRADES["UNIT_SLINGER"] == "UNIT_ARCHER"


def test_iron_working_unlocks_swordsman_upgrade():
    """计划书点名的场景:铁器完成 → 勇士可升剑士(需要铁)。"""
    unlocks = ew.upgrades_unlocked_by("TECH_IRON_WORKING")
    assert ("UNIT_WARRIOR", "UNIT_SWORDSMAN", "RESOURCE_IRON") in unlocks


def test_tech_with_no_upgrade_returns_empty():
    assert ew.upgrades_unlocked_by("TECH_POTTERY") == []
    assert ew.upgrades_unlocked_by("BOGUS") == []


def test_strategic_reveal_table_matches_resources_json():
    assert ew.STRATEGIC_REVEALS["TECH_BRONZE_WORKING"][0] == "RESOURCE_IRON"
    assert ew.STRATEGIC_REVEALS["TECH_RADIO"][0] == "RESOURCE_ALUMINUM"
    # 马不需要科技就能看见,所以不该出现在这张表里
    assert not any(v[0] == "RESOURCE_HORSES" for v in ew.STRATEGIC_REVEALS.values())


def test_names_are_localised_from_gamedata():
    assert ew.unit_cn("UNIT_SWORDSMAN") == "剑客"
    assert ew.tech_cn("TECH_IRON_WORKING") == "炼铁术"
    assert ew.government_cn("GOVERNMENT_OLIGARCHY") == "寡头政体"
    assert ew.unit_cn("BOGUS") == "BOGUS"


# --- 升级窗口 ---


def test_upgrade_window_warns_about_missing_strategic_resource():
    out = ew.get_event_window_advice(
        _ctx(
            researching_tech="TECH_IRON_WORKING",
            tech_turns_left=3,
            my_unit_types=("UNIT_WARRIOR", "UNIT_WARRIOR"),
        )
    )
    text = "\n".join(out)
    assert "剑客" in text
    assert "RESOURCE_IRON" in text
    assert "库存为 0" in text


def test_upgrade_window_confirms_when_resource_in_stock():
    out = ew.get_event_window_advice(
        _ctx(
            researching_tech="TECH_IRON_WORKING",
            tech_turns_left=2,
            my_unit_types=("UNIT_WARRIOR",),
            my_strategic_stock={"RESOURCE_IRON": 6},
        )
    )
    assert any("够用" in line for line in out)


def test_upgrade_window_silent_when_tech_is_far_off():
    assert (
        ew.get_event_window_advice(
            _ctx(
                researching_tech="TECH_IRON_WORKING",
                tech_turns_left=20,
                my_unit_types=("UNIT_WARRIOR",),
            )
        )
        == []
    )


def test_upgrade_window_silent_without_the_old_unit():
    """没有勇士就不该提醒勇士能升级。"""
    assert not any(
        "剑客" in line
        for line in ew.get_event_window_advice(
            _ctx(
                researching_tech="TECH_IRON_WORKING",
                tech_turns_left=2,
                my_unit_types=("UNIT_SCOUT",),
            )
        )
    )


# --- 政体窗口 ---


def test_first_government_change_is_flagged_as_free():
    out = ew.get_event_window_advice(
        _ctx(
            current_government="GOVERNMENT_CHIEFDOM",
            unlocked_governments=("GOVERNMENT_OLIGARCHY",),
        )
    )
    assert any("免费" in line and "寡头政体" in line for line in out)


def test_later_government_change_is_not_called_free():
    out = ew.get_event_window_advice(
        _ctx(
            current_government="GOVERNMENT_OLIGARCHY",
            unlocked_governments=("GOVERNMENT_MONARCHY",),
            has_changed_government=True,
        )
    )
    assert not any("免费" in line for line in out)


def test_current_government_is_not_suggested():
    assert (
        ew.get_event_window_advice(
            _ctx(
                current_government="GOVERNMENT_OLIGARCHY",
                unlocked_governments=("GOVERNMENT_OLIGARCHY",),
            )
        )
        == []
    )


def test_unknown_government_type_is_ignored():
    assert (
        ew.get_event_window_advice(_ctx(unlocked_governments=("GOVERNMENT_BOGUS",)))
        == []
    )


# --- 时代窗口 ---


def test_era_score_shortfall_is_flagged():
    out = ew.get_event_window_advice(
        _ctx(era_turns_left=6, era_score=8, era_score_needed_for_golden=17)
    )
    assert any("还差 9 分" in line for line in out)


def test_era_score_met_gets_a_different_message():
    out = ew.get_event_window_advice(
        _ctx(era_turns_left=6, era_score=20, era_score_needed_for_golden=17)
    )
    assert any("已达标" in line for line in out)


def test_era_window_silent_when_far_from_end():
    assert (
        ew.get_event_window_advice(
            _ctx(era_turns_left=40, era_score=1, era_score_needed_for_golden=17)
        )
        == []
    )


# --- 对手奇观 ---


def test_rival_wonder_on_my_path_is_top_priority():
    out = ew.get_event_window_advice(
        _ctx(rival_wonders_completed=(("英国", "大图书馆", True),))
    )
    assert out[1].startswith("⚠️")
    assert "已经没了" in out[1]


def test_rival_wonder_by_same_victory_rival_is_a_softer_note():
    out = ew.get_event_window_advice(
        _ctx(
            victory_type="SCIENCE",
            rival_wonders_completed=(("英国", "大图书馆", False),),
            rival_same_victory=("英国",),
        )
    )
    assert any("同一条" in line for line in out)


def test_irrelevant_rival_wonder_is_ignored():
    assert (
        ew.get_event_window_advice(
            _ctx(rival_wonders_completed=(("刚果", "空中花园", False),))
        )
        == []
    )


# --- 静默与截断 ---


def test_quiet_turn_produces_nothing():
    assert ew.get_event_window_advice(_ctx(turn=30)) == []


def test_output_is_capped():
    out = ew.get_event_window_advice(
        _ctx(
            researching_tech="TECH_IRON_WORKING",
            tech_turns_left=2,
            my_unit_types=("UNIT_WARRIOR",),
            current_government="GOVERNMENT_CHIEFDOM",
            unlocked_governments=("GOVERNMENT_OLIGARCHY",),
            era_turns_left=5,
            era_score=1,
            era_score_needed_for_golden=17,
            rival_wonders_completed=(
                ("英国", "大图书馆", True),
                ("法国", "神谕", True),
            ),
        ),
        max_items=2,
    )
    assert any("已省略" in line for line in out)
