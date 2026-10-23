"""2026-08-05 第二轮:作战不只看纸面数值 + 三模块接线 + 联合宣战受邀门禁。

用户实战总结:"科技一落后再多军队也没用 —— 科技是第一生产力。"
T335 实证:军力 1137"全场第一"拆开是 13 个中世纪弩手打现代战争;
一架敌战斗机压制整个后方而军力总分完全无感。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from civ_mcp import end_turn as et
from civ_mcp import event_window
from civ_mcp import power_balance as pb
from civ_mcp import war_planner as wp
from civ_mcp.game_state import GameState
from civ_mcp.lua.diplomacy import build_pending_deals_query

# ---------------------------------------------------------------------------
# 时代阶梯与军队画像(war_planner,真实 gamedata)
# ---------------------------------------------------------------------------


def test_era_ladder_and_unit_eras():
    assert wp.era_index("ERA_ANCIENT") == 1
    assert wp.era_index("ERA_MODERN") == 6
    assert wp.unit_era("UNIT_WARRIOR")[0] == 1
    assert wp.unit_era("UNIT_CROSSBOWMAN") == (3, "ERA_MEDIEVAL")
    assert wp.unit_era("UNIT_FIELD_CANNON")[0] == 5
    assert wp.unit_era("UNIT_INFANTRY")[0] == 6


def test_t335_army_profile_is_medieval():
    prof = wp.army_era_profile(
        ["UNIT_CROSSBOWMAN"] * 13 + ["UNIT_FIELD_CANNON"] * 4 + ["UNIT_MUSKETMAN"] * 3
    )
    assert prof is not None
    assert prof["median"] == 3 and prof["total"] == 20
    assert prof["lagging"][0] == (3, 13)  # 主力 = 13 个中世纪单位


def test_aircraft_and_anti_air_classification():
    assert wp.is_aircraft("UNIT_FIGHTER") and wp.is_aircraft("UNIT_BOMBER")
    assert not wp.is_aircraft("UNIT_CROSSBOWMAN")
    for ut in (
        "UNIT_ANTIAIR_GUN",
        "UNIT_MOBILE_SAM",
        "UNIT_BATTLESHIP",
        "UNIT_FIGHTER",
    ):
        assert wp.can_anti_air(ut), ut
    assert not wp.can_anti_air("UNIT_BOMBER")  # 轰炸机不拦截
    assert not wp.can_anti_air("UNIT_CROSSBOWMAN")


# ---------------------------------------------------------------------------
# ARMY OBSOLETE 预警(end_turn 纯函数)
# ---------------------------------------------------------------------------


def _units_t335():
    mk = lambda ut: NS(unit_type=ut, combat_strength=30)  # noqa: E731
    return [mk("UNIT_CROSSBOWMAN")] * 13 + [mk("UNIT_FIELD_CANNON")] * 4


def test_army_obsolete_fires_in_modern_era():
    ev = et.army_obsolescence_event(_units_t335(), "ERA_MODERN")
    assert ev is not None and "ARMY OBSOLETE" in ev.message
    assert "3 eras behind" in ev.message and "upgrade_unit" in ev.message


def test_army_obsolete_quiet_when_current():
    assert et.army_obsolescence_event(_units_t335(), "ERA_MEDIEVAL") is None
    assert et.army_obsolescence_event([], "ERA_MODERN") is None
    assert et.army_obsolescence_event(_units_t335(), "") is None


# ---------------------------------------------------------------------------
# 科技是第一生产力:代差 ≥2 时军力升档作废(power_balance)
# ---------------------------------------------------------------------------


def _t335_sides():
    me = pb.Side("罗马", 0, cities=8, pop=67, techs=39, military=1137)
    rivals = [pb.Side("刚果", 3, cities=11, pop=100, techs=60, military=642)]
    return me, rivals


def test_military_bump_cancelled_by_era_lag():
    me, rivals = _t335_sides()
    snap = pb.measure(335, me, rivals, army_era_lag=3)
    assert snap.tier == pb.PowerTier.BEHIND  # 不升档,落后就是落后
    assert snap.military_masks_deficit is False  # 没升档,无所谓掩盖
    assert snap.is_survival_mode is True
    assert any("第一生产力" in r for r in snap.reasons)


def test_military_bump_kept_for_current_era_army():
    """T110 反例保留:当代兵的军力优势升档照旧。"""
    me, rivals = _t335_sides()
    for lag in (0, 1, None):
        snap = pb.measure(335, me, rivals, army_era_lag=lag)
        assert snap.tier == pb.PowerTier.EVEN, f"lag={lag}"
        assert snap.military_masks_deficit is True


# ---------------------------------------------------------------------------
# 联合宣战受邀门禁(respond_to_trade 入口,门禁第三入口)
# ---------------------------------------------------------------------------


class _ExplodingConn:
    async def execute_write(self, lua):
        raise AssertionError("blocked accept must not reach the game")


def _invite_gs(jw_amount=3):
    gs = GameState.__new__(GameState)
    gs.conn = _ExplodingConn()
    deal = NS(
        other_player_id=7,
        other_player_name="英国",
        items_from_them=[
            NS(item_type="AGREEMENT", name="Joint War vs 阿拉伯", amount=jw_amount)
        ],
        items_from_us=[],
    )

    async def _deals():
        return [deal]

    async def _civs():
        vc = NS(name="开罗", population=8, has_walls=True)
        return [NS(player_id=3, civ_name="阿拉伯", visible_cities=[vc])]

    async def _units():
        # 带近战,好让检查停在破墙那一关(T362 的 NO_CITY_TAKERS 更靠前)
        return [NS(unit_type="UNIT_CROSSBOWMAN")] * 13 + [
            NS(unit_type="UNIT_MUSKETMAN")
        ] * 2

    gs.get_pending_deals = _deals
    gs.get_diplomacy = _civs
    gs.get_units = _units
    return gs


def test_accepting_joint_war_invite_is_gated():
    gs = _invite_gs()
    result = asyncio.run(gs.respond_to_deal(7, accept=True))
    assert "联合宣战" in result and "NO_SIEGE_VS_WALLS" in result


def test_joint_war_invite_force_bypasses_gate():
    gs = _invite_gs()

    class _OkConn:
        async def execute_write(self, lua):
            return ["OK:DEAL_ACCEPTED", "SENTINEL"]

    gs.conn = _OkConn()
    result = asyncio.run(gs.respond_to_deal(7, accept=True, force=True))
    assert "BLOCKED" not in result and "联合宣战" not in result


def test_reject_skips_gate_entirely():
    gs = _invite_gs()

    class _OkConn:
        async def execute_write(self, lua):
            return ["OK:DEAL_REJECTED", "SENTINEL"]

    gs.conn = _OkConn()

    async def _boom():
        raise AssertionError("reject must not query deals")

    gs.get_pending_deals = _boom
    result = asyncio.run(gs.respond_to_deal(7, accept=False))
    assert "BLOCKED" not in result


def test_pending_deals_lua_emits_joint_war_target():
    lua = build_pending_deals_query()
    assert "Joint War vs" in lua
    assert "amount = valueType" in lua


# ---------------------------------------------------------------------------
# event_window 接线辅助(F2-4)
# ---------------------------------------------------------------------------


def test_tech_type_reverse_lookup_roundtrip():
    row = event_window.TECHS.get("TECH_BALLISTICS")
    assert row is not None
    localized = row.get("Name") or ""
    assert event_window.tech_type_from_name(localized) == "TECH_BALLISTICS"
    assert event_window.tech_type_from_name("TECH_BALLISTICS") == "TECH_BALLISTICS"
    assert event_window.tech_type_from_name("不存在的科技") == ""


def test_upgrade_window_fires_for_t335_shape():
    """军事学(弩手→野战加农炮的解锁科技)完成前给升级窗口提醒。"""
    unlocks = event_window.upgrades_unlocked_by("TECH_BALLISTICS")
    assert any(old == "UNIT_CROSSBOWMAN" for old, _new, _r in unlocks)
    ctx = event_window.EventContext(
        turn=335,
        researching_tech="TECH_BALLISTICS",
        tech_turns_left=3,
        my_unit_types=("UNIT_CROSSBOWMAN",) * 13,
    )
    lines = event_window.get_event_window_advice(ctx)
    assert lines and any("弩手" in ln or "CROSSBOWMAN" in ln for ln in lines)
