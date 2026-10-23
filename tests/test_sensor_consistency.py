"""P0 传感器审计 —— 每个量一个权威源,多处读数必须相等。

═══════════════════════════════════════════════════════════════════════════
为什么需要这一层测试(方案 v4 §L0.2)

校准局最贵的两条缺陷都不是"算错了",是**同一个量有两套口径**:

    1e  军力:世界扫描发 GetMilitaryStrength(真值 191),而排名/简报读的是
        单位数(7)。如果 L1 分档当时已上线,它会命令"转守备、放弃开战窗口"
        —— 而正确决策是立刻宣战,且靠宣战拿下了一座首都。
    1c  探索度:一处按全图算、一处按陆地算,导致计划链 65 回合零推进。

**坏传感器 + 决策树 = 自信的错误决策,比只提醒更糟。**
所以在 L1/L2/L3 全部上线之后,这个文件的职责是:枚举策略层会读的每一个量,
钉死它的权威源,并断言所有消费方读到的是同一个数。

新增一个策略层读数时,在这里加一条 —— 加不出断言说明这个量没有权威源。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from civ_mcp import civ_kit, end_turn, opponents
from civ_mcp import power_balance as pb
from civ_mcp import world_model as wm_mod
from civ_mcp.lua import cities as lua_cities
from civ_mcp.lua import overview as lua_overview
from civ_mcp.lua import units as lua_units
from civ_mcp.narrate import narrate_combat_estimate

_LUA_DIR = Path(__file__).resolve().parents[1] / "src" / "civ_mcp" / "lua"

# 单位血量的唯一正确表达式。任何别的写法都会在某个上下文读到战前值。
_HP_EXPR = "GetMaxDamage() - "


# ===========================================================================
# 量 1:我方军力 —— 权威源 Players[me]:GetStats():GetMilitaryStrength()
# ===========================================================================


class TestMilitaryStrength:
    def test_scan_lua_reads_the_stats_api_not_a_unit_count(self):
        lua = wm_mod.build_world_scan_query()
        assert "GetMilitaryStrength()" in lua
        assert 'print("MILSTR|"' in lua

    def test_scan_keeps_strength_and_count_as_separate_fields(self):
        """两个量都要有,但不许互相冒充 —— 这正是 1e 的形态。"""
        scan = wm_mod.parse_world_scan(["STATS|5|7|20|30", "MILSTR|191"])
        assert scan.military_strength == 191
        assert scan.military == 7

    def test_power_balance_is_fed_strength_not_count(self):
        """L1 分档必须拿真军力。校准局若拿 7 会判劣势并放弃开战窗口。"""
        gs, wm, scan = _fake_turn(military_strength=191, military_count=7)
        snap = asyncio.run(end_turn.measure_power(gs, wm, scan, 110))
        assert snap is not None
        assert snap.me.military == 191

    def test_ranking_prefers_strength_when_available(self):
        rows = opponents.get_military_ranking(
            [{"civ_name": "对手", "military_strength": 65, "player_id": 1}],
            my_military=191,
            my_name="我方",
        )
        assert any("191" in line for line in rows)
        assert not any("我方(7)" in line for line in rows)


# ===========================================================================
# 量 2:探索度 —— 权威源:陆地格口径(not plot:IsWater())
# ===========================================================================


class TestExploration:
    def test_scan_counts_land_only(self):
        lua = wm_mod.build_world_scan_query()
        assert "local isLand = not plot:IsWater()" in lua

    def test_single_emit_single_consumer(self):
        """revealed/total 只从一条 EXPLORE| 行来,百分比不许再算第二遍。"""
        wm = wm_mod.WorldModel()
        scan = wm_mod.parse_world_scan(["EXPLORE|120|400"])
        wm.tiles_revealed, wm.tiles_total = scan.revealed, scan.total
        assert wm.exploration_pct == 0.3

    def test_zero_total_does_not_divide(self):
        assert wm_mod.WorldModel().exploration_pct == 0.0


# ===========================================================================
# 量 2b:我方人口 —— 2026-08-03 T110 实机抓到:根本没有来源
# ===========================================================================


class TestOwnPopulation:
    def test_scan_lua_emits_total_population(self):
        assert 'print("POP|"' in wm_mod.build_world_scan_query()

    def test_parser_fills_it(self):
        assert wm_mod.parse_world_scan(["POP|23"]).population == 23

    def test_missing_own_value_is_marked_unusable_not_zero_point_zero(self):
        """实机现象:我方人口无来源 → 恒报「人口 0.00×」的假劣势。

        我方为 0 而对手有值,几乎一定是传感器缺口,不是真的一个人都没有。
        """
        snap = pb.measure(
            110,
            pb.Side("我", 0, cities=5, techs=12, pop=0),
            [pb.Side("对手", 1, cities=4, techs=17, pop=40)],
        )
        assert not snap.ratios["pop"].usable
        assert "0.00×" not in pb.render(snap)

    def test_the_gap_is_named_as_a_sensor_problem(self):
        snap = pb.measure(
            110,
            pb.Side("我", 0, cities=5, techs=12, pop=0),
            [pb.Side("对手", 1, cities=4, techs=17, pop=40)],
        )
        assert "我方无数据" in pb.render(snap)

    def test_real_population_flows_all_the_way_to_the_classifier(self):
        gs, wm, scan = _fake_turn()
        scan.population = 23
        snap = asyncio.run(end_turn.measure_power(gs, wm, scan, 110))
        assert snap is not None and snap.me.pop == 23
        assert snap.ratios["pop"].usable


# ===========================================================================
# 量 2c:已研科技集合 —— UU 解锁状态的判据,必须跨调用可用
# ===========================================================================


class TestResearchedTechsPersistence:
    def test_world_model_round_trips_the_tech_set(self):
        """按需工具单独调用时没有本回合扫描,拿不到就会误报「还需某科技」。"""
        wm = wm_mod.WorldModel()
        wm.researched_techs = ["TECH_IRON_WORKING", "TECH_WRITING"]
        wm.my_population = 23
        restored = wm_mod.WorldModel.from_json(wm.to_json())
        assert "TECH_IRON_WORKING" in restored.researched_techs
        assert restored.my_population == 23

    def test_unlocked_uu_renders_as_unlocked(self):
        kit = civ_kit.derive(
            "CIVILIZATION_ROME", unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}
        )
        text = civ_kit.render(kit, researched={"TECH_IRON_WORKING"})
        assert "✅ 已解锁" in text
        assert "⏳" not in text

    def test_missing_tech_set_still_renders_but_as_pending(self):
        kit = civ_kit.derive(
            "CIVILIZATION_ROME", unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}
        )
        assert "⏳" in civ_kit.render(kit)


# ===========================================================================
# 量 3:单位 HP —— 权威源 GetMaxDamage() - GetDamage()
# ===========================================================================


class TestUnitHp:
    def test_every_hp_producer_uses_the_same_expression(self):
        """三个不同上下文的查询必须用同一个表达式,否则会读到战前值。"""
        for lua in (
            lua_units.build_units_query(),
            lua_units.build_threat_scan_query(),
            lua_units.build_unit_hp_query(3),
            lua_units.build_attack_followup_query(5, 5),
        ):
            assert _HP_EXPR in lua

    def test_no_lua_reports_raw_damage_as_hp(self):
        """GetDamage() 是【已受伤害】,直接当血量报是符号反了。"""
        for path in sorted(_LUA_DIR.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            assert 'hp:" .. u:GetDamage()' not in src
            assert 'hp:" .. unit:GetDamage()' not in src

    def test_parser_roundtrips_hp(self):
        units = lua_units.parse_units_response(
            ["65537|1|罗马军团|UNIT_ROMAN_LEGION|10,10|2/2|51/100|40|0|0||0|0|||"]
        )
        assert units and units[0].health == 51 and units[0].max_health == 100


# ===========================================================================
# 量 4:对手科技/市政 —— v4 表里标 ❌「未实现」,实际早就有
# ===========================================================================


class TestRivalIntel:
    def test_rival_query_already_carries_tech_and_civic_counts(self):
        lua = lua_overview.build_rival_snapshot_query()
        assert "GetNumTechsResearched()" in lua
        assert "GetNumCivicsCompleted()" in lua

    def test_parser_fills_them(self):
        rows = lua_overview.parse_rival_snapshot_response(
            ["RIVAL|1|巴西|400|9|40|90.0|50.0|12.0|65|36|25|10.0|0|0|IRON:4"]
        )
        assert rows and rows[0].techs == 36 and rows[0].civics == 25

    def test_side_maps_every_field_without_renaming(self):
        rows = lua_overview.parse_rival_snapshot_response(
            ["RIVAL|1|巴西|400|9|40|90.0|50.0|12.0|65|36|25|10.0|0|0|"]
        )
        side = pb.Side.from_rival(rows[0])
        assert (side.techs, side.civics, side.military, side.cities) == (36, 25, 65, 9)
        assert side.science == 90.0 and side.culture == 50.0

    def test_no_second_source_for_rival_tech(self):
        """L1 只认 RivalSnapshot.techs;别处若冒出第二个来源,这条会提醒。"""
        assert "techs" in pb.Side.__dataclass_fields__
        snap = pb.measure(
            50,
            pb.Side("我", 0, techs=10),
            [
                pb.Side.from_rival(
                    lua_overview.parse_rival_snapshot_response(
                        ["RIVAL|1|巴西|1|1|1|1.0|1.0|1.0|10|20|5|0.0|0|0|"]
                    )[0]
                )
            ],
        )
        assert snap.ratios["techs"].best == 20


# ===========================================================================
# 量 5:城防 / 城墙 HP —— v4 表里标 ❌「待补查」,实际 cities 查询已有
# ===========================================================================


class TestCityDefense:
    def test_city_query_reads_district_defense(self):
        lua = lua_cities.build_cities_query()
        assert "DefenseTypes.DISTRICT_OUTER" in lua
        assert "DefenseTypes.DISTRICT_GARRISON" in lua

    def test_attack_path_reports_walls_separately_from_garrison(self):
        """打城墙时守军血不动 —— 两个数混报会让人以为攻击没生效。"""
        lua = lua_units.build_attack_followup_query(5, 5)
        assert "DISTRICT_OUTER" in lua and "DISTRICT_GARRISON" in lua

    def test_visible_city_walls_flag_feeds_the_engagement_tree(self):
        """决策树 13「有城墙且无攻城单位不许打」读的就是这个标志。"""
        from civ_mcp.lua.models import VisibleCity

        assert "has_walls" in VisibleCity.__dataclass_fields__


# ===========================================================================
# 量 6:战斗修饰符 —— 估算器必须自报缺口
# ===========================================================================


class TestCombatModifiers:
    def test_estimate_declares_what_it_does_not_include(self):
        """政策/信仰加成不在估算里。不说明就会被当成精确值用。"""
        text = narrate_combat_estimate(
            lua_units.parse_combat_estimate(
                ["ESTIMATE|UNIT_ROMAN_LEGION|UNIT_SPEARMAN|40|25|0||100|100"], 0, 0
            )
        )
        assert "policies" in text or "政策" in text

    def test_wounded_units_reduce_both_sides(self):
        """残血战力衰减必须双向计入,只算一侧就是系统性高估自己。"""
        est = lua_units.parse_combat_estimate(
            ["ESTIMATE|UNIT_ROMAN_LEGION|UNIT_SPEARMAN|40|25|0||50|50"], 0, 0
        )
        assert est is not None
        assert est.attacker_hp == 50 and est.defender_hp == 50


# ===========================================================================
# 量 7:文明套件 —— 权威源是实机 UU 解析,离线推断必须自报
# ===========================================================================


class TestCivKitProvenance:
    def test_runtime_pairs_win_over_prefix_inference(self):
        kit = civ_kit.derive(
            "CIVILIZATION_ROME", unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}
        )
        assert all(u.attribution == "runtime" for u in kit.uu)

    def test_scan_lua_resolves_uu_through_civilization_traits(self):
        lua = wm_mod.build_world_scan_query()
        assert "GameInfo.CivilizationTraits()" in lua
        assert "GameInfo.UnitReplaces()" in lua

    def test_unit_stats_come_from_gamedata_not_literals(self):
        """代差数字必须查表。这条钉死的是「凭记忆写数值」。"""
        row = civ_kit.unit_row("UNIT_ROMAN_LEGION")
        assert row.get("Combat") == "40"
        assert row.get("StrategicResource") == ""


# ===========================================================================
# 交叉校验:简报读到的数 == 分档读到的数
# ===========================================================================


class TestBriefingMatchesClassifier:
    def test_same_scan_feeds_both(self):
        gs, wm, scan = _fake_turn(
            military_strength=148, military_count=6, cities=5, science=30
        )
        snap = asyncio.run(end_turn.measure_power(gs, wm, scan, 110))
        assert snap is not None
        assert snap.me.cities == scan.cities == wm.city_count
        assert snap.me.military == wm.military_strength

    def test_empire_line_never_calls_a_unit_count_military_strength(self):
        """缺陷 1e 的形态:同一个词指两个数(真值 191 显示成 7)。"""
        gs, wm, _scan = _fake_turn(military_strength=191, military_count=7)
        line = end_turn._situation_block(
            gs, wm, None, 110, "", [], "", {"expl_last": -999}
        )
        assert "军事 7 支" in line and "军力 191" in line

    def test_measurement_is_cached_not_recomputed_per_call(self):
        """每 10 回合测一次是成本约束;重复调用不许重复发查询。"""
        gs, wm, scan = _fake_turn()
        asyncio.run(end_turn.measure_power(gs, wm, scan, 110))
        calls = gs.conn.calls
        asyncio.run(end_turn.measure_power(gs, wm, scan, 112))
        assert gs.conn.calls == calls


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    async def execute_write(self, _lua):
        self.calls += 1
        return list(self.lines)

    execute_read = execute_write


class _FakeGS:
    def __init__(self, lines):
        self.conn = _FakeConn(lines)
        self._local_player_id = 0
        self._high_water_turn = 110
        self._game_identity = ("CIVILIZATION_ROME", 1)


def _fake_turn(
    military_strength=191, military_count=7, cities=5, science=30, culture=18
):
    gs = _FakeGS(
        [
            "RIVAL|1|巴西|400|9|40|90.0|50.0|12.0|65|36|25|10.0|0|0|",
        ]
    )
    wm = wm_mod.WorldModel()
    wm.city_count = cities
    wm.military_count = military_count
    wm.military_strength = military_strength
    wm.my_science = science
    scan = wm_mod.WorldScan(
        cities=cities,
        military=military_count,
        military_strength=military_strength,
        science=science,
        culture=culture,
        techs={f"TECH_{i}" for i in range(14)},
        civics={f"CIVIC_{i}" for i in range(12)},
    )
    return gs, wm, scan
