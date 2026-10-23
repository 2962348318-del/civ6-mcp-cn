"""第三轮:任务 Z / AC / AB / Y / G —— 后期收尾与博弈的单元测试。"""

from __future__ import annotations

import json
from civ_mcp.resources import gamedata_path

from civ_mcp import culture_endgame as ce
from civ_mcp import diplomacy_advisor as da
from civ_mcp import lategame as lg
from civ_mcp import war_planner as wp
from civ_mcp import world_model as wm

_GAMEDATA = gamedata_path()


def _tables():
    return json.loads((_GAMEDATA / "_all.json").read_text(encoding="utf-8"))["tables"]


# ===========================================================================
# 任务 Z:胜利阻断(lategame 追加)
# ===========================================================================


def _zctx(**kw):
    base = dict(turn=180, my_victory_type="SCIENCE", total_civs=8)
    base.update(kw)
    return lg.RivalVictoryContext(**base)


def test_z_existing_lategame_api_untouched():
    for name in (
        "check_lategame",
        "victory_playbook",
        "evaluate_great_person",
        "science_endgame",
        "track_dvp_progress",
        "SCIENCE_CHAIN",
    ):
        assert hasattr(lg, name)


def test_z_religion_one_civ_away_is_red():
    out = lg.check_rival_victory_threat(_zctx(religion_majority={"西班牙": 6}))
    assert out and "🔴" in out[1]
    assert "只差 1 个文明" in out[1]
    assert any("传教士" in line or "使徒" in line for line in out)


def test_z_religion_two_away_is_yellow():
    out = lg.check_rival_victory_threat(_zctx(religion_majority={"西班牙": 5}))
    assert any("🟡" in line for line in out)


def test_z_religion_far_behind_is_silent():
    assert lg.check_rival_victory_threat(_zctx(religion_majority={"西班牙": 2})) == []


def test_z_space_denominator_from_science_chain():
    """分母来自 SCIENCE_CHAIN(Projects.json),不写死 5。"""
    out = lg.check_rival_victory_threat(_zctx(space_projects={"中国": 4}))
    assert f"/{len(lg.SCIENCE_CHAIN)} 个太空项目" in out[1]
    assert any("SABOTAGE_PRODUCTION" in line for line in out)


def test_z_culture_ratio_threshold():
    near = lg.check_rival_victory_threat(
        _zctx(my_domestic_tourists=100, tourism={"法国": 85})
    )
    assert any("🟡" in line for line in near)
    over = lg.check_rival_victory_threat(
        _zctx(my_domestic_tourists=100, tourism={"法国": 110})
    )
    assert any("🔴" in line for line in over)
    safe = lg.check_rival_victory_threat(
        _zctx(my_domestic_tourists=100, tourism={"法国": 50})
    )
    assert safe == []


def test_z_culture_blocking_names_border_and_trade():
    out = lg.check_rival_victory_threat(
        _zctx(my_domestic_tourists=100, tourism={"法国": 110})
    )
    text = "\n".join(out)
    assert "开放边界" in text and "贸易路线" in text


def test_z_domination_capitals_threshold():
    out = lg.check_rival_victory_threat(_zctx(original_capitals={"蒙古": 3}))
    assert any("原始首都" in line for line in out)
    assert lg.check_rival_victory_threat(_zctx(original_capitals={"蒙古": 2})) == []


def test_z_at_war_note_added():
    out = lg.check_rival_victory_threat(
        _zctx(religion_majority={"西班牙": 6}, at_war_with=("西班牙",))
    )
    assert any("已在和 西班牙 交战" in line for line in out)


def test_z_tradeoff_line_mentions_my_route():
    out = lg.check_rival_victory_threat(_zctx(religion_majority={"西班牙": 6}))
    assert any("你在走 SCIENCE" in line and "否决项" in line for line in out)


def test_z_thresholds_labelled_as_editorial():
    out = lg.check_rival_victory_threat(_zctx(religion_majority={"西班牙": 6}))
    assert any("不是导出值" in line for line in out)


