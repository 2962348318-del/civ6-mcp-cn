"""L0 硬知识注入的单元测试(改造方案 §4.2)。"""

from __future__ import annotations

from civ_mcp import knowledge
from civ_mcp.knowledge import flags_from_events, select_briefing


class _Ev:
    def __init__(self, message: str):
        self.message = message


# --------------------------------------------------------------------------
# 恒定规则:任何情况下都必须出现
# --------------------------------------------------------------------------


def test_always_rules_present_even_without_era():
    """时代查不到时也要给恒定规则 —— 不能因为一次查询失败就静默什么都不发。"""
    b = select_briefing(None)
    assert "平民单位" in b
    assert "同一格" in b
    assert "远程单位攻击会耗尽" in b


def test_always_rules_present_in_every_era():
    for era in ["ERA_ANCIENT", "ERA_MEDIEVAL", "ERA_INFORMATION"]:
        b = select_briefing(era)
        assert "战斗力为 0" in b, f"{era} 缺少平民规则"


def test_briefing_has_header():
    assert select_briefing("ERA_ANCIENT").startswith("== 本回合规则提醒 ==")


# --------------------------------------------------------------------------
# 分时代
# --------------------------------------------------------------------------


def test_ancient_era_covers_barbarian_camps():
    """52 回合复盘的根本死因:营地不拆,兵永远不停。"""
    b = select_briefing("ERA_ANCIENT")
    assert "蛮族营地" in b
    assert "拆" in b
    assert "侦察兵" in b


def test_ancient_era_has_verified_numbers():
    """数值必须来自游戏库导出,这几个是已核对的。"""
    b = select_briefing("ERA_ANCIENT")
    assert "投石兵射程 1" in b  # Units.md: UNIT_SLINGER Range=1
    assert "弓箭手射程 2" in b  # Units.md: UNIT_ARCHER Range=2
    assert "移动力 2" in b  # Units.md: SETTLER/BUILDER BaseMoves=2


def test_medieval_differs_from_ancient():
    a = select_briefing("ERA_ANCIENT")
    m = select_briefing("ERA_MEDIEVAL")
    assert a != m
    assert "城墙" in m


def test_late_eras_share_industrial_rules():
    for era in ["ERA_MODERN", "ERA_ATOMIC", "ERA_INFORMATION"]:
        assert "战略资源" in select_briefing(era)


def test_unknown_era_degrades_gracefully():
    b = select_briefing("ERA_MADE_UP")
    assert "平民单位" in b  # 恒定部分仍在
    assert "蛮族营地" not in b


# --------------------------------------------------------------------------
# 局势触发
# --------------------------------------------------------------------------


def test_lost_contact_flag_adds_fog_warning():
    """治"看不见就当没了" —— T28 误判"蛮族全清"就是这么来的。"""
    b = select_briefing("ERA_ANCIENT", {"lost_contact"})
    assert "看不见不等于不存在" in b


def test_situational_rules_are_capped():
    """警告疲劳:局势规则最多 3 条(方案 §13.3)。"""
    allf = {"threat_near", "lost_contact", "at_war", "civilian_exposed"}
    b = select_briefing("ERA_ANCIENT", allf, max_situational=3)
    hits = sum(
        1
        for frag in [
            "视野里有敌方军事单位",
            "看不见不等于不存在",
            "处于战争状态",
            "平民单位在城外",
        ]
        if frag in b
    )
    assert hits == 3


def test_situational_order_is_stable():
    """同样的输入必须产生同样的输出,便于诊断。"""
    f = {"at_war", "threat_near"}
    assert select_briefing("ERA_ANCIENT", f) == select_briefing("ERA_ANCIENT", f)


def test_no_flags_means_no_situational():
    b = select_briefing("ERA_ANCIENT", set())
    assert "处于战争状态" not in b


# --------------------------------------------------------------------------
# 从事件推导标记
# --------------------------------------------------------------------------


def test_flags_from_threat_event():
    ev = [_Ev("THREAT: Barbarian UNIT_HORSEMAN CS:20 spotted 3 tiles away at (13,34)")]
    assert "threat_near" in flags_from_events(ev)


def test_flags_from_lost_contact_event():
    """end_turn.py:1570 生成的那条,现在终于被用上了。"""
    ev = [_Ev("LOST CONTACT: Barbarian UNIT_HORSEMAN HP:80/100 last seen at (23,15)")]
    assert "lost_contact" in flags_from_events(ev)


def test_flags_from_vanished_event():
    ev = [_Ev("VANISHED: Barbarian UNIT_SCOUT HP:40/40 last at (11,33)")]
    assert "lost_contact" in flags_from_events(ev)


def test_flags_empty_for_boring_events():
    ev = [_Ev("Research complete: TECH_POTTERY"), _Ev("City grew to population 3")]
    assert flags_from_events(ev) == set()


def test_flags_tolerates_malformed_events():
    class Weird:
        pass

    assert flags_from_events([Weird(), _Ev("")]) == set()


# --------------------------------------------------------------------------
# 体量控制
# --------------------------------------------------------------------------


def test_briefing_stays_compact():
    """每回合都注入,不能失控 —— 太长会淹没重点(方案 §13.3)。"""
    b = select_briefing("ERA_ANCIENT", {"threat_near", "lost_contact", "at_war"})
    assert len(b) < 1400, f"简报 {len(b)} 字符,过长"


def test_all_eras_covered():
    """GameInfo.Eras 导出的 8 个时代都要有规则,不能漏。"""
    for era in [
        "ERA_ANCIENT",
        "ERA_CLASSICAL",
        "ERA_MEDIEVAL",
        "ERA_RENAISSANCE",
        "ERA_INDUSTRIAL",
        "ERA_MODERN",
        "ERA_ATOMIC",
        "ERA_INFORMATION",
    ]:
        assert era in knowledge._ERA_RULES, f"{era} 没有对应规则"
