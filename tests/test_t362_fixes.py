"""T362 弃局复盘落地的两条门禁 —— 回归测试。

复盘原文见 docs/postmortem/最终复盘_T362_重开建议.md:
  · 战争前置条件"近战占城单位 ≥ 目标城墙数" → NO_CITY_TAKERS
  · 经济纪律"买单位前算维护费,军事维护 ≤ 60% 收入" → UPKEEP_CEILING

两条都是这局的直接死因:
  T362 军队 = 7 加农炮 + 6 弩手 + 只有 2 个线列步兵 → 一座城都没打下来;
  T361 信仰买 5 门加农炮 → T362 维护 -51/回合破产 → 又解散 4 门。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from civ_mcp import war_planner as wp
from civ_mcp.game_state import GameState

# ---------------------------------------------------------------------------
# 占城单位判定(真实 gamedata)
# ---------------------------------------------------------------------------


def test_only_land_melee_can_capture():
    for ut in (
        "UNIT_WARRIOR",
        "UNIT_MUSKETMAN",
        "UNIT_ROMAN_LEGION",
        "UNIT_KNIGHT",
        "UNIT_PIKEMAN",
        "UNIT_INFANTRY",
    ):
        assert wp.can_capture_city(ut), ut


def test_ranged_siege_naval_air_cannot_capture():
    """⚠️ 这些单位的 Units.CanCapture 字段是 true(那是"能否俘获平民"),
    但它们占不了城 —— 判定必须走兵种,不能走那个字段。"""
    for ut in (
        "UNIT_FIELD_CANNON",
        "UNIT_CROSSBOWMAN",
        "UNIT_CATAPULT",
        "UNIT_ARTILLERY",
        "UNIT_BOMBER",
        "UNIT_FRIGATE",
        "UNIT_BATTLESHIP",
    ):
        assert not wp.can_capture_city(ut), ut


def test_civilians_cannot_capture():
    assert not wp.can_capture_city("UNIT_SETTLER")
    assert not wp.can_capture_city("UNIT_BUILDER")
    assert not wp.can_capture_city("")


def test_t362_army_shape():
    """复盘里那支军队:破墙够、占城严重不足。"""
    army = (
        ["UNIT_FIELD_CANNON"] * 7
        + ["UNIT_LINE_INFANTRY"] * 2
        + ["UNIT_CROSSBOWMAN"] * 6
    )
    assert sum(1 for u in army if wp.can_break_walls(u)) == 7
    assert sum(1 for u in army if wp.can_capture_city(u)) == 2


# ---------------------------------------------------------------------------
# NO_CITY_TAKERS 门禁
# ---------------------------------------------------------------------------


class _ExplodingConn:
    async def execute_write(self, lua):
        raise AssertionError("blocked declaration must not reach the game")


def _gs(unit_types, walls=True, n_cities=1):
    gs = GameState.__new__(GameState)
    gs.conn = _ExplodingConn()
    cities = [NS(name=f"城{i}", population=6, has_walls=walls) for i in range(n_cities)]

    async def _civs():
        return [NS(player_id=3, civ_name="阿拉伯", visible_cities=cities)]

    async def _units():
        return [NS(unit_type=t) for t in unit_types]

    gs.get_diplomacy = _civs
    gs.get_units = _units
    return gs


def test_no_city_takers_blocks_declaration():
    """T362 阵容:破墙 7 个但零近战 —— 必须拦。"""
    gs = _gs(["UNIT_FIELD_CANNON"] * 7 + ["UNIT_CROSSBOWMAN"] * 6)
    block, _adv = asyncio.run(gs._pre_war_gate(3))
    assert block.startswith("BLOCKED: NO_CITY_TAKERS")
    assert "陆地近战系" in block and "force=True" in block


def test_takers_present_passes():
    gs = _gs(["UNIT_FIELD_CANNON"] * 3 + ["UNIT_MUSKETMAN"] * 2)
    block, _adv = asyncio.run(gs._pre_war_gate(3))
    assert block == ""


def test_takers_fewer_than_walled_cities_warns_not_blocks():
    """3 座带墙城 vs 1 个近战:放行,但提醒集火一座。"""
    gs = _gs(["UNIT_FIELD_CANNON"] * 4 + ["UNIT_MUSKETMAN"], walls=True, n_cities=3)
    block, adv = asyncio.run(gs._pre_war_gate(3))
    assert block == ""
    assert "集火" in adv and "3" in adv


def test_no_takers_checked_before_wall_breakers():
    """既无近战也无破墙时,先报占城缺失 —— 那是更根本的缺口。"""
    gs = _gs(["UNIT_CROSSBOWMAN"] * 5)
    block, _adv = asyncio.run(gs._pre_war_gate(3))
    assert block.startswith("BLOCKED: NO_CITY_TAKERS")


def test_unwalled_target_still_needs_takers():
    gs = _gs(["UNIT_FIELD_CANNON"] * 3, walls=False)
    block, _adv = asyncio.run(gs._pre_war_gate(3))
    assert block.startswith("BLOCKED: NO_CITY_TAKERS")


# ---------------------------------------------------------------------------
# UPKEEP_CEILING 门禁
# ---------------------------------------------------------------------------


def test_unit_maintenance_from_gamedata():
    assert wp.unit_maintenance("UNIT_FIELD_CANNON") == 5
    assert wp.unit_maintenance("UNIT_CROSSBOWMAN") == 3
    assert wp.unit_maintenance("UNIT_SETTLER") == 0


def _buy_gs(income, unit_upkeep, gold=500.0, gpt=5.0):
    gs = GameState.__new__(GameState)
    gs._last_overview = NS(
        gold_income=income,
        unit_maintenance=unit_upkeep,
        gold=gold,
        gold_per_turn=gpt,
    )
    return gs


def test_upkeep_gate_blocks_when_over_ceiling():
    """T361 形态:收入 90、军事维护已 52,再买 5 维护的炮 → 63% 超线。"""
    gs = _buy_gs(income=90.0, unit_upkeep=52.0)
    msg = asyncio.run(gs._upkeep_gate("UNIT_FIELD_CANNON"))
    assert msg.startswith("BLOCKED: UPKEEP_CEILING")
    assert "63%" in msg and "force=True" in msg
    assert "升级现有单位" in msg  # 给出更划算的替代方案


def test_upkeep_gate_allows_under_ceiling():
    gs = _buy_gs(income=200.0, unit_upkeep=40.0)
    assert asyncio.run(gs._upkeep_gate("UNIT_FIELD_CANNON")) == ""


def test_upkeep_gate_ignores_zero_maintenance_units():
    gs = _buy_gs(income=10.0, unit_upkeep=9.0)
    assert asyncio.run(gs._upkeep_gate("UNIT_SETTLER")) == ""


def test_upkeep_gate_fails_open_without_income_data():
    gs = _buy_gs(income=0.0, unit_upkeep=50.0)
    assert asyncio.run(gs._upkeep_gate("UNIT_FIELD_CANNON")) == ""


def test_purchase_building_skips_gate():
    """只拦军事单位 —— 买建筑不该被维护费门禁挡住。"""
    gs = _buy_gs(income=90.0, unit_upkeep=52.0)

    class _OkConn:
        async def execute_write(self, lua):
            return ["OK:PURCHASED", "SENTINEL"]

    gs.conn = _OkConn()
    result = asyncio.run(gs.purchase_item(65536, "BUILDING", "BUILDING_GRANARY"))
    assert "BLOCKED" not in result


def test_purchase_unit_force_bypasses_gate():
    gs = _buy_gs(income=90.0, unit_upkeep=52.0)

    class _OkConn:
        async def execute_write(self, lua):
            return ["OK:PURCHASED", "SENTINEL"]

    gs.conn = _OkConn()
    result = asyncio.run(
        gs.purchase_item(65536, "UNIT", "UNIT_FIELD_CANNON", force=True)
    )
    assert "BLOCKED" not in result