def test_z_nobody_close_is_silent():
    assert lg.check_rival_victory_threat(_zctx()) == []
    assert lg.check_rival_victory_threat(lg.RivalVictoryContext()) == []


def test_z_garbage_safe():
    assert (
        lg.check_rival_victory_threat(
            _zctx(religion_majority={"X": "垃圾"}, space_projects={"Y": None})
        )
        == []
    )


# ===========================================================================
# 任务 AC:现代战争(war_planner 追加)
# ===========================================================================


def test_ac_existing_war_api_untouched():
    for name in (
        "assess_attack",
        "suggest_siege_plan",
        "post_war_advice",
        "get_pillage_advice",
        "classify_unit",
    ):
        assert hasattr(wp, name)


def test_ac_air_units_discovered_from_domain():
    air = wp.air_units()
    assert air
    types = {ut for ut, _c, _p, _r in air}
    assert "UNIT_BOMBER" in types and "UNIT_FIGHTER" in types
    tables = _tables()
    for ut, _cls, _p, _r in air:
        row = next(r for r in tables["Units"]["rows"] if r["UnitType"] == ut)
        assert row["Domain"] == "DOMAIN_AIR"


def test_ac_gdr_absent_in_this_ruleset():
    """计划书要求做 GDR;数据说原版没有这个单位。"""
    assert wp.gdr_available() is False
    blob = json.dumps(_tables(), ensure_ascii=False).upper()
    assert "GIANT_DEATH" not in blob


def test_ac_gdr_absence_is_reported_not_silent():
    out = wp.modern_war_assess(
        {"my_units": [{"type": "UNIT_BOMBER"}]}, "ERA_INFORMATION"
    )
    assert any("GDR" in line and "不存在" in line for line in out)


def test_ac_nuclear_projects_from_export():
    projects = wp.nuclear_projects()
    types = {p["type"] for p in projects}
    assert "PROJECT_MANHATTAN_PROJECT" in types
    assert "PROJECT_BUILD_NUCLEAR_DEVICE" in types
    assert all(p["cost"] > 0 and p["tech"] for p in projects)


def test_ac_nuke_diplomatic_cost_stated_without_damage_numbers():
    out = wp.modern_war_assess(
        {
            "my_units": [{"type": "UNIT_NUCLEAR_SUBMARINE"}],
            "strategic_stock": {"RESOURCE_URANIUM": 2},
        },
        "ERA_ATOMIC",
    )
    text = "\n".join(out)
    assert "谴责" in text
    assert "威力/辐射数值未导出" in text


def test_ac_air_chain_uses_real_stats():
    out = wp.modern_war_assess(
        {"my_units": [{"type": "UNIT_FIGHTER"}, {"type": "UNIT_BOMBER"}]}, "ERA_MODERN"
    )
    line = next(l for l in out if l.startswith("✈"))
    assert "轰炸 110" in line and "射程 10" in line  # Units.json 导出值


def test_ac_missing_aluminum_flagged():
    out = wp.modern_war_assess(
        {"my_units": [{"type": "UNIT_BOMBER"}], "strategic_stock": {}}, "ERA_MODERN"
    )
    assert any("铝" in line and "库存为 0" in line for line in out)


def test_ac_no_airforce_in_modern_era_is_called_out():
    out = wp.modern_war_assess({"my_units": [{"type": "UNIT_TANK"}]}, "ERA_MODERN")
    assert any("你没有任何空军" in line for line in out)


def test_ac_carrier_without_planes():
    out = wp.modern_war_assess(
        {"my_units": [{"type": "UNIT_AIRCRAFT_CARRIER"}]}, "ERA_MODERN"
    )
    assert any("空船一条" in line for line in out)


def test_ac_silent_before_industrial():
    assert (
        wp.modern_war_assess({"my_units": [{"type": "UNIT_BOMBER"}]}, "ERA_CLASSICAL")
        == []
    )


