"""L0 工具层拦截的单元测试。

对应改造方案 §4.3。每个用例都锚定 52 回合复盘里的一次真实死亡,
注释里写明拦的是哪一次。
"""

from __future__ import annotations

import pytest

from civ_mcp import safety
from civ_mcp.safety import EnemySighting, MoveContext, check_move, check_ranged_attack


def _enemy(
    x=15, y=34, dist=1, utype="UNIT_HORSEMAN", cs=20, owner="Barbarian", moves=4, rng=0
) -> EnemySighting:
    """构造一个敌军观测。moves 默认 4(骑手) —— 复盘里杀死平民的就是它。"""
    return EnemySighting(
        x=x,
        y=y,
        dist=dist,
        unit_type=utype,
        cs=cs,
        owner=owner,
        moves=moves,
        range_=rng,
    )


def _ctx(**kw) -> MoveContext:
    base = dict(
        found=True,
        unit_type="UNIT_BUILDER",
        formation=safety.CIVILIAN,
        promotion="",
        moves_remaining=2.0,
        max_moves=2.0,
        from_x=15,
        from_y=35,
        enemies=[],
        escorts=0,
        escort_cs=0,
        adjacent_military=0,
        target_is_own_city=False,
    )
    base.update(kw)
    return MoveContext(**base)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def test_parse_full_response():
    lines = [
        "UNIT|UNIT_BUILDER|FORMATION_CLASS_CIVILIAN||2|2|16|33",
        "ENEMY|15|34|1|UNIT_HORSEMAN|20|Barbarian",
        "ENEMY|13|33|3|UNIT_SCOUT|10|Barbarian",
        "ESCORTS|0",
        "ESCORTCS|0",
        "ADJACENT|2",
    ]
    ctx = safety.parse_move_safety(lines)
    assert ctx.found
    assert ctx.unit_type == "UNIT_BUILDER"
    assert ctx.is_civilian
    assert len(ctx.enemies) == 2
    assert ctx.enemies[0].dist == 1
    # 旧格式(无机动字段)必须能解析,且兜底成保守值
    assert ctx.enemies[0].moves == safety.FALLBACK_ENEMY_MOVES
    assert ctx.escorts == 0
    assert ctx.adjacent_military == 2


def test_parse_reads_enemy_mobility():
    """新格式带 moves/range —— 对手推演的输入。"""
    ctx = safety.parse_move_safety(
        [
            "UNIT|UNIT_BUILDER|FORMATION_CLASS_CIVILIAN||2|2|16|33",
            "ENEMY|15|34|3|UNIT_HORSEMAN|36|Barbarian|4|0",
            "ENEMY|20|20|5|UNIT_ARCHER|25|Barbarian|2|2",
        ]
    )
    horseman, archer = ctx.enemies
    assert (horseman.moves, horseman.range_, horseman.reach) == (4, 0, 4)
    assert (archer.moves, archer.range_, archer.reach) == (2, 2, 4)


def test_parse_stale_unit_error():
    ctx = safety.parse_move_safety(["ERR|UNIT_STALE"])
    assert ctx.error == "UNIT_STALE"
    assert not ctx.found


def test_parse_in_city_flag():
    ctx = safety.parse_move_safety(
        ["UNIT|UNIT_SETTLER|FORMATION_CLASS_CIVILIAN||2|2|15|35", "INCITY|1"]
    )
    assert ctx.target_is_own_city


def test_parse_tolerates_garbage():
    ctx = safety.parse_move_safety(["ENEMY|bad|data", "ESCORTS|x", "noise"])
    assert ctx.enemies == []
    assert ctx.escorts == 0


# --------------------------------------------------------------------------
# 平民保护 — 复盘中 T19/T27/T45 建造者、T33/T51 开拓者
# --------------------------------------------------------------------------


def test_blocks_builder_walking_next_to_barbarian():
    """T19:建造者 #1 走到 (16,33),被蛮族骑手击杀。"""
    ctx = _ctx(enemies=[_enemy(15, 34, 1)])
    v = check_move(ctx, 16, 33)
    assert v.blocked
    assert v.code == "CIVILIAN_UNESCORTED"
    assert "HORSEMAN" in v.message
    assert "force=True" in v.message


