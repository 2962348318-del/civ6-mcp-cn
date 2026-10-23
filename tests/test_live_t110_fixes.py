"""2026-08-03 T110 实机会话抓到的缺陷回归测试。

这一批的共同点:**单元测试全绿也发现不了**。它们要么是两个上下文之间的
契约问题(GameCore 读不到 InGame 的 API),要么是两个模块给出互相矛盾的
结论,要么是根本没人检查的空白(城市断生产)。只有把系统接到真实游戏上
跑一回合才会显形 —— 这正是校准局的价值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_mcp import params
from civ_mcp.end_turn import check_turn_completion
from civ_mcp.lua import overview as lua_overview
from civ_mcp.lua import units as lua_units


@dataclass
class City:
    name: str = "罗马"
    currently_building: str = "UNIT_ARCHER"


@dataclass
class Unit:
    name: str = "军团"
    unit_type: str = "UNIT_ROMAN_LEGION"
    x: int = 10
    y: int = 10
    moves_remaining: float = 0.0
    max_moves: float = 2.0
    combat_strength: int = 40
    build_charges: int = 0


# ===========================================================================
# 缺陷 1(P0):我方军力在 GameCore 读不到 —— 缺陷 1e 的真正根因
# ===========================================================================


class TestOwnMilitaryStrengthSource:
    def test_rival_query_also_emits_our_own_strength(self):
        """世界扫描走 GameCore,那里 GetMilitaryStrength() 取不到值。

        于是 wm.military_strength 恒为 0,军力排名/对手推演/危机判断全部
        回落到【单位数】—— 实机 T111 简报里出现「军力 46 vs 你 6」,
        而同一份简报的 L1 分档写着「军力 1.35×(148 vs 110)」。
        两个数在一页纸上打架。权威源必须挂在 InGame 查询上。
        """
        lua = lua_overview.build_rival_snapshot_query()
        assert "MYMILSTR|" in lua
        assert "GetMilitaryStrength()" in lua

    def test_parser_extracts_it(self):
        lines = [
            "MYMILSTR|148",
            "RIVAL|1|巴西|400|9|40|90.0|50.0|12.0|65|36|25|10.0|0|0|",
        ]
        assert lua_overview.parse_own_military_strength(lines) == 148

    def test_absent_line_returns_zero_not_a_crash(self):
        assert (
            lua_overview.parse_own_military_strength(
                ["RIVAL|1|x|1|1|1|1.0|1.0|1.0|1|1|1|0.0|0|0|"]
            )
            == 0
        )

    def test_garbage_value_returns_zero(self):
        assert lua_overview.parse_own_military_strength(["MYMILSTR|abc"]) == 0


# ===========================================================================
# 缺陷 2(P0):按难度名硬编码的城市数阈值与 L1 实测结论矛盾
# ===========================================================================


class TestPaceUsesMeasuredMedian:
    def test_measured_median_wins_over_the_difficulty_table(self):
        """实机 T111:我 5 城、对手中位数 4 城(不落后),

        而经验表同时在喊「国王难度这个阶段应有 8 座」。
        难度还可能是随机的 —— 按难度名下的结论没有预测力。
        """
        out = params.check_pace(
            111, cities=5, military=6, difficulty="DIFFICULTY_KING", rival_city_median=4
        )
        joined = " ".join(out)
        assert "8 座" not in joined
        assert "国王难度这个阶段应有" not in joined

    def test_behind_the_median_is_still_reported(self):
        out = params.check_pace(
            111, cities=3, military=6, difficulty="DIFFICULTY_KING", rival_city_median=6
        )
        assert any("实测对手中位数 6 座" in line for line in out)

    def test_city_count_is_explicitly_not_a_verdict(self):
        out = params.check_pace(111, cities=3, military=6, rival_city_median=6)
        assert any("不单独作为成败判据" in line for line in out)

    def test_falls_back_to_the_table_when_unmeasured_and_labels_it(self):
        out = params.check_pace(60, cities=1, military=2, difficulty="DIFFICULTY_KING")
        assert any("〔经验值,非实测〕" in line for line in out)

    def test_military_gap_check_is_unaffected(self):
        out = params.check_pace(60, cities=4, military=0, rival_city_median=4)
        assert any("仍无军事单位" in line for line in out)


# ===========================================================================
# 缺陷 3(P1):支援单位整类不可用 —— 破城槌永远贴不上近战
# ===========================================================================


class TestSupportUnitStacking:
    def test_move_query_compares_real_formation_class(self):
        """文明6 有三类编队:CIVILIAN / 军事 / SUPPORT,同类才冲突。

        此前是二值判断(isCivilian == otherCivilian),把 SUPPORT 当军事,
        于是破城槌/攻城塔/医疗兵永远无法与近战同格 —— 而那是它们唯一的用法。
        实机 T110:破城槌移向披甲战士所在格被 STACKING_CONFLICT 拒绝。
        """
        lua = lua_units.build_move_unit(1, 5, 5)
        assert "myClass == otherClass" in lua
        # 断言的是【变量不再存在】,不是「这串字符没出现过」——
        # 注释里解释旧写法是合法的(校准局已经在这个坑上栽过两次)。
        assert "local isCivilian" not in lua
        assert "local otherCivilian" not in lua

    def test_the_three_classes_are_actually_distinct_in_gamedata(self):
        from civ_mcp import civ_kit

        assert (
            civ_kit.unit_row("UNIT_BATTERING_RAM")["FormationClass"]
            == "FORMATION_CLASS_SUPPORT"
        )
        assert (
            civ_kit.unit_row("UNIT_MAN_AT_ARMS")["FormationClass"]
            == "FORMATION_CLASS_LAND_COMBAT"
        )
        assert (
            civ_kit.unit_row("UNIT_BUILDER")["FormationClass"]
            == "FORMATION_CLASS_CIVILIAN"
        )


# ===========================================================================
# 缺陷 4(P0):断生产从来没有被门禁拦过
# ===========================================================================


class TestIdleProductionGate:
    def test_city_building_nothing_blocks_the_turn(self):
        """实机 T110:罗马(产能10)与奥斯蒂亚(产能12)同时空转,门禁一声不吭。

        空转的产能不会攒着,是当场蒸发,而且事后没有任何地方会告诉你。
        """
        blockers, _ = check_turn_completion(
            [], cities=[City(name="罗马", currently_building="NONE")]
        )
        assert any("没有在生产" in b for b in blockers)
        assert any("罗马" in b for b in blockers)

    def test_empty_string_counts_as_idle(self):
        blockers, _ = check_turn_completion([], cities=[City(currently_building="")])
        assert blockers

    def test_producing_cities_pass(self):
        blockers, _ = check_turn_completion(
            [], cities=[City(currently_building="BUILDING_GRANARY")]
        )
        assert not blockers

    def test_it_is_a_blocker_not_a_warning(self):
        """提醒会被「重调放行」轻易跳过,而这条修复成本只是一次调用。"""
        blockers, warnings = check_turn_completion(
            [], cities=[City(currently_building="NONE")]
        )
        assert blockers and not any("没有在生产" in w for w in warnings)

    def test_no_cities_argument_keeps_old_behaviour(self):
        """老调用点(只传单位)不受影响。"""
        assert check_turn_completion([Unit()]) == check_turn_completion(
            [Unit()], None, None
        )

    def test_skipping_get_units_blocks_the_turn(self):
        """换模型接手时最常见的失败是**跳过步骤**,不是判断错。

        没读过部队就结束回合,在存档里看不出异常,但这一回合已经废了。
        门禁自己会调 get_units,所以判据必须是「调用方主动读过」。
        """
        from civ_mcp.game_state import GameState

        gs = GameState.__new__(GameState)
        gs._turn_reads = set()
        assert not gs.has_read("units")
        gs.note_read("units")
        assert gs.has_read("units")
        # 回合真正推进后清空 —— 新回合必须重读。
        # 刻意不按回合号比对:note_read 曾用 _high_water_turn,而它只在
        # end_turn 成功推进时更新,一旦脱节(重启后为 0、或某次 end_turn
        # 没推进)门禁就误报。实机两次触发,所以改成集合 + 显式清空。
        gs.clear_turn_reads()
        assert not gs.has_read("units")

    def test_all_idle_cities_are_named(self):
        blockers, _ = check_turn_completion(
            [],
            cities=[
                City(name="罗马", currently_building="NONE"),
                City(name="奥斯蒂亚", currently_building="NONE"),
                City(name="拉文那", currently_building="BUILDING_GRANARY"),
            ],
        )
        joined = " ".join(blockers)
        assert "罗马" in joined and "奥斯蒂亚" in joined
        assert "拉文那" not in joined


# ===========================================================================
# 缺陷 5/6/8:红警按城判距、城市攻击射程、移动结算中途坐标
# ===========================================================================


class TestRedAlertPerCity:
    def _wm(self):
        from civ_mcp import world_model as wm_mod

        wm = wm_mod.WorldModel()
        wm.threat_level = "RED"
        wm.threat_positions = [[39, 30, 4, "Barbarian"]]
        wm.city_positions = {"65536": [38, 42], "196610": [36, 38]}
        return wm

    def test_far_city_is_measured_as_far(self):
        """实机:罗马(38,42) 距该蛮族(39,30) 12 格,却被红警拦住造建造者。"""
        assert self._wm().threat_distance_to(65536) == 12

    def test_near_city_is_measured_as_near(self):
        assert self._wm().threat_distance_to(196610) == 8

    def test_unknown_city_returns_none_not_zero(self):
        """拿不到距离要显式 None,不能返回 0 —— 0 会被当成「贴脸」。"""
        assert self._wm().threat_distance_to(999999) is None

    def test_no_threats_returns_none(self):
        from civ_mcp import world_model as wm_mod

        wm = wm_mod.WorldModel()
        wm.city_positions = {"1": [0, 0]}
        assert wm.threat_distance_to(1) is None

    def test_positions_survive_persistence(self):
        from civ_mcp import world_model as wm_mod

        restored = wm_mod.WorldModel.from_json(self._wm().to_json())
        assert restored.threat_distance_to(65536) == 12


class TestCityAttackRange:
    def test_target_scan_matches_the_two_tile_range(self):
        """实机:奥斯蒂亚(34,42) 报了两个 3 格外的目标,city_action 直接拒绝。

        列出打不到的目标不只是浪费一次调用 —— 它会让人以为有一次免费
        攻击可用,从而改变整个回合的部署。
        """
        from civ_mcp.lua import cities as lua_cities

        lua = lua_cities.build_cities_query()
        assert "d >= 1 and d <= 2" in lua
        assert "d >= 1 and d <= 3" not in lua


class TestPendingDealVisibility:
    """用户实机反馈:游戏里弹出阿拉伯的求和提议，工具却报「没有待处理交易」。

    根因:查询要求 `FindOpenSessionID(me,i) >= 0` 才往下读，即**必须已经
    打开那个外交会话**。而 AI 主动发来的提议挂在通知/弹窗里，没人点开就
    没有 session，于是整段被跳过。这是「工具说没有、实际有」的最坏情况，
    而且那个弹窗还会卡住回合推进(实测 T116 出现 116->116 不前进)。
    """

    def test_query_no_longer_requires_an_open_session(self):
        from civ_mcp.lua import diplomacy as lua_diplo

        lua = lua_diplo.build_pending_deals_query()
        assert "GetWorkingDeal(DealDirection.INCOMING" in lua
        assert "if sid and sid >= 0 then" not in lua

    def test_query_reports_session_state_so_caller_knows(self):
        from civ_mcp.lua import diplomacy as lua_diplo

        assert "|sid=" in lua_diplo.build_pending_deals_query()

    def test_responding_opens_the_session_itself(self):
        """读到了却响应不了同样没用 —— 响应端要能自己补开会话。"""
        from civ_mcp.lua import diplomacy as lua_diplo

        lua = lua_diplo.build_respond_to_deal(5, True)
        assert 'RequestSession(me, target, "MAKE_DEAL")' in lua

    def test_parser_tolerates_the_new_sid_field(self):
        from civ_mcp.lua import diplomacy as lua_diplo

        deals = lua_diplo.parse_pending_deals_response(
            [
                "DEAL|5|阿拉伯|萨拉丁|sid=-1",
                "ITEM|5|THEM|GOLD|Gold (lump sum)|100|0",
            ]
        )
        assert deals and deals[0].other_player_id == 5
        assert deals[0].other_player_name == "阿拉伯"

    def test_parser_still_reads_the_old_format(self):
        from civ_mcp.lua import diplomacy as lua_diplo

        deals = lua_diplo.parse_pending_deals_response(["DEAL|5|阿拉伯|萨拉丁"])
        assert deals and deals[0].other_leader_name == "萨拉丁"


class TestPostMoveCoordinate:
    def test_hp_query_also_returns_position(self):
        """MOVE_TO 的结算不在请求那一帧完成,同帧读到的是中途快照。"""
        assert "OK:UNIT_POS|" in lua_units.build_unit_hp_query(3)
