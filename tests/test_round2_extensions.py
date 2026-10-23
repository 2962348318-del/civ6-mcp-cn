"""第二轮收尾:任务 S / V / W / X —— 四个追加函数的单元测试。

各自追加在已有模块末尾,所以这里也一并验证"原有 API 没被动过"。
"""

from __future__ import annotations

from civ_mcp import city_state_advisor as csa
from civ_mcp import district_planner as dp
from civ_mcp import policy
from civ_mcp import war_planner as wp


# ===========================================================================
# 任务 S:区域效应共享(district_planner 追加)
# ===========================================================================


def test_s_existing_api_untouched():
    for name in (
        "suggest_specialization",
        "get_district_advice",
        "district_slot_limit",
        "building_chain",
    ):
        assert hasattr(dp, name)


def _city(name, x, y, districts=()):
    return {"name": name, "x": x, "y": y, "districts": list(districts)}


def test_s_cluster_found_within_range():
    clusters = dp.find_area_clusters(
        [_city("罗马", 10, 10), _city("南特", 13, 12), _city("安条克", 8, 14)]
    )
    assert clusters
    hub, covered = clusters[0]
    assert len(covered) == 3


def test_s_distant_city_excluded():
    clusters = dp.find_area_clusters(
        [_city("罗马", 10, 10), _city("南特", 13, 12), _city("远城", 40, 40)]
    )
    _hub, covered = clusters[0]
    assert all(c["name"] != "远城" for c in covered)


def test_s_lone_city_yields_nothing():
    assert dp.find_area_clusters([_city("孤城", 0, 0)]) == []
    assert dp.get_area_effect_advice([_city("孤城", 0, 0)]) == []


def test_s_advice_scales_with_city_count():
    out = dp.get_area_effect_advice(
        [_city("罗马", 10, 10), _city("南特", 13, 12), _city("安条克", 8, 14)]
    )
    text = "\n".join(out)
    assert "3 座城" in text
    assert "+9~+21 产能" in text  # 3×3 ~ 3×7
    assert "娱乐中心" in text


def test_s_existing_industrial_zone_is_noticed():
    out = dp.get_area_effect_advice(
        [_city("罗马", 10, 10, ["DISTRICT_INDUSTRIAL_ZONE"]), _city("南特", 13, 12)]
    )
    assert any("已有工业区" in line for line in out)


def test_s_hansa_counts_as_industrial_zone():
    out = dp.get_area_effect_advice(
        [_city("亚琛", 10, 10, ["DISTRICT_HANSA"]), _city("科隆", 12, 12)]
    )
    assert any("已有工业区" in line for line in out)


def test_s_numbers_marked_unexported():
    out = dp.get_area_effect_advice([_city("A", 0, 0), _city("B", 2, 2)])
    assert any("未导出" in line for line in out)


def test_s_garbage_safe():
    assert dp.get_area_effect_advice(None) == []
    assert dp.get_area_effect_advice([{"name": "无坐标"}]) == []
    assert dp.get_area_effect_advice("垃圾") == []


# ===========================================================================
# 任务 V:政策卡微操(policy 追加)
# ===========================================================================


def test_v_existing_api_untouched():
    for name in (
        "check_policies",
        "render_policy_advice",
        "PolicyContext",
        "PolicyAdvice",
    ):
        assert hasattr(policy, name)


def test_v_card_discovered_from_gamedata():
    """卡名/折扣都是反查出来的,不是硬编码。"""
    card = policy.upgrade_discount_card()
    assert card["policy"] == "POLICY_PROFESSIONAL_ARMY"
    assert card["name"] == "职业军队"
    assert card["civic"] == "CIVIC_MERCENARIES"
    assert card["slot"] == "SLOT_MILITARY"
    assert card["discount"] == 50


def test_v_free_window_is_pure_profit():
    out = policy.get_policy_micro_advice(3, 240, free_swap_available=True)
    text = "\n".join(out)
    assert "省 120 金" in text
    assert "换卡免费" in text and "零成本" in text


