"""2026-08-06 T64 复盘:三个误伤类 bug 的回归测试。

误伤比漏拦更贵:门禁喊过几次狼来了,下一次真狼来时就没人信了。
法国局 T64 实测——建造者门禁误伤 8+ 次(全靠 force 放行,单位始终安然无恙)、
NO_LOS 误报 3 次。

  A. lua/units.py 的 `local lp = {{}}` —— build_units_query 是普通字符串,
     花括号不折叠,下发到 Lua 是 { [1] = {} } 而非空表,参数编组被污染,
     get_units 的 targets 漏报本可攻击的敌人。
  B. 远程攻击失败一律归因 "LOS blocked" —— 目标在迷雾里、本回合已攻击
     都被说成"视线被挡",Agent 于是反复换位置(换了也没用)。
  C. 回合门禁不认"平民在自己城里"和"同格有护卫" —— 而拦截消息恰恰
     让你去做这两件事。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import end_turn as et
from civ_mcp.lua.units import build_attack_unit, build_units_query

# ---------------------------------------------------------------------------
# A. Lua 参数表构造
# ---------------------------------------------------------------------------


def _code_lines(lua: str) -> list[str]:
    """只留 Lua 代码行,滤掉注释 —— 注释里可能正当地提到坏写法本身。"""
    return [ln for ln in lua.splitlines() if not ln.strip().startswith("--")]


def test_units_query_emits_valid_empty_table():
    """普通字符串里的双花括号不会折叠 —— 参数表必须写成单花括号。"""
    lua = build_units_query()
    assert "local lp = {}" in lua
    code = "\n".join(_code_lines(lua))
    assert "{{" not in code, "下发到 Lua 的双花括号是 {[1]={}},会污染参数编组"


def test_attack_path_fstring_still_folds_correctly():
    """对照组:build_attack_unit 是 f-string,双花括号会正常折叠。"""
    lua = build_attack_unit(1, 5, 5)
    assert "local losParams = {}" in lua
    code = "\n".join(_code_lines(lua))
    assert "{{" not in code


# ---------------------------------------------------------------------------
# B. 远程攻击失败的归因分流
# ---------------------------------------------------------------------------


def test_attack_lua_separates_failure_causes():
    """三种原因要分开报,不能一律说成视线被挡。"""
    lua = build_attack_unit(1, 5, 5)
    assert "ERR:NOT_VISIBLE" in lua, "目标在迷雾里 —— 换位置解决不了"
    assert "ERR:ALREADY_ATTACKED" in lua, "本回合已攻击 —— 换位置解决不了"
    assert "ERR:NO_LOS" in lua, "真视线被挡仍要能报"


def test_not_visible_message_says_scout_not_reposition():
    lua = build_attack_unit(1, 5, 5)
    idx = lua.index("ERR:NOT_VISIBLE")
    msg = lua[idx : idx + 200]
    assert "迷雾" in msg
    assert "换位置不解决" in msg


def test_already_attacked_message_teaches_order():
    lua = build_attack_unit(1, 5, 5)
    idx = lua.index("ERR:ALREADY_ATTACKED")
    msg = lua[idx : idx + 200]
    assert "先攻击" in msg


# ---------------------------------------------------------------------------
# C. 回合门禁的两条豁免
# ---------------------------------------------------------------------------


def _civilian(x=10, y=10):
    return NS(
        unit_type="UNIT_BUILDER",
        name="BUILDER",
        x=x,
        y=y,
        moves_remaining=2.0,
        max_moves=2.0,
        build_charges=3,
        combat_strength=0,
    )


def _guard(x=10, y=10, cs=10):
    return NS(
        unit_type="UNIT_SCOUT",
        name="SCOUT",
        x=x,
        y=y,
        moves_remaining=3.0,
        max_moves=3.0,
        build_charges=0,
        combat_strength=cs,
    )


def _threat(x=12, y=10, cs=15, moves=3, rng=1):
    return NS(
        x=x,
        y=y,
        base_moves=moves,
        range=rng,
        unit_type="UNIT_BARBARIAN_HORSEMAN",
        owner_name="Barbarian",
        combat_strength=cs,
    )


def _city(x=10, y=10):
    return NS(name="巴黎", currently_building="BUILDING_MONUMENT", x=x, y=y)


def _endangered(blockers):
    return [b for b in blockers if "打击范围内" in b]


def test_civilian_in_own_city_is_not_endangered():
    """拦截消息让你"撤进城" —— 撤了就必须认。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(10, 10)], [_threat()], [_city(10, 10)]
    )
    assert _endangered(blockers) == [], "站在自己城里的平民不该报危险"


def test_civilian_outside_city_still_endangered():
    """豁免不能过头:城在别处时照拦。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(10, 10)], [_threat()], [_city(40, 40)]
    )
    assert _endangered(blockers), "不在城里就该照常报危险"


def test_strong_same_tile_escort_clears_danger():
    """拦截消息让你"调一个军事单位同格护卫" —— 照做了就必须认。
    侦察兵 CS10 vs 蛮族骑手 CS15:10×2 >= 15,判够强(与 safety 同一条比)。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(10, 10), _guard(10, 10, cs=10)], [_threat(cs=15)], []
    )
    assert _endangered(blockers) == []


