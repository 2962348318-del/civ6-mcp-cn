"""2026-08-05 全程序审查修复 —— 回归测试。

两个只读审查代理 + 实机 T335 验证共报 20 项,此处覆盖可单测的核心项:
  F1-1   读档后 _pending_end_turn 旗标必须清除(否则假 HANG → 误杀游戏)
  F2-1   建议排序:同级同置信度时 L3 策略层(playbook/doctrine)先占配额
  F2-2   playbook/doctrine 破墙判定与宣战门禁同一把尺(can_break_walls)
  F2-3   gold_audit 正则支持负 GPT(经济最崩时审计不能瞎)
  F2-7   军力升档掩盖主轴落后时,生存纪律(is_survival_mode)照旧生效
  F2-8   LEADERTYPE 行:Lua 发射 → 解析 → CivInfo.leader_type
  F2-9   对手维度全 0 → usable=False(不捏造 2.00× 假优势)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from civ_mcp import advice
from civ_mcp import doctrine
from civ_mcp import gold_audit
from civ_mcp import playbook
from civ_mcp import power_balance as pb
from civ_mcp.game_state import GameState
from civ_mcp.lua.diplomacy import parse_diplomacy_response

# ---------------------------------------------------------------------------
# F1-1:读档清除 in-flight 旗标
# ---------------------------------------------------------------------------


def test_record_save_load_clears_pending_end_turn():
    gs = GameState.__new__(GameState)
    gs._high_water_turn = 335
    gs._save_load_history = []
    gs._pending_end_turn = True
    gs._pending_end_turn_from = 335
    gs._record_save_load("0_MCP_0335")
    assert gs._pending_end_turn is False
    assert gs._pending_end_turn_from is None


# ---------------------------------------------------------------------------
# F2-7 / F2-9:power_balance
# ---------------------------------------------------------------------------


def _t335_shape():
    """T335 实测形态:科技 0.65×(落后) + 军力 1.57×(全场第一)。"""
    me = pb.Side("罗马", 0, cities=8, pop=67, techs=39, military=1137)
    rivals = [
        pb.Side("刚果", 3, cities=11, pop=100, techs=60, military=642),
        pb.Side("英国", 7, cities=11, pop=90, techs=55, military=722),
    ]
    return pb.measure(335, me, rivals)


def test_military_bump_from_behind_keeps_survival_mode():
    snap = _t335_shape()
    assert snap.tier == pb.PowerTier.EVEN  # 升档本身保留(该打就打)
    assert snap.military_masks_deficit is True
    assert snap.is_survival_mode is True  # 但生存纪律照旧


def test_survival_tree_fires_under_masked_deficit():
    """T335 复盘:8 城齐造军事没人拦 —— survival_priority 整棵树沉默。"""
    inp = doctrine.DoctrineInput(turn=335, power=_t335_shape())
    items = doctrine.survival_priority(inp)
    assert items, "掩盖态下生存树必须发声"


def test_masked_deficit_headline_is_coherent():
    text = pb.headline(_t335_shape())
    assert "掩盖" in text and "兑现" in text
    assert "均衡" not in text  # 实机抓到的自相矛盾话术


def test_healthy_leader_bump_is_not_survival():
    """基础档不落后时的军力升档不触发生存纪律(别把领先局吓进龟壳)。"""
    me = pb.Side("我", 0, cities=10, techs=50, military=800)
    rivals = [pb.Side("AI", 1, cities=9, techs=52, military=400)]
    snap = pb.measure(200, me, rivals)
    assert snap.military_masks_deficit is False
    assert snap.is_survival_mode is False


def test_opponent_zero_dimension_not_usable():
    """F2-9:对手全 0 的维度是传感器缺口,不是 2.00× 优势。"""
    me = pb.Side("我", 0, cities=5, techs=20, military=100, culture=30)
    rivals = [pb.Side("AI", 1, cities=5, techs=20, military=100, culture=0)]
    snap = pb.measure(100, me, rivals)
    r = snap.ratios.get("culture")
    assert r is not None and r.usable is False
    axis = snap.strongest_axis
    assert axis is None or axis.dim != "culture"


# ---------------------------------------------------------------------------
# F2-1:排序末位规则 —— L3 只赢平局
# ---------------------------------------------------------------------------


def _adv(module, conf=advice.Confidence.RECOMMENDED):
    return advice.Advice(
        module=module,
        priority=advice.Priority.CORE,
        action=f"{module} 的建议",
        reason="",
        consequence="",
        confidence=conf,
    )


def test_l3_wins_ties_but_not_confidence():
    legacy = _adv("tech_path")
    l3 = _adv("playbook.war")
    certain_legacy = _adv("pace", conf=advice.Confidence.CERTAIN)
    ranked = advice.sort_advice([legacy, l3, certain_legacy])
    assert ranked[0].module == "pace"  # 置信度仍然优先
    assert ranked[1].module == "playbook.war"  # 同级同置信度 L3 在前
    assert ranked[2].module == "tech_path"


# ---------------------------------------------------------------------------
# F2-2:破墙判定统一
# ---------------------------------------------------------------------------


def _war_civ():
    vc = NS(name="开罗", has_walls=True)
    return NS(civ_name="阿拉伯", is_at_war=True, visible_cities=[vc])


def _mil(ut):
    return NS(unit_type=ut, combat_strength=30, health=100, x=1, y=1)


def test_doctrine_no_false_positive_with_field_cannon():
    """野战加农炮(RANGED 类攻城主力)在场时不再喊"你没有攻城单位"。"""
    inp = doctrine.DoctrineInput(
        turn=335,
        civs=[_war_civ()],
        my_units=[_mil("UNIT_FIELD_CANNON"), _mil("UNIT_MUSKETMAN")],
    )
    items = doctrine.asymmetric_engagement(inp)
    assert not any("不要进攻" in i.action for i in items)


def test_doctrine_blocks_crossbow_only_army():
    inp = doctrine.DoctrineInput(
        turn=314, civs=[_war_civ()], my_units=[_mil("UNIT_CROSSBOWMAN")] * 13
    )
    items = doctrine.asymmetric_engagement(inp)
    assert any("不要进攻" in i.action for i in items)


def test_playbook_siege_grade_uses_wall_breakers():
    """弩手为主力 → 🟠 停止磨墙(不再是 T335 那句"先用远程磨城墙")。"""
    power = _t335_shape()
    inp = doctrine.DoctrineInput(
        turn=335,
        power=power,
        civs=[_war_civ()],
        my_units=[_mil("UNIT_CROSSBOWMAN")] * 13,
    )
    items = [
        i for i in playbook.war_timing(inp) if i.dedup_key == "playbook:war:siege_grade"
    ]
    assert items, "交战中必须给攻城分档"
    assert "停止磨墙" in items[0].action
    assert "磨城墙" not in items[0].action


# ---------------------------------------------------------------------------
# F2-3:gold_audit 负 GPT
# ---------------------------------------------------------------------------


def test_gold_audit_regex_accepts_negative_gpt():
    m = gold_audit._OVERVIEW_RE.search(
        "Gold: 155 (-20/turn) | Income: 110 | Maintenance: -102 (units: 71)"
    )
    assert m is not None and int(m.group(2)) == -20
    m2 = gold_audit._OVERVIEW_RE.search(
        "Gold: 155 (+8/turn) | Income: 110 | Maintenance: -102 (units: 71)"
    )
    assert m2 is not None and int(m2.group(2)) == 8


# ---------------------------------------------------------------------------
# F2-8:LEADERTYPE 解析
# ---------------------------------------------------------------------------


def test_leadertype_line_parses_into_civinfo():
    lines = [
        "CIV|5|阿拉伯|萨拉丁|1|1|WAR|0|0|0|0|0|0",
        "LEADERTYPE|5|LEADER_SALADIN",
        "CIV|6|巴西|佩德罗二世|1|0|DENOUNCED|0|0|0|0|0|0",
        "LEADERTYPE|6|nil",
    ]
    civs = {c.player_id: c for c in parse_diplomacy_response(lines)}
    assert civs[5].leader_type == "LEADER_SALADIN"
    assert civs[6].leader_type == ""  # nil 不入账