def test_v_outside_window_demands_arithmetic():
    out = policy.get_policy_micro_advice(3, 240, free_swap_available=False)
    text = "\n".join(out)
    assert "不在免费换卡窗口" in text
    assert "先算账" in text


def test_v_single_unit_not_worth_it():
    assert policy.get_policy_micro_advice(1, 200, True) == []


def test_v_zero_cost_is_silent():
    assert policy.get_policy_micro_advice(5, 0, True) == []


def test_v_locked_card_only_flags_the_future():
    out = policy.get_policy_micro_advice(3, 240, True, card_unlocked=False)
    assert any("还没解锁" in line and "CIVIC_MERCENARIES" in line for line in out)


def test_v_already_slotted_says_just_upgrade():
    out = policy.get_policy_micro_advice(3, 240, True, card_already_slotted=True)
    assert any("已在槽位上" in line for line in out)


def test_v_garbage_safe():
    assert policy.get_policy_micro_advice("x", None, True) == []


# ===========================================================================
# 任务 W:宗主加成(city_state_advisor 追加)
# ===========================================================================


def test_w_existing_api_untouched():
    for name in (
        "get_city_state_advice",
        "envoys_to_suzerain",
        "next_tier",
        "priority_order",
        "CITY_STATE_YIELD",
    ):
        assert hasattr(csa, name)


def test_w_table_built_from_modifiers():
    """加成表从 178 条 MINOR_CIV_* 修正器反查,不是硬编码。"""
    table = csa.suzerain_bonus_table()
    assert len(table) >= 20
    assert all(isinstance(v, list) and v for v in table.values())


def test_w_known_bonuses_match_export():
    table = csa.suzerain_bonus_table()
    assert table["BRUSSELS"][0][0] == "所有城市奇观建造 +15% 产能"
    assert "所有城市 +15% 科技" in [d for d, _v in table["GENEVA"]]
    assert "所有项目 +20% 产能(含太空船项目)" in [d for d, _v in table["HONG_KONG"]]
    assert "区域效应辐射范围 +3 格(工厂/娱乐中心覆盖更多城)" in [
        d for d, _v in table["TORONTO"]
    ]


def test_w_jerusalem_special_case_captured():
    """耶路撒冷没有 UNIQUE_INFLUENCE 包装,靠前缀兜底也要抓到。"""
    effects = [d for d, _v in csa.suzerain_bonus_table()["JERUSALEM"]]
    assert any("圣城" in d for d in effects)
    assert any("盟友" in d for d in effects)


def test_w_generic_type_bonuses_excluded():
    """按类型分档的通用加成不属于"独有宗主加成",不进表。"""
    table = csa.suzerain_bonus_table()
    for k in ("SCIENTIFIC", "CULTURAL", "TRADE", "INDUSTRIAL"):
        assert k not in table


def test_w_papal_primacy_variants_excluded():
    assert not any("PAPAL" in k for k in csa.suzerain_bonus_table())


def test_w_victory_derived_from_yield():
    """日内瓦的 +15% 科技应为科学路线加星(路线由 YieldType 推出)。"""
    out = csa.get_suzerain_bonus_advice(
        [{"name": "日内瓦", "key": "GENEVA", "type_name": "Scientific"}], "SCIENCE"
    )
    assert any("★" in line for line in out)
    out2 = csa.get_suzerain_bonus_advice(
        [{"name": "日内瓦", "key": "GENEVA"}], "RELIGION"
    )
    assert not any("★" in line for line in out2)


def test_w_matching_by_chinese_name_falls_back_to_type():
    out = csa.get_suzerain_bonus_advice(
        [{"name": "某未知城邦", "type_name": "Trade"}], "SCIENCE"
    )
    assert any("未匹配到导出数据" in line for line in out)
    assert any("金币" in line for line in out)


def test_w_starred_entries_sort_first():
    out = csa.get_suzerain_bonus_advice(
        [{"name": "布鲁塞尔", "key": "BRUSSELS"}, {"name": "香港", "key": "HONG_KONG"}],
        "SCIENCE",
    )
    assert "香港" in out[1]