def test_weak_escort_reported_as_weak_not_unguarded():
    """护卫太弱仍要拦,但要说清是"挡不住"而不是"没人管"。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(10, 10), _guard(10, 10, cs=5)], [_threat(cs=40)], []
    )
    danger = _endangered(blockers)
    assert danger, "护卫不够强时仍要拦"
    assert "挡不住" in danger[0]


def test_adjacent_escort_does_not_count():
    """相邻不算护卫 —— 敌人会绕过旁边的护卫直接抓平民。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(10, 10), _guard(11, 10, cs=40)], [_threat(cs=15)], []
    )
    assert _endangered(blockers), "护卫在隔壁格不算数"


def test_city_without_coordinates_does_not_falsely_exempt():
    """坐标缺失的城不参与豁免 —— 不能把 (-1,-1) 当成真城格。"""
    stale = NS(name="旧快照城", currently_building="X", x=-1, y=-1)
    blockers, _w = et.check_turn_completion([_civilian(10, 10)], [_threat()], [stale])
    assert _endangered(blockers), "没有坐标就不该豁免"


def test_threat_reach_now_carries_combat_strength():
    """护卫强弱比需要威胁的 CS —— _threat_reach 要带回来。"""
    r = et._threat_reach(_threat(cs=25))
    assert r is not None
    assert len(r) == 5 and r[4] == 25


# ---------------------------------------------------------------------------
# D. 海军威胁的可达范围(船开不上陆地)
# ---------------------------------------------------------------------------


def _naval(x=12, y=10, rng=1, moves=4, coastal=True):
    return NS(
        x=x,
        y=y,
        base_moves=moves,
        range=rng,
        unit_type="UNIT_GALLEY",
        owner_name="Barbarian",
        combat_strength=20,
        domain="DOMAIN_SEA",
        is_naval=True,
        has_coastal_city=coastal,
        city_distance=3,
    )


def test_naval_reach_excludes_movement():
    """船的移动力只在水面有意义 —— 按 moves+range 算会把内陆一片标成危险。"""
    r = et._threat_reach(_naval(rng=2, moves=4))
    assert r is not None
    assert r[2] == 2, "海军可达应取射程,不是 moves+range"
    assert "海军" in r[3]


def test_naval_melee_reach_is_one():
    r = et._threat_reach(_naval(rng=0, moves=4))
    assert r is not None and r[2] == 1


def test_naval_ignored_without_coastal_city():
    """我方没有沿海城市 —— 船够不着任何东西。"""
    assert et._threat_reach(_naval(coastal=False)) is None


def test_land_threat_reach_unchanged():
    """陆地威胁的口径不变。"""
    r = et._threat_reach(_threat(moves=3, rng=1))
    assert r is not None and r[2] == 4


def test_inland_civilian_not_flagged_by_coastal_ship():
    """回归实景:沿岸游走的船不该让内陆建造者被拦。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(10, 10)], [_naval(x=14, y=10, rng=1, moves=4)], []
    )
    assert _endangered(blockers) == [], "距 4 格的船射程 1,够不到内陆平民"


# ---------------------------------------------------------------------------
# E. 距离铁律:硬拦截路径不许用 Map.GetPlotDistance
# ---------------------------------------------------------------------------


def test_attack_path_uses_hex_distance():
    """OUT_OF_RANGE 是硬拦截,误报一格就挡掉合法攻击。"""
    lua = build_attack_unit(1, 5, 5)
    assert "local function hexDist" in lua, "必须注入 hexDist"
    assert "hexDist(ux, uy," in lua
    code = _code_lines(lua)
    assert not [ln for ln in code if "Map.GetPlotDistance" in ln], (
        "_helpers.py 铁律:一切距离判定用 hexDist,GetPlotDistance 在 offset "
        "网格上有实测误报"
    )


def test_units_query_target_scan_uses_hex_distance():
    """目标列表误报会让本可攻击的敌人消失。"""
    lua = build_units_query()
    assert "local function hexDist" in lua
    assert "hexDist(x, y, tx, ty)" in lua
    code = _code_lines(lua)
    assert not [ln for ln in code if "Map.GetPlotDistance" in ln]


# ---------------------------------------------------------------------------
# F. 坐标 0 是合法值(2026-08-06 自查)
# ---------------------------------------------------------------------------


def test_zero_coordinate_survives_normalization():
    """`int(v or -1)` 会把合法坐标 0 变成 -1 —— 而 -1 表示"无坐标,
    不参与豁免",于是 x=0 的城里的平民会被误报危险。"""
    from civ_mcp.game_state import _coord

    assert _coord(0) == 0
    assert _coord(None) == -1
    assert _coord("bad") == -1
    assert _coord(12) == 12


def test_civilian_in_city_at_origin_is_exempt():
    """回归实景:地图边缘 (0,0) 的城同样要触发"在自己城里"的豁免。"""
    blockers, _w = et.check_turn_completion(
        [_civilian(0, 0)], [_threat(x=2, y=0)], [_city(0, 0)]
    )
    assert _endangered(blockers) == [], "坐标 0 的城不能被当成没有坐标"
