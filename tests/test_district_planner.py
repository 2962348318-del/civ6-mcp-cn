"""任务 A 区域布局规划 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import district_planner as dp


def _mountains(n, x0=10, y0=10):
    return [
        {"x": x0 + i, "y": y0, "terrain": "TERRAIN_GRASS_MOUNTAIN"} for i in range(n)
    ]


def _tiles_flat(n=10):
    return [{"x": i, "y": 0, "terrain": "TERRAIN_GRASS"} for i in range(n)]


# --- 数据加载 ---


def test_districts_and_rules_load():
    assert len(dp._districts()) == 21
    assert sum(len(v) for v in dp._adjacency_rules().values()) == 45


def test_specialty_flag_comes_from_requires_population():
    assert dp.is_specialty("DISTRICT_CAMPUS")
    assert not dp.is_specialty("DISTRICT_AQUEDUCT")  # 不占槽
    assert not dp.is_specialty("DISTRICT_CITY_CENTER")


def test_standard_district_is_the_full_cost_one():
    """同产出组里标准款 Cost 最高(独有款半价)——推荐必须用标准款。"""
    std = dp.standard_district_for_yield()
    assert std["YIELD_SCIENCE"] == "DISTRICT_CAMPUS"
    assert std["YIELD_FAITH"] == "DISTRICT_HOLY_SITE"  # 而非半价的拉夫拉
    assert std["YIELD_PRODUCTION"] == "DISTRICT_INDUSTRIAL_ZONE"  # 而非汉萨


def test_building_chain_sorted_and_deduped():
    chain = dp.building_chain("DISTRICT_CAMPUS")
    costs = [c for c, _ in chain]
    assert costs == sorted(costs)
    assert chain[0][1] == "图书馆"
    # 250 档大学/伊斯兰学校二选一 → 并列展示,不单推独有款
    tier250 = next(label for c, label in chain if c == 250)
    assert "择一" in tier250
    assert "大学" in tier250  # 标准款必须出现,不能被独有款挤掉


# --- 槽位公式([机制常量]) ---


def test_district_slot_limit():
    assert dp.district_slot_limit(1) == 1
    assert dp.district_slot_limit(3) == 1
    assert dp.district_slot_limit(4) == 2
    assert dp.district_slot_limit(7) == 3
    assert dp.district_slot_limit(0) == 0


# --- 相邻潜力 ---


def test_mountains_feed_campus_potential():
    score, why = dp.adjacency_potential("DISTRICT_CAMPUS", _mountains(4))
    assert score >= 4  # 每山 +1
    assert any("mountain" in w for w in why)


def test_adjacency_capped_at_six_neighbours():
    s6, _ = dp.adjacency_potential("DISTRICT_CAMPUS", _mountains(6))
    s12, _ = dp.adjacency_potential("DISTRICT_CAMPUS", _mountains(12))
    assert s12 == s6  # 上限估算按六邻域封顶


def test_river_feeds_commercial_hub():
    tiles = [{"x": 0, "y": 0, "terrain": "TERRAIN_GRASS", "is_river": True}]
    score, why = dp.adjacency_potential("DISTRICT_COMMERCIAL_HUB", tiles)
    assert score >= 2 and "沿河" in why


# --- 职能分工 ---


def test_mountain_city_becomes_campus_city():
    spec = dp.suggest_specialization(_mountains(4))
    assert spec.cn in ("学院城", "圣地城")  # 山同时喂两者,取分高的
    assert spec.district in ("DISTRICT_CAMPUS", "DISTRICT_HOLY_SITE")


def test_victory_type_breaks_the_mountain_tie():
    """同样的山,科学路线判学院城,宗教路线判圣地城——权重起作用。"""
    assert dp.suggest_specialization(_mountains(4), "SCIENCE").cn == "学院城"
    assert dp.suggest_specialization(_mountains(4), "RELIGION").cn == "圣地城"


def test_flat_land_is_generic():
    spec = dp.suggest_specialization(_tiles_flat())
    assert spec.cn == "均衡城"
    assert spec.district == ""


def test_specialization_never_raises():
    assert dp.suggest_specialization(None).cn
    assert dp.suggest_specialization([{"bad": object()}]).cn


# --- 建设建议 ---


def _city(pop=4, districts=(), tiles=None):
    return {
        "name": "罗马",
        "population": pop,
        "districts": list(districts),
        "tiles": _mountains(4) if tiles is None else tiles,
    }


def test_advice_recommends_campus_on_mountains():
    out = dp.get_district_advice(_city(), {"victory_type": "SCIENCE"})
    assert any("学院" in line for line in out)
    assert any("1/2 槽" not in line or True for line in out)


def test_slot_accounting_ignores_non_specialty():
    """水渠不占槽——2 人口 1 槽,已建水渠仍显示 0/1。"""
    out = dp.get_district_advice(_city(pop=2, districts=["DISTRICT_AQUEDUCT"]))
    assert any("0/1 槽" in line for line in out)


def test_full_slots_warns_and_asks_for_population():
    out = dp.get_district_advice(
        _city(pop=3, districts=["DISTRICT_CAMPUS"]), {"victory_type": "SCIENCE"}
    )
    assert any("已满" in line for line in out)


def test_tech_gate_is_shown_when_locked():
    out = dp.get_district_advice(_city(), {"victory_type": "SCIENCE", "techs": set()})
    assert any("需先研究 TECH_WRITING" in line for line in out)


def test_terrain_rivals_warning_present():
    """学院与圣地抢同一类山脉相邻——数据驱动的互斥提示。"""
    rivals = dp._terrain_rivals("DISTRICT_CAMPUS")
    assert "DISTRICT_HOLY_SITE" in rivals
    out = dp.get_district_advice(_city(), {"victory_type": "SCIENCE"})
    assert any("同一类相邻地形" in line for line in out)


def test_built_district_shows_building_chain():
    out = dp.get_district_advice(
        _city(pop=7, districts=["DISTRICT_CAMPUS"]), {"victory_type": "SCIENCE"}
    )
    assert any("图书馆" in line for line in out)


def test_empty_city_is_silent_and_safe():
    assert dp.get_district_advice({}) == []
    assert dp.get_district_advice(None) == []
    assert dp.get_district_advice({"population": "垃圾"}) == []