def test_w_empty_and_garbage_safe():
    assert csa.get_suzerain_bonus_advice([]) == []
    assert csa.get_suzerain_bonus_advice(None) == []
    assert csa.get_suzerain_bonus_advice(["垃圾", None]) == []


# ===========================================================================
# 任务 X:掠夺经济(war_planner 追加)
# ===========================================================================


def test_x_existing_api_untouched():
    for name in (
        "assess_attack",
        "suggest_siege_plan",
        "post_war_advice",
        "classify_unit",
        "wall_chain",
    ):
        assert hasattr(wp, name)


def test_x_tiering_orders_by_value():
    mine = wp.pillage_value({"improvement": "IMPROVEMENT_MINE"})
    farm = wp.pillage_value({"improvement": "IMPROVEMENT_FARM"})
    district = wp.pillage_value({"district": "DISTRICT_CAMPUS"})
    assert district[0] > mine[0] > farm[0]
    assert mine[2] == "矿山" and farm[2] == "农场"
    assert district[2] == "学院"  # 区域名也走导出表,不报英文


def test_x_pillaged_tile_is_worthless():
    assert (
        wp.pillage_value({"improvement": "IMPROVEMENT_MINE", "is_pillaged": True})[0]
        == 0
    )


def test_x_empty_tile_is_worthless():
    assert wp.pillage_value({})[0] == 0
    assert wp.pillage_value(None)[0] == 0


def test_x_unlisted_improvement_defaults_low():
    score, label, name = wp.pillage_value({"improvement": "IMPROVEMENT_FORT"})
    assert score == 1 and label == "低"
    assert name == "堡垒"  # 名字仍从导出表取


def test_x_advice_sorts_and_routes():
    out = wp.get_pillage_advice(
        [
            {"x": 22, "y": 31, "improvement": "IMPROVEMENT_FARM"},
            {"x": 20, "y": 30, "improvement": "IMPROVEMENT_MINE"},
            {"x": 21, "y": 29, "district": "DISTRICT_CAMPUS"},
        ],
        [{"type": "UNIT_HORSEMAN", "x": 18, "y": 30}],
    )
    assert "学院" in out[1]  # 区域排最前
    assert any("路线" in line for line in out)
    assert any("骑手" in line for line in out)


def test_x_mobile_vs_foot_advice_differs():
    tiles = [{"x": 20, "y": 30, "improvement": "IMPROVEMENT_MINE"}]
    horse = wp.get_pillage_advice(tiles, [{"type": "UNIT_HORSEMAN", "x": 18, "y": 30}])
    foot = wp.get_pillage_advice(tiles, [{"type": "UNIT_WARRIOR", "x": 18, "y": 30}])
    assert any("打了就跑" in line for line in horse)
    assert any("耗尽移动力" in line for line in foot)


def test_x_policy_amplifiers_are_named():
    out = wp.get_pillage_advice(
        [{"x": 1, "y": 1, "improvement": "IMPROVEMENT_MINE"}],
        [{"type": "UNIT_HORSEMAN", "x": 0, "y": 0}],
    )
    assert any("翻倍" in line for line in out)


def test_x_never_reports_exact_yields():
    out = wp.get_pillage_advice(
        [{"x": 1, "y": 1, "improvement": "IMPROVEMENT_MINE"}],
        [{"type": "UNIT_HORSEMAN", "x": 0, "y": 0}],
    )
    assert any("未导出" in line and "不报数字" in line for line in out)


def test_x_no_raider_or_no_target_is_silent():
    assert wp.get_pillage_advice([], [{"type": "UNIT_HORSEMAN"}]) == []
    assert wp.get_pillage_advice([{"improvement": "IMPROVEMENT_MINE"}], []) == []
    assert (
        wp.get_pillage_advice(
            [{"improvement": "IMPROVEMENT_MINE"}], [{"type": "UNIT_BUILDER"}]
        )
        == []
    )  # 平民不能掠夺


def test_x_garbage_safe():
    assert wp.get_pillage_advice(None, None) == []
    assert wp.get_pillage_advice(["垃圾"], [None]) == []
