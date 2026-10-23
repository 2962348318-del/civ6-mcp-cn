"""2026-08-10 罗马局 T176-T191(伦敦围城)暴露的缺陷回归测试。

这一段是整局最贵的一次误判:8 个回合、三个火枪手打残、一门射石炮跨越
半个帝国推到前线,最后靠 lua 直查才发现伦敦【数学上打不动】。逐条根因:

  (66) 城防不进判定:assess_tier 只比"我方近战 CS vs 守军【单位】CS"。
       伦敦无墙、守军是弓箭手(CS15),我方火枪手 CS55 → 差 +40 → 🟢
       「预期损失≤1,2-4 回合破城」。真实防御方是城中心:城防 ~80、
       驻军 200 血、每回合回 20。CS55 打城防 80 每击 10-15 伤,五个单位
       一回合合计还不够抵消回血 —— 4 次近战 1 次炮击只把 200 打到 183。
       修法:城防进档位判定,净伤 ≤ 回血直接 🔴;城防读不到【不给绿灯】。

  (67) 城防没有数据源:assess_attack 让调用方手填 defense_strength ——
       手填等于永远不填,于是"城防未知"这条降级路径从来没被触发过,
       评估照常发绿灯。修法:InGame 的 CityManager.GetCityAt 直读
       (GameCore 的 City 对象没有 GetDistricts/GetDefenseStrength)。

  (68) 战斗估算把守军单位当防御方:打城市时 defender 取的是驻军弓箭手,
       于是估算报 "24 × 10^((72-15)/30) ≈ 1906 伤 → LIKELY KILL"。
       那个荒谬数字本身就是"防御方选错了"的最大证据,却被当成捷报。
       修法:城市目标用城防值估算 + 单次伤害封顶 100 + 城市不吃"残血
       战力衰减"(那是单位机制,拿 100-183 去算衰减纯属胡来)。

  (69) 城战战报读不出真城血:攻击路径用 Cities.GetCityInPlot 拿句柄,
       其上 GetDamage 读不到真实伤害,战报一路 "city HP:200 -> 200/200"
       (lua 直查是 183)—— 打了没效果和打了有效果长得一模一样。
       修法:换 CityManager.GetCityAt;城血没下降时明写 NO_PROGRESS。

  (70) 寻路静默登船(6 次):陆战单位 move 到【陆地】目标,引擎从水面
       抄近道,单位登船、移动力耗尽、搁浅海上;其中一次是给 13 血火枪手
       下的撤退令,把它送进了敌城射程。登船=战力归零、一击即死。
       修法:位置重读那一帧(移动是异步的,同帧读不准)检测并显式告警。

  (71) upgrade_unit 换掉单位对象,旧复合 ID 立即失效 —— 升级完想指挥它
       西进,直接 UNIT_NOT_FOUND。修法:返回里带 new_unit_id。

  (72) 占格诊断把中立城邦喊"敌方":两次占路的都是城邦(普雷斯拉夫、
       维尔纽斯),文案一律"敌方 X 占据",于是我下了 attack,靠
       NOT_AT_WAR 门禁才没打成外交事故。修法:带归属+交战状态。

  (73) DOMAIN_MISMATCH 文案里的 {target_x},{target_y} 占位符没格式化
       (嵌套字符串没加 f 前缀)—— 报错里直接印出花括号。

  (74) 文化胜利威胁刷屏:门柱一过 50,六家【0 游客】的对手每回合各刷
       一条 "CLOSING",连刷 8 回合。没有游客的文明不是文化威胁。

  (75) 完成度门禁拦下 end_turn 后重调,文档说"第一次的反思会保留",
       实际仍要求五项全填 —— 每次拦截都要把五段反思原样再打一遍。
"""

from __future__ import annotations

import asyncio

import pytest

from civ_mcp import war_planner
from civ_mcp.lua import cities as lua_cities
from civ_mcp.lua import governance as lua_gov
from civ_mcp.lua import units as lua_units


def _force(units: list[tuple[str, int]]) -> war_planner._Force:
    return war_planner._parse_force(
        [{"type": ut, "cs": cs, "x": 0, "y": 0} for ut, cs in units]
    )


