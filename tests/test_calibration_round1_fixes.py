"""2026-08-02 第一局校准局(rome_-758731782, T8-T75)暴露缺陷的回归测试。

每个测试对应《当前进度_交接.md》待修清单中的一条编号缺陷。
"""

from __future__ import annotations

import json

import pytest

from civ_mcp import city_state_advisor, diary, tech_path
from civ_mcp import world_model as wm_mod
from civ_mcp.end_turn import check_turn_completion
from civ_mcp.lua import units as lua_units
from civ_mcp.lua.units import parse_combat_estimate
from civ_mcp.narrate import narrate_combat_estimate


# ---------------------------------------------------------------------------
# P0 #6 — 远程单位在射程内攻击必须走远程估算(零反伤)
# ---------------------------------------------------------------------------


class TestRangedEstimate:
    def test_estimate_query_ranged_regardless_of_distance(self):
        """投石兵(RS>0, 射程1)贴脸攻击也是远程 —— isRanged 不看距离。"""
        lua = lua_units.build_combat_estimate_query(1, 10, 10)
        assert "local isRanged = attRS > 0\n" in lua
        assert "local isRanged = attRS > 0 and dist > 1" not in lua

    def test_parse_ranged_no_counterattack(self):
        lines = ["ESTIMATE|UNIT_SLINGER|UNIT_WARRIOR|15|22|1||100|100"]
        est = parse_combat_estimate(lines, 0, 0)
        assert est is not None
        assert est.is_ranged
        assert est.est_damage_to_attacker == 0

    def test_narrate_ranged_has_no_attacker_damage_line(self):
        lines = ["ESTIMATE|UNIT_SLINGER|UNIT_WARRIOR|15|22|1||100|100"]
        est = parse_combat_estimate(lines, 0, 0)
        text = narrate_combat_estimate(est)
        assert "(Ranged)" in text
        assert "Est damage to attacker" not in text


# ---------------------------------------------------------------------------
# 0e — 残血战力衰减:对残血守军的估算不再按满值反击
# ---------------------------------------------------------------------------


class TestWoundedScaling:
    def test_wounded_defender_deals_less(self):
        full = parse_combat_estimate(
            ["ESTIMATE|UNIT_WARRIOR|UNIT_WARRIOR|20|20|0||100|100"], 0, 0
        )
        hurt = parse_combat_estimate(
            ["ESTIMATE|UNIT_WARRIOR|UNIT_WARRIOR|20|20|0||100|10"], 0, 0
        )
        # 守军 10 血:反击应显著低于满血,且我方输出应更高
        assert hurt.est_damage_to_attacker < full.est_damage_to_attacker
        assert hurt.est_damage_to_defender > full.est_damage_to_defender
        assert any("def wounded" in m for m in hurt.modifiers)

    def test_wounded_attacker_penalized(self):
        est = parse_combat_estimate(
            ["ESTIMATE|UNIT_WARRIOR|UNIT_WARRIOR|20|20|0||30|100"], 0, 0
        )
        assert any("att wounded" in m for m in est.modifiers)
        assert est.attacker_cs < 20


# ---------------------------------------------------------------------------
# 4c/4d — 威胁集合按宣战状态过滤(中立文明单位不是威胁)
# ---------------------------------------------------------------------------


class TestPeacefulCivFilter:
    def test_threat_scan_gates_majors_on_war(self):
        lua = lua_units.build_threat_scan_query()
        assert "if not isBarbarian and not pDiplo:IsAtWarWith(pid) then" in lua

    def test_move_safety_gates_majors_on_war(self):
        from civ_mcp import safety

        lua = safety.build_move_safety_query(1, 10, 10)
        assert "if not isBarb and not pDiplo:IsAtWarWith(pid) then" in lua


# ---------------------------------------------------------------------------
# P0 1c — 探索度陆地口径 + 计划链 pending 也判完成
# ---------------------------------------------------------------------------


class TestExplorationAndPlans:
    def test_world_scan_counts_land_only(self):
        lua = wm_mod.build_world_scan_query()
        assert "IsWater" in lua

    def test_pending_plan_completes_when_condition_met(self):
        wm = wm_mod.WorldModel()
        p1 = wm.add_plan("探索 8%", cond="exploration_at_least", args="0.08", turn=1)
        p2 = wm.add_plan(
            "第二城", cond="cities_at_least", args="2", after=p1.id, turn=1
        )
        assert p2.status == "pending"
        # 前置未达标(探索 5%),但第二城已建成 —— pending 也应判完成
        newly = wm.evaluate_plans({"cities": 2, "exploration": 0.05}, turn=35)
        assert p2.status == "done"
        assert p2 in newly
        assert p1.status == "active"  # 前置本身仍未完成

    def test_pending_manual_plan_not_auto_completed(self):
        wm = wm_mod.WorldModel()
        p1 = wm.add_plan("先做", cond="cities_at_least", args="99", turn=1)
        p2 = wm.add_plan("手动目标", cond="manual", after=p1.id, turn=1)
        wm.evaluate_plans({"cities": 3}, turn=10)
        assert p2.status == "pending"


