"""任务 C 战前评估 + 攻城模板 + 战后处理 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import war_planner as wp


def _gs(
    my=(),
    enemy=(),
    wall_max=0,
    wall_hp=0,
    wall_b="",
    cities=None,
    techs=None,
    defense=28,
):
    return {
        "target": {
            "name": "伦敦",
            "population": 7,
            "defense_strength": defense,
            "wall_hp": wall_hp,
            "wall_max_hp": wall_max,
            "wall_building": wall_b,
        },
        "my_units": [{"type": t} for t in my],
        "enemy_units": [{"type": t} for t in enemy],
        "my_cities": cities if cities is not None else [{"x": 10, "y": 30}],
        "my_techs": techs or set(),
    }


# --- 数据加载与分类(TypeTags 的能力混标陷阱) ---


def test_unit_classification_filters_abilities():
    """CLASS_SIEGE 标签下 20+ 个 ABILITY_ —— 分类必须只认 Units 表里的。"""
    siege = [u for u, tags in wp._unit_tags().items() if "CLASS_SIEGE" in tags]
    assert len(siege) == 5
    assert all(u.startswith("UNIT_") for u in siege)
    assert wp.classify_unit("UNIT_CATAPULT") == "siege"
    assert wp.classify_unit("UNIT_ARCHER") == "ranged"
    assert wp.classify_unit("UNIT_WARRIOR") == "melee"
    assert wp.classify_unit("UNIT_KNIGHT") == "cavalry"
    assert wp.classify_unit("UNIT_SPEARMAN") == "anti_cav"
    assert wp.classify_unit("UNIT_BUILDER") == "other"
    assert wp.classify_unit("ABILITY_ANTI_CAVALRY") == "other"


def test_siege_setup_tag_from_export():
    assert wp.needs_setup("UNIT_CATAPULT")
    assert not wp.needs_setup("UNIT_ARCHER")


def test_wall_chain_discovered_dynamically():
    """三级城墙按 Cost 升序:远古(80)→中世纪(225)→文艺复兴(305)。"""
    chain = wp.wall_chain()
    assert [w["cost"] for w in chain] == [80, 225, 305]
    assert chain[0]["name"] == "远古城墙"
    assert chain[2]["tech"] == "TECH_SIEGE_TACTICS"


def test_anti_cavalry_bonus_from_export():
    assert wp.anti_cavalry_bonus() == 10


# --- 分档 ---


def test_red_pure_melee_vs_walls():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_WARRIOR", "UNIT_WARRIOR"],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    assert any(wp.TIER_RED in l for l in out)
    assert any("微量伤害" in l for l in out)


def test_red_hopeless_cs_gap():
    out = wp.assess_attack(
        14,
        30,
        _gs(["UNIT_WARRIOR", "UNIT_WARRIOR"], ["UNIT_MUSKETMAN", "UNIT_MUSKETMAN"]),
    )  # 55 vs 20
    assert any(wp.TIER_RED in l for l in out)


def test_red_single_unit():
    out = wp.assess_attack(14, 30, _gs(["UNIT_SWORDSMAN"], []))
    assert any(wp.TIER_RED in l for l in out)
    assert any("不足 2 个" in l for l in out)


def test_orange_walls_without_siege():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_SWORDSMAN", "UNIT_SWORDSMAN", "UNIT_ARCHER"],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    assert any(wp.TIER_ORANGE in l for l in out)


def test_orange_long_supply_line():
    out = wp.assess_attack(
        30,
        30,
        _gs(
            ["UNIT_SWORDSMAN", "UNIT_SWORDSMAN"],
            ["UNIT_SWORDSMAN", "UNIT_SWORDSMAN"],
            cities=[{"x": 10, "y": 30}],
        ),
    )  # 20 格补给
    assert any(wp.TIER_ORANGE in l for l in out)
    assert any("补给线" in l for l in out)


def test_yellow_full_siege_kit_vs_walls():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            [
                "UNIT_CATAPULT",
                "UNIT_ARCHER",
                "UNIT_ARCHER",
                "UNIT_SWORDSMAN",
                "UNIT_SWORDSMAN",
            ],
            ["UNIT_ARCHER", "UNIT_SPEARMAN"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    assert any(wp.TIER_YELLOW in l for l in out)


def test_green_no_walls_and_overwhelming():
    out = wp.assess_attack(
        14, 30, _gs(["UNIT_SWORDSMAN"] * 3 + ["UNIT_ARCHER"], ["UNIT_WARRIOR"])
    )
    assert any(wp.TIER_GREEN in l for l in out)


def test_broken_walls_count_as_open():
    """墙体 ≤25% 视为已破 —— 满编近战可直接推。"""
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_SWORDSMAN"] * 3 + ["UNIT_ARCHER"],
            ["UNIT_WARRIOR"],
            wall_max=100,
            wall_hp=10,
            wall_b="BUILDING_WALLS",
        ),
    )
    assert any(wp.TIER_GREEN in l for l in out)
    assert any("已残" in l for l in out)


# --- 范围语言(与其他模块的根本区别) ---


def test_all_turn_estimates_are_ranges_not_points():
    for scenario in (
        _gs(["UNIT_SWORDSMAN"] * 3 + ["UNIT_ARCHER"], ["UNIT_WARRIOR"]),
        _gs(
            [
                "UNIT_CATAPULT",
                "UNIT_ARCHER",
                "UNIT_ARCHER",
                "UNIT_SWORDSMAN",
                "UNIT_SWORDSMAN",
            ],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    ):
        text = "\n".join(wp.assess_attack(14, 30, scenario))
        assert "±25%" in text or "通常" in text
        assert "胜率" not in text  # 不报百分数胜率


def test_wall_defense_value_never_invented():
    """城墙防御值不在导出表里 —— 输出禁止出现"古代墙防御 50"这类编造。"""
    out = wp.assess_attack(
        14, 30, _gs(["UNIT_WARRIOR", "UNIT_WARRIOR"], [], 100, 100, "BUILDING_WALLS")
    )
    text = "\n".join(out)
    assert "城墙防御" not in text and "外部防御 50" not in text


def test_unknown_wall_level_stays_conservative():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_SWORDSMAN", "UNIT_SWORDSMAN", "UNIT_ARCHER"],
            [],
            wall_max=100,
            wall_hp=100,
        ),
    )  # 有墙但不知等级
    assert any("等级未知" in l for l in out)


def test_assess_capped_at_12_lines():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_WARRIOR", "UNIT_WARRIOR", "UNIT_SPEARMAN"],
            ["UNIT_KNIGHT", "UNIT_KNIGHT", "UNIT_MUSKETMAN"],
            100,
            100,
            "BUILDING_WALLS",
            cities=[{"x": 40, "y": 30}],
        ),
    )
    assert len(out) <= 12


# --- 替代方案与克制 ---


def test_alternative_names_next_siege_unit_and_tech():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_WARRIOR", "UNIT_WARRIOR"],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    assert any("TECH_ENGINEERING" in l and "石弩" in l for l in out)


def test_alternative_skips_tech_already_owned():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_WARRIOR", "UNIT_WARRIOR"],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
            techs={"TECH_ENGINEERING"},
        ),
    )
    assert any("科技已具备" in l for l in out)


def test_anti_cavalry_counter_is_surfaced_with_export_value():
    out = wp.assess_attack(
        14,
        30,
        _gs(
            ["UNIT_SPEARMAN", "UNIT_SPEARMAN", "UNIT_ARCHER"],
            ["UNIT_KNIGHT", "UNIT_HORSEMAN"],
        ),
    )
    assert any("+10" in l and "抗骑" in l for l in out)


# --- 攻城计划 ---


def test_siege_plan_refused_on_red():
    out = wp.suggest_siege_plan(
        14,
        30,
        _gs(
            ["UNIT_WARRIOR", "UNIT_WARRIOR"],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    assert len(out) == 1
    assert "不出攻城计划" in out[0]


def test_siege_plan_steps_for_yellow():
    out = wp.suggest_siege_plan(
        14,
        30,
        _gs(
            [
                "UNIT_CATAPULT",
                "UNIT_ARCHER",
                "UNIT_ARCHER",
                "UNIT_SWORDSMAN",
                "UNIT_SWORDSMAN",
            ],
            ["UNIT_ARCHER", "UNIT_SPEARMAN"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    text = "\n".join(out)
    assert "第1步" in text and "第4步" in text
    assert "架设" in text  # CLASS_SIEGE_SETUP 导出标签
    assert "近战单位进城占领" in text
    assert "post_war_advice" in text


def test_siege_plan_warns_about_city_counterattack():
    out = wp.suggest_siege_plan(
        14,
        30,
        _gs(
            [
                "UNIT_CATAPULT",
                "UNIT_ARCHER",
                "UNIT_ARCHER",
                "UNIT_SWORDSMAN",
                "UNIT_SWORDSMAN",
            ],
            ["UNIT_ARCHER"],
            100,
            100,
            "BUILDING_WALLS",
        ),
    )
    text = "\n".join(out)
    assert "反击" in text and "别贴墙罚站" in text


def test_siege_plan_skips_wall_step_when_no_walls():
    out = wp.suggest_siege_plan(
        14, 30, _gs(["UNIT_SWORDSMAN"] * 3 + ["UNIT_ARCHER"], ["UNIT_WARRIOR"])
    )
    assert not any("轰墙" in l for l in out)


# --- 战后处理 ---


def test_post_war_keep_when_loyalty_positive():
    out = wp.post_war_advice(
        14,
        30,
        {
            "own_cities": [{"x": 12, "y": 30, "population": 8}],
            "enemy_cities": [{"x": 20, "y": 30, "population": 4}],
        },
    )
    assert any("保留" in l for l in out)
    assert any("下一目标" in l for l in out)


def test_post_war_rescue_when_marginal():
    # 敌城 pop14 距 3 格:10 - 14×(1-3/9) ≈ +0.7,落在 (-8, 2) 救援区间
    out = wp.post_war_advice(
        14,
        30,
        {"own_cities": [], "enemy_cities": [{"x": 17, "y": 30, "population": 14}]},
    )
    assert any("救得回" in l and "总督" in l for l in out)


def test_post_war_raze_when_hopeless():
    out = wp.post_war_advice(
        14,
        30,
        {
            "own_cities": [],
            "enemy_cities": [
                {"x": 16, "y": 30, "population": 20},
                {"x": 14, "y": 32, "population": 20},
            ],
        },
    )
    assert any("夷平" in l for l in out)
    assert any("叛变" in l for l in out)


def test_post_war_marks_unexported_numbers():
    out = wp.post_war_advice(
        14,
        30,
        {"own_cities": [], "enemy_cities": [{"x": 17, "y": 30, "population": 14}]},
    )
    assert any("未导出" in l for l in out)


# --- 健壮性 ---


def test_garbage_input_never_raises():
    assert wp.assess_attack("x", None, None) == []
    assert wp.suggest_siege_plan(0, 0, {"my_units": "垃圾"}) is not None
    assert wp.post_war_advice(0, 0, {"own_cities": "nope"}) is not None
    out = wp.assess_attack(
        14, 30, {"my_units": [{"type": "UNIT_不存在"}, None], "enemy_units": [{}]}
    )
    assert isinstance(out, list)