def test_blocks_settler_without_escort():
    """T33/T51:开拓者无护送出城,两次都被杀。"""
    ctx = _ctx(
        unit_type="UNIT_SETTLER",
        enemies=[_enemy(12, 32, 2)],
    )
    assert check_move(ctx, 13, 33).blocked


def test_allows_civilian_with_strong_same_tile_escort():
    """同格护卫且战力够 —— 这是正确打法,不该拦。"""
    ctx = _ctx(
        enemies=[_enemy(15, 34, 1)],
        escorts=1,
        escort_cs=20,
    )
    assert not check_move(ctx, 16, 33).blocked


def test_blocks_when_escort_too_weak():
    """复盘 T27:投石兵(CS5)"护送"建造者面对 CS20 骑手,建造者照样被杀。"""
    ctx = _ctx(
        enemies=[_enemy(15, 34, 1)],
        escorts=1,
        escort_cs=5,
    )
    v = check_move(ctx, 16, 33)
    assert v.blocked
    assert v.code == "ESCORT_TOO_WEAK"


def test_adjacent_military_is_not_escort():
    """文明6里相邻的军事单位保护不了平民,必须同格。

    这是本次实机验证抓出来的规则理解错误 —— 初版按"相邻即护送"写,
    会把复盘里 T27 那次死亡放行。
    """
    ctx = _ctx(
        enemies=[_enemy(15, 34, 1)],
        escorts=0,
        adjacent_military=1,
    )
    v = check_move(ctx, 16, 33)
    assert v.blocked
    assert v.code == "CIVILIAN_UNESCORTED"
    assert "同格" in v.message  # 必须点破这个误解


def test_allows_civilian_into_own_city():
    """撤回自己城里是最正确的避险动作,绝不能拦。"""
    ctx = _ctx(
        enemies=[_enemy(15, 34, 1)],
        target_is_own_city=True,
    )
    assert not check_move(ctx, 15, 35).blocked


def test_allows_when_enemy_cannot_reach():
    """敌军够不到就放行 —— 慢速步兵(移动 2)在 5 格外构不成威胁。"""
    ctx = _ctx(enemies=[_enemy(10, 30, 5, utype="UNIT_WARRIOR", moves=2)])
    assert not check_move(ctx, 16, 33).blocked


# --------------------------------------------------------------------------
# 对手推演 —— "假设自己是对面"(用户 2026-07-31 提出)
#
# 关键差异:初版按固定 2 格判危险。而复盘 T33 抓死开拓者的是移动力 4 的骑手,
# 它在 3-4 格外照样能扑上来。下面两个用例锁住这个改进。
# --------------------------------------------------------------------------


def test_fast_enemy_threatens_beyond_old_fixed_radius():
    """骑手(移动 4)在 4 格外仍威胁 —— 旧的固定 2 格会放行,导致 T33 那次死亡。"""
    ctx = _ctx(enemies=[_enemy(11, 31, 4, moves=4)])
    v = check_move(ctx, 13, 33)
    assert v.blocked, "移动力 4 的骑手在 4 格外必须算威胁"
    assert "下回合可达" in v.message  # 拒绝理由要讲清推演依据


def test_slow_enemy_at_same_distance_is_safe():
    """同样 4 格,慢速步兵(移动 2)够不到 —— 证明判定真的按单位算,不是一刀切。"""
    ctx = _ctx(enemies=[_enemy(11, 31, 4, utype="UNIT_WARRIOR", moves=2)])
    assert not check_move(ctx, 13, 33).blocked


def test_ranged_enemy_reach_includes_its_range():
    """远程敌军的可达 = 移动 + 射程。弓箭手(2+2)在 4 格外仍能打到。"""
    ctx = _ctx(enemies=[_enemy(11, 31, 4, utype="UNIT_ARCHER", cs=25, moves=2, rng=2)])
    assert check_move(ctx, 13, 33).blocked


def test_reach_never_below_one():
    """移动力数据缺失(0)时至少按 1 算,不能推出"零威胁"。"""
    e = _enemy(15, 34, 1, moves=0)
    assert e.reach >= 1
    assert e.threatens


