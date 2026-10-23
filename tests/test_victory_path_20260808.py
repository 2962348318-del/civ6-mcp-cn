"""2026-08-08 苏美尔实机局 T190:统治胜利进度漏算未接触文明。

发现经过(真实对局,不是设想场景):
  用户问「美国都原子能时代了我们真的能打过吗」。查下来美国是 era 6 但军事科技
  一片空白 —— 打得过。但顺手核对统治胜利进度时才发现更要命的事:

  get_victory_progress 报「Domination 40% — 2 known capitals to capture」,
  DOMINATION 段只列出 4 个文明。而本局有 6 个存活大文明,希腊和刚果从未接触。
  统治胜利要求拥有【全部】原始首都,所以真实剩余是 4 座,其中 2 座在只探索了
  40% 的地图的未知大陆上。

根因:lua/victory.py 的整段查询外层是 `if pDiplo:HasMet(i) or i == me`,
未接触的文明连 PLAYER 行都不发,CAPITAL 行自然也没有,统治分母直接少 2。

后果不是显示错误那么轻 —— 我在 T158 锁定统治胜利,之后 30 多回合一直读到
「只差 2 座首都」,从没重新评估过路径。用户的原话:「明明时代落后很多但是你
不在意就只是想打架也不想着别的方法获胜」。工具给了一个虚假的近在咫尺的胜利,
这是那个行为的直接成因。

修法:
  1. Lua 增发 DOMCIV| 行,枚举全部存活大文明(met 与否都发)。未接触文明的
     首都归属对我不可见,如实标 unknown,但必须计入分母。
  2. 渲染层加「统治进度:敌方首都 x/y 到手」台账 + 未接触文明警告。
  3. 评估分数随未接触文明数塌陷 —— 光把它们计入分母不够,只要「军力强」还能
     撑住 40%,"能打" 就会继续盖过 "这条路要多久"。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import narrate
from civ_mcp.lua.models import DominationCiv, VictoryPlayerProgress
from civ_mcp.lua.victory import (
    build_victory_progress_query,
    parse_victory_progress_response,
)


def _civ(pid, met, name, holds):
    return DominationCiv(player_id=pid, met=met, name=name, holds_own_capital=holds)


# 本局 T190 实况:我=0(苏美尔), 1=希腊未接触, 2=美国, 3=刚果未接触,
# 4=日本(首都已被我打下), 5=印度
T190_CIVS = [
    _civ(0, True, "苏美尔", True),
    _civ(1, False, "UNKNOWN", None),
    _civ(2, True, "美国", True),
    _civ(3, False, "UNKNOWN", None),
    _civ(4, True, "日本", False),
    _civ(5, True, "印度", True),
]


class TestLuaEmitsAllMajors:
    def test_domciv_line_is_emitted(self):
        assert "DOMCIV|" in build_victory_progress_query()

    def test_domciv_loop_is_outside_the_hasmet_filter(self):
        """DOMCIV 必须在 `if met then` 之外 —— 这正是原缺陷所在。"""
        q = build_victory_progress_query()
        # 计数循环枚举全部存活大文明,DOMCIV 挂在它里面
        head = q.index("local nMajors = 0")
        assert q.index("DOMCIV|") > head, "DOMCIV 应在全量 major 循环内发出"

    def test_parses_unmet_as_unknown_capital(self):
        vp = parse_victory_progress_response(
            [
                "DOMCIV|0|true|苏美尔|true",
                "DOMCIV|1|false|UNKNOWN|unknown",
                "DOMCIV|4|true|日本|false",
            ]
        )
        by_id = {c.player_id: c for c in vp.domination_civs}
        assert by_id[0].holds_own_capital is True
        # 未接触 = 真的不知道,不能默认 True 也不能默认 False
        assert by_id[1].met is False
        assert by_id[1].holds_own_capital is None
        assert by_id[4].holds_own_capital is False


class TestTally:
    def test_counts_unmet_civs_in_the_denominator(self):
        vp = NS(domination_civs=T190_CIVS)
        total, captured, unmet = narrate._domination_tally(vp, NS(player_id=0))
        # 5 个对手,不是工具原来报的 3 个
        assert total == 5
        assert captured == 1  # 京都
        assert unmet == 2  # 希腊 + 刚果

    def test_remaining_is_four_not_two(self):
        """原工具说"还差 2 座",真实是 4 座 —— 这个数字是整个误判的源头。"""
        vp = NS(domination_civs=T190_CIVS)
        total, captured, _ = narrate._domination_tally(vp, NS(player_id=0))
        assert total - captured == 4

    def test_returns_none_without_data(self):
        assert (
            narrate._domination_tally(NS(domination_civs=[]), NS(player_id=0)) is None
        )


class TestLedgerRendering:
    def test_warns_about_unmet_civs(self):
        out = "\n".join(
            narrate._domination_ledger(NS(domination_civs=T190_CIVS), NS(player_id=0))
        )
        assert "未接触文明" in out
        assert "player 1" in out and "player 3" in out

    def test_states_true_remaining_count(self):
        out = "\n".join(
            narrate._domination_ledger(NS(domination_civs=T190_CIVS), NS(player_id=0))
        )
        assert "1/5" in out
        assert "还差 4 座" in out
        assert "未探索" in out

    def test_no_unmet_no_warning(self):
        all_met = [c for c in T190_CIVS if c.met]
        out = "\n".join(
            narrate._domination_ledger(NS(domination_civs=all_met), NS(player_id=0))
        )
        assert "未接触文明" not in out
        assert "未探索" not in out


class TestAssessmentScoreCollapses:
    """光把未接触文明计入分母不够,评分也得掉下来。"""

    def _assess(self, civs):
        us = VictoryPlayerProgress(
            player_id=0,
            name="苏美尔",
            score=254,
            science_vp=0,
            science_vp_needed=0,
            diplomatic_vp=0,
            tourism=30,
            military_strength=593,
            techs_researched=29,
            civics_completed=25,
            religion_cities=0,
            staycationers=30,
        )
        vp = NS(
            players=[us],
            domination_civs=civs,
            capitals_held={"苏美尔": True, "美国": True, "印度": True, "日本": False},
            demographics={
                "Soldiers": NS(rank=1, value=593, best=593, average=232, worst=32)
            },
            our_tourists_from={},
            their_staycationers={},
            religion_majority={},
            religion_founded_names={},
            religions_founded=4,
            religions_max=4,
            space_projects=[],
            enabled_victories=set(),
        )
        text = narrate.narrate_victory_progress(vp)
        for line in text.splitlines():
            if "Domination" in line and "%" in line:
                return line
        raise AssertionError(f"no Domination assessment line in:\n{text}")

    def test_score_drops_when_civs_are_unmet(self):
        line = self._assess(T190_CIVS)
        pct = int(line.split("]")[1].split("%")[0].strip())
        assert pct <= 20, f"2 个未接触文明时统治分不该还有 {pct}%: {line}"

    def test_score_stays_high_when_everyone_is_met(self):
        line = self._assess([c for c in T190_CIVS if c.met])
        pct = int(line.split("]")[1].split("%")[0].strip())
        assert pct == 40

    def test_reason_names_the_expedition_cost(self):
        line = self._assess(T190_CIVS)
        assert "未接触文明" in line
        assert "远征" in line


class TestPolicyReasonMatchesTrigger:
    """2026-08-08 T191:「演习」因造战车触发,理由却喊"锁定统治胜利"。

    当时胜利路线是 T190 刚改的 SCIENCE —— 改道后第一回合就收到一条
    把你往回拽的假信号。触发条件是什么,理由就必须说什么。
    """

    def _ctx(self, producing, victory):
        from civ_mcp.policy import PolicyContext

        return PolicyContext(
            slots={"SLOT_MILITARY": ["", ""], "SLOT_ECONOMIC": ["", ""]},
            available={"POLICY_MANEUVER"},
            producing=producing,
            victory_type=victory,
            threat_level="GREEN",
            exploration=0.4,
        )

    def test_production_trigger_speaks_of_production(self):
        from civ_mcp.policy import check_policies

        out = check_policies(self._ctx(["UNIT_HEAVY_CHARIOT"], "SCIENCE"))
        maneuver = [a.text for a in out if "演习" in a.text]
        assert maneuver, "造战车时应建议演习卡"
        assert "锁定统治胜利" not in maneuver[0]
        assert "正在生产骑乘单位" in maneuver[0]

    def test_victory_trigger_keeps_domination_wording(self):
        from civ_mcp.policy import check_policies

        out = check_policies(self._ctx([], "DOMINATION"))
        maneuver = [a.text for a in out if "演习" in a.text]
        assert maneuver, "锁定统治时应建议演习卡"
        assert "锁定统治胜利" in maneuver[0]
