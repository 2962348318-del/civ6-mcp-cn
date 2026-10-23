"""LAST_MILITARY_UNIT 对远程单位放行(2026-08-06 修)。

实证出处:法国局 T64,弓箭手作为全国唯一军事单位在城内远程攻击,
被 `LAST_MILITARY_UNIT` 拦了 7 次,7 次全部 force 通过,零损失,
每次都造成击杀或重创。

这是 discipline.py docstring §1.1 明确要避免的误伤:远程攻击零反伤,
「唯一单位阵亡 = 城市裸奔」这个前提根本不成立。更糟的是连续 7 次
「拦了、越过、没事」会把 force 训练成肌肉记忆,让这一层对后面真正
该拦的那一次也失效。

本文件锁住三件事:远程放行、近战仍拦(T22 那次不能漏)、force 仍能越过。
"""

from __future__ import annotations

from civ_mcp import discipline, safety
from civ_mcp.safety import MoveContext

# 构造风格与 tests/test_strategy_layer.py 的 _ctx 保持一致
MELEE = "PROMOTION_CLASS_MELEE"


def _ctx(**kw) -> MoveContext:
    """默认:满血近战、全国唯一军事单位、目标是野战敌军(不是城市)。

    刻意把 target_is_enemy_city 设为 False —— 这样只有
    check_last_military_unit 可能命中,不会被攻城类拦截干扰结论。
    """
    base = dict(
        found=True,
        unit_type="UNIT_ARCHER",
        formation="FORMATION_CLASS_LAND_COMBAT",
        promotion=safety.RANGED,
        hp=100,
        max_hp=100,
        my_military_total=1,
        target_is_enemy_city=False,
    )
    base.update(kw)
    return MoveContext(**base)


# --------------------------------------------------------------------------
# 远程放行 —— 本次修复的正题
# --------------------------------------------------------------------------


def test_last_military_ranged_unit_passes():
    """T64:全国唯一的弓箭手远程攻击,不该被拦。"""
    v = discipline.check_last_military_unit(_ctx())
    assert not v.blocked, "远程攻击零反伤,唯一单位不会因此阵亡"


def test_last_military_ranged_unit_passes_against_city():
    """唯一远程单位炮击城市同样放行 —— 这是攻城的正解,不是错误。"""
    ctx = _ctx(target_is_enemy_city=True, enemy_city_name="开罗")
    assert not discipline.check_last_military_unit(ctx).blocked


def test_check_attack_lets_last_ranged_unit_through():
    """走完整入口:三条拦截串起来跑,唯一远程单位一条都不该命中。

    check_low_hp_assault / check_staggered_assault 本来就放行远程,
    修复后 check_last_military_unit 与它们口径一致 —— 用残血 + 有援军
    在路上的最坏组合确认没有任何一条漏网。
    """
    ctx = _ctx(hp=10, target_is_enemy_city=True, adjacent_military=0, nearby_military=5)
    assert not discipline.check_attack(ctx).blocked


def test_ranged_promotion_is_what_lifts_the_block():
    """同一份局面,只把 promotion 从远程改成近战,结论必须翻转。

    这条是为了证明放行来自 is_ranged,而不是别的字段碰巧让拦截失效。
    """
    passed = discipline.check_last_military_unit(_ctx(promotion=safety.RANGED))
    blocked = discipline.check_last_military_unit(_ctx(promotion=MELEE))
    assert not passed.blocked
    assert blocked.blocked


# --------------------------------------------------------------------------
# 近战仍然拦 —— 修复不能顺手把 T22 放过去
# --------------------------------------------------------------------------


def test_last_military_melee_unit_still_blocked():
    """T22:全国唯一勇士去追蛮族侦察兵,阵亡,开局崩盘。这条必须还在。"""
    ctx = _ctx(unit_type="UNIT_WARRIOR", promotion=MELEE)
    v = discipline.check_last_military_unit(ctx)
    assert v.blocked and v.code == "LAST_MILITARY_UNIT"
    assert "T22" in v.message


def test_melee_block_survives_full_check_attack():
    ctx = _ctx(unit_type="UNIT_WARRIOR", promotion=MELEE)
    assert discipline.check_attack(ctx).code == "LAST_MILITARY_UNIT"


def test_second_military_unit_still_lifts_melee_block():
    """不是唯一单位就放行 —— 原有行为不受本次修复影响。"""
    ctx = _ctx(unit_type="UNIT_WARRIOR", promotion=MELEE, my_military_total=2)
    assert not discipline.check_last_military_unit(ctx).blocked


def test_empty_promotion_is_treated_as_melee():
    """promotion 字段缺失时按近战处理(保守),不能因为读不到就放行。"""
    ctx = _ctx(unit_type="UNIT_WARRIOR", promotion="")
    assert discipline.check_last_military_unit(ctx).blocked


# --------------------------------------------------------------------------
# force 仍可越过
# --------------------------------------------------------------------------


def test_force_bypasses_melee_block():
    ctx = _ctx(unit_type="UNIT_WARRIOR", promotion=MELEE)
    assert not discipline.check_last_military_unit(ctx, force=True).blocked


def test_force_bypasses_via_check_attack():
    ctx = _ctx(unit_type="UNIT_WARRIOR", promotion=MELEE, hp=10)
    assert not discipline.check_attack(ctx, force=True).blocked


# --------------------------------------------------------------------------
# 与 check_low_hp_assault 的口径一致性
# --------------------------------------------------------------------------


def test_ranged_guard_matches_low_hp_assault():
    """两条拦截对同一个远程局面必须给出同一个结论。

    本次 bug 的根因就是这两条的守卫条件写得不一样
    (check_low_hp_assault 有 ctx.is_ranged,check_last_military_unit 没有)。
    这条测试把「口径一致」本身钉住,防止以后又漂开。
    """
    ctx = _ctx(hp=30, target_is_enemy_city=True, enemy_city_name="开罗")
    assert not discipline.check_last_military_unit(ctx).blocked
    assert not discipline.check_low_hp_assault(ctx).blocked