def test_never_blocks_military_units():
    """军事单位主动接敌是正常战术,拦了会瘫痪整个游戏。"""
    ctx = _ctx(
        unit_type="UNIT_WARRIOR",
        formation="FORMATION_CLASS_LAND_COMBAT",
        enemies=[_enemy(15, 34, 1)],
    )
    assert not check_move(ctx, 16, 33).blocked


def test_force_overrides():
    ctx = _ctx(enemies=[_enemy(15, 34, 1)])
    assert not check_move(ctx, 16, 33, force=True).blocked


def test_unknown_unit_passes():
    """查不到单位时放行 —— 让原有的 UNIT_NOT_FOUND 报错去处理。"""
    assert not check_move(MoveContext(found=False), 1, 1).blocked


# --------------------------------------------------------------------------
# 远程攻击顺序 — 复盘中至少 3 次
# --------------------------------------------------------------------------


def test_blocks_ranged_attack_with_no_moves_left():
    """移动力归零才是真的打不出去。"""
    ctx = _ctx(
        unit_type="UNIT_SLINGER",
        formation="FORMATION_CLASS_LAND_COMBAT",
        promotion=safety.RANGED,
        moves_remaining=0.0,
        max_moves=2.0,
    )
    v = check_ranged_attack(ctx)
    assert v.blocked
    assert v.code == "RANGED_ALREADY_MOVED"
    assert "耗尽" in v.message


def test_allows_ranged_attack_after_moving_with_moves_left():
    """2026-08-11 罗马局 T229 实机:野战加农炮走 1 格(剩 1.0/2.0)后
    对沃罗涅什开火,打掉 18 点城墙(100→82)。旧门禁按 has_moved 拦,
    把「推进到射程内再开火」这个攻城标准动作整个挡住了 —— 而那正是
    本局最需要的动作。误报比漏拦贵,这条测试守住它。"""
    ctx = _ctx(
        unit_type="UNIT_FIELD_CANNON",
        formation="FORMATION_CLASS_LAND_COMBAT",
        promotion="PROMOTION_CLASS_SIEGE",
        moves_remaining=1.0,
        max_moves=2.0,
    )
    assert not check_ranged_attack(ctx).blocked


def test_allows_ranged_attack_with_full_moves():
    ctx = _ctx(
        unit_type="UNIT_SLINGER",
        formation="FORMATION_CLASS_LAND_COMBAT",
        promotion=safety.RANGED,
        moves_remaining=2.0,
        max_moves=2.0,
    )
    assert not check_ranged_attack(ctx).blocked


def test_melee_can_attack_after_moving():
    """近战单位移动后攻击是完全正常的,不能误伤。"""
    ctx = _ctx(
        unit_type="UNIT_WARRIOR",
        formation="FORMATION_CLASS_LAND_COMBAT",
        promotion="PROMOTION_CLASS_MELEE",
        moves_remaining=1.0,
        max_moves=2.0,
    )
    assert not check_ranged_attack(ctx).blocked


def test_ranged_force_overrides():
    ctx = _ctx(
        promotion=safety.RANGED,
        formation="FORMATION_CLASS_LAND_COMBAT",
        moves_remaining=0.0,
        max_moves=2.0,
    )
    assert not check_ranged_attack(ctx, force=True).blocked


# --------------------------------------------------------------------------
# Lua 生成
# --------------------------------------------------------------------------


def test_query_embeds_generation_check():
    """安全查询自己也必须做世代校验,否则会读到错的单位。"""
    lua = safety.build_move_safety_query(1048577, 10, 20)
    assert "UNIT_STALE" in lua
    assert "65535" in lua
    assert "1048577" in lua


def test_query_respects_fog_of_war():
    """只能统计可见敌军 —— 否则就是作弊。"""
    lua = safety.build_move_safety_query(1, 5, 5)
    assert "IsVisible" in lua


def test_query_skips_peaceful_city_states():
    lua = safety.build_move_safety_query(1, 5, 5)
    assert "IsAtWarWith" in lua


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (safety.Verdict(True, "X", "msg"), "Error: X|msg"),
    ],
)
def test_verdict_error_format(verdict, expected):
    """错误格式必须和现有 Error: CODE|msg 一致。"""
    assert verdict.as_error() == expected
