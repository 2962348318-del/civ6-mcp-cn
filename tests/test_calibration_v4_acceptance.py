"""P7 验收 —— 端到端:合成局面 → L1 → L2 → L3 → arbitrate → 简报文本。

分支级测试在 `test_playbook_branches.py` 与 `test_strategy_layer.py`。
这个文件测的是**层与层之间的接缝**:分档有没有真的传到决策树、
决策树的输出有没有真的排进简报前几行、该覆盖的有没有覆盖。

三个局面全部是**校准局的真实回放**或方案 v4 §3.2 的验收配置,
断言的是"当时系统没给、现在必须给"的那句话。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_mcp import advice as advice_mod
from civ_mcp import civ_kit, doctrine, playbook
from civ_mcp import power_balance as pb


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@dataclass
class City:
    name: str = "罗马"
    population: int = 7
    production: float = 20.0
    districts: list = field(default_factory=list)
    loyalty_per_turn: float = 0.0
    turns_to_loyalty_flip: int = 0


@dataclass
class Unit:
    name: str = "罗马军团"
    unit_type: str = "UNIT_ROMAN_LEGION"
    combat_strength: int = 40
    ranged_strength: int = 0
    health: int = 100
    max_health: int = 100


@dataclass
class VisCity:
    name: str = "开罗"
    has_walls: bool = True


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
class Threat:
    distance: int = 3
    owner_name: str = "巴西"
    owner_id: int = 1
    is_city_state: bool = False


def brief(inp: doctrine.DoctrineInput) -> str:
    """跑完整策略栈并渲染成简报正文 —— 和实机走的是同一条路径。"""
    items = playbook.evaluate(inp) + doctrine.evaluate(inp)
    situation = pb.headline(inp.power) if inp.power else ""
    return advice_mod.arbitrate(items, situation=situation)


# ===========================================================================
# 局面 1:校准局 T110 回放
#   罗马 5 城 / 科技 14 / 军力 148 ‖ 巴西 9 城 / 科技 36 / 军力 65
#   与阿拉伯交战中,正围攻有城墙的开罗,手上一个 51 血的军团、没有攻城单位。
#   当时系统给的是:什么都没给。下面每一条都是它当时该说而没说的。
# ===========================================================================


def _t110() -> doctrine.DoctrineInput:
    me = pb.Side(
        "罗马",
        0,
        cities=5,
        pop=20,
        techs=14,
        civics=12,
        science=30,
        culture=18,
        military=148,
    )
    rivals = [
        pb.Side(
            "巴西",
            1,
            cities=9,
            pop=40,
            techs=36,
            civics=25,
            science=90,
            culture=50,
            military=65,
            score=400,
        ),
        pb.Side(
            "阿拉伯",
            2,
            cities=3,
            pop=10,
            techs=20,
            civics=15,
            science=40,
            culture=20,
            military=65,
            score=150,
        ),
    ]
    power = pb.measure(110, me, rivals)
    kit = civ_kit.derive(
        "CIVILIZATION_ROME", unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}
    )
    return doctrine.DoctrineInput(
        turn=110,
        power=power,
        kit=kit,
        production=20,
        civs=[
            Civ(
                player_id=1,
                civ_name="巴西",
                diplomatic_state="UNFRIENDLY",
                military_strength=65,
            ),
            Civ(
                player_id=2,
                civ_name="阿拉伯",
                is_at_war=True,
                military_strength=65,
                visible_cities=[VisCity()],
            ),
        ],
        threats=[Threat() for _ in range(3)],
        my_units=[
            Unit(health=51),
            Unit(name="罗马军团2"),
            Unit(
                name="弓箭手",
                unit_type="UNIT_ARCHER",
                combat_strength=15,
                ranged_strength=25,
                health=90,
            ),
        ],
        my_cities=[City(name=f"城{i}") for i in range(5)],
        my_military=148,
        researched={"TECH_IRON_WORKING"},
    )


class TestT110Replay:
    def test_tier_is_behind_not_crushed(self):
        """科技 0.39× 会让单轴分类判被碾压;军力 2.28× 必须把它拉回来。"""
        assert _t110().power.tier is pb.PowerTier.BEHIND

    def test_headline_names_the_single_bet(self):
        assert "军力" in pb.headline(_t110().power)

    def test_wounded_legion_is_told_to_rotate_out(self):
        """T108 就是 51 血的军团继续攻城阵亡的。"""
        text = brief(_t110())
        assert "回血" in text and "51%" in text

    def test_walled_city_without_siege_is_a_refusal(self):
        text = brief(_t110())
        assert "不要进攻" in text

    def test_massing_neighbour_reaches_the_survival_band(self):
        """劣势档 + 屯兵 → 必须排在最前且永不截断。"""
        text = brief(_t110())
        head = text.split("== 主线 ==")[0]
        assert "屯了 3 个单位" in head

    def test_wonder_race_is_called_off(self):
        assert "放弃奇观" in brief(_t110())

    def test_legion_window_is_not_sold_as_a_reason_to_fight(self):
        """UU 代差只有 +5,不构成开战理由 —— 开战理由是军力比。"""
        text = civ_kit.render(_t110().kit)
        assert "L1 军力比" in text

    def test_city_count_deficit_is_reported_against_measured_median(self):
        text = brief(_t110())
        assert "对手中位数" in text or "补" in text

    def test_no_difficulty_name_anywhere_in_the_output(self):
        """难度可能是随机的,任何按难度名下的结论都没有预测力。"""
        text = brief(_t110())
        for token in ("国王", "神级", "King", "Deity", "DIFFICULTY_"):
            assert token not in text


# ===========================================================================
# 局面 2:神级开局(方案 v4 §3.2 第 3 局的前 30 回合形态)
#   AI 起手就领先,我方 1 城 4 产能、唯一一个勇士,附近有蛮族侦察兵。
#   v3 的决策树 2 在这里会命令追杀 —— 那正是 T22 赔掉勇士的原样复刻。
# ===========================================================================


def _deity_opening() -> doctrine.DoctrineInput:
    me = pb.Side("我方", 0, cities=1, techs=3, military=20)
    rivals = [
        pb.Side(f"AI{i}", i + 1, cities=3, techs=8, military=120, score=200)
        for i in range(3)
    ]
    return doctrine.DoctrineInput(
        turn=22,
        power=pb.measure(22, me, rivals),
        production=4,
        my_units=[Unit(name="勇士", unit_type="UNIT_WARRIOR", combat_strength=20)],
        my_cities=[City(name="罗马", population=3, production=4.0)],
        threats=[
            Threat(distance=2, owner_name="Barbarian", owner_id=63),
        ],
        camps=[(5, 5, 4), (12, 3, 9)],
        my_military=20,
        exploration_pct=0.06,
    )


class TestDeityOpening:
    def test_tier_is_crushed(self):
        assert _deity_opening().power.tier is pb.PowerTier.CRUSHED

    def test_lone_warrior_is_told_not_to_hunt(self):
        text = brief(_deity_opening())
        assert "不要追击" in text and "驱离" in text

    def test_the_falsified_v3_wording_never_appears(self):
        assert "所有可动单位" not in brief(_deity_opening())

    def test_camps_do_not_preempt_expansion(self):
        """全图 6 个营地时照 v3「先清营再出开拓者」会一座城都铺不出来。"""
        assert "暂不清剿" in brief(_deity_opening())

    def test_survival_mode_is_the_first_line_of_the_body(self):
        text = brief(_deity_opening())
        body = text.split("\n\n", 1)[1] if "\n\n" in text else text
        assert "活下去" in body.split("\n")[0]

    def test_exploration_gap_is_still_raised_in_survival_mode(self):
        """求生不等于停止探图 —— 看不见的营地不会停止出兵。"""
        assert "探图" in brief(_deity_opening())


# ===========================================================================
# 局面 3:领先档收官(方案 v4 §3.2 第 1 局的后半形态)
# ===========================================================================


def _dominant() -> doctrine.DoctrineInput:
    me = pb.Side("我方", 0, cities=12, techs=45, military=400)
    rivals = [pb.Side("AI", 1, cities=5, techs=25, military=120, score=200)]
    return doctrine.DoctrineInput(
        turn=140,
        power=pb.measure(140, me, rivals),
        production=60,
        victory_type="DOMINATION",
        civs=[Civ(military_strength=120, visible_cities=[VisCity()])],
        my_units=[Unit() for _ in range(8)],
        my_cities=[
            City(name=f"城{i}", districts=["DISTRICT_CAMPUS"]) for i in range(12)
        ],
        my_military=400,
    )


class TestDominantEndgame:
    def test_tier_is_dominant(self):
        assert _dominant().power.tier is pb.PowerTier.DOMINANT

    def test_survival_tree_stays_out_of_the_way(self):
        assert "活下去" not in brief(_dominant())

    def test_expansion_switches_to_vertical(self):
        assert "纵向发展" in brief(_dominant())

    def test_war_window_is_surfaced(self):
        assert "开战性价比最高的窗口" in brief(_dominant())

    def test_declaration_turn_delay_is_spelled_out(self):
        """宣战当回合打不了 —— 这条不说,窗口建议会被执行成白扔一回合。"""
        assert "下回合" in brief(_dominant())


# ===========================================================================
# 全局不变量 —— 三个局面都要满足
# ===========================================================================


class TestInvariants:
    def _all(self):
        return (_t110(), _deity_opening(), _dominant())

    def test_briefing_never_empty(self):
        for inp in self._all():
            assert brief(inp).strip()

    def test_briefing_respects_the_line_budget(self):
        for inp in self._all():
            body = brief(inp)
            assert len(body.split("\n")) <= advice_mod.BRIEFING_MAX_LINES + 12

    def test_survival_band_is_never_truncated(self):
        """生存档建议被截断 = 这一层白做。"""
        inp = _deity_opening()
        items = playbook.evaluate(inp) + doctrine.evaluate(inp)
        survival = [a for a in items if a.priority is advice_mod.Priority.SURVIVAL]
        text = advice_mod.arbitrate(items, line_budget=5)
        for a in survival:
            assert a.action[:12] in text

    def test_no_advice_promises_unmeasured_numbers(self):
        """方案 v4 §5:AI 战术弱点的量级没有实测支撑,一个字都不许写。"""
        for inp in self._all():
            text = brief(inp)
            for banned in ("1.5-2 倍兵力", "能吃掉 2 倍", "80% 产能加成"):
                assert banned not in text

    def test_every_advice_has_a_reason_or_consequence(self):
        """答不出"为什么是现在"的建议自动降级 —— 这里直接钉死。"""
        for inp in self._all():
            for a in playbook.evaluate(inp) + doctrine.evaluate(inp):
                assert a.reason or a.consequence, a.action

    def test_gamble_tier_advice_always_carries_a_fallback(self):
        for inp in self._all():
            for a in playbook.evaluate(inp) + doctrine.evaluate(inp):
                if a.confidence is advice_mod.Confidence.GAMBLE:
                    assert a.alternative, a.action
