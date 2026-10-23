"""T335 校准局(rome_-758731782)修复 —— 单元测试。

对应《对局复盘_T335_问题清单.md》:
  缺陷 3  宣战门禁 NO_SIEGE_VS_WALLS(无破墙单位时对有墙文明宣战 → 拦截)
  缺陷 1  开拓者滞留升级(>5 回合没坐下 → 阻断;skip 过的开拓者旧检查看不见)
  缺陷 4  维护费占比预警(≥60% 毛收入 → MAINTENANCE BURDEN)

P0(进 AI 回合前先存档)是时序行为,由下一局实机验证,不在此测。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from civ_mcp import end_turn as et
from civ_mcp import war_planner as wp
from civ_mcp.game_state import GameState

# ---------------------------------------------------------------------------
# 缺陷 3a:破墙单位分类(war_planner.can_break_walls,真实 gamedata)
# ---------------------------------------------------------------------------


def test_siege_class_breaks_walls():
    for ut in ("UNIT_CATAPULT", "UNIT_BOMBARD", "UNIT_ARTILLERY"):
        assert wp.can_break_walls(ut), ut


def test_support_siege_units_break_walls():
    assert wp.can_break_walls("UNIT_BATTERING_RAM")
    assert wp.can_break_walls("UNIT_SIEGE_TOWER")


def test_field_cannon_qualifies_crossbow_does_not():
    """T335 实测边界:弩手(40)21 回合磨不动墙,野战加农炮(60)是攻城主力。"""
    assert wp.can_break_walls("UNIT_FIELD_CANNON")
    assert not wp.can_break_walls("UNIT_CROSSBOWMAN")
    assert not wp.can_break_walls("UNIT_ARCHER")


def test_naval_ranged_and_bombers_break_walls():
    """风云起海军远程对城防全额伤害;轰炸机 Bombard 110。
    导出数据里 Frigate 是 rcs 55/bombard 0/分类 other —— 必须按类放行。"""
    for ut in ("UNIT_FRIGATE", "UNIT_QUADRIREME", "UNIT_BATTLESHIP", "UNIT_BOMBER"):
        assert wp.can_break_walls(ut), ut
    assert not wp.can_break_walls("UNIT_GALLEY")  # 海军近战不算


def test_melee_does_not_break_walls():
    for ut in ("UNIT_WARRIOR", "UNIT_MUSKETMAN", "UNIT_ROMAN_LEGION"):
        assert not wp.can_break_walls(ut), ut
    assert not wp.can_break_walls("")


# ---------------------------------------------------------------------------
# 缺陷 3b:宣战门禁(game_state._pre_war_gate)
# ---------------------------------------------------------------------------


class _ExplodingConn:
    """宣战被拦截时绝不能碰到游戏连接。"""

    async def execute_write(self, lua):
        raise AssertionError(f"blocked declaration must not reach the game: {lua}")

    async def execute_read(self, lua):
        raise AssertionError("blocked declaration must not reach the game")


def _war_gs(civs, units) -> GameState:
    gs = GameState.__new__(GameState)
    gs.conn = _ExplodingConn()

    async def _civs():
        return civs

    async def _units():
        return units

    gs.get_diplomacy = _civs
    gs.get_units = _units
    return gs


def _civ(pid=3, walls=True, name="阿拉伯"):
    vc = NS(name="开罗", population=8, has_walls=walls)
    return NS(player_id=pid, civ_name=name, visible_cities=[vc])


def test_war_on_walled_civ_without_siege_is_blocked():
    # 带上近战单位,好让检查停在破墙这一关 —— 否则会先撞 NO_CITY_TAKERS
    # (2026-08-05 T362 复盘新增的更靠前的门禁)。
    gs = _war_gs(
        [_civ()],
        [NS(unit_type="UNIT_CROSSBOWMAN")] * 13 + [NS(unit_type="UNIT_MUSKETMAN")] * 2,
    )
    result = asyncio.run(gs.send_diplomatic_action(3, "DECLARE_SURPRISE_WAR"))
    assert result.startswith("BLOCKED: NO_SIEGE_VS_WALLS")
    assert "开罗" in result and "force=True" in result
    assert "assess_attack" in result and "薄墙小城" in result


def test_war_allowed_with_field_cannon():
    gs = _war_gs(
        [_civ()],
        [
            NS(unit_type="UNIT_CROSSBOWMAN"),
            NS(unit_type="UNIT_FIELD_CANNON"),
            NS(unit_type="UNIT_MUSKETMAN"),  # 占城单位,T362 门禁要求
        ],
    )
    assert asyncio.run(gs._pre_war_gate(3)) == ("", "")


def test_war_allowed_when_no_visible_walls():
    gs = _war_gs([_civ(walls=False)], [NS(unit_type="UNIT_WARRIOR")])
    assert asyncio.run(gs._pre_war_gate(3)) == ("", "")


def test_blind_declaration_passes_with_scouting_advisory():
    """目标 0 可见城市:放行,但提醒先侦察(T314 目标选错的温床)。"""
    civ = NS(player_id=3, civ_name="阿拉伯", visible_cities=[])
    gs = _war_gs([civ], [NS(unit_type="UNIT_WARRIOR")])
    block, advisory = asyncio.run(gs._pre_war_gate(3))
    assert block == ""
    assert "一无所知" in advisory and "get_map_area" in advisory


def test_war_gate_fails_open_on_unknown_target_or_error():
    assert asyncio.run(_war_gs([_civ(pid=5)], [])._pre_war_gate(3)) == ("", "")

    gs = GameState.__new__(GameState)

    async def _boom():
        raise RuntimeError("query died")

    gs.get_diplomacy = _boom
    assert asyncio.run(gs._pre_war_gate(3)) == ("", "")


def test_joint_war_via_trade_is_gated():
    """联合宣战走交易通道,同样受 NO_SIEGE_VS_WALLS 约束。"""
    gs = _war_gs(
        [_civ()],
        [NS(unit_type="UNIT_CROSSBOWMAN"), NS(unit_type="UNIT_MUSKETMAN")],
    )
    jw_item = {"type": "AGREEMENT", "subtype": "JOINT_WAR", "target_player": 3}
    result = asyncio.run(gs.propose_trade(5, [jw_item], [jw_item]))
    assert result.startswith("BLOCKED: NO_SIEGE_VS_WALLS")


def test_joint_war_target_reaches_lua():
    """2026-08-05 修复:此前 joint_war_target 被静默丢弃,deal 残缺。"""
    from civ_mcp import lua as lq

    lua = lq.build_propose_trade(
        5,
        [{"type": "AGREEMENT", "subtype": "JOINT_WAR", "target_player": 3}],
        [],
    )
    assert "SetSubType(DealAgreementTypes.JOINT_WAR)" in lua
    assert "SetValueType(3)" in lua
    # 不带 target_player 的协议(开放边界)不应带 SetValueType
    lua2 = lq.build_propose_trade(
        5, [{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}], []
    )
    assert "SetValueType" not in lua2


def test_non_war_actions_skip_the_gate():
    """外交使团不该触发任何战争检查(get_diplomacy 都不许调)。"""

    class _PlainConn:
        async def execute_write(self, lua):
            return ["OK|sent", "SENTINEL"]

    gs = GameState.__new__(GameState)
    gs.conn = _PlainConn()

    async def _boom():
        raise AssertionError("gate must not run for non-war actions")

    gs.get_diplomacy = _boom
    result = asyncio.run(gs.send_diplomatic_action(3, "DIPLOMATIC_DELEGATION"))
    assert "BLOCKED" not in result


# ---------------------------------------------------------------------------
# 缺陷 1:开拓者滞留升级(_turn_completion_gate 的跨回合追踪)
# ---------------------------------------------------------------------------


def _settler(uid=901, moves=0.0):
    """moves=0 模拟被 skip 的开拓者 —— 「整回合未动」检查看不见它。"""
    return NS(
        unit_type="UNIT_SETTLER",
        combat_strength=0,
        unit_id=uid,
        moves_remaining=moves,
        max_moves=2.0,
        build_charges=0,
        name="SETTLER",
        x=30,
        y=30,
    )


def _gate_gs(units):
    gs = NS()

    async def _units():
        return units

    async def _cities():
        # These tests isolate civilian tracking; expansion has its own tests.
        return [NS(currently_building="BUILDING_MONUMENT") for _ in range(8)], ""

    gs.get_units = _units
    gs.get_cities = _cities
    gs.has_read = lambda kind: True
    return gs


def test_settler_stale_escalates_after_5_turns():
    gs = _gate_gs([_settler()])
    assert asyncio.run(et._turn_completion_gate(gs, 100)) == ""  # 首见,不拦
    assert asyncio.run(et._turn_completion_gate(gs, 103)) == ""  # 3 回合,不拦
    msg = asyncio.run(et._turn_completion_gate(gs, 107))  # 7 回合,拦
    assert "滞留 7 回合" in msg and "found_city" in msg
    assert asyncio.run(et._turn_completion_gate(gs, 107)) == ""  # 同回合重调放行


def test_settler_tracking_stops_after_founding():
    gs = _gate_gs([_settler()])
    asyncio.run(et._turn_completion_gate(gs, 100))
    st = et._gate_state(gs)
    assert 901 in st["settler_first_seen"]
    gs2_units = []  # 开拓者坐下,单位消失

    async def _empty():
        return gs2_units

    gs.get_units = _empty
    assert asyncio.run(et._turn_completion_gate(gs, 110)) == ""
    assert st["settler_first_seen"] == {}


def test_moved_settler_still_tracked():
    """滞留判据是存在时长,不是本回合动没动 —— 来回遛开拓者一样拦。"""
    gs = _gate_gs([_settler(moves=2.0)])  # 没动过 → 也吃普通闲置档
    asyncio.run(et._turn_completion_gate(gs, 200))
    msg = asyncio.run(et._turn_completion_gate(gs, 206))
    assert "滞留 6 回合" in msg


def _builder(uid=77, charges=3):
    return NS(
        unit_type="UNIT_BUILDER",
        combat_strength=0,
        unit_id=uid,
        moves_remaining=0.0,
        max_moves=2.0,
        build_charges=charges,
        name="BUILDER",
        x=10,
        y=10,
    )


def test_builder_stalled_without_spending_charges_is_blocked():
    """审查 F2-5:被 skip 的建造者和开拓者是同一个洞 —— 按充能停滞时长追踪。"""
    gs = _gate_gs([_builder()])
    assert asyncio.run(et._turn_completion_gate(gs, 50)) == ""
    msg = asyncio.run(et._turn_completion_gate(gs, 56))
    assert "建造者(10,10) 滞留 6 回合" in msg and "get_builder_tasks" in msg


def test_builder_spending_charges_resets_baseline():
    b = _builder()
    gs = _gate_gs([b])
    asyncio.run(et._turn_completion_gate(gs, 50))
    b.build_charges = 2  # 干活了 → 基线重置
    assert asyncio.run(et._turn_completion_gate(gs, 56)) == ""
    assert asyncio.run(et._turn_completion_gate(gs, 61)) == ""  # 5 回合内不拦
    assert "滞留" in asyncio.run(et._turn_completion_gate(gs, 62))


def test_waived_stale_civilian_snoozes_5_turns():
    """审查 F1-7:知情豁免后 5 回合内不再重复拦截(合法等待不必每回合双调)。"""
    gs = _gate_gs([_settler()])
    asyncio.run(et._turn_completion_gate(gs, 100))
    assert "滞留" in asyncio.run(et._turn_completion_gate(gs, 107))
    assert asyncio.run(et._turn_completion_gate(gs, 107)) == ""  # 知情豁免
    assert asyncio.run(et._turn_completion_gate(gs, 108)) == ""  # 退避中
    assert asyncio.run(et._turn_completion_gate(gs, 112)) == ""
    assert "滞留" in asyncio.run(et._turn_completion_gate(gs, 113))  # 退避结束


def test_lazy_end_turn_without_reading_units_is_blocked():
    """审查 F2:取证不再被门禁自身污染 —— 「偷懒检测」上线以来第一次能触发。"""
    gs = _gate_gs([])
    gs.has_read = lambda kind: False
    msg = asyncio.run(et._turn_completion_gate(gs, 100))
    assert "get_units" in msg and "🔴" in msg
    assert asyncio.run(et._turn_completion_gate(gs, 100)) == ""  # 重调放行


def test_gate_consumes_snapshot_without_querying():
    """审查 F8:传入新鲜快照时门禁零查询、零取证污染。"""

    async def _boom():
        raise AssertionError("gate must not query when snapshot provided")

    gs = NS(get_units=_boom, get_cities=_boom, has_read=lambda k: True)
    snap = NS(units={901: _settler()}, cities={})
    assert asyncio.run(et._turn_completion_gate(gs, 100, None, snap)) == ""
    assert 901 in et._gate_state(gs)["settler_first_seen"]


# ---------------------------------------------------------------------------
# 缺陷 4:维护费占比预警(纯函数)
# ---------------------------------------------------------------------------


def _ov(income=90.0, maint=54.0, unit=54):
    return NS(gold_income=income, total_maintenance=maint, unit_maintenance=unit)


def test_maintenance_burden_fires_at_60_pct():
    ev = et.maintenance_burden_event(_ov())  # 54/90 = 60%
    assert ev is not None and ev.category == "economy"
    assert "MAINTENANCE BURDEN" in ev.message and "60%" in ev.message
    # 2026-08-06(A9):消息从纯诊断升级为修复清单,措辞改中文。
    # 触发阈值未变;"解散过时单位"仍在,只是排到了收入端修法之后。
    assert "解散" in ev.message and "unit_action delete" in ev.message


def test_maintenance_burden_quiet_below_threshold():
    assert et.maintenance_burden_event(_ov(maint=53.9)) is None
    assert et.maintenance_burden_event(_ov(income=0, maint=10)) is None
    assert et.maintenance_burden_event(_ov(income=None, maint=None)) is None