def test_ac_garbage_safe():
    assert wp.modern_war_assess(None, None) is not None
    assert wp.modern_war_assess({"my_units": "垃圾"}, "ERA_MODERN") is not None


# ===========================================================================
# 任务 AB:文化胜利收尾(新模块)
# ===========================================================================


def test_ab_rock_band_absent_in_this_ruleset():
    """计划书要求做摇滚乐队策略;Units 表里根本没有这个单位。"""
    assert ce.rock_band_available() is False
    units = {r["UnitType"] for r in _tables()["Units"]["rows"]}
    assert not any("ROCK_BAND" in u for u in units)


def test_ab_naturalist_and_beach_resort_do_exist():
    """国家公园和海滨度假区是原版内容 —— 这两条要做。"""
    assert ce.naturalist_available() is True
    assert ce.beach_resort_available() is True


def test_ab_tourism_boosters_from_modifiers():
    boosters = {b["key"]: b for b in ce.tourism_boosters()}
    assert "TECH_COMPUTERS" in boosters
    assert "+100%" in boosters["TECH_COMPUTERS"]["effect"]
    assert "POLICY_HERITAGE_TOURISM" in boosters
    assert "CIVIC_CONSERVATION" in boosters


def test_ab_eiffel_and_cristo_values_from_export():
    assert ce.eiffel_appeal() == 2
    assert ce.beach_resort_multiplier() == 2.0


def test_ab_park_shape_validation():
    good = [
        {"x": 10, "y": 10},
        {"x": 10, "y": 11},
        {"x": 11, "y": 10},
        {"x": 11, "y": 11},
    ]
    assert ce.is_valid_park_shape(good)
    assert not ce.is_valid_park_shape(good[:3])  # 只有 3 格
    assert not ce.is_valid_park_shape(
        [{"x": 10, "y": 10}, {"x": 10, "y": 11}, {"x": 13, "y": 10}, {"x": 13, "y": 11}]
    )  # 列不相邻
    assert not ce.is_valid_park_shape(
        [{"x": 10, "y": 10}, {"x": 10, "y": 12}, {"x": 11, "y": 10}, {"x": 11, "y": 12}]
    )  # 纵向不相邻
    assert not ce.is_valid_park_shape([])


def test_ab_park_appeal_unknown_is_flagged_not_guessed():
    good = [
        {"x": 10, "y": 10},
        {"x": 10, "y": 11},
        {"x": 11, "y": 10},
        {"x": 11, "y": 11},
    ]
    ok, why = ce.check_park_candidate(good)
    assert ok and "魅力值未导出" in why


def test_ab_park_appeal_checked_when_supplied():
    hi = [
        dict(t, appeal=3)
        for t in (
            {"x": 10, "y": 10},
            {"x": 10, "y": 11},
            {"x": 11, "y": 10},
            {"x": 11, "y": 11},
        )
    ]
    ok, why = ce.check_park_candidate(hi)
    assert ok and "可建" in why
    lo = [dict(t, appeal=1) for t in hi]
    ok2, why2 = ce.check_park_candidate(lo)
    assert not ok2 and "种森林" in why2


def test_ab_missing_boosters_are_listed():
    out = ce.get_culture_endgame_advice(
        {"victory_type": "CULTURE", "techs": set(), "civics": set()}
    )
    assert any("电脑" in line for line in out)


def test_ab_open_borders_and_routes_checked():
    out = ce.get_culture_endgame_advice(
        {
            "victory_type": "CULTURE",
            "techs": {"TECH_COMPUTERS"},
            "known_civs": ("英国", "法国", "巴西"),
            "open_borders_with": ("英国",),
            "trade_routes_to": (),
        }
    )
    text = "\n".join(out)
    assert "2/3 个文明没有开放边界" in text
    assert "3/3 个文明没有你的贸易路线" in text


