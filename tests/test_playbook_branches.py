"""P7 §3.1 验收 —— 决策树 1-10 的每条分支喂合成局面,断言推荐结果。

每个 class 对应一棵树。**三条被校准局证伪的分支各有一对测试**:
一条断言改写后的新行为,一条断言旧行为不再出现(防止改回去)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_mcp import playbook
from civ_mcp import power_balance as pb
from civ_mcp.advice import Priority
from civ_mcp.doctrine import DoctrineInput


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@dataclass
class City:
    name: str = "罗马"
    x: int = 10
    y: int = 10
    population: int = 4
    production: float = 10.0
    districts: list = field(default_factory=list)
    loyalty_per_turn: float = 0.0
    turns_to_loyalty_flip: int = 0


@dataclass
class Unit:
    name: str = "勇士"
    unit_type: str = "UNIT_WARRIOR"
    combat_strength: int = 20
    ranged_strength: int = 0
    health: int = 100
    max_health: int = 100


@dataclass
class Civ:
    player_id: int = 1
    civ_name: str = "对手"
    has_met: bool = True
    is_at_war: bool = False
    diplomatic_state: str = "NEUTRAL"
    military_strength: int = 100
    agendas: list = field(default_factory=list)
    visible_cities: list = field(default_factory=list)


@dataclass
class VisCity:
    name: str = "开罗"
    has_walls: bool = True


@dataclass
class Threat:
    distance: int = 2
    owner_name: str = "Barbarian"
    owner_id: int = 63
    is_city_state: bool = False


def snap(
    my_cities=5,
    my_techs=20,
    my_mil=150,
    rival_cities=(6, 8),
    rival_techs=20,
    rival_mil=100,
    turn=110,
):
    me = pb.Side("我方", 0, cities=my_cities, techs=my_techs, military=my_mil)
    rivals = [
        pb.Side(
            f"对手{i}",
            i + 1,
            cities=c,
            techs=rival_techs,
            military=rival_mil,
            score=100,
        )
        for i, c in enumerate(rival_cities)
    ]
    return pb.measure(turn, me, rivals)


def actions(items) -> str:
    return " || ".join(a.action for a in items)


# ===========================================================================
# 决策树 1:开局
# ===========================================================================


class TestOpeningQueue:
    def test_no_military_by_t8_is_flagged(self):
        items = playbook.opening_queue(DoctrineInput(turn=10, my_units=[]))
        assert "军事单位" in actions(items)

    def test_silent_after_turn_30(self):
        assert playbook.opening_queue(DoctrineInput(turn=31, my_units=[])) == []

    def test_settler_when_pop_and_production_pass_the_line(self):
        items = playbook.opening_queue(
            DoctrineInput(
                turn=20,
                my_units=[Unit()],
                my_cities=[City(population=3, production=9.0)],
            )
        )
        assert "开拓者" in actions(items)

    def test_no_settler_advice_below_the_line(self):
        items = playbook.opening_queue(
            DoctrineInput(
                turn=20,
                my_units=[Unit()],
                my_cities=[City(population=1, production=4.0)],
            )
        )
        assert "转产开拓者" not in actions(items)

    def test_gold_route_when_production_is_short(self):
        items = playbook.opening_queue(
            DoctrineInput(
                turn=20,
                my_units=[Unit()],
                gold=300,
                my_cities=[City(population=1, production=4.0)],
            )
        )
        assert "买开拓者" in actions(items)

    def test_low_exploration_is_flagged(self):
        items = playbook.opening_queue(
            DoctrineInput(turn=12, my_units=[Unit()], exploration_pct=0.05)
        )
        assert "探图" in actions(items)


# ===========================================================================
# 决策树 2:蛮族(v3 已被证伪,这里钉死改写后的行为)
# ===========================================================================


class TestBarbarianResponse:
    def _inp(self, units, production):
        return DoctrineInput(
            turn=22, my_units=units, production=production, threats=[Threat(distance=2)]
        )

    def test_single_unit_low_production_forbids_the_hunt(self):
        """T22 实证:全国唯一勇士去追侦察兵,阵亡,开局崩盘。"""
        items = playbook.barbarian_response(self._inp([Unit()], 4))
        text = actions(items)
        assert "不要追击" in text and "驱离" in text

    def test_v3_hunt_all_units_wording_is_gone(self):
        """防回归:旧版是「立刻调所有可动单位追杀(最高优先级)」。"""
        items = playbook.barbarian_response(self._inp([Unit()], 4))
        assert "所有可动单位" not in actions(items)

    def test_two_units_and_cheap_replacement_allow_the_hunt(self):
        items = playbook.barbarian_response(self._inp([Unit(), Unit()], 20))
        text = actions(items)
        assert "追掉" in text and "不是全部" in text

    def test_camps_deferred_when_production_is_low_and_cities_few(self):
        """T10 实证:照 v3「先清营再出开拓者」,全图 6 个营地会一座城都铺不出来。"""
        items = playbook.barbarian_response(
            DoctrineInput(
                turn=10,
                my_units=[Unit()],
                production=4,
                my_cities=[City()],
                camps=[(5, 5, 4), (9, 9, 7)],
            )
        )
        assert "暂不清剿" in actions(items)

    def test_camps_cleared_once_the_army_can_afford_it(self):
        items = playbook.barbarian_response(
            DoctrineInput(
                turn=40,
                my_units=[Unit(), Unit()],
                production=20,
                my_cities=[City(), City(), City()],
                camps=[(5, 5, 4)],
            )
        )
        assert "清掉" in actions(items)

    def test_far_camps_do_not_trigger_clearing(self):
        items = playbook.barbarian_response(
            DoctrineInput(
                turn=40,
                my_units=[Unit(), Unit()],
                production=20,
                my_cities=[City(), City(), City()],
                camps=[(50, 50, 30)],
            )
        )
        assert "清掉" not in actions(items)


# ===========================================================================
# 决策树 3:邻居
# ===========================================================================


class TestNeighborPolicy:
    def test_strong_adjacent_neighbor_is_high_risk(self):
        items = playbook.neighbor_policy(
            DoctrineInput(
                turn=40,
                my_military=100,
                civs=[Civ(military_strength=200, visible_cities=[VisCity()])],
            )
        )
        assert "高危邻居" in actions(items)

    def test_friendly_state_suppresses_the_warning(self):
        items = playbook.neighbor_policy(
            DoctrineInput(
                turn=40,
                my_military=100,
                civs=[
                    Civ(
                        military_strength=200,
                        diplomatic_state="FRIENDLY",
                        visible_cities=[VisCity()],
                    )
                ],
            )
        )
        assert "高危邻居" not in actions(items)

    def test_weak_adjacent_neighbor_is_named_as_an_option(self):
        items = playbook.neighbor_policy(
            DoctrineInput(
                turn=40,
                my_military=200,
                civs=[Civ(military_strength=100, visible_cities=[VisCity()])],
            )
        )
        assert "贸易伙伴" in actions(items) or "开战对象" in actions(items)

    def test_no_visible_cities_means_no_adjacency_claim(self):
        items = playbook.neighbor_policy(
            DoctrineInput(
                turn=40,
                my_military=100,
                civs=[Civ(military_strength=300, visible_cities=[])],
            )
        )
        assert items == []

    def test_at_war_neighbours_are_left_to_the_war_tree(self):
        items = playbook.neighbor_policy(
            DoctrineInput(
                turn=40,
                my_military=100,
                civs=[
                    Civ(
                        military_strength=300,
                        is_at_war=True,
                        visible_cities=[VisCity()],
                    )
                ],
            )
        )
        assert items == []


# ===========================================================================
# 决策树 4:科技 / 市政
# ===========================================================================


class TestResearchRationale:
    def test_available_eurekas_are_surfaced(self):
        items = playbook.research_rationale(
            DoctrineInput(turn=30, boosted={"TECH_ARCHERY", "TECH_MINING"})
        )
        assert "尤里卡" in actions(items)

    def test_tech_deficit_narrows_the_line(self):
        items = playbook.research_rationale(
            DoctrineInput(turn=80, power=snap(my_techs=10, rival_techs=30))
        )
        assert "收窄" in actions(items)

    def test_no_deficit_no_narrowing(self):
        items = playbook.research_rationale(
            DoctrineInput(turn=80, power=snap(my_techs=30, rival_techs=30))
        )
        assert "收窄" not in actions(items)


# ===========================================================================
# 决策树 5:战争
# ===========================================================================


class TestWarTiming:
    def test_silent_without_l1(self):
        assert playbook.war_timing(DoctrineInput(turn=80, civs=[Civ()])) == []

    def test_military_advantage_opens_a_window(self):
        items = playbook.war_timing(
            DoctrineInput(turn=80, power=snap(my_mil=300, rival_mil=100), civs=[Civ()])
        )
        assert "开战性价比最高的窗口" in actions(items)

    def test_window_advice_warns_about_the_declaration_turn_delay(self):
        items = playbook.war_timing(
            DoctrineInput(turn=80, power=snap(my_mil=300, rival_mil=100), civs=[Civ()])
        )
        detail = " ".join(d for a in items for d in a.detail)
        assert "下回合" in detail

    def test_no_window_without_military_edge(self):
        items = playbook.war_timing(
            DoctrineInput(turn=80, power=snap(my_mil=100, rival_mil=100), civs=[Civ()])
        )
        assert "窗口" not in actions(items)

    def test_walls_without_siege_or_ranged_is_red(self):
        items = playbook.war_timing(
            DoctrineInput(
                turn=80,
                power=snap(),
                my_units=[Unit()],
                civs=[Civ(is_at_war=True, visible_cities=[VisCity()])],
            )
        )
        assert "🔴" in actions(items)

    def test_ranged_only_is_orange(self):
        archer = Unit(
            name="弓箭手",
            unit_type="UNIT_ARCHER",
            combat_strength=15,
            ranged_strength=25,
        )
        items = playbook.war_timing(
            DoctrineInput(
                turn=80,
                power=snap(),
                my_units=[archer],
                civs=[Civ(is_at_war=True, visible_cities=[VisCity()])],
            )
        )
        assert "🟠" in actions(items)

    def test_siege_present_and_strong_is_green(self):
        cat = Unit(name="石弩", unit_type="UNIT_CATAPULT", combat_strength=25)
        items = playbook.war_timing(
            DoctrineInput(
                turn=80,
                power=snap(my_mil=300, rival_mil=100),
                my_units=[cat],
                civs=[Civ(is_at_war=True, visible_cities=[VisCity()])],
            )
        )
        assert "🟢" in actions(items)

    def test_loyalty_countdown_forces_a_decision(self):
        items = playbook.war_timing(
            DoctrineInput(
                turn=80,
                power=snap(),
                civs=[Civ(is_at_war=True)],
                my_cities=[
                    City(name="哈丁", loyalty_per_turn=-7.0, turns_to_loyalty_flip=8)
                ],
            )
        )
        text = actions(items)
        assert "哈丁" in text and "叛变" in text

    def test_stable_city_gets_no_loyalty_advice(self):
        items = playbook.war_timing(
            DoctrineInput(
                turn=80,
                power=snap(),
                civs=[Civ(is_at_war=True)],
                my_cities=[City(loyalty_per_turn=2.0, turns_to_loyalty_flip=0)],
            )
        )
        assert "叛变" not in actions(items)


# ===========================================================================
# 决策树 6:扩城(阈值改为实测中位数)
# ===========================================================================


class TestExpansionTiming:
    def test_compared_against_measured_rival_median(self):
        items = playbook.expansion_timing(
            DoctrineInput(
                turn=100,
                power=snap(my_cities=5, rival_cities=(8, 10)),
                my_cities=[City()] * 5,
            )
        )
        assert "对手中位数" in actions(items)

    def test_no_hardcoded_difficulty_threshold_in_the_text(self):
        """防回归:v3 是「King: T50 应有 4 城, T100 应有 8 城」。"""
        items = playbook.expansion_timing(
            DoctrineInput(
                turn=100,
                difficulty="DIFFICULTY_KING",
                power=snap(my_cities=5, rival_cities=(8, 10)),
                my_cities=[City()] * 5,
            )
        )
        text = actions(items) + " ".join(a.consequence for a in items)
        assert "King" not in text and "难度" not in text

    def test_city_count_is_explicitly_not_a_verdict(self):
        items = playbook.expansion_timing(
            DoctrineInput(
                turn=100,
                power=snap(my_cities=5, rival_cities=(8, 10)),
                my_cities=[City()] * 5,
            )
        )
        assert any("不单独" in a.consequence for a in items)

    def test_ahead_of_median_switches_to_vertical(self):
        items = playbook.expansion_timing(
            DoctrineInput(
                turn=100,
                power=snap(my_cities=9, rival_cities=(4, 5)),
                my_cities=[City()] * 9,
            )
        )
        assert "纵向发展" in actions(items)

    def test_silent_without_rivals(self):
        assert (
            playbook.expansion_timing(
                DoctrineInput(
                    turn=100,
                    power=pb.measure(100, pb.Side("我", 0, cities=3), []),
                    my_cities=[City()] * 3,
                )
            )
            == []
        )


# ===========================================================================
# 决策树 7:区域布局
# ===========================================================================


class TestDistrictRoles:
    def test_grown_cities_without_districts_get_a_role(self):
        items = playbook.district_roles(
            DoctrineInput(turn=60, my_cities=[City(name="A"), City(name="B")])
        )
        assert "专长" in actions(items)

    def test_single_city_empire_is_too_early_for_division_of_labour(self):
        assert playbook.district_roles(DoctrineInput(turn=60, my_cities=[City()])) == []

    def test_small_cities_are_not_nagged(self):
        assert (
            playbook.district_roles(
                DoctrineInput(
                    turn=60, my_cities=[City(population=2), City(population=2)]
                )
            )
            == []
        )

    def test_already_specialised_cities_are_left_alone(self):
        specialised = City(districts=["DISTRICT_CAMPUS", "DISTRICT_HARBOR"])
        assert (
            playbook.district_roles(
                DoctrineInput(turn=60, my_cities=[specialised, specialised])
            )
            == []
        )


# ===========================================================================
# 决策树 8:伟人
# ===========================================================================


class TestGreatPersonPolicy:
    def test_needs_a_locked_victory_path(self):
        assert playbook.great_person_policy(DoctrineInput(turn=60)) == []

    def test_fires_on_review_turns_only(self):
        on = playbook.great_person_policy(
            DoctrineInput(turn=60, victory_type="DOMINATION")
        )
        off = playbook.great_person_policy(
            DoctrineInput(turn=61, victory_type="DOMINATION")
        )
        assert on and not off

    def test_advice_is_bonus_tier_not_core(self):
        items = playbook.great_person_policy(
            DoctrineInput(turn=60, victory_type="DOMINATION")
        )
        assert items[0].priority is Priority.BONUS


# ===========================================================================
# 决策树 9:中期
# ===========================================================================


class TestMidgameReview:
    def test_silent_before_midgame(self):
        assert (
            playbook.midgame_review(
                DoctrineInput(turn=40, power=snap(my_mil=10, rival_mil=300))
            )
            == []
        )

    def test_military_deficit_triggers_rearm(self):
        items = playbook.midgame_review(
            DoctrineInput(turn=80, power=snap(my_mil=50, rival_mil=300))
        )
        assert "扩军" in actions(items)

    def test_review_lists_measured_ratios_not_difficulty_advice(self):
        items = playbook.midgame_review(DoctrineInput(turn=80, power=snap()))
        text = actions(items)
        assert "路线复审" in text and "×" in text

    def test_review_is_periodic(self):
        assert not any(
            "路线复审" in a.action
            for a in playbook.midgame_review(DoctrineInput(turn=81, power=snap()))
        )


# ===========================================================================
# 决策树 10:危机
# ===========================================================================


class TestCrisisResponse:
    def test_no_war_no_crisis(self):
        assert (
            playbook.crisis_response(
                DoctrineInput(turn=80, civs=[Civ()], my_units=[Unit()])
            )
            == []
        )

    def test_civilians_are_pulled_in_first(self):
        builder = Unit(
            name="建造者",
            unit_type="UNIT_BUILDER",
            combat_strength=0,
            ranged_strength=0,
        )
        items = playbook.crisis_response(
            DoctrineInput(turn=80, civs=[Civ(is_at_war=True)], my_units=[builder])
        )
        assert items[0].priority is Priority.SURVIVAL
        assert "建造者" in items[0].action

    def test_weak_defender_is_told_to_turtle(self):
        items = playbook.crisis_response(
            DoctrineInput(
                turn=80,
                civs=[Civ(is_at_war=True)],
                power=snap(my_mil=50, rival_mil=300),
            )
        )
        assert "防守消耗" in actions(items)

    def test_strong_defender_is_not_told_to_turtle(self):
        items = playbook.crisis_response(
            DoctrineInput(
                turn=80,
                civs=[Civ(is_at_war=True)],
                power=snap(my_mil=300, rival_mil=100),
            )
        )
        assert "防守消耗" not in actions(items)


# ===========================================================================
# 汇总层
# ===========================================================================


class TestEvaluate:
    def test_every_tree_is_registered(self):
        """每棵树都要挂进 TREES —— 写过没接线的模块在这个项目里出现过太多次。

        2026-08-06:原来断言的是硬编码的棵数(11),加一棵树就得改一个数字,
        而那个数字本身不说明任何问题。改成"模块里所有决策树函数都在 TREES 里",
        这样漏接线会被抓住,正常新增则不需要动测试。
        """
        import inspect

        # 决策树 = 模块级公开函数,签名收 DoctrineInput、返回建议列表。
        # evaluate 是跑全部树的入口,签名相同但不是树本身,排除掉。
        candidates = {
            name
            for name, fn in inspect.getmembers(playbook, inspect.isfunction)
            if not name.startswith("_")
            and fn.__module__ == playbook.__name__
            and name != "evaluate"
            and list(inspect.signature(fn).parameters) == ["inp"]
        }
        registered = {fn.__name__ for fn in playbook.TREES}
        assert candidates - registered == set(), (
            f"这些决策树没有挂进 TREES(写了不接线等于没写):"
            f"{sorted(candidates - registered)}"
        )

    def test_empty_input_produces_nothing_and_does_not_raise(self):
        assert playbook.evaluate(DoctrineInput()) == []

    def test_one_broken_tree_does_not_take_down_the_rest(self, monkeypatch):
        def boom(_inp):
            raise RuntimeError("模拟单棵树崩了")

        monkeypatch.setattr(playbook, "TREES", (boom, playbook.opening_queue))
        assert playbook.evaluate(DoctrineInput(turn=10, my_units=[]))
