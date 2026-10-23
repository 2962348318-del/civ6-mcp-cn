"""2026-08-06 提分方案批 1:分数归因/斜率 + 扩张计划 + 增长停滞。

用户观察「它打每次得分都差别人好多」。两局复盘的病根一致:分数是帝国规模
存量的时间积分,而系统对分数完全沉默 —— agent 每回合看得到"你排第 6",
但没人告诉它差距来自哪一项、是在收窄还是扩大、该做什么。

三条设计决定要被测住:
  A. 开局存量落后**不报警**(高难 AI 带开局加成,那是结构性的);
     斜率连续落后才报警 —— 否则会催出"提前打没准备的仗"这种坏决策。
  B. 归因只给排序不给分值(计分权重拿不到真值)。
  C. 扩张树给的是**计划**(落点/产地/ETA)不是诊断("补 2 座城")。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import end_turn as et
from civ_mcp import playbook
from civ_mcp import power_balance as pb
from civ_mcp import score_gap as sg
from civ_mcp.doctrine import DoctrineInput

# ---------------------------------------------------------------------------
# A. 分数归因
# ---------------------------------------------------------------------------


def _snap(turn=64, my_score=39, rivals=((82, 4, 12, 8, 10),)):
    """rivals: (score, cities, pop, techs, civics) 各一家。"""
    me = pb.Side(
        name="我方", cities=2, pop=7, techs=7, civics=4, military=173, score=my_score
    )
    rs = [
        pb.Side(
            name=f"对手{i}",
            player_id=i,
            score=s,
            cities=c,
            pop=p,
            techs=t,
            civics=cv,
            military=137,
        )
        for i, (s, c, p, t, cv) in enumerate(rivals)
    ]
    return pb.PowerSnapshot(turn=turn, me=me, rivals=rs)


def test_gap_attributes_shortfall_by_relative_size():
    """归因的核心:按【相对差】排序,最落后的排最前。

    本例的比值:市政 4/10=0.40 < 城市 2/4=0.50 < 人口 7/12=0.58
    < 科技 7/8=0.88 —— 绝对差最大的是人口(-5),但最该先治的是市政。
    这正是用相对差而不是绝对差排序的理由。
    """
    gap = sg.build(_snap())
    assert gap is not None
    assert gap.my_score == 39 and gap.median == 82
    names = [cn for cn, _m, _r in gap.shortfalls]
    assert names == ["市政数", "城市数", "人口", "科技数"]


def test_gap_silent_without_rival_scores():
    """没有对手分数时静默 —— 任何归因都会是编的。"""
    snap = _snap(rivals=((0, 4, 12, 8, 10),))
    assert sg.build(snap) is None


def test_gap_silent_without_my_score():
    assert sg.build(_snap(my_score=0)) is None


def test_leading_renders_single_confirmation_line():
    """领先时不该刷屏,一行确认即可。"""
    lines = sg.render(sg.build(_snap(my_score=200)))
    assert len(lines) == 1 and "高于对手中位" in lines[0]


def test_render_marks_ordering_not_values():
    """设计决定 B:不能出现编造的分值权重。"""
    lines = sg.render(sg.build(_snap()))
    assert any("按相对差排序" in ln for ln in lines)


# ---------------------------------------------------------------------------
# B. 斜率哨兵
# ---------------------------------------------------------------------------


def _history(pairs):
    """pairs: [(turn, my_score, median_score), ...]"""
    h = pb.PowerHistory()
    h.records = [{"turn": t, "my_score": m, "median_score": md} for t, m, md in pairs]
    return h


def test_early_stock_lag_does_not_alarm():
    """设计决定 A:开局存量落后是结构性的,不报警。"""
    gap = sg.build(_snap(turn=20), _history([(20, 20, 40)]))
    lines = sg.render(gap)
    assert not any("⚠️" in ln for ln in lines)
    assert any("盯斜率不盯存量" in ln for ln in lines)


def test_two_lagging_windows_raise_alarm():
    """连续 2 窗口斜率落后 = 差距在扩大,这才是真信号。"""
    h = _history([(40, 20, 40), (50, 25, 55), (60, 30, 72)])
    gap = sg.build(_snap(turn=60), h)
    assert gap.lag_windows >= sg.LAG_ALERT
    lines = sg.render(gap)
    assert any("⚠️" in ln and "斜率连续" in ln for ln in lines)


def test_alarm_says_army_does_not_score():
    """最诱人的错误结论是"打一仗把分抢回来" —— 必须当场堵住。"""
    h = _history([(40, 20, 40), (50, 25, 55), (60, 30, 72)])
    lines = sg.render(sg.build(_snap(turn=60), h))
    assert any("军队不产分" in ln for ln in lines)


def test_catching_up_is_reported_positively():
    """追上了要明说 —— 否则 agent 会为已经修好的问题继续乱动作。"""
    h = _history([(50, 20, 60), (60, 45, 70)])
    lines = sg.render(sg.build(_snap(turn=60), h))
    assert any("在收窄" in ln for ln in lines)


def test_single_record_makes_no_trend():
    """一个采样点不编趋势(与 expansion_rates 同一条纪律)。"""
    gap = sg.build(_snap(turn=30), _history([(30, 20, 40)]))
    assert gap.slope_mine is None and gap.lag_windows == 0


def test_legacy_history_without_scores_is_skipped():
    """旧版本历史没有 my_score 字段 —— 当缺数据,不猜。"""
    h = pb.PowerHistory()
    h.records = [{"turn": 40, "cities": 3}, {"turn": 50, "cities": 4}]
    gap = sg.build(_snap(turn=50), h)
    assert gap.slope_mine is None


def test_power_snapshot_persists_score_fields():
    """斜率的数据源:to_dict 必须带 my_score/median_score。"""
    d = _snap(rivals=((82, 4, 12, 8, 10), (60, 3, 9, 7, 8))).to_dict()
    assert d["my_score"] == 39
    assert d["median_score"] == 82  # 排序后取中位(2 家取上位)


# ---------------------------------------------------------------------------
# C. 扩张计划树
# ---------------------------------------------------------------------------


def _city(name="伦敦", prod=10, pop=4, housing=5, food=2.0):
    return NS(
        name=name,
        production=prod,
        population=pop,
        housing=housing,
        food_surplus=food,
        districts=[],
    )


def _power(mine=2, rival_cities=(4, 4, 3)):
    me = pb.Side(name="我方", cities=mine)
    rivals = [
        pb.Side(name=f"R{i}", player_id=i, cities=c) for i, c in enumerate(rival_cities)
    ]
    return pb.PowerSnapshot(turn=64, me=me, rivals=rivals)


def _settler(moves=2.0, mx=2.0):
    return NS(unit_type="UNIT_SETTLER", moves_remaining=moves, max_moves=mx)


def test_settler_in_hand_gets_land_it_now():
    """开拓者在手是最贵的闲置 —— 必须催落地。"""
    inp = DoctrineInput(
        turn=64, power=_power(), my_cities=[_city()], my_units=[_settler()]
    )
    out = playbook.expansion_campaign(inp)
    assert out and "get_settle_advisor" in out[0].action
    assert out[0].subject == "unit:settler"


def test_settler_with_threats_gets_escort_note():
    inp = DoctrineInput(
        turn=64,
        power=_power(),
        my_cities=[_city()],
        my_units=[_settler()],
        threats=[NS(x=1, y=1)],
    )
    out = playbook.expansion_campaign(inp)
    assert any("护送" in d for d in out[0].detail)


def test_no_settler_behind_gets_production_site_and_eta():
    """设计决定 C:计划要含"从哪造、几回合"。"""
    inp = DoctrineInput(
        turn=64,
        power=_power(),
        my_cities=[_city("伦敦", prod=10), _city("谢菲尔德", prod=4)],
    )
    out = playbook.expansion_campaign(inp)
    assert out
    act = out[0].action
    assert "伦敦" in act, "要点名产能最高的城"
    assert "T72" in act, "80 锤 / 10 产能 = 8 回合 → T64+8"


def test_eta_flags_settler_cost_inflation():
    """开拓者成本随城市数上浮 —— ETA 偏乐观必须注明,否则会误导扩张节奏。"""
    inp = DoctrineInput(turn=64, power=_power(), my_cities=[_city()])
    out = playbook.expansion_campaign(inp)
    assert any("上浮" in d for d in out[0].detail)


def test_red_alert_keeps_campaign_silent():
    """红警下 game_state 会拒绝造平民 —— 两棵树对同一件事不说两遍。"""
    inp = DoctrineInput(
        turn=64, power=_power(), my_cities=[_city()], threat_level="RED"
    )
    assert playbook.expansion_campaign(inp) == []


def test_not_behind_stays_silent():
    inp = DoctrineInput(turn=64, power=_power(mine=5), my_cities=[_city()] * 5)
    assert playbook.expansion_campaign(inp) == []


def test_zero_production_gives_no_fake_eta():
    """产能为 0 时不能报 ETA —— 除零或无穷大的计划比不给更坏。"""
    inp = DoctrineInput(turn=64, power=_power(), my_cities=[_city(prod=0)])
    assert playbook.expansion_campaign(inp) == []


def test_campaign_is_registered():
    assert playbook.expansion_campaign in playbook.TREES


def test_expansion_campaign_module_differs_from_timing():
    """两棵树不能挤同一个仲裁配额。"""
    inp = DoctrineInput(turn=64, power=_power(), my_cities=[_city()])
    plan = playbook.expansion_campaign(inp)
    timing = playbook.expansion_timing(inp)
    assert plan and timing
    assert plan[0].module != timing[0].module


def test_settler_base_cost_from_gamedata():
    from civ_mcp import war_planner

    assert war_planner.unit_base_cost("UNIT_SETTLER") == 80
    assert war_planner.unit_base_cost("UNIT_NONSENSE") == 0


# ---------------------------------------------------------------------------
# D. 增长停滞
# ---------------------------------------------------------------------------


def test_housing_cap_is_reported():
    ev = et.growth_stall_event([_city("奥斯蒂亚", pop=5, housing=5)])
    assert ev is not None and "奥斯蒂亚" in ev.message and "住房满" in ev.message


def test_food_deficit_is_reported():
    ev = et.growth_stall_event([_city("安条克", pop=3, housing=9, food=0.0)])
    assert ev is not None and "粮盈余" in ev.message


def test_healthy_city_reports_nothing():
    assert et.growth_stall_event([_city(pop=3, housing=9, food=2.5)]) is None


def test_empty_city_list_is_safe():
    assert et.growth_stall_event([]) is None
    assert et.growth_stall_event(None) is None


def test_amenities_field_is_not_used_for_stall():
    """lua/cities.py 的 amenities 是 needed+have 的复合值,不是净值 ——
    拿它判"宜居不足"会得出错误结论,所以停滞判定必须不碰它。"""
    import inspect

    src = inspect.getsource(et.growth_stall_event)
    code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(code)
    # 只允许出现在文档字符串的警告里,不允许出现在取值逻辑
    assert 'getattr(c, "amenities"' not in body
