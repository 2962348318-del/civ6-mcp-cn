"""作战与伟人缺口 —— 2026-08-12 罗马 T384 战报暴露的两块空白。

那局场上同时站着 7 个从未激活的伟人(含 2 个大科学家,而它走的正是科学
胜利),23 个军事单位全程没编过队(民族主义与动员都早已解锁)。
两处的共同点:引擎允许、工具层要么缺入口要么缺提醒。
"""

from __future__ import annotations

from civ_mcp import end_turn
from civ_mcp.end_turn import _ScanUnit


def _mil(utype: str, x: int = 0, y: int = 0) -> _ScanUnit:
    return _ScanUnit(utype, x, y, True)


def _civilian(utype: str, x: int = 0, y: int = 0) -> _ScanUnit:
    return _ScanUnit(utype, x, y, False)


# --- 闲置伟人 ---------------------------------------------------------------


def test_idle_great_people_are_named():
    """罗马 T384 的真实阵容:伟人存在 = 充能没用完 = 待办。"""
    units = [
        _civilian("UNIT_GREAT_SCIENTIST", 60, 40),
        _civilian("UNIT_GREAT_SCIENTIST", 61, 41),
        _civilian("UNIT_GREAT_WRITER", 62, 42),
        _mil("UNIT_MECHANIZED_INFANTRY"),
    ]
    ev = end_turn.idle_great_person_event(units)
    assert ev is not None
    assert "GREAT_SCIENTIST" in ev.message
    assert "activate" in ev.message


def test_aura_great_people_get_positioning_advice_not_activation():
    """大将军/海军统帅靠站位光环生效,不该被催「快激活」。"""
    ev = end_turn.idle_great_person_event([_civilian("UNIT_GREAT_GENERAL", 5, 5)])
    assert ev is not None
    assert "光环" in ev.message and "2 格内" in ev.message
    assert "未激活伟人" not in ev.message


def test_no_great_people_is_silent():
    assert end_turn.idle_great_person_event([_mil("UNIT_WARRIOR")]) is None
    assert end_turn.idle_great_person_event([]) is None


# --- 军团/大军编队 -----------------------------------------------------------


def test_corps_reminder_needs_the_civic():
    """没解锁民族主义就不提 —— 提了也做不到,只是噪音。"""
    units = [_mil("UNIT_SWORDSMAN"), _mil("UNIT_SWORDSMAN")]
    assert end_turn.corps_formation_event(units, ["CIVIC_CRAFTSMANSHIP"]) is None


def test_corps_reminder_fires_on_pairs():
    units = [_mil("UNIT_SWORDSMAN"), _mil("UNIT_SWORDSMAN"), _mil("UNIT_ARCHER")]
    ev = end_turn.corps_formation_event(units, ["CIVIC_NATIONALISM"])
    assert ev is not None
    assert "军团" in ev.message and "form_corps" in ev.message
    assert "SWORDSMAN×2" in ev.message


def test_mobilization_upgrades_the_advice_to_army():
    units = [_mil("UNIT_MECHANIZED_INFANTRY") for _ in range(4)]
    ev = end_turn.corps_formation_event(
        units, ["CIVIC_NATIONALISM", "CIVIC_MOBILIZATION"]
    )
    assert ev is not None
    assert "大军" in ev.message and "form_army" in ev.message
    assert "可编队 2 组" in ev.message  # 4 个单位 = 2 组


def test_lone_units_do_not_trigger():
    """每种只有一个 = 无法合编,不该响(否则变成每回合噪音)。"""
    units = [_mil("UNIT_SWORDSMAN"), _mil("UNIT_ARCHER")]
    assert end_turn.corps_formation_event(units, ["CIVIC_NATIONALISM"]) is None


def test_civilians_and_great_people_never_counted_as_pairs():
    """回归:判据必须用 combat_strength —— _ScanUnit 没有 is_military 字段,
    用它做判据会让整条检测变成恒不触发的死代码(写这个函数时踩过)。"""
    units = [
        _civilian("UNIT_BUILDER"),
        _civilian("UNIT_BUILDER"),
        _civilian("UNIT_GREAT_SCIENTIST"),
        _civilian("UNIT_GREAT_SCIENTIST"),
    ]
    assert end_turn.corps_formation_event(units, ["CIVIC_NATIONALISM"]) is None
    # 而真军事单位必须被数到 —— 证明判据不是「恒 False」
    real = [_mil("UNIT_SWORDSMAN"), _mil("UNIT_SWORDSMAN")]
    assert end_turn.corps_formation_event(real, ["CIVIC_NATIONALISM"]) is not None


