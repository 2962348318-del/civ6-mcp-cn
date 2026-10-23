"""任务 R 危机管理预案 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import crisis_manager as cm


# --- 预案一:被宣战 ---


def _attacker(mil=320, direction="西"):
    return {"civ_name": "英国", "military_strength": mil, "direction": direction}


def test_war_crisis_matches_plan_format():
    out = cm.war_declared_crisis(
        _attacker(),
        {
            "my_military": 180,
            "civilians_outside": 2,
            "cities_without_walls": ["安条克"],
            "gold": 450,
        },
    )
    text = "\n".join(out)
    assert out[0].startswith("🔴") and "英国向你宣战" in out[0]
    assert "320 vs 你 180" in out[0]
    assert "2 个平民撤回" in text
    assert "驻军" in text
    assert "安条克" in text and "切城墙" in text
    assert "买 1-2 个" in text
    assert "从西面来" in text


def test_war_crisis_capped_at_8_lines():
    out = cm.war_declared_crisis(
        _attacker(),
        {
            "my_military": 100,
            "civilians_outside": 3,
            "cities_without_walls": ["A", "B", "C"],
            "gold": 999,
        },
    )
    assert len(out) <= 8


def test_war_crisis_double_military_gets_survival_framing():
    out = cm.war_declared_crisis(_attacker(mil=400), {"my_military": 100})
    assert "硬拼必输" in out[0]


def test_war_crisis_no_direction_not_invented():
    """战线方向来自观测——没传就不出现,绝不猜。"""
    out = cm.war_declared_crisis(
        {"civ_name": "英国", "military_strength": 300}, {"my_military": 100}
    )
    assert not any("战线" in l for l in out)


def test_war_crisis_poor_treasury_skips_buy_advice():
    out = cm.war_declared_crisis(_attacker(), {"my_military": 100, "gold": 50})
    assert not any("买 1-2 个" in l for l in out)


def test_war_crisis_garbage_degrades_gracefully():
    """attacker=None 时降级为通用预案:仍是完整行动清单,不炸、不超行。"""
    out = cm.war_declared_crisis(None, None)
    assert out[0].startswith("🔴")
    assert len(out) <= 8
    text = "\n".join(out)
    assert "驻军" in text and "平民" in text


# --- 预案二:平民被杀 ---


def test_civilian_crisis_matches_plan_format():
    out = cm.civilian_killed_crisis("UNIT_BUILDER", (12, 34), {"camps_nearby": 1})
    text = "\n".join(out)
    assert "建造者在 (12,34) 被杀" in out[0]
    assert "死亡格档案" in out[0]
    assert "营地不拆、出兵不停" in text
    assert "清掉 6 格内的营地" in text


def test_civilian_crisis_no_camps_points_to_fog():
    out = cm.civilian_killed_crisis("UNIT_SETTLER", (5, 5), {"camps_nearby": 0})
    text = "\n".join(out)
    assert "开拓者" in out[0]
    assert "迷雾" in text


def test_civilian_crisis_teaches_same_tile_escort():
    """T27 的 215 金教训:同格才算护送,相邻不算。每次都要重复这一课。"""
    out = cm.civilian_killed_crisis("UNIT_BUILDER", (1, 2), {})
    assert any("同格" in l and "相邻" in l for l in out)


def test_civilian_crisis_names_killer_when_known():
    out = cm.civilian_killed_crisis("UNIT_BUILDER", (1, 2), {"killer": "蛮族骑手"})
    assert any("蛮族骑手" in l for l in out)


def test_civilian_crisis_bad_location_degrades():
    out = cm.civilian_killed_crisis("UNIT_BUILDER", None, {})
    assert "(位置未知)" in out[0]
    assert len(out) <= 8


# --- 预案三:忠诚度崩溃 ---


def test_loyalty_crisis_matches_plan_format():
    out = cm.loyalty_collapse_crisis({"name": "占领城", "loyalty": 98}, -7)
    text = "\n".join(out)
    assert out[0].startswith("🔴") and "-7.0" in out[0]
    assert "14 回合后叛变" in out[0]
    assert "总督" in text and "驻军" in text and "纪念碑" in text and "人口" in text


def test_loyalty_crisis_urgent_flip_pushes_selloff():
    out = cm.loyalty_collapse_crisis({"name": "危城", "loyalty": 20}, -8)
    assert any("卖" in l and "白送" in l for l in out)
    assert any("只剩" in l for l in out)


def test_loyalty_crisis_positive_pressure_is_silent():
    assert cm.loyalty_collapse_crisis({"name": "稳城"}, +3) == []
    assert cm.loyalty_collapse_crisis({"name": "稳城"}, 0) == []


def test_loyalty_crisis_defaults_full_loyalty():
    out = cm.loyalty_collapse_crisis({"name": "新城"}, -4)
    assert "25 回合后叛变" in out[0]  # 100/4


def test_loyalty_crisis_capped_and_safe():
    out = cm.loyalty_collapse_crisis({"name": "X", "loyalty": 50}, -2.5)
    assert len(out) <= 8
    assert cm.loyalty_collapse_crisis(None, "垃圾") == []
    assert cm.loyalty_collapse_crisis(None, -5)  # 鸭子类型缺字段也不炸


def test_unexported_numbers_are_labelled():
    out = cm.loyalty_collapse_crisis({"name": "X"}, -5)
    assert any("未导出" in l for l in out)