def test_ab_rock_band_absence_is_reported():
    out = ce.get_culture_endgame_advice({"victory_type": "CULTURE"})
    assert any("摇滚乐队" in line and "不存在" in line for line in out)


def test_ab_silent_on_non_culture_route():
    assert ce.get_culture_endgame_advice({"victory_type": "SCIENCE"}) == []


def test_ab_garbage_safe():
    assert ce.get_culture_endgame_advice(None) == []
    assert ce.get_culture_endgame_advice({"park_candidates": "垃圾"}) is not None


# ===========================================================================
# 任务 Y:忠诚翻转(world_model 追加)
# ===========================================================================


def test_y_existing_world_model_api_untouched():
    for name in (
        "WorldModel",
        "loyalty_pressure",
        "evaluate_settle_location",
        "compute_threat_level",
        "render_briefing",
    ):
        assert hasattr(wm, name)


def test_y_flippable_city_reports_countdown():
    out = wm.assess_loyalty_flip(
        {"name": "小城", "x": 0, "y": 0, "population": 3},
        my_cities=[
            {"x": 2, "y": 0, "population": 14},
            {"x": 0, "y": 2, "population": 14},
        ],
        enemy_cities=[],
    )
    assert any("回合后叛变加入你" in line for line in out)
    assert any("别打它" in line for line in out)


def test_y_unflippable_reports_gap():
    out = wm.assess_loyalty_flip(
        {"name": "坚城", "x": 0, "y": 0, "population": 10},
        my_cities=[],
        enemy_cities=[{"x": 1, "y": 0, "population": 12}],
    )
    assert any("当前不可翻转" in line and "还差" in line for line in out)


def test_y_target_city_counts_as_its_own_support():
    """目标城自身的人口也支撑它 —— 调用方没传时要自动补上。"""
    with_self = wm.assess_loyalty_flip(
        {"name": "A", "x": 0, "y": 0, "population": 12},
        my_cities=[{"x": 3, "y": 0, "population": 6}],
        enemy_cities=[],
    )
    assert any("当前不可翻转" in line for line in with_self)


def test_y_acceleration_options_listed():
    out = wm.assess_loyalty_flip(
        {"name": "小城", "x": 0, "y": 0, "population": 2},
        my_cities=[{"x": 1, "y": 0, "population": 20}],
        game_state={"can_settle_nearby": True, "spy_available": True},
    )
    text = "\n".join(out)
    assert "9 格内再铺一座城" in text
    assert "Amani" in text
    assert "面包与马戏" in text
    assert "NEUTRALIZE_GOVERNOR" in text


def test_y_amani_wording_differs_by_ownership():
    have = wm.assess_loyalty_flip(
        {"name": "A", "x": 0, "y": 0, "population": 2},
        my_cities=[{"x": 1, "y": 0, "population": 20}],
        game_state={"has_amani": True},
    )
    lack = wm.assess_loyalty_flip(
        {"name": "A", "x": 0, "y": 0, "population": 2},
        my_cities=[{"x": 1, "y": 0, "population": 20}],
        game_state={"has_amani": False},
    )
    assert any("派到附近城邦" in l for l in have)
    assert any("你还没有" in l for l in lack)


def test_y_unexported_numbers_labelled():
    out = wm.assess_loyalty_flip(
        {"name": "A", "x": 0, "y": 0, "population": 2},
        my_cities=[{"x": 1, "y": 0, "population": 20}],
    )
    assert any("未导出" in line for line in out)


def test_y_garbage_safe():
    assert wm.assess_loyalty_flip({}, None, None) == []
    assert wm.assess_loyalty_flip({"x": "?", "y": 0}, []) == []
    assert wm.assess_loyalty_flip(None) == []


# ===========================================================================
# 任务 G:外交博弈(新模块)
# ===========================================================================