class TestCityDefenseEntersVerdict:
    """(66) 伦敦局面必须不再是绿灯。"""

    LONDON_ATTACKERS = [
        ("UNIT_MUSKETMAN", 55),
        ("UNIT_MUSKETMAN", 55),
        ("UNIT_MUSKETMAN", 55),
        ("UNIT_CROSSBOWMAN", 40),
        ("UNIT_CROSSBOWMAN", 40),
    ]
    LONDON_GARRISON = [("UNIT_ARCHER", 15)]

    def test_london_shape_was_green_before_and_is_red_now(self):
        mine = _force(self.LONDON_ATTACKERS)
        theirs = _force(self.LONDON_GARRISON)
        # 旧行为的形态条件(无墙 + 战力领先 + 兵力占优)依然成立……
        assert mine.best_melee_cs - theirs.best_cs >= war_planner.CS_EVEN_BAND
        assert mine.count > theirs.count
        # ……但带上真实城防后判定必须是 🔴。
        tier, reasons = war_planner.assess_tier(
            mine,
            theirs,
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            city_defense=80,
            garrison_hp=200,
        )
        assert tier == war_planner.TIER_RED
        assert any("城防" in r for r in reasons)

    def test_unknown_city_defense_never_green(self):
        """(66/67) 城防读不到 = 不给绿灯,只能降到黄并说明原因。"""
        tier, reasons = war_planner.assess_tier(
            _force(self.LONDON_ATTACKERS),
            _force(self.LONDON_GARRISON),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
        )
        assert tier != war_planner.TIER_GREEN
        assert any("城防" in r for r in reasons)

    def test_weak_city_still_green(self):
        """真正能速推的城不能被误伤:城防低 → 仍是绿灯。"""
        tier, _ = war_planner.assess_tier(
            _force(self.LONDON_ATTACKERS),
            _force([("UNIT_WARRIOR", 20)]),
            has_walls=False,
            wall_broken=False,
            supply_dist=3,
            city_defense=25,
            garrison_hp=200,
        )
        assert tier == war_planner.TIER_GREEN

    def test_hit_damage_matches_observed(self):
        """实机:5 次攻击(4 近战 + 1 炮击)共削 17 血 ≈ 3.4/次。"""
        assert 2 <= war_planner.city_hit_damage(55, 80) <= 8

    def test_hit_damage_scales_with_advantage(self):
        """战力反超城防时伤害要显著上去,否则一切城都判打不动。"""
        assert war_planner.city_hit_damage(90, 40) > 60

    def test_net_damage_below_heal_is_hopeless(self):
        math_ = war_planner.city_assault_math(_force(self.LONDON_ATTACKERS), 80, 200)
        assert math_ is not None
        assert math_["net_per_turn"] <= 0
        assert math_["turns"] is None

    def test_assess_output_names_defense_when_known(self):
        out = war_planner.assess_attack(
            60,
            51,
            {
                "target": {
                    "name": "伦敦",
                    "population": 11,
                    "defense_strength": 80,
                    "garrison_hp": 183,
                    "garrison_max_hp": 200,
                },
                "my_units": [
                    {"type": ut, "cs": cs, "x": 61, "y": 50}
                    for ut, cs in self.LONDON_ATTACKERS
                ],
                "enemy_units": [{"unit_type": "UNIT_ARCHER", "cs": 15}],
                "my_cities": [{"x": 62, "y": 48}],
            },
        )
        text = "\n".join(out)
        assert "城防值 80" in text
        assert war_planner.TIER_RED in text

    def test_assess_output_flags_unknown_defense(self):
        out = war_planner.assess_attack(
            60,
            51,
            {
                "target": {"name": "伦敦"},
                "my_units": [
                    {"type": ut, "cs": cs, "x": 61, "y": 50}
                    for ut, cs in self.LONDON_ATTACKERS
                ],
                "enemy_units": [{"unit_type": "UNIT_ARCHER", "cs": 15}],
                "my_cities": [{"x": 62, "y": 48}],
            },
        )
        text = "\n".join(out)
        assert "城防值未知" in text
        assert war_planner.TIER_GREEN not in text


class TestEnemyCityDefenseQuery:
    """(67) 城防的数据源本身。"""

    def test_query_uses_ingame_handle(self):
        code = lua_cities.build_enemy_city_defense_query(60, 51)
        assert "CityManager.GetCityAt(60, 51)" in code
        assert "DISTRICT_GARRISON" in code
        assert "GetDefenseStrength" in code

    def test_parse_roundtrip(self):
        parsed = lua_cities.parse_enemy_city_defense(
            ["CITY_DEFENSE|伦敦|7|11|80|183|200|0|0|1"]
        )
        assert parsed == {
            "name": "伦敦",
            "owner": 7,
            "population": 11,
            "defense_strength": 80,
            "garrison_hp": 183,
            "garrison_max_hp": 200,
            "wall_hp": 0,
            "wall_max_hp": 0,
            "is_capital": True,
        }

    def test_parse_missing_line(self):
        assert lua_cities.parse_enemy_city_defense(["ERR:NO_CITY|..."]) is None

    def test_server_merges_live_defense(self):
        from civ_mcp import server

        class _GS:
            async def get_enemy_city_defense(self, x, y):
                return {
                    "name": "伦敦",
                    "population": 11,
                    "defense_strength": 80,
                    "garrison_hp": 183,
                    "garrison_max_hp": 200,
                    "wall_hp": 0,
                    "wall_max_hp": 0,
                }

        merged = asyncio.run(
            server._merge_live_city_defense(_GS(), 60, 51, {"name": ""})
        )
        assert merged["defense_strength"] == 80
        assert merged["garrison_hp"] == 183

    def test_server_survives_unreadable_defense(self):
        """城不在视野 → 降级为未知,绝不抛异常。"""
        from civ_mcp import server

        class _GS:
            async def get_enemy_city_defense(self, x, y):
                raise RuntimeError("not visible")

        merged = asyncio.run(
            server._merge_live_city_defense(_GS(), 1, 1, {"name": "X"})
        )
        assert merged == {"name": "X"}


