"""方案 v4 策略层(L1 局势测量 / L2 文明套件 / L3 决策树 / L4 执行纪律)。

每个测试对应方案 `方案_v4_随机文明神级取胜.md` 里的一条设计约束,
或校准局(rome_-758731782, T8-T110)的一条实证教训。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_mcp import civ_kit, discipline, doctrine
from civ_mcp import power_balance as pb
from civ_mcp.advice import Priority
from civ_mcp.safety import MoveContext, parse_move_safety


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@dataclass
class FakeCiv:
    player_id: int = 1
    civ_name: str = "对手"
    leader_name: str = "领袖"
    has_met: bool = True
    is_at_war: bool = False
    diplomatic_state: str = "NEUTRAL"
    military_strength: int = 100
    agendas: list = field(default_factory=list)
    visible_cities: list = field(default_factory=list)


@dataclass
class FakeAgenda:
    name: str


@dataclass
class FakeCity:
    name: str = "开罗"
    x: int = 30
    y: int = 36
    has_walls: bool = True


@dataclass
class FakeUnit:
    name: str = "罗马军团"
    unit_type: str = "UNIT_ROMAN_LEGION"
    combat_strength: int = 40
    ranged_strength: int = 0
    health: int = 100
    max_health: int = 100


@dataclass
class FakeThreat:
    distance: int = 3
    owner_name: str = "巴西"
    is_city_state: bool = False


def _snapshot(
    turn=110,
    techs=14,
    military=148,
    cities=5,
    rival_techs=36,
    rival_military=65,
    rival_cities=9,
):
    me = pb.Side(
        "我方", 0, cities=cities, techs=techs, military=military, science=30, culture=18
    )
    rivals = [
        pb.Side(
            "巴西",
            1,
            cities=rival_cities,
            techs=rival_techs,
            military=rival_military,
            science=90,
            culture=50,
            score=400,
        )
    ]
    return pb.measure(turn, me, rivals)


# ===========================================================================
# L1 局势测量
# ===========================================================================


class TestPowerBalance:
    def test_no_rivals_is_even_not_crushed(self):
        """还没遇到对手时不许判劣势 —— 没有测量对象就没有差距。"""
        snap = pb.measure(5, pb.Side("我方", 0, cities=1, techs=1), [])
        assert snap.tier is pb.PowerTier.EVEN
        assert "还没遇到" in snap.reasons[0]

    def test_early_game_small_absolute_values_do_not_classify(self):
        """方案 §2.2:绝对量太小的维度比值不作数(文化 70% 假象的成因)。"""
        me = pb.Side("我方", 0, cities=1, techs=2, military=20)
        snap = pb.measure(8, me, [pb.Side("对手", 1, cities=1, techs=3, military=25)])
        assert not snap.ratios["techs"].usable
        assert snap.tier is pb.PowerTier.EVEN

    def test_military_dominance_promotes_even_when_tech_crushed(self):
        """校准局 T110:科技 0.39× 但军力 2.28× —— 正确决策是开战而非龟缩。

        只看科技会判 CRUSHED 并建议求生,而实际靠宣战拿下了一座首都。
        """
        snap = _snapshot()
        assert snap.tech_ratio < 0.5
        assert snap.military_ratio > 1.5
        assert snap.tier is pb.PowerTier.BEHIND
        assert any("保质期" in r for r in snap.reasons)

    def test_weak_military_caps_tier_at_behind(self):
        """军力只有对手一半时,任何领先都可能被一次宣战清零。"""
        snap = _snapshot(techs=40, rival_techs=30, military=20, rival_military=200)
        assert snap.tier <= pb.PowerTier.BEHIND

    def test_both_axes_low_is_crushed(self):
        snap = _snapshot(techs=10, rival_techs=40, military=30, rival_military=200)
        assert snap.tier is pb.PowerTier.CRUSHED

    def test_dominant_when_ahead_on_both(self):
        snap = _snapshot(techs=40, rival_techs=20, military=300, rival_military=100)
        assert snap.tier is pb.PowerTier.DOMINANT

    def test_strongest_axis_names_the_single_bet(self):
        """BEHIND 档的「单点押注」必须给出押哪一点。"""
        snap = _snapshot()
        axis = snap.strongest_axis
        assert axis is not None and axis.dim == "military"
        assert "军力" in pb.headline(snap)

    def test_per_dimension_best_rival_not_single_top_civ(self):
        """各维度各自取最强对手 —— 科技落后与否看科技最快那家。"""
        me = pb.Side("我方", 0, cities=5, techs=20, military=200)
        rivals = [
            pb.Side("科技国", 1, cities=3, techs=40, military=10, score=100),
            pb.Side("军事国", 2, cities=3, techs=10, military=400, score=500),
        ]
        snap = pb.measure(80, me, rivals)
        assert snap.ratios["techs"].best_name == "科技国"
        assert snap.ratios["military"].best_name == "军事国"

    def test_history_tracks_expansion_and_trend(self):
        hist = pb.PowerHistory()
        old = _snapshot(turn=90, cities=3)
        hist.record(old)
        now = _snapshot(turn=110, cities=5)
        pb.attach_history(now, hist)
        assert now.my_expansion_rate == 0.1  # 2 城 / 20 回合
        assert hist.trend(now)

    def test_history_refuses_to_extrapolate_from_one_point(self):
        """只有一个采样点时不报趋势 —— 宁可不报也不编。"""
        hist = pb.PowerHistory()
        snap = _snapshot()
        pb.attach_history(snap, hist)
        assert snap.my_expansion_rate is None

    def test_render_never_raises_on_garbage(self):
        assert pb.render(None) == ""  # type: ignore[arg-type]

    def test_every_dimension_has_a_signal_floor(self):
        """漏配门槛的维度会恒为"可用",于是双方都是 1 也能算出 1.00×。"""
        assert set(pb._DIMS) <= set(pb.MIN_SIGNAL)

    def test_early_civics_parity_is_not_a_strength(self):
        """2026-08-03 目视检查实抓:T22 时「市政 1.00×」被当成最强轴,
        产出「资源优先给市政」——而当时双方各只有 1 个市政。"""
        me = pb.Side("我方", 0, cities=1, techs=3, civics=1, military=20)
        rivals = [pb.Side("AI", 1, cities=3, techs=8, civics=1, military=120)]
        snap = pb.measure(22, me, rivals)
        assert not snap.ratios["civics"].usable
        axis = snap.strongest_axis
        assert axis is None or axis.dim != "civics"
        assert "市政" not in pb.headline(snap)


# ===========================================================================
# L2 文明套件推导
# ===========================================================================


class TestCivKit:
    def test_legion_needs_no_iron_swordsman_does(self):
        """校准局 0a:手写模板写着「军团需要铁」,与 Units.json 相反。"""
        kit = civ_kit.derive(
            "CIVILIZATION_ROME",
            unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"},
        )
        legion = kit.uu[0]
        assert legion.strategic_resource == ""
        assert legion.base_resource == "RESOURCE_IRON"
        assert legion.resource_free
        assert "不需要战略资源" in civ_kit.render(kit)

    def test_runtime_data_marked_authoritative(self):
        kit = civ_kit.derive(
            "CIVILIZATION_ROME",
            unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"},
        )
        assert kit.uu[0].attribution == "runtime"
        assert "〔归属推断〕" not in civ_kit.render(kit)

    def test_inferred_attribution_is_labelled(self):
        """没有实机数据时按前缀推断,但必须标出来(诚实机制)。"""
        kit = civ_kit.derive("CIVILIZATION_ROME")
        assert kit.uu and kit.uu[0].attribution == "inferred"
        assert "〔归属推断〕" in civ_kit.render(kit)

    def test_standalone_uu_without_replacement_is_found(self):
        """战象/虎蹲炮不在 UnitReplaces 里,漏掉等于误报「该文明没特色」。"""
        kit = civ_kit.derive("CIVILIZATION_INDIA")
        assert any(u.unit_type == "UNIT_INDIAN_VARU" for u in kit.uu)

    def test_warrior_monk_excluded_from_generic_baseline(self):
        """武僧 CS40 无前置科技,算进基线会把远古 UU 的代差全部抹成 0。"""
        assert not civ_kit.is_generic_unit("UNIT_WARRIOR_MONK")
        assert not civ_kit.is_generic_unit("UNIT_BARBARIAN_HORSEMAN")
        assert not civ_kit.is_generic_unit("UNIT_ROMAN_LEGION")
        assert civ_kit.is_generic_unit("UNIT_SWORDSMAN")

    def test_peer_prefers_same_promotion_class(self):
        """武士(近战 48)该和披甲战士(45)比,不和骑士(重骑 50)比。"""
        kit = civ_kit.derive("CIVILIZATION_JAPAN")
        w = kit.best_window
        assert w is not None
        assert w.peer_type == "UNIT_MAN_AT_ARMS"
        assert w.edge > 0

    def test_window_has_open_and_close_era(self):
        kit = civ_kit.derive(
            "CIVILIZATION_ROME",
            unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"},
        )
        w = kit.best_window
        assert w is not None
        assert w.uu.era == "ERA_CLASSICAL"
        assert w.counter_era_index > w.uu.era_index

    def test_marginal_window_does_not_claim_war_is_correct(self):
        """UU 代差小时不许说「开战」—— 是否开战归 L1 军力比管。"""
        kit = civ_kit.derive(
            "CIVILIZATION_ROME",
            unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"},
        )
        text = civ_kit.render(kit)
        assert "L1 军力比" in text

    def test_military_lean_never_negative(self):
        kit = civ_kit.derive("CIVILIZATION_GREECE")
        assert kit.lean("military") >= 0.0

    def test_unknown_civ_says_so_instead_of_guessing(self):
        kit = civ_kit.derive("CIVILIZATION_NOT_A_REAL_CIV")
        assert not kit.uu
        assert any("不要假设" in n for n in kit.notes)


# ===========================================================================
# L3 决策树
# ===========================================================================


class TestSurvivalTree:
    def test_silent_without_power_snapshot(self):
        """没有 L1 档位就不出手 —— 坏传感器 + 决策树 = 自信的错误决策。"""
        assert doctrine.survival_priority(doctrine.DoctrineInput(turn=50)) == []

    def test_silent_when_not_behind(self):
        snap = _snapshot(techs=40, rival_techs=20, military=300, rival_military=100)
        inp = doctrine.DoctrineInput(turn=110, power=snap)
        assert doctrine.survival_priority(inp) == []

    def test_massing_enemy_triggers_survival_priority(self):
        snap = _snapshot(techs=10, rival_techs=40, military=30, rival_military=200)
        inp = doctrine.DoctrineInput(
            turn=110, power=snap, threats=[FakeThreat() for _ in range(3)]
        )
        items = doctrine.survival_priority(inp)
        assert any(a.priority is Priority.SURVIVAL and "屯" in a.action for a in items)

    def test_lone_scout_is_not_massing(self):
        snap = _snapshot(techs=10, rival_techs=40, military=30, rival_military=200)
        inp = doctrine.DoctrineInput(turn=110, power=snap, threats=[FakeThreat()])
        assert not any("屯" in a.action for a in doctrine.survival_priority(inp))

    def test_two_front_advises_appeasing_the_weaker(self):
        snap = _snapshot()
        civs = [
            FakeCiv(
                player_id=1,
                civ_name="强敌",
                diplomatic_state="UNFRIENDLY",
                military_strength=300,
            ),
            FakeCiv(
                player_id=2,
                civ_name="弱敌",
                diplomatic_state="HOSTILE",
                military_strength=50,
            ),
        ]
        items = doctrine.survival_priority(
            doctrine.DoctrineInput(turn=110, power=snap, civs=civs)
        )
        assert any("弱敌" in a.action for a in items)

    def test_deep_tech_deficit_drops_wonders(self):
        snap = _snapshot()
        items = doctrine.survival_priority(doctrine.DoctrineInput(turn=110, power=snap))
        assert any("奇观" in a.action for a in items)


class TestAgendaTree:
    def test_localized_agenda_name_resolves_to_type(self):
        """opponents.read_opponent 拿中文名匹配 AGENDA_* 永远失败,这里修掉。"""
        assert doctrine.agenda_type_from_name("尼罗河女王") == "AGENDA_QUEEN_OF_NILE"
        assert doctrine.agenda_type_from_name("不存在的议程") == ""

    def test_leader_historical_agenda_resolves(self):
        assert doctrine.historical_agenda("LEADER_BARBAROSSA") == "AGENDA_IRON_CROWN"

    def test_hidden_agenda_is_skipped_not_guessed(self):
        civ = FakeCiv(agendas=[FakeAgenda("???")])
        assert doctrine.resolve_agendas(civ) == []

    def test_high_risk_agenda_gets_core_priority_and_remedy(self):
        civ = FakeCiv(player_id=3, civ_name="德国", agendas=[])
        items = doctrine.agenda_management(
            doctrine.DoctrineInput(
                turn=40, civs=[civ], leader_types={3: "LEADER_BARBAROSSA"}
            )
        )
        assert items and items[0].priority is Priority.CORE
        assert "城邦" in items[0].action

    def test_low_risk_agenda_is_noise_and_skipped(self):
        civ = FakeCiv(agendas=[FakeAgenda("文化人")])
        assert (
            doctrine.agenda_management(doctrine.DoctrineInput(turn=40, civs=[civ]))
            == []
        )

    def test_already_at_war_skips_agenda_advice(self):
        civ = FakeCiv(player_id=3, is_at_war=True)
        assert (
            doctrine.agenda_management(
                doctrine.DoctrineInput(
                    turn=40, civs=[civ], leader_types={3: "LEADER_BARBAROSSA"}
                )
            )
            == []
        )


class TestEngagementTree:
    def _war_input(self, units):
        civ = FakeCiv(civ_name="阿拉伯", is_at_war=True, visible_cities=[FakeCity()])
        return doctrine.DoctrineInput(turn=110, civs=[civ], my_units=units)

    def test_silent_at_peace(self):
        inp = doctrine.DoctrineInput(turn=110, civs=[FakeCiv()], my_units=[FakeUnit()])
        assert doctrine.asymmetric_engagement(inp) == []

    def test_walls_without_siege_blocks_the_assault(self):
        items = doctrine.asymmetric_engagement(self._war_input([FakeUnit()]))
        assert any("不要进攻" in a.action for a in items)

    def test_having_siege_units_removes_the_wall_warning(self):
        siege = FakeUnit(name="石弩", unit_type="UNIT_CATAPULT", combat_strength=25)
        items = doctrine.asymmetric_engagement(self._war_input([siege]))
        assert not any("不要进攻" in a.action for a in items)

    def test_wounded_unit_rotation_cites_t108(self):
        hurt = FakeUnit(health=51)
        items = doctrine.asymmetric_engagement(self._war_input([hurt]))
        rotate = [a for a in items if "回血" in a.action]
        assert rotate and "T108" in rotate[0].consequence

    def test_evaluate_never_raises(self):
        assert doctrine.evaluate(doctrine.DoctrineInput()) == []


# ===========================================================================
# L4 执行纪律
# ===========================================================================


def _ctx(**kw) -> MoveContext:
    base = dict(
        found=True,
        unit_type="UNIT_ROMAN_LEGION",
        formation="FORMATION_CLASS_LAND_COMBAT",
        promotion="PROMOTION_CLASS_MELEE",
        hp=100,
        max_hp=100,
        my_military_total=3,
        target_is_enemy_city=True,
        enemy_city_name="开罗",
    )
    base.update(kw)
    return MoveContext(**base)


class TestDiscipline:
    def test_low_hp_melee_assault_on_city_blocked(self):
        """T108 实证:51 血军团攻开罗,阵亡。"""
        v = discipline.check_low_hp_assault(_ctx(hp=51))
        assert v.blocked and v.code == "LOW_HP_ASSAULT"
        assert "T108" in v.message

    def test_healthy_unit_passes(self):
        assert not discipline.check_low_hp_assault(_ctx(hp=80)).blocked

    def test_ranged_low_hp_city_attack_passes(self):
        """远程攻城零反伤,拦它就是误伤 —— 误伤会养成无脑 force 的习惯。"""
        v = discipline.check_low_hp_assault(
            _ctx(hp=20, promotion="PROMOTION_CLASS_RANGED")
        )
        assert not v.blocked

    def test_low_hp_field_attack_passes(self):
        """只拦攻城:野战里残血单位补最后一刀常常是对的。"""
        assert not discipline.check_low_hp_assault(
            _ctx(hp=20, target_is_enemy_city=False)
        ).blocked

    def test_missing_hp_data_does_not_block(self):
        """缺数据时不拦截,不猜。"""
        assert not discipline.check_low_hp_assault(_ctx(hp=0, max_hp=0)).blocked

    def test_last_military_unit_blocked(self):
        """T22 实证:全国唯一勇士去追蛮族侦察兵,阵亡,开局崩盘。"""
        v = discipline.check_last_military_unit(_ctx(my_military_total=1))
        assert v.blocked and v.code == "LAST_MILITARY_UNIT"
        assert "T22" in v.message

    def test_second_military_unit_lifts_the_block(self):
        assert not discipline.check_last_military_unit(
            _ctx(my_military_total=2)
        ).blocked

    def test_civilian_not_subject_to_attack_rules(self):
        assert not discipline.check_last_military_unit(
            _ctx(my_military_total=1, formation="FORMATION_CLASS_CIVILIAN")
        ).blocked

    def test_staggered_assault_blocked_when_reinforcements_pending(self):
        v = discipline.check_staggered_assault(
            _ctx(adjacent_military=0, nearby_military=2)
        )
        assert v.blocked and v.code == "STAGGERED_ASSAULT"

    def test_no_reinforcements_means_no_stagger_block(self):
        """本来就没有援军可等,拦了也没用。"""
        assert not discipline.check_staggered_assault(
            _ctx(adjacent_military=0, nearby_military=1)
        ).blocked

    def test_friendly_unit_already_adjacent_is_not_staggered(self):
        assert not discipline.check_staggered_assault(
            _ctx(adjacent_military=1, nearby_military=3)
        ).blocked

    def test_force_bypasses_every_check(self):
        ctx = _ctx(hp=10, my_military_total=1, nearby_military=5)
        assert not discipline.check_attack(ctx, force=True).blocked

    def test_check_attack_orders_by_regret(self):
        """输掉整局 > 赔一个单位:唯一军事单位的拦截排在残血之前。"""
        ctx = _ctx(hp=10, my_military_total=1)
        assert discipline.check_attack(ctx).code == "LAST_MILITARY_UNIT"


class TestCombatMemory:
    def test_first_attack_passes(self):
        mem = discipline.CombatMemory()
        assert not discipline.check_unread_combat_state(mem, 5, 10).blocked

    def test_second_attack_without_reread_blocked(self):
        mem = discipline.CombatMemory()
        mem.notice_attack(5, 10)
        v = discipline.check_unread_combat_state(mem, 5, 10)
        assert v.blocked and v.code == "UNREAD_COMBAT_STATE"

    def test_reading_units_lifts_the_block_without_force(self):
        mem = discipline.CombatMemory()
        mem.notice_attack(5, 10)
        mem.notice_units_read()
        assert not discipline.check_unread_combat_state(mem, 5, 10).blocked

    def test_new_turn_resets(self):
        mem = discipline.CombatMemory()
        mem.notice_attack(5, 10)
        assert not discipline.check_unread_combat_state(mem, 5, 11).blocked

    def test_other_unit_unaffected(self):
        mem = discipline.CombatMemory()
        mem.notice_attack(5, 10)
        assert not discipline.check_unread_combat_state(mem, 6, 10).blocked


class TestSafetyQueryExtensions:
    def test_query_emits_new_fields(self):
        from civ_mcp.safety import build_move_safety_query

        lua = build_move_safety_query(1, 10, 10)
        for token in (
            'print("HP|"',
            'print("ENEMYCITY|"',
            'print("MYMIL|"',
            'print("NEARBY|"',
        ):
            assert token in lua

    def test_query_avoids_ingame_only_globals(self):
        """本查询走 GameCore,Cities.GetCityInPlot 在那里不存在。

        断言的是【调用】不存在,不是字符串不出现 —— 注释里提到这个 API
        是合法的(校准局踩过一次「断言匹配到注释」的坑)。
        """
        from civ_mcp.safety import build_move_safety_query

        assert "Cities.GetCityInPlot(" not in build_move_safety_query(1, 5, 5)

    def test_parser_reads_new_fields(self):
        ctx = parse_move_safety(
            [
                "UNIT|UNIT_ROMAN_LEGION|FORMATION_CLASS_LAND_COMBAT|"
                "PROMOTION_CLASS_MELEE|2|2|10|10",
                "HP|51|100",
                "ENEMYCITY|开罗",
                "MYMIL|4",
                "NEARBY|2",
            ]
        )
        assert ctx.hp == 51 and ctx.max_hp == 100
        assert ctx.hp_pct == 51.0
        assert ctx.target_is_enemy_city and ctx.enemy_city_name == "开罗"
        assert ctx.my_military_total == 4 and ctx.nearby_military == 2

    def test_parser_tolerates_missing_new_fields(self):
        ctx = parse_move_safety(
            [
                "UNIT|UNIT_BUILDER|FORMATION_CLASS_CIVILIAN||2|2|10|10",
            ]
        )
        assert ctx.hp_pct is None
        assert not ctx.target_is_enemy_city


# ===========================================================================
# 接线:简报里的策略层
# ===========================================================================


class TestBriefingWiring:
    def test_tier_headline_goes_first(self):
        """档位是读其余每一行的解释框架,必须在局面段最前。"""
        from civ_mcp.end_turn import _prepend_strategy

        snap = _snapshot()
        out = _prepend_strategy("🟢 威胁 GREEN", 110, snap, None, None)
        assert out.split("\n")[0].startswith("T110 实测档位")
        assert "威胁 GREEN" in out

    def test_missing_power_leaves_situation_untouched(self):
        from civ_mcp.end_turn import _prepend_strategy

        assert _prepend_strategy("原样", 50, None, None, None) == "原样"

    def test_kit_lines_only_on_review_turns(self):
        """套件是常驻上下文,每回合复述是噪音;按间隔复述。"""
        from civ_mcp.end_turn import _prepend_strategy

        kit = civ_kit.derive(
            "CIVILIZATION_ROME", unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}
        )

        class _Scan:
            techs = {"TECH_IRON_WORKING"}

        on = _prepend_strategy("", 20, None, kit, _Scan())
        off = _prepend_strategy("", 21, None, kit, _Scan())
        assert "罗马军团" in on
        assert off == ""

    def test_tier_gated_trees_stay_silent_without_l1(self):
        """没有 L1 档位时,凡是读档位的树都必须静默。

        不受门控的是**资源事实**类建议(T10 还没有军事单位),它们不依赖
        与对手的比较,缺 L1 也照样成立。
        """
        from civ_mcp.end_turn import strategy_items

        class _Scan:
            military_strength = 0
            production = 0
            techs: set = set()
            civics: set = set()
            units: dict = {}

        items = strategy_items(None, None, _Scan(), 110, None, None, [], [], [], [])
        gated = {
            "playbook.war",
            "playbook.midgame",
            "playbook.expand",
            "doctrine.survival",
        }
        assert not [a for a in items if a.module in gated]