# ---------------------------------------------------------------------------
# 1e — 军力字段:扫描解析 + 持久化
# ---------------------------------------------------------------------------


class TestMilitaryStrength:
    def test_scan_parses_milstr(self):
        scan = wm_mod.parse_world_scan(["MILSTR|96", "STATS|3|4"])
        assert scan.military_strength == 96
        assert scan.military == 4  # 单位数仍单独保留

    def test_scan_query_emits_milstr(self):
        lua = wm_mod.build_world_scan_query()
        assert "MILSTR" in lua and "GetMilitaryStrength" in lua

    def test_wm_persists_strength_and_uu(self):
        wm = wm_mod.WorldModel(
            military_strength=101,
            my_unique_units={"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"},
        )
        wm2 = wm_mod.WorldModel.from_json(wm.to_json())
        assert wm2.military_strength == 101
        assert wm2.my_unique_units == {"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}


# ---------------------------------------------------------------------------
# 0c — UU 替换:军事主线纳入本文明 UU,剔除被替换的基础单位
# ---------------------------------------------------------------------------


class TestUniqueUnitMainline:
    MY_UU = {"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}

    def test_legion_included_swordsman_excluded(self):
        out = tech_path._domination_target_techs(
            {"TECH_MINING", "TECH_BRONZE_WORKING"}, self.MY_UU
        )
        techs = [pre for pre, _n, _c in out]
        names_combat = {(n, c) for _p, n, c in out}
        # 炼铁术应该在候选里,且对应的是军团(40)而不是剑士(35)
        assert "TECH_IRON_WORKING" in techs
        assert any(c == 40 for _n, c in names_combat)
        assert not any(c == 35 for _n, c in names_combat)

    def test_without_uu_legion_still_excluded(self):
        out = tech_path._domination_target_techs(
            {"TECH_MINING", "TECH_BRONZE_WORKING"}, None
        )
        assert not any(c == 40 for _p, _n, c in out)

    def test_scan_parses_uu_lines(self):
        scan = wm_mod.parse_world_scan(["UU|UNIT_ROMAN_LEGION|UNIT_SWORDSMAN"])
        assert scan.unique_units == {"UNIT_ROMAN_LEGION": "UNIT_SWORDSMAN"}

    def test_units_query_resolves_uu(self):
        lua = lua_units.build_units_query()
        assert "resolveUU" in lua

    def test_upgrade_builders_resolve_uu(self):
        from civ_mcp.lua import governance

        assert "resolveUU" in governance.build_unit_upgrade_query(1)
        assert "resolveUU" in governance.build_upgrade_unit(1)


# ---------------------------------------------------------------------------
# 5e — 平民判定看战斗力,不看充能(罗马军团 charges:1 不是平民)
# ---------------------------------------------------------------------------


class _FakeUnit:
    def __init__(
        self, utype, cs, charges, moves=2.0, max_moves=2.0, x=10, y=10, name=""
    ):
        self.unit_type = utype
        self.combat_strength = cs
        self.build_charges = charges
        self.moves_remaining = moves
        self.max_moves = max_moves
        self.x, self.y = x, y
        self.name = name or utype


class TestCivilianClassification:
    def test_legion_with_charges_is_military(self):
        legion = _FakeUnit("UNIT_ROMAN_LEGION", cs=40, charges=1)
        blockers, warnings = check_turn_completion([legion], [])
        assert not blockers  # 军事单位闲置只进 warnings
        assert any("军事单位" in w for w in warnings)

    def test_builder_still_civilian(self):
        builder = _FakeUnit("UNIT_BUILDER", cs=0, charges=3)
        blockers, _ = check_turn_completion([builder], [])
        assert any("平民" in b for b in blockers)


# ---------------------------------------------------------------------------
# 5c — 失联:半径上限 + 按目击销档
# ---------------------------------------------------------------------------


class TestContacts:
    def test_reacquire_by_sightings_within_radius(self):
        wm = wm_mod.WorldModel()
        wm.record_lost_contact("UNIT_SCOUT", "Barbarian", 36, 43, 11, moves=3)
        n = wm.reacquire_by_sightings([("UNIT_SCOUT", 32, 42)], turn=13)
        assert n == 1
        assert not wm.stale_contacts()

    def test_reacquire_ignores_wrong_type(self):
        wm = wm_mod.WorldModel()
        wm.record_lost_contact("UNIT_SCOUT", "Barbarian", 36, 43, 11, moves=3)
        n = wm.reacquire_by_sightings([("UNIT_WARRIOR", 36, 43)], turn=13)
        assert n == 0

    def test_briefing_skips_huge_radius(self):
        wm = wm_mod.WorldModel(tiles_total=100, tiles_revealed=50)
        wm.record_lost_contact("UNIT_SCOUT", "英国", 19, 34, 27, moves=2)
        text = wm_mod.render_briefing(wm, turn=50)  # 23 回合 × 2 = 半径 46
        assert "失联" not in text


# ---------------------------------------------------------------------------
# 5b — 城邦建议附带使者存量
# ---------------------------------------------------------------------------


class TestEnvoyStock:
    def test_suzerain_line_reports_stock(self):
        cs = city_state_advisor.CityState(
            player_id=9,
            name="普雷斯拉夫",
            city_state_type="Militaristic",
            envoys_sent=1,
            suzerain_id=-1,
            suzerain_name="None",
            can_send_envoy=True,
        )
        ctx = city_state_advisor.CityStateContext(
            tokens_available=0,
            city_states=[cs],
            victory_type="DOMINATION",
            my_player_id=0,
        )
        lines = city_state_advisor.get_city_state_advice(ctx)
        text = "\n".join(lines)
        assert "你手上有 0 个" in text
        assert "使者来源" in text


# ---------------------------------------------------------------------------
# 1b — diary 跨 run_id 归并
# ---------------------------------------------------------------------------


class TestDiaryMerge:
    def test_merges_all_run_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diary, "DIARY_DIR", tmp_path)
        f1 = tmp_path / "diary_rome_-758731782_run-aaa.jsonl"
        f2 = tmp_path / "diary_rome_-758731782_run-bbb.jsonl"
        f1.write_text(
            json.dumps(
                {
                    "v": 2,
                    "is_agent": True,
                    "turn": 5,
                    "reflections": {"tactical": "old"},
                }
            )
            + "\n"
            + json.dumps({"v": 2, "is_agent": True, "turn": 7, "reflections": {}})
            + "\n"
        )
        f2.write_text(
            json.dumps(
                {
                    "v": 2,
                    "is_agent": True,
                    "turn": 7,
                    "reflections": {"tactical": "new"},
                }
            )
            + "\n"
            + json.dumps({"v": 2, "is_agent": True, "turn": 9, "reflections": {}})
            + "\n"
        )
        import os
        import time

        past = time.time() - 100
        os.utime(f1, (past, past))  # f1 是较早的 run

        entries = diary.read_all_game_entries("rome", -758731782)
        turns = [e["turn"] for e in entries]
        assert turns == [5, 7, 9]  # 跨 run 归并且有序
        t7 = next(e for e in entries if e["turn"] == 7)
        assert t7["reflections"].get("tactical") == "new"  # 后写入的覆盖

    def test_skips_cities_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(diary, "DIARY_DIR", tmp_path)
        (tmp_path / "diary_rome_1_run-a.jsonl").write_text(
            json.dumps({"v": 2, "is_agent": True, "turn": 1}) + "\n"
        )
        (tmp_path / "diary_rome_1_run-a_cities.jsonl").write_text(
            json.dumps({"v": 2, "is_agent": True, "turn": 99}) + "\n"
        )
        entries = diary.read_all_game_entries("rome", 1)
        assert [e["turn"] for e in entries] == [1]


# ---------------------------------------------------------------------------
# P1 #1 — 开局评估 fallback 渲染为「触发条件」而非「已发生」
# ---------------------------------------------------------------------------


class TestFallbackRendering:
    def test_trigger_rendered_as_condition(self):
        from civ_mcp import strategy_space
        from civ_mcp.opening_assessor import assess_opening

        try:
            s = strategy_space._find("rome")
        except Exception:
            pytest.skip("策略库不可用")
        if s is None or not getattr(s, "fallbacks", None):
            pytest.skip("罗马策略无 fallback")
        out = assess_opening(
            {
                "civ": "罗马",
                "leader": "图拉真",
                "turn": 1,
                "difficulty": "DIFFICULTY_KING",
                "map_type": "PANGAEA",
                "capital": {"x": 38, "y": 42},
                "explored_tiles": [],
                "neighbors": [],
                "my_military": 20,
            }
        )
        if "风险" in out:
            head = out.split("风险", 1)[1][:30]
            assert "尚未观测" in head or "若" in head