def test_g_world_congress_absent_in_this_ruleset():
    """外交胜利/世界议会/外交favor 都不在原版规则集里。"""
    assert da.WORLD_CONGRESS_AVAILABLE is False
    blob = json.dumps(_tables(), ensure_ascii=False).upper()
    assert "WORLD_CONGRESS" not in blob
    assert "VICTORY_DIPLOMATIC" not in blob
    note = da.wc_availability_note()
    assert "VICTORY_DIPLOMATIC" in note


def test_g_wc_silent_without_data():
    assert da.get_wc_voting_advice([]) == []
    assert da.get_wc_voting_advice(None, my_favor=5) == []


def test_g_wc_activates_when_data_supplied():
    """换到有议会的规则集后,算术部分不用改一行。"""
    out = da.get_wc_voting_advice(
        [
            {
                "name": "军事条约",
                "dvp_reward": 2,
                "predicted_winner": "A",
                "my_preference": "A",
            }
        ],
        my_favor=30,
    )
    assert out and any("性价比最高" in line for line in out)


def test_g_wc_penalty_resolution_is_top_priority():
    out = da.get_wc_voting_advice(
        [
            {
                "name": "小决议",
                "dvp_reward": 1,
                "predicted_winner": "A",
                "my_preference": "A",
            },
            {"name": "剥夺你的 DVP", "targets_me": True, "dvp_penalty": 2},
        ],
        my_favor=20,
    )
    assert out[1].startswith("🔴")
    assert "倒退" in out[1]


def test_g_wc_uphill_vote_is_discouraged():
    out = da.get_wc_voting_advice(
        [{"name": "某决议", "predicted_winner": "B", "my_preference": "A"}],
        my_favor=10,
        victory_type="SCIENCE",
    )
    assert any("逆势投票是纯亏" in line for line in out)


def test_g_joint_war_needs_two_threats():
    one = [{"civ_name": "英国", "military_strength": 300, "war_risk": "高"}]
    assert da.get_joint_war_advice(one, {"my_military": 100}) == []


def test_g_joint_war_picks_hireable_neighbour():
    out = da.get_joint_war_advice(
        [
            {"civ_name": "英国", "military_strength": 300, "war_risk": "高"},
            {
                "civ_name": "蒙古",
                "military_strength": 280,
                "war_risk": "高",
                "agendas": ["AGENDA_TLATOANI"],
            },
        ],
        {"my_military": 120, "gold": 600, "gold_per_turn": 20},
    )
    text = "\n".join(out)
    assert "买通 蒙古" in text and "英国" in text
    assert "牵制" in text
    assert 'mode=\\"test\\"' in text or 'mode="test"' in text


def test_g_joint_war_rejects_too_weak_hireling():
    out = da.get_joint_war_advice(
        [
            {"civ_name": "英国", "military_strength": 400, "war_risk": "高"},
            {"civ_name": "小国", "military_strength": 50, "war_risk": "高"},
        ],
        {"my_military": 100},
    )
    assert any("打不动" in line and "送人头" in line for line in out)


def test_g_joint_war_uses_agenda_for_pricing():
    out = da.get_joint_war_advice(
        [
            {"civ_name": "英国", "military_strength": 300, "war_risk": "高"},
            {
                "civ_name": "阿兹特克",
                "military_strength": 290,
                "war_risk": "高",
                "agendas": [{"name": "AGENDA_TLATOANI"}],
            },
        ],
        {"my_military": 120},
    )
    assert any("奢侈" in line for line in out)  # 议程画像里的 cares


def test_g_joint_war_warns_against_opportunism():
    out = da.get_joint_war_advice(
        [
            {"civ_name": "英国", "military_strength": 300, "war_risk": "高"},
            {"civ_name": "蒙古", "military_strength": 280, "war_risk": "高"},
        ],
        {"my_military": 100},
    )
    assert any("守住发育窗口" in line for line in out)


def test_g_garbage_safe():
    assert da.get_joint_war_advice(None, None) == []
    assert da.get_joint_war_advice(["垃圾"], {"my_military": "x"}) == []
    assert da.get_wc_voting_advice(["垃圾"]) == []
