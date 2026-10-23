"""缺陷 105:战前评估不查陆路可达性 —— 又是伦敦,又是同一座城。

2026-08-11 罗马局 T307。end_turn 主线把伦敦(60,51)列成头号目标:

    ⚠️ 军力窗口 3.78× —— 目标定 英国 的 伦敦(60,51)。编成齐了 —— 可以动手
       目标 英国 的 伦敦(60,51),人口 15,无城墙、【原始首都:打下计统治 +1】

assess_attack 跟着给了绿灯:

    判定:🟢 可打
    · 无墙 + 近战战力领先 75 + 兵力占优;城防 73 下净伤 471/回合,
      约 1 回合破城,预期损失 ≤1
    补给:最近己方城市 3 格

每一个数都对,结论完全错。实测 BFS 从我方陆军所在陆块出发:

    VISITED=1603  REACHED_LONDON_ADJ=nil

1603 格是这块大陆的【全部】陆地 —— 走遍了,一个伦敦邻格都到不了。
伦敦在海对面。陆战单位下水就是登船状态,战力归零,一击即死。

那句"补给:最近己方城市 3 格"是**直线**距离,而陆路距离是无穷。它读起来
像"很近,好打",恰恰把人推向错误结论。

代价不是假设的:同一座伦敦在 T177-191 已经围城失败过一次,尼德罗斯
(65,58)这局也栽在同一件事上 —— 它六个邻格里四个是山或海,唯二陆地入口
在城西南,而我方陆块与那片地隔着水。三次教训,同一个漏判。

判据只能是 BFS,不能是坐标算术:**相邻的陆地格不等于连通的陆块**。这条
在 safety.py 的移动路径上早就实现了(LANDPATH/land_hops),战前评估这条
路径上却完全没有 —— 同一个知识,只在一处落地。和缺陷 102 的
"执行侧早就读 Bombard、估算侧没读"是同一个形状。

可达性是【前置条件】不是加权项:部队站不到发起线,火力数学一发都打不出去。
所以它排在所有战力计算之前,直接判死。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.map import build_land_reach_query, parse_land_reach
from civ_mcp.war_planner import (
    TIER_GREEN,
    TIER_RED,
    _Force,
    assess_attack,
    assess_tier,
)


def _strong_force() -> _Force:
    """T307 实况的西线编成 —— 火力上完全压制伦敦。"""
    return _Force(
        groups={
            "melee": [("UNIT_INFANTRY", 75), ("UNIT_INFANTRY", 75)],
            "siege": [("UNIT_ARTILLERY", 80)] * 5,
            "ranged": [("UNIT_FIELD_CANNON", 60)],
        },
        best_melee_cs=75,
        best_cs=80,
        count=8,
    )


def _weak_garrison() -> _Force:
    return _Force(groups={}, best_melee_cs=0, best_cs=0, count=0)


class TestUnreachableIsFatalRegardlessOfFirepower:
    def test_overwhelming_force_still_loses_to_water(self):
        """T307 的原始输入:无墙、城防73、我方压制 —— 旧判据给 🟢。"""
        tier, reasons = assess_tier(
            _strong_force(),
            _weak_garrison(),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            city_defense=73,
            garrison_hp=200,
            land_reachable=False,
        )
        assert tier == TIER_RED
        assert any("陆路走不到" in r for r in reasons)

    def test_same_input_reachable_is_still_green(self):
        """可达时不许改变原判定 —— 这道门只砍不可达的那一支。"""
        tier, _ = assess_tier(
            _strong_force(),
            _weak_garrison(),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            city_defense=73,
            garrison_hp=200,
            land_reachable=True,
        )
        assert tier == TIER_GREEN

    def test_unknown_does_not_block(self):
        """测不出来就别判死 —— 误判不可达会挡掉一场本来能打的仗。"""
        tier, _ = assess_tier(
            _strong_force(),
            _weak_garrison(),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            city_defense=73,
            garrison_hp=200,
            land_reachable=None,
        )
        assert tier == TIER_GREEN

    def test_default_argument_keeps_old_behaviour(self):
        """老调用点不传这个参数时行为不变。"""
        tier, _ = assess_tier(
            _strong_force(),
            _weak_garrison(),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            city_defense=73,
            garrison_hp=200,
        )
        assert tier == TIER_GREEN

    def test_reason_names_the_actual_fix(self):
        """要告诉调用方怎么办,不然它只会换个坐标再试一次。"""
        _, reasons = assess_tier(
            _strong_force(),
            _weak_garrison(),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            land_reachable=False,
        )
        blob = " ".join(reasons)
        assert "海运" in blob or "登陆" in blob
        assert "get_pathing_estimate" in blob

    def test_reason_explains_why_walking_around_does_not_work(self):
        _, reasons = assess_tier(
            _strong_force(),
            _weak_garrison(),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            land_reachable=False,
        )
        blob = " ".join(reasons)
        assert "登船" in blob or "战力归零" in blob


class TestAssessOutputIsHonestAboutDistance:
    def _state(self, reachable):
        return {
            "target": {
                "name": "伦敦",
                "population": 15,
                "defense_strength": 73,
                "garrison_hp": 200,
            },
            "my_units": [
                {"type": "UNIT_INFANTRY", "x": 63, "y": 50},
                {"type": "UNIT_INFANTRY", "x": 63, "y": 47},
                {"type": "UNIT_ARTILLERY", "x": 64, "y": 46},
            ],
            "enemy_units": [],
            "my_cities": [{"x": 62, "y": 48}],
            "land_reachable": reachable,
        }

    def test_supply_line_says_it_is_straight_line(self):
        """"最近己方城市 3 格"读起来像"很近",而陆路是无穷 ——
        不标注单位就是在误导。"""
        out = "\n".join(assess_attack(60, 51, self._state(True)))
        assert "直线" in out

    def test_unreachable_target_is_refused(self):
        out = "\n".join(assess_attack(60, 51, self._state(False)))
        assert TIER_RED in out
        assert "陆路走不到" in out

    def test_reachable_target_says_so_explicitly(self):
        """实测连通要说出来 —— 否则调用方无从知道这一项到底查过没有。"""
        out = "\n".join(assess_attack(60, 51, self._state(True)))
        assert "陆路" in out and "连通" in out

    def test_missing_field_is_not_treated_as_unreachable(self):
        state = self._state(True)
        del state["land_reachable"]
        out = "\n".join(assess_attack(60, 51, state))
        assert "陆路走不到" not in out

    def test_non_bool_field_is_ignored(self):
        state = self._state("maybe")
        out = "\n".join(assess_attack(60, 51, state))
        assert "陆路走不到" not in out


class TestBfsQuery:
    def test_query_targets_adjacent_tiles_not_the_city_tile(self):
        """占城是【进城】,发起线是相邻格 —— 拿城市那一格当目标会永远不可达。"""
        src = build_land_reach_query(63, 50, 60, 51)
        assert "GetAdjacentPlot(60, 51" in src

    def test_query_ignores_ownership_and_units(self):
        """问的是"理论上走不走得到"。边境和堵路是 move 时的问题,
        混进来会把"暂时过不去"误判成"永远过不去"。"""
        src = build_land_reach_query(63, 50, 60, 51)
        assert "IsWater" in src
        assert "IsImpassable" in src
        assert "GetOwner" not in src

    def test_query_is_bounded(self):
        src = build_land_reach_query(63, 50, 60, 51)
        assert "4000" in src, "这张图一块大陆 1603 格,上限要留余量但不能没有"

    def test_truncation_reports_unknown_not_no(self):
        """没走完就截断时不能说"不可达" —— 那是把上限当成了地形。"""
        src = build_land_reach_query(63, 50, 60, 51)
        i = src.index("visited >=")
        assert "UNKNOWN" in src[i : i + 200]

    def test_island_city_with_no_land_neighbours_is_no(self):
        src = build_land_reach_query(63, 50, 60, 51)
        assert "goalCount == 0" in src
        i = src.index("goalCount == 0")
        assert "LANDREACH|NO" in src[i : i + 300]

    def test_impassable_start_is_unknown(self):
        src = build_land_reach_query(63, 50, 60, 51)
        i = src.index("start == nil")
        assert "UNKNOWN" in src[i : i + 200]


class TestParse:
    def test_yes(self):
        assert parse_land_reach(["LANDREACH|YES|412"]) is True

    def test_no(self):
        assert parse_land_reach(["LANDREACH|NO|1603"]) is False

    def test_unknown_is_none(self):
        assert parse_land_reach(["LANDREACH|UNKNOWN|4000"]) is None

    def test_no_output_is_none(self):
        assert parse_land_reach([]) is None
        assert parse_land_reach(["something else"]) is None

    def test_real_t307_reading(self):
        """实测输出:1603 格穷举完,到不了。"""
        assert parse_land_reach(["LANDREACH|NO|1603"]) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
