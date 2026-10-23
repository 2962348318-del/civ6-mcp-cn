"""任务 M 铺城位置深度评估(world_model 追加部分)—— 单元测试。"""

from __future__ import annotations

from civ_mcp import world_model as wm


def _city(x, y, pop):
    return {"x": x, "y": y, "population": pop}


# --- 忠诚度公式(计划书给定) ---


def test_loyalty_formula_matches_plan():
    """计划书场景:敌方 7 人口城 5 格外,己方无城 → 10 - 7×(1-5/9) ≈ +6.9;
    公式逐字实现,含 +10 基础项。"""
    net = wm.loyalty_pressure(0, 0, [], [_city(5, 0, 7)])
    assert abs(net - (10 - 7 * (1 - 5 / 9))) < 1e-9


def test_loyalty_range_cutoff_at_nine():
    assert wm.loyalty_pressure(0, 0, [], [_city(9, 0, 20)]) == 10.0
    assert wm.loyalty_pressure(0, 0, [], [_city(10, 0, 20)]) == 10.0
    assert wm.loyalty_pressure(0, 0, [], [_city(8, 0, 9)]) < 10.0


def test_loyalty_own_cities_push_positive():
    # hex 距离: (0,0)->(3,0) = 3, (0,0)->(0,3) = 3 (同距对消)
    net = wm.loyalty_pressure(0, 0, [_city(3, 0, 10)], [_city(0, 3, 10)])
    assert abs(net - 10.0) < 1e-9  # 同距同人口对消,剩基础 +10


def test_loyalty_negative_when_surrounded():
    net = wm.loyalty_pressure(0, 0, [], [_city(2, 0, 12), _city(0, 2, 12)])
    assert net < 0


# --- 六维评估 ---


def _tiles(*specs):
    out = []
    for x, y, kw in specs:
        t = {"x": x, "y": y, "terrain": "TERRAIN_GRASS"}
        t.update(kw)
        out.append(t)
    return out


def test_flip_warning_with_turn_estimate():
    out = wm.evaluate_settle_location(
        0, 0, [], enemy_cities=[_city(2, 0, 12), _city(0, 2, 12)]
    )
    assert any("叛变" in line and "回合" in line for line in out)
    assert out[0].startswith("== 铺城深评")


def test_borderline_loyalty_is_flagged():
    out = wm.evaluate_settle_location(0, 0, [], enemy_cities=[_city(4, 0, 12)])
    assert any("及格线" in line for line in out)


def test_strategic_resource_dimension():
    tiles = _tiles((2, 0, {"resource": "RESOURCE_IRON", "resource_class": "strategic"}))
    out = wm.evaluate_settle_location(0, 0, tiles)
    assert any("战略资源" in line and "RESOURCE_IRON" in line for line in out)


def test_luxury_dimension_only_counts_new_types():
    tiles = _tiles(
        (1, 0, {"resource": "RESOURCE_SILK", "resource_class": "luxury"}),
        (2, 0, {"resource": "RESOURCE_TEA", "resource_class": "luxury"}),
    )
    out = wm.evaluate_settle_location(0, 0, tiles, owned_luxuries=("RESOURCE_SILK",))
    line = next(l for l in out if "奢侈覆盖" in l)
    assert "RESOURCE_TEA" in line and "RESOURCE_SILK" not in line


def test_defense_terrain_dimension():
    tiles = _tiles(
        (1, 0, {"terrain": "TERRAIN_GRASS_MOUNTAIN"}),
        (0, 1, {"terrain": "TERRAIN_GRASS_MOUNTAIN"}),
        (-1, 0, {"is_river": True}),
    )
    out = wm.evaluate_settle_location(0, 0, tiles)
    assert any("防御地形" in line and "贴山" in line for line in out)


def test_chokepoint_dimension():
    tiles = [
        {"x": 2, "y": dy, "terrain": "TERRAIN_GRASS_MOUNTAIN"} for dy in range(-2, 3)
    ] + [{"x": -2, "y": dy, "terrain": "TERRAIN_OCEAN"} for dy in range(-2, 3)]
    out = wm.evaluate_settle_location(0, 0, tiles)
    assert any("扼守" in line and "走廊" in line for line in out)


def test_forward_settle_dimension():
    out = wm.evaluate_settle_location(
        0, 0, [], own_cities=[_city(8, 0, 5)], enemy_cities=[_city(4, 0, 3)]
    )
    assert any("前哨" in line for line in out)


def test_forward_settle_with_bad_loyalty_is_called_out():
    out = wm.evaluate_settle_location(
        0,
        0,
        [],
        own_cities=[_city(8, 0, 2)],
        enemy_cities=[_city(2, 0, 14), _city(0, 2, 14)],
    )
    line = next(l for l in out if "前哨" in l)
    assert "卡位变送城" in line


def test_no_dimensions_is_silent():
    assert wm.evaluate_settle_location(0, 0, _tiles((1, 0, {}))) == []
    assert wm.evaluate_settle_location(0, 0, []) == []


def test_garbage_input_never_raises():
    assert (
        wm.evaluate_settle_location(
            "x", 0, [{"bad": 1}, None], own_cities=[{"x": "?"}], enemy_cities="nope"
        )
        == []
    )
    assert wm.evaluate_settle_location(0, 0, None) == []


def test_output_capped():
    tiles = _tiles(
        (2, 0, {"resource": "RESOURCE_IRON", "resource_class": "strategic"}),
        (1, 0, {"resource": "RESOURCE_TEA", "resource_class": "luxury"}),
        (0, 1, {"terrain": "TERRAIN_GRASS_MOUNTAIN"}),
        (-1, 0, {"terrain": "TERRAIN_GRASS_MOUNTAIN"}),
        (1, 1, {"is_river": True}),
    ) + [{"x": 2, "y": dy, "terrain": "TERRAIN_OCEAN"} for dy in range(-2, 3)]
    out = wm.evaluate_settle_location(
        0,
        0,
        tiles,
        own_cities=[_city(8, 0, 5)],
        enemy_cities=[_city(4, 0, 10)],
        max_items=2,
    )
    assert any("已省略" in line for line in out)