class TestCombatEstimateVsCity:
    """(68) 估算的防御方必须是城市本身。"""

    def test_query_reads_city_defense(self):
        code = lua_units.build_combat_estimate_query(1, 60, 51)
        assert "CityManager.GetCityAt(60, 51)" in code
        assert "GetDefenseStrength" in code
        assert '"ESTIMATE|" .. attType .. "|CITY ' in code

    def test_city_estimate_is_not_absurd(self):
        est = lua_units.parse_combat_estimate(
            ["ESTIMATE|UNIT_MUSKETMAN|CITY 伦敦|72|80|0|city defense|100|183"],
            0,
            0,
        )
        assert est is not None
        assert est.defender_type.startswith("CITY ")
        # 旧实现在这一格报 ~1906。
        assert est.est_damage_to_defender <= 100

    def test_city_defender_skips_wound_decay(self):
        """城血 183 不该被当成"受伤单位"给城防打折。"""
        est = lua_units.parse_combat_estimate(
            ["ESTIMATE|UNIT_MUSKETMAN|CITY 伦敦|55|80|0|x|100|183"], 0, 0
        )
        assert est is not None
        assert est.defender_cs == 80
        assert not any("def wounded" in m for m in est.modifiers)

    def test_unit_defender_keeps_wound_decay(self):
        est = lua_units.parse_combat_estimate(
            ["ESTIMATE|UNIT_MUSKETMAN|UNIT_ARCHER|55|20|0|x|100|40"], 0, 0
        )
        assert est is not None
        assert any("def wounded" in m for m in est.modifiers)


class TestCityAttackReportsTruth:
    """(69) 城战战报必须能区分"打动了"和"没打动"。"""

    def test_attack_uses_citymanager_handle(self):
        code = lua_units.build_attack_unit(1, 60, 51)
        assert "CityManager.GetCityAt(60, 51)" in code

    def test_no_progress_branch_exists(self):
        code = lua_units.build_attack_unit(1, 60, 51)
        assert "NO_PROGRESS" in code


class TestEmbarkWarning:
    """(70) 登船必须在移动结果里说出来。"""

    def test_position_query_detects_embark(self):
        code = lua_units.build_unit_position_query(
            1, move_target_x=61, move_target_y=50
        )
        assert "EMBARKED|1" in code
        assert "IsEmbarked" in code

    def test_position_query_without_move_target_still_checks(self):
        code = lua_units.build_unit_position_query(1)
        assert "EMBARKED|1" in code


class TestUpgradeReturnsNewId:
    """(71) 升级换 ID,返回里必须带新的。"""

    def test_query_emits_new_unit_id(self):
        code = lua_gov.build_upgrade_unit(1)
        assert "new_unit_id:" in code
        assert "旧 ID 已失效" in code


class TestOccupiedTileNamesOwner:
    """(72) 中立城邦的单位不能被喊成"敌方"。"""

    def test_diagnostic_includes_war_status(self):
        code = lua_units.build_unit_position_query(
            1, move_target_x=70, move_target_y=50
        )
        assert "非交战方" in code
        assert "IsAtWarWith" in code
        assert "NOT_AT_WAR" in code


class TestDomainMismatchPlaceholders:
    """(73) 报错文案里不能出现未替换的花括号占位符。"""

    def test_coordinates_are_formatted(self):
        code = lua_units.build_attack_unit(1, 61, 49)
        assert "{target_x}" not in code
        assert "在水面格 (61,49)" in code


class TestCultureThreatNoise:
    """(74) 0 游客的对手不是文化威胁。"""

    def test_zero_tourist_rival_is_silent(self):
        """罗马局实录:我方门柱 55/56,六家 0 游客的对手全被报成 CLOSING。"""
        from civ_mcp import end_turn as et

        assert not et.culture_threat_is_live(0, 56)

    def test_real_contender_still_warns(self):
        from civ_mcp import end_turn as et

        assert et.culture_threat_is_live(50, 56)

    def test_early_game_stays_silent(self):
        """缺陷58 的老行为不能被这次修改破坏。"""
        from civ_mcp import end_turn as et

        assert not et.culture_threat_is_live(2, 4)

    def test_late_game_real_race_warns(self):
        """T384 败局那一格(375 vs 381)必须照旧报警。"""
        from civ_mcp import end_turn as et

        assert et.culture_threat_is_live(375, 381)

    def test_gate_consumes_helper(self):
        import inspect

        from civ_mcp import end_turn as et

        assert "culture_threat_is_live" in inspect.getsource(
            et._check_victory_proximity
        )


class TestEndTurnRetryKeepsReflections:
    """(75) 门禁拦下后重调,不必把五段反思重打一遍。"""

    def test_gate_message_marks_blocked(self):
        import inspect

        from civ_mcp import server

        src = inspect.getsource(server.end_turn)
        assert "_last_reflections" in src

    def test_gate_result_sets_blocked_flag(self):
        import inspect

        from civ_mcp import server

        src = inspect.getsource(server)
        assert 'elif "Cannot end turn" in result:' in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
