"""2026-08-06 法国局 T64 复盘修复 —— 回归测试。

两条都是实机事故:

1. **军力比假象**:T4-T10 我方 1 勇士 + 1 侦察兵、对手军力未知,
   `military_ratio` 吐出 2.00×,war_timing 反复推荐"开战性价比最高的窗口"。
   8-05 加过 `if best <= 0 < mine: usable = False`,但那条只作用于分档与
   渲染 —— 三个 ratio property 绕过了它,决策树照样中招。
   指纹:同一份快照,render() 说"军力不作数",决策树说"2.00×,去打"。

2. **RED 自相矛盾**:红警期间一边硬拦平民生产(工具层直接 Error),
   一边照旧建议"首都转产开拓者"。根因是 DoctrineInput 拿不到 threat_level,
   L3 策略层写不出让步分支。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import playbook
from civ_mcp import power_balance as pb
from civ_mcp.doctrine import DoctrineInput

# ---------------------------------------------------------------------------
# 1. 军力比:不可用的维度必须返回中性值,不能返回 2.00×
# ---------------------------------------------------------------------------


def _t7_snapshot():
    """T4-T10 实况:我方 1 勇士(军力 30),对手未遭遇(军力 0)。"""
    me = pb.Side("法国", 0, cities=1, techs=2, military=30)
    rivals = [pb.Side("中国", 1, cities=1, techs=2, military=0)]
    return pb.measure(7, me, rivals)


def test_unusable_military_ratio_is_neutral_not_two():
    snap = _t7_snapshot()
    assert snap.ratio_usable("military") is False
    assert snap.military_ratio == 1.0, (
        "对手军力无数据时必须给中性值 —— 返回 2.00× 会被 war_timing "
        "当成开战窗口(这正是 T4-T10 的事故)"
    )


def test_render_and_decision_trees_now_agree():
    """漏洞的指纹是两个口径打架 —— 现在必须一致。"""
    snap = _t7_snapshot()
    text = pb_render(snap)
    says_unusable = "不作数" in text
    assert says_unusable, "渲染层本来就认为军力不作数"
    # 决策树侧:同一份快照不该再算出可用的军力优势
    assert snap.ratio_usable("military") is False
    assert snap.military_ratio == 1.0


def pb_render(snap):
    return pb.render(snap)


def test_war_window_silent_when_military_unknown():
    """端到端:T7 那份快照不该再推荐开战。"""
    inp = DoctrineInput(turn=7, power=_t7_snapshot(), civs=[])
    items = playbook.war_timing(inp)
    assert not any("开战性价比最高" in i.action for i in items), (
        "军力数据不可信时不许推荐开战"
    )


def test_war_window_still_fires_with_real_advantage():
    """真有可信的军力优势时,窗口建议照旧出 —— 别把有用的也关掉。"""
    me = pb.Side("我", 0, cities=4, techs=20, military=300)
    rivals = [pb.Side("AI", 1, cities=4, techs=20, military=120)]
    snap = pb.measure(90, me, rivals)
    assert snap.ratio_usable("military") is True
    inp = DoctrineInput(turn=90, power=snap, civs=[])
    items = playbook.war_timing(inp)
    assert any("开战性价比最高" in i.action for i in items)


def test_tech_and_city_ratios_also_guarded():
    """三个 property 是同一个洞,一起堵。"""
    me = pb.Side("我", 0, cities=3, techs=8, military=100)
    rivals = [pb.Side("AI", 1, cities=0, techs=0, military=100)]
    snap = pb.measure(50, me, rivals)
    if not snap.ratio_usable("techs"):
        assert snap.tech_ratio == 1.0
    if not snap.ratio_usable("cities"):
        assert snap.city_ratio == 1.0


# ---------------------------------------------------------------------------
# 2. RED 让步:不再一边硬拦、一边催
# ---------------------------------------------------------------------------


def _cap(pop=4, prod=12):
    return NS(name="巴黎", population=pop, production=prod, districts=[])


def test_opening_settler_silent_under_red():
    """红警下 game_state 会直接拒绝造平民 —— 催了也执行不了。"""
    inp = DoctrineInput(turn=20, my_cities=[_cap()], gold=0, threat_level="RED")
    items = playbook.opening_queue(inp)
    assert not any("转产开拓者" in i.action for i in items)


def test_opening_settler_fires_when_safe():
    inp = DoctrineInput(turn=20, my_cities=[_cap()], gold=0, threat_level="GREEN")
    items = playbook.opening_queue(inp)
    assert any("转产开拓者" in i.action for i in items)


def _expand_inp(threat):
    me = pb.Side("我", 0, cities=2, techs=10, military=50)
    rivals = [
        pb.Side("A", 1, cities=5, techs=10, military=50),
        pb.Side("B", 2, cities=5, techs=10, military=50),
    ]
    return DoctrineInput(
        turn=64,
        power=pb.measure(64, me, rivals),
        my_cities=[_cap(), _cap()],
        threat_level=threat,
    )


def test_expansion_keeps_fact_but_drops_action_under_red():
    """红警下让步但不静默:保留"你落后几座城"这个事实。"""
    items = playbook.expansion_timing(_expand_inp("RED"))
    behind = [i for i in items if i.dedup_key == "playbook:expand:behind"]
    assert behind, "事实不该消失"
    a = behind[0]
    assert "先解除红色警报" in a.action
    assert a.priority.name == "OPTIMIZE", "红警下降级,不与生存指令抢版面"
    assert "送人头" in a.reason or "拒绝" in a.reason


def test_expansion_normal_when_not_red():
    items = playbook.expansion_timing(_expand_inp("GREEN"))
    behind = [i for i in items if i.dedup_key == "playbook:expand:behind"]
    assert behind
    assert behind[0].action.startswith("补 ")
    assert behind[0].priority.name == "CORE"


def test_expansion_has_subject_for_future_conflict_detection():
    """subject 是仲裁器做冲突检测的挂钩,补上它是零成本的前置条件。"""
    items = playbook.expansion_timing(_expand_inp("GREEN"))
    behind = [i for i in items if i.dedup_key == "playbook:expand:behind"]
    assert behind and behind[0].subject == "production:civilian"


def test_threat_level_defaults_empty_and_is_harmless():
    """老调用方不传 threat_level 时行为不变。"""
    inp = DoctrineInput(turn=20, my_cities=[_cap()], gold=0)
    assert inp.threat_level == ""
    items = playbook.opening_queue(inp)
    assert any("转产开拓者" in i.action for i in items)