# --- Lua 生成 ---------------------------------------------------------------


# --- 碾压期兑现 -------------------------------------------------------------


class _Snap:
    def __init__(self, tier=4, rate=0.0, survival=False):
        self.tier = tier
        self.my_expansion_rate = rate
        self.is_survival_mode = survival


def test_dominance_stall_fires_with_airpower():
    """罗马 T383 的形状:统治级 + 有轰炸机 + 20 回合没新城。"""
    units = [_mil("UNIT_JET_BOMBER") for _ in range(3)] + [
        _mil("UNIT_MECHANIZED_INFANTRY") for _ in range(11)
    ]
    ev = end_turn.dominance_stall_event(_Snap(), units)
    assert ev is not None and ev.priority == 1
    assert "3 架战机" in ev.message
    assert "优势在贬值" in ev.message


def test_dominance_stall_silent_while_still_expanding():
    """还在铺城 = 正在兑现,不该催。"""
    units = [_mil("UNIT_JET_BOMBER")]
    assert end_turn.dominance_stall_event(_Snap(rate=0.15), units) is None


def test_dominance_stall_silent_when_behind_or_surviving():
    units = [_mil("UNIT_WARRIOR")]
    assert end_turn.dominance_stall_event(_Snap(tier=2), units) is None
    assert end_turn.dominance_stall_event(_Snap(survival=True), units) is None
    assert end_turn.dominance_stall_event(None, units) is None


def test_dominance_stall_without_airpower_gives_ground_plan():
    units = [_mil("UNIT_SWORDSMAN"), _mil("UNIT_SWORDSMAN")]
    ev = end_turn.dominance_stall_event(_Snap(tier=3), units)
    assert ev is not None
    assert "原始首都" in ev.message  # 统治只认原始首都
    assert "战机" not in ev.message


def test_loyalty_ruleset_probe_parses_three_states():
    """忠诚度是 R&F 机制,原版没有 —— 探测必须能如实说「没有」和「不知道」。"""
    from civ_mcp import lua as lq

    assert lq.parse_loyalty_ruleset_probe(["LOYALTY_RULESET|yes"]) is True
    assert lq.parse_loyalty_ruleset_probe(["LOYALTY_RULESET|no"]) is False
    # 一座城都没有 / 探测失败 → None,而不是默认成 True
    assert lq.parse_loyalty_ruleset_probe(["LOYALTY_RULESET|unknown"]) is None
    assert lq.parse_loyalty_ruleset_probe([]) is None
    probe = lq.build_loyalty_ruleset_probe()
    assert "GetCulturalIdentity" in probe and "pcall" in probe


def test_form_lua_targets_the_right_command():
    from civ_mcp import lua as lq

    corps = lq.build_form_unit(3, 10, 20, army=False)
    army = lq.build_form_unit(3, 10, 20, army=True)
    assert "UNITCOMMAND_FORM_CORPS" in corps
    assert "UNITCOMMAND_FORM_ARMY" in army
    # 规则集探测:命令缺失时明确报错而不是静默失败
    assert "ERR:NO_FORM_COMMAND" in corps
    # 同型校验与引擎否决都要有出口
    assert "ERR:NO_PARTNER" in corps and "ERR:CANNOT_FORM" in corps
    assert "PARAM_UNIT_ID" in corps
    # 回归:f-string 只插值最外层花括号 —— _bail("…{label}…") 里的占位符
    # 在字符串字面量内部,会原样进 Lua。这里确认没有漏网的字面占位符。
    for leaked in ("{label}", "{civic}", "{cmd}", "{target_x}", "{target_y}"):
        assert leaked not in corps, f"未插值的占位符漏进 Lua:{leaked}"
        assert leaked not in army, f"未插值的占位符漏进 Lua:{leaked}"
    # 且真实值确实出现了
    assert "军团" in corps and "民族主义" in corps
    assert "大军" in army and "动员" in army
    assert "(10,20)" in corps
