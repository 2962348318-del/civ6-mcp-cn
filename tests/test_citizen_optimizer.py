"""任务 L 公民地块分配优化 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import citizen_optimizer as co


def _tile(x, y, f=0, p=0, g=0, s=0, c=0, fa=0, worked=False, locked=False):
    return co.Tile(x, y, (f, p, g, s, c, fa), worked=worked, locked=locked)


def test_yield_index_matches_tileinfo_order():
    """产出索引必须和 lua/models.py 的 TileInfo.yields 一致,错位就是全盘错。"""
    assert (co.FOOD, co.PROD, co.GOLD, co.SCIENCE, co.CULTURE, co.FAITH) == (
        0,
        1,
        2,
        3,
        4,
        5,
    )


def test_obvious_swap_is_found():
    """计划书里的原始场景:公民站 1粮1锤,旁边 2粮2锤 闲着。"""
    city = co.CityCitizens(
        "罗马",
        [
            _tile(16, 35, f=1, p=1, worked=True),
            _tile(14, 34, f=2, p=2),
        ],
    )
    out = co.get_citizen_advice([city])
    assert out
    assert "罗马" in out[0]
    assert "(14,34)" in out[0]


def test_small_gain_stays_silent():
    """差距 < 15% 不提醒 —— 每回合为 5% 收益刷屏会淹没重要信号。"""
    city = co.CityCitizens(
        "罗马",
        [
            _tile(1, 1, f=3, p=3, worked=True),
            _tile(2, 2, f=2, p=2, worked=True),
            _tile(3, 3, f=2, p=3),
        ],
    )
    current, best, _ = co.optimize_city(city)
    assert (best - current) / current < co.MIN_GAIN_RATIO
    assert co.get_citizen_advice([city]) == []


def test_already_optimal_is_silent():
    city = co.CityCitizens(
        "罗马",
        [
            _tile(1, 1, f=4, p=4, worked=True),
            _tile(2, 2, f=1, p=1),
        ],
    )
    assert co.get_citizen_advice([city]) == []


def test_science_victory_prefers_science_tile():
    """同样的裸产出,科学胜利下科技格应该胜出。"""
    city = co.CityCitizens(
        "罗马",
        [
            _tile(1, 1, p=3, worked=True),
            _tile(2, 2, s=3),
        ],
    )
    assert co.get_citizen_advice([city], victory_type="SCIENCE")
    # 统治路线下产能翻倍,同一组地块反而不该动
    assert co.get_citizen_advice([city], victory_type="DOMINATION") == []


def test_locked_tile_is_never_moved():
    city = co.CityCitizens(
        "罗马",
        [
            _tile(1, 1, f=1, worked=True, locked=True),
            _tile(2, 2, f=9),
        ],
    )
    _, _, swaps = co.optimize_city(city)
    assert swaps == []


def test_city_with_no_worked_tiles_is_silent():
    city = co.CityCitizens("空城", [_tile(1, 1, f=5)])
    assert co.get_citizen_advice([city]) == []


def test_empty_input_returns_empty():
    assert co.get_citizen_advice([]) == []


def test_advice_is_capped_and_flags_omissions():
    cities = [
        co.CityCitizens(
            f"城{i}",
            [
                _tile(1, 1, f=1, worked=True),
                _tile(2, 2, f=8),
            ],
        )
        for i in range(5)
    ]
    out = co.get_citizen_advice(cities, max_cities=3)
    assert any("已省略" in line for line in out)


def test_tile_brief_skips_zero_yields():
    assert _tile(0, 0, f=2, p=2).brief() == "2粮2锤"
    assert _tile(0, 0).brief() == "无产出"


def test_unknown_victory_type_falls_back_to_flat_weights():
    city = co.CityCitizens("罗马", [_tile(1, 1, f=1, worked=True), _tile(2, 2, f=2)])
    a = co.optimize_city(city, "BOGUS")
    b = co.optimize_city(city, None)
    assert a[:2] == b[:2]
