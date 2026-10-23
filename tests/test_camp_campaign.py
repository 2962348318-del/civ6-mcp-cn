"""营地清剿计划(playbook.camp_campaign)—— 单元测试。

2026-08-06 法国局 T64 的核心教训:系统对营地只会重复诊断
("营地不拆出兵不停",说了 40 回合),从不给计划。模型自述:
"我 T20-T40 一直想拆但始终差兵力,系统也没推先补兵再拆营的主线。"
结果 T12 见营地、T64 仍未拆,50 回合被出兵 15+ 次、赔掉 5 个单位。

本树补的正是 barbarian_response 的盲区:营地在放血、但兵力不够那一段。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import playbook
from civ_mcp.doctrine import DoctrineInput


def _camp(x=70, y=37, first_seen=12, dist=7):
    return NS(x=x, y=y, first_seen_turn=first_seen, dist_to_capital=dist)


def _unit(ut):
    return NS(unit_type=ut, combat_strength=20, ranged_strength=0)


def _inp(turn=64, camps=None, units=None, production=6, cities=2):
    return DoctrineInput(
        turn=turn,
        camps=camps if camps is not None else [_camp()],
        my_units=units if units is not None else [],
        production=production,
        my_cities=[NS(name=f"城{i}") for i in range(cities)],
    )


def _plan(items):
    return [i for i in items if i.dedup_key == "playbook:camp:plan"]


def _strike(items):
    return [i for i in items if i.dedup_key == "playbook:camp:strike_now"]


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------


def test_silent_before_bleed_threshold():
    """刚发现的营地不催 —— 开局本来就不该为一个营地打乱节奏。"""
    items = playbook.camp_campaign(_inp(turn=20, camps=[_camp(first_seen=12)]))
    assert items == []


def test_silent_without_camps():
    assert playbook.camp_campaign(_inp(camps=[])) == []


def test_silent_when_first_seen_unknown():
    """首见回合拿不到就不猜放血时长。"""
    items = playbook.camp_campaign(_inp(camps=[NS(x=1, y=1, dist_to_capital=5)]))
    assert items == []


# ---------------------------------------------------------------------------
# 兵力不足 → 给时间表(原来完全缺失的那一段)
# ---------------------------------------------------------------------------


def test_short_of_troops_gives_schedule():
    """T64 实际形态:1 个弓箭手、产能 6、营地放血 52 回合。"""
    items = playbook.camp_campaign(
        _inp(turn=64, units=[_unit("UNIT_ARCHER")], production=6)
    )
    plan = _plan(items)
    assert plan, "营地放血 52 回合还兵力不足时必须给计划"
    a = plan[0]
    assert a.priority.name == "CORE"
    assert "还差" in a.action and "近战" in a.action
    assert "T" in a.action  # 给出了可动手的回合数
    assert "52 回合" in a.reason


def test_schedule_counts_melee_separately():
    """3 个远程 ≠ 可以拆营:摧毁营地必须近战踩进那一格。"""
    items = playbook.camp_campaign(
        _inp(units=[_unit("UNIT_ARCHER")] * 3, production=10)
    )
    plan = _plan(items)
    assert plan, "只有远程时仍要报缺口"
    assert "近战" in plan[0].action


def test_defensive_posture_in_detail():
    """攒兵期的防守姿态要说出来 —— 这局 5 个单位都是攒兵期冒进丢的。"""
    items = playbook.camp_campaign(_inp(units=[_unit("UNIT_WARRIOR")]))
    plan = _plan(items)
    assert plan
    detail = " ".join(plan[0].detail)
    assert "回城" in detail or "驻防" in detail
    assert "野外过夜" in detail or "追击" in detail


def test_gives_giveup_alternative():
    """攒不出来时要有退路,不能只会催。"""
    items = playbook.camp_campaign(_inp(units=[], production=2))
    plan = _plan(items)
    assert plan and ("撤进城" in plan[0].alternative or "守不住" in plan[0].alternative)


# ---------------------------------------------------------------------------
# 兵力够了 → 催动手,并说清机制
# ---------------------------------------------------------------------------


def test_enough_troops_says_strike_now():
    units = [_unit("UNIT_WARRIOR"), _unit("UNIT_ARCHER"), _unit("UNIT_ARCHER")]
    items = playbook.camp_campaign(_inp(units=units))
    strike = _strike(items)
    assert strike, "编成够了要催动手"
    assert "本回合" in strike[0].action
    assert _plan(items) == [], "够了就不该再报缺口"


def test_strike_explains_melee_must_enter_tile():
    """这是最容易踩的机制坑:远程打不掉营地本身。"""
    units = [_unit("UNIT_MUSKETMAN"), _unit("UNIT_ARCHER"), _unit("UNIT_ARCHER")]
    items = playbook.camp_campaign(_inp(units=units))
    strike = _strike(items)
    assert strike
    assert "踩进" in strike[0].alternative or "那一格" in strike[0].alternative


def test_picks_longest_bleeding_camp():
    """多个营地时,先处理放血最久的那个。"""
    camps = [_camp(x=1, y=1, first_seen=50), _camp(x=9, y=9, first_seen=12)]
    items = playbook.camp_campaign(_inp(turn=64, camps=camps, units=[]))
    plan = _plan(items)
    assert plan and "(9,9)" in plan[0].reason


# ---------------------------------------------------------------------------
# 接线
# ---------------------------------------------------------------------------


def test_wired_into_trees():
    assert playbook.camp_campaign in playbook.TREES


def test_evaluate_includes_camp_plan():
    """走完整 evaluate 也能出来(不被别的树的异常吃掉)。"""
    items = playbook.evaluate(_inp(units=[_unit("UNIT_ARCHER")]))
    assert any(i.dedup_key == "playbook:camp:plan" for i in items)
