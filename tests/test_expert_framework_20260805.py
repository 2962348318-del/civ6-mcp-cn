"""2026-08-05 第四轮:外部高手攻略的【思路框架】落地 —— 回归测试。

用户定的原则:学思路不学数值。所以本轮每条规则的判据都是**机制事实**
(gamedata 可验证),攻略只提供"该看哪个维度"的框架:

  UU timing 窗口   窗口开=UU 前置科技完成;窗口关=升级目标的科技完成
                   (此后永远造不出 UU)。UnitUpgrades + PrereqTech 可验证。
  科研来源纠偏     科研来自人口与学院邻接,不来自研究顺序 → 落后时的
                   处方是补城/补人口/补学院,不是换科技顺序。
  占领单位硬约束   远程/攻城只能把城打到 0 血,必须近战系进城才算占领。

攻略里的数值主张(T100 该有 20-30 城、八城阈值、政策加成百分比)一律未采信。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import civ_kit as ck
from civ_mcp import doctrine
from civ_mcp import playbook
from civ_mcp import power_balance as pb
from civ_mcp import war_planner as wp

# ---------------------------------------------------------------------------
# UU 可建造窗口(gamedata 推导,真实文明验证)
# ---------------------------------------------------------------------------


def _rome_window():
    kit = ck.derive(civ_type="CIVILIZATION_ROME")
    return kit, next(w for w in kit.windows if "LEGION" in w.uu.unit_type.upper())


def test_rome_legion_window_endpoints_from_gamedata():
    """罗马军团:铁器时代开窗 → 学徒制关窗(解锁披甲战士即造不出军团)。"""
    _kit, w = _rome_window()
    assert w.buildable_window_known
    assert w.uu.prereq == "TECH_IRON_WORKING"
    assert w.upgrade_prereq == "TECH_APPRENTICESHIP"
    assert w.edge > 0  # 相对同期通用单位确有代差


def test_window_state_four_phases():
    _kit, w = _rome_window()
    assert ck.window_state(w, set()) == ck.WINDOW_LOCKED
    assert ck.window_state(w, {"TECH_IRON_WORKING"}) == ck.WINDOW_OPEN
    assert (
        ck.window_state(w, {"TECH_IRON_WORKING"}, researching="TECH_APPRENTICESHIP")
        == ck.WINDOW_CLOSING
    )
    assert (
        ck.window_state(w, {"TECH_IRON_WORKING"}, available={"TECH_APPRENTICESHIP"})
        == ck.WINDOW_CLOSING
    )
    assert (
        ck.window_state(w, {"TECH_IRON_WORKING", "TECH_APPRENTICESHIP"})
        == ck.WINDOW_CLOSED
    )


def test_window_state_silent_without_upgrade_data():
    """升级链数据不全 → 不猜,返回空串让调用方静默。"""
    uu = NS(unit_type="UNIT_X", name="X", prereq="TECH_A", power=40, era_index=2)
    w = ck.UUWindow(uu=uu, peer_type="", peer_name="", peer_power=30)
    assert not w.buildable_window_known
    assert ck.window_state(w, {"TECH_A"}) == ""


def test_multiple_civs_windows_derive_without_error():
    for civ in (
        "CIVILIZATION_ROME",
        "CIVILIZATION_GREECE",
        "CIVILIZATION_SCYTHIA",
        "CIVILIZATION_ENGLAND",
    ):
        kit = ck.derive(civ_type=civ)
        assert kit.windows, civ
        for w in kit.windows:
            ck.window_state(w, {"TECH_IRON_WORKING"})  # 不抛异常即可


# ---------------------------------------------------------------------------
# UU 窗口 → 建议(playbook.uu_timing_push)
# ---------------------------------------------------------------------------


def _inp(researched, **kw):
    kit = ck.derive(civ_type="CIVILIZATION_ROME")
    return doctrine.DoctrineInput(turn=60, kit=kit, researched=set(researched), **kw)


def test_open_window_advises_cashing_in_identity():
    items = playbook.uu_timing_push(_inp({"TECH_IRON_WORKING"}))
    assert items and "窗口开着" in items[0].action
    assert any("timing push" in d for d in items[0].detail)
    # 长期机会不占 CORE 配额 —— 那一档留给"不做就赔"的风险项
    assert items[0].priority is not doctrine.Priority.CORE


def test_closing_window_is_core_and_irreversible():
    items = playbook.uu_timing_push(
        _inp({"TECH_IRON_WORKING"}, boosted={"TECH_APPRENTICESHIP"})
    )
    closing = [i for i in items if "即将关闭" in i.action]
    assert closing and closing[0].priority is doctrine.Priority.CORE
    assert "不可逆" in closing[0].consequence
    assert "先研究别的科技" in closing[0].alternative  # 窗口可主动延长


def test_locked_and_closed_windows_are_silent():
    assert playbook.uu_timing_push(_inp(set())) == []
    assert (
        playbook.uu_timing_push(_inp({"TECH_IRON_WORKING", "TECH_APPRENTICESHIP"}))
        == []
    )


def test_no_kit_no_advice():
    assert playbook.uu_timing_push(doctrine.DoctrineInput(turn=60)) == []


# ---------------------------------------------------------------------------
# 科研来源纠偏(playbook.research_rationale)
# ---------------------------------------------------------------------------


def _t335_power():
    me = pb.Side("罗马", 0, cities=8, pop=67, techs=39, science=78.6, military=1137)
    rivals = [
        pb.Side("刚果", 3, cities=11, pop=100, techs=60, science=310.0, military=642)
    ]
    return pb.measure(335, me, rivals)


def test_science_lag_points_at_population_and_campuses():
    """T335 形态:科研 0.25× —— 处方必须是补城/人口/学院,不是换科技顺序。"""
    inp = doctrine.DoctrineInput(turn=335, power=_t335_power())
    items = playbook.research_rationale(inp)
    src = [i for i in items if i.dedup_key == "playbook:research:science_source"]
    assert src, "科研落后时必须给出来源纠偏"
    assert "人口与学院" in src[0].action
    assert "补城" in src[0].alternative


def test_science_source_advice_counts_campuses_when_cities_known():
    cities = [NS(districts=["CAMPUS", "COMMERCIAL_HUB"]), NS(districts=[])]
    inp = doctrine.DoctrineInput(turn=335, power=_t335_power(), my_cities=cities)
    src = [
        i
        for i in playbook.research_rationale(inp)
        if i.dedup_key == "playbook:research:science_source"
    ]
    assert src and "有学院的 1 座" in src[0].reason


def test_no_science_source_advice_when_science_is_fine():
    me = pb.Side("我", 0, cities=10, techs=30, science=200.0, military=300)
    rivals = [pb.Side("AI", 1, cities=10, techs=50, science=210.0, military=300)]
    inp = doctrine.DoctrineInput(turn=200, power=pb.measure(200, me, rivals))
    assert not [
        i
        for i in playbook.research_rationale(inp)
        if i.dedup_key == "playbook:research:science_source"
    ]


# ---------------------------------------------------------------------------
# 占领单位硬约束(war_planner + doctrine)
# ---------------------------------------------------------------------------


def test_capture_capability_by_class():
    for ut in ("UNIT_WARRIOR", "UNIT_SWORDSMAN", "UNIT_SPEARMAN", "UNIT_KNIGHT"):
        assert wp.can_capture_city(ut), ut
    for ut in (
        "UNIT_CROSSBOWMAN",
        "UNIT_CATAPULT",
        "UNIT_FIELD_CANNON",
        "UNIT_FRIGATE",
    ):
        assert not wp.can_capture_city(ut), ut


def _war_civ():
    return NS(
        civ_name="阿拉伯",
        is_at_war=True,
        visible_cities=[NS(name="麦加", has_walls=False)],
    )


def _mil(ut):
    return NS(unit_type=ut, combat_strength=40, health=100, x=1, y=1)


def test_army_without_capture_units_is_flagged():
    """T335 编成:13 弩手 + 4 野战加农炮 —— 磨到 0 血也拿不下城。"""
    inp = doctrine.DoctrineInput(
        turn=335,
        civs=[_war_civ()],
        my_units=[_mil("UNIT_CROSSBOWMAN")] * 13 + [_mil("UNIT_FIELD_CANNON")] * 4,
    )
    items = [
        i
        for i in doctrine.asymmetric_engagement(inp)
        if i.dedup_key == "doctrine:engage:no_capture_unit"
    ]
    assert items and "占不下任何城" in items[0].action
    assert items[0].confidence is doctrine.Confidence.CERTAIN  # 机制事实,不是判断


def test_army_with_melee_is_not_flagged():
    inp = doctrine.DoctrineInput(
        turn=335,
        civs=[_war_civ()],
        my_units=[_mil("UNIT_CROSSBOWMAN"), _mil("UNIT_MUSKETMAN")],
    )
    assert not [
        i
        for i in doctrine.asymmetric_engagement(inp)
        if i.dedup_key == "doctrine:engage:no_capture_unit"
    ]


def test_capture_check_only_applies_at_war():
    inp = doctrine.DoctrineInput(
        turn=100,
        civs=[NS(civ_name="英国", is_at_war=False, visible_cities=[])],
        my_units=[_mil("UNIT_CROSSBOWMAN")] * 5,
    )
    assert doctrine.asymmetric_engagement(inp) == []
