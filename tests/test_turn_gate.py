"""任务 J 回合完成度门禁(end_turn 追加部分)—— 单元测试。

只测纯函数与状态机,不碰网络/游戏连接。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import end_turn as et


def _unit(
    utype="UNIT_WARRIOR",
    moves=2.0,
    max_moves=2.0,
    charges=0,
    cs=20,
    name="",
    x=10,
    y=10,
):
    return NS(
        unit_type=utype,
        moves_remaining=moves,
        max_moves=max_moves,
        build_charges=charges,
        combat_strength=cs,
        name=name or utype.replace("UNIT_", ""),
        x=x,
        y=y,
    )


# --- 纯函数分档 ---


def _threat(x=12, y=10, moves=3, rng=1, utype="UNIT_BARBARIAN_HORSE_ARCHER"):
    return NS(
        x=x, y=y, base_moves=moves, range=rng, unit_type=utype, owner_name="Barbarian"
    )


def test_idle_builder_is_a_blocker():
    blockers, _w = et.check_turn_completion([_unit("UNIT_BUILDER", charges=3, cs=0)])
    assert len(blockers) == 1
    assert "平民整回合未动" in blockers[0]
    assert "BUILDER(10,10)" in blockers[0]


# --- 危险档:2026-08-01 实机教训(闲置平民 + 敌军可达 = 送人头) ---


def test_endangered_civilian_is_escalated():
    """建造者在(10,10),弓骑手在(12,10)距 2 格、可达 4 格 → 危险档。"""
    blockers, _w = et.check_turn_completion(
        [_unit("UNIT_BUILDER", charges=3, cs=0)], [_threat()]
    )
    assert len(blockers) == 1
    assert blockers[0].startswith("🔴")
    assert "打击范围内" in blockers[0]
    assert "2 格外(可达 4 格)" in blockers[0]
    assert "同格" in blockers[0] and "相邻不算" in blockers[0]


def test_far_threat_does_not_escalate():
    """威胁在 20 格外(可达 4 格)→ 够不着,保持普通闲置档。"""
    blockers, _w = et.check_turn_completion(
        [_unit("UNIT_BUILDER", charges=3, cs=0)], [_threat(x=30, y=10)]
    )
    assert blockers and not blockers[0].startswith("🔴")


def test_threat_reach_uses_moves_plus_range():
    """可达 = 移动力 + 射程,与 safety/对手推演同一套上界推演。"""
    # 距离 5:移动 3+射程 1=4 够不着;移动 4+射程 2=6 够得着
    near = et.check_turn_completion(
        [_unit("UNIT_BUILDER", charges=3, cs=0)], [_threat(x=15, y=10)]
    )[0]
    assert not near[0].startswith("🔴")
    far = et.check_turn_completion(
        [_unit("UNIT_BUILDER", charges=3, cs=0)], [_threat(x=15, y=10, moves=4, rng=2)]
    )[0]
    assert far[0].startswith("🔴")


def test_endangered_and_safe_civilians_listed_separately():
    units = [
        _unit("UNIT_BUILDER", charges=3, cs=0, x=10, y=10),
        _unit("UNIT_SETTLER", charges=0, cs=0, x=40, y=40),
    ]
    blockers, _w = et.check_turn_completion(units, [_threat()])
    assert len(blockers) == 2  # 危险档 + 普通档各一条
    assert blockers[0].startswith("🔴") and "BUILDER" in blockers[0]
    assert not blockers[1].startswith("🔴") and "SETTLER" in blockers[1]


def test_military_not_escalated_by_threat():
    """军事单位在威胁范围内不升级 —— 它们本来就该在前线。"""
    blockers, warnings = et.check_turn_completion([_unit("UNIT_WARRIOR")], [_threat()])
    assert blockers == [] and warnings


def test_garbage_threats_never_raise():
    blockers, _w = et.check_turn_completion(
        [_unit("UNIT_BUILDER", charges=3, cs=0)],
        [
            NS(
                x="?",
                y=None,
                base_moves="x",
                range=None,
                unit_type=None,
                owner_name=None,
            )
        ],
    )
    assert isinstance(blockers, list)


def test_idle_settler_is_a_blocker():
    blockers, _w = et.check_turn_completion([_unit("UNIT_SETTLER", charges=0, cs=0)])
    assert blockers and "SETTLER" in blockers[0]


def test_idle_military_is_warning_not_blocker():
    """军事单位可能在驻守 —— 数据层分不出,只警告,绝不阻止。"""
    blockers, warnings = et.check_turn_completion([_unit("UNIT_WARRIOR")])
    assert blockers == []
    assert warnings and "skip 不会撤退" in warnings[0]


def test_moved_units_are_ignored():
    blockers, warnings = et.check_turn_completion(
        [
            _unit("UNIT_BUILDER", moves=1.0, max_moves=2.0, charges=3, cs=0),
            _unit("UNIT_WARRIOR", moves=0.0),
        ]
    )
    assert blockers == [] and warnings == []


def test_zero_max_moves_units_ignored():
    """max_moves=0(如被运输/特殊状态)不算闲置。"""
    blockers, warnings = et.check_turn_completion(
        [_unit("UNIT_BUILDER", moves=0.0, max_moves=0.0, charges=3, cs=0)]
    )
    assert blockers == [] and warnings == []


def test_noncombat_zero_charge_units_silent():
    """无 charges 无战力的单位(商人/宗教单位等)不进任何档 —— 它们的
    闲置语义各异,宁可漏报。"""
    blockers, warnings = et.check_turn_completion(
        [_unit("UNIT_TRADER", charges=0, cs=0)]
    )
    assert blockers == [] and warnings == []


def test_blocker_lists_capped_at_four():
    units = [_unit("UNIT_BUILDER", charges=3, cs=0, x=i) for i in range(6)]
    blockers, _w = et.check_turn_completion(units)
    assert "6 个平民" in blockers[0] and " 等" in blockers[0]


def test_garbage_units_never_raise():
    blockers, warnings = et.check_turn_completion(
        [NS(moves_remaining="x", max_moves=None), None, 42]
    )
    assert blockers == [] and warnings == []
    assert et.check_turn_completion(None) == ([], [])


# --- 首拦-重调放行状态机 ---


def _gs():
    return NS(_game_identity=("rome", 1))


def test_gate_state_waive_cycle():
    gs = _gs()
    st = et._gate_state(gs)
    assert st["waived_turn"] == -1
    st["waived_turn"] = 25
    assert et._gate_state(gs)["waived_turn"] == 25  # 同对象取回同状态


def test_pop_gate_warnings_drains():
    gs = _gs()
    et._gate_state(gs)["warnings"] = ["· 提醒 A"]
    assert et._pop_gate_warnings(gs) == ["· 提醒 A"]
    assert et._pop_gate_warnings(gs) == []  # 取走即清空
    assert et._pop_gate_warnings(NS()) == []  # 无状态对象安全
