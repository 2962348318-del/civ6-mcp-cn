"""hex 距离 —— 全仓库唯一权威距离函数。

Civ6 使用 odd-r offset 坐标(奇数行右移)。此前 war_planner._dist、
world_model._settle_dist 等全部用 Chebyshev(max(|dx|,|dy|)),
在斜对角上系统性算短(2 格对角报 2,实际 3)。方案 v5 P0-a:
hex 距离全仓库替换 + 测试。
"""

from civ_mcp.hexgrid import hex_distance, hex_neighbors


def test_adjacent_east_west():
    assert hex_distance(5, 5, 6, 5) == 1  # E
    assert hex_distance(5, 5, 4, 5) == 1  # W


def test_adjacent_vertical_even_row():
    # 偶数行: 正南/正北邻居仍是同列 (odd-r 只在奇数行右移)
    assert hex_distance(5, 4, 5, 5) == 1  # S (even row -> odd row)
    assert hex_distance(5, 5, 5, 4) == 1  # N


def test_adjacent_diagonal_odd_row():
    # 奇数行右移: (5,5) 的 SE 是 (6,6), SW 是 (5,6)
    assert hex_distance(5, 5, 6, 6) == 1
    assert hex_distance(5, 5, 5, 6) == 1
    # 偶数行: (5,4) 的 SE 是 (5,5), SW 是 (4,5)
    assert hex_distance(5, 4, 5, 5) == 1
    assert hex_distance(5, 4, 4, 5) == 1


def test_hex_vs_chebyshev_diagonal():
    # Chebyshev 认为 (0,0)->(2,2) 是 2; hex 距离实际是 3
    assert hex_distance(0, 0, 2, 2) == 3
    assert max(abs(0 - 2), abs(0 - 2)) == 2


def test_known_grid_offsets():
    # 同排: 距离 = x 差
    assert hex_distance(0, 0, 4, 0) == 4
    # 相邻两排斜向: (0,0)->(1,1) 经 (0,1) 中转 = 2
    assert hex_distance(0, 0, 1, 1) == 2


def test_symmetry():
    for a, b in [((3, 7), (9, 2)), ((0, 0), (1, 1)), ((12, 30), (5, 8))]:
        assert hex_distance(*a, *b) == hex_distance(*b, *a)


def test_string_coords_lua_style():
    # Lua 输出坐标以字符串到达, 必须能直接吃
    assert hex_distance("9", "24", "9", "26") == 2  # 正南两格


def test_malformed_input_far():
    assert hex_distance(None, 1, 2, 3) == 999
    assert hex_distance(1, "x", 2, 3) == 999


def test_neighbors_even_row():
    got = set(hex_neighbors(5, 4))
    assert got == {(6, 4), (5, 3), (4, 3), (4, 4), (4, 5), (5, 5)}
    for n in got:
        assert hex_distance(5, 4, *n) == 1


def test_neighbors_odd_row():
    got = set(hex_neighbors(5, 5))
    assert got == {(6, 5), (6, 4), (5, 4), (4, 5), (5, 6), (6, 6)}
    for n in got:
        assert hex_distance(5, 5, *n) == 1
