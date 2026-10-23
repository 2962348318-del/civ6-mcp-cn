"""L1 世界模型的单元测试(改造方案 §5)。

核心不变量:观测缺失 ≠ 事实消失。营地只增不减,失联单位不会因为看不见就消失。
"""

from __future__ import annotations

from civ_mcp import world_model as wm_mod
from civ_mcp.world_model import WorldModel, parse_world_scan, render_briefing


# --------------------------------------------------------------------------
# 铁律二:观测 ≠ 真相
# --------------------------------------------------------------------------


def test_camp_persists_across_turns():
    """营地一旦发现就永久记住 —— 这是 T28 误判"蛮族全清"的解药。"""
    wm = WorldModel()
    assert wm.observe_camp(9, 33, turn=12, dist=6) is True
    assert wm.observe_camp(9, 33, turn=20, dist=6) is False  # 再次确认不是新发现
    assert len(wm.active_camps()) == 1
    assert wm.camps["9,33"].first_seen_turn == 12
    assert wm.camps["9,33"].last_confirmed_turn == 20


def test_contact_survives_going_dark():
    """单位进迷雾后仍在档案里,不会因为看不见就蒸发。"""
    wm = WorldModel()
    wm.record_lost_contact(
        "UNIT_HORSEMAN",
        "Barbarian",
        13,
        34,
        turn=47,
        hp="80/100",
        fled_dir="西",
        moves=4,
    )
    assert len(wm.stale_contacts()) == 1


def test_guess_radius_grows_with_time():
    """推测半径 = 失联回合数 × 移动力。越久范围越大,不能假装它还在原地。"""
    wm = WorldModel()
    wm.record_lost_contact("UNIT_HORSEMAN", "Barbarian", 13, 34, turn=47, moves=4)
    c = wm.stale_contacts()[0]
    assert c.guess_radius(47) == 0
    assert c.guess_radius(50) == 12  # 3 回合 × 移动力 4
    assert c.guess_radius(48) == 4


def test_death_tile_accumulates():
    """复盘里 (15,34) 死了建造者和投石兵两个。"""
    wm = WorldModel()
    wm.record_death(15, 34, "UNIT_BUILDER", 19)
    wm.record_death(15, 34, "UNIT_SLINGER", 35)
    assert len(wm.death_tiles["15,34"].victims) == 2


def test_prune_only_drops_old_contacts():
    wm = WorldModel()
    wm.record_lost_contact("UNIT_SCOUT", "Barbarian", 1, 1, turn=10)
    wm.record_lost_contact("UNIT_HORSEMAN", "Barbarian", 2, 2, turn=48)
    wm.prune_contacts(current_turn=50, max_age=15)
    assert len(wm.contacts) == 1


# --------------------------------------------------------------------------
# 探索度 —— 实测揭示的最靠前失败环节
# --------------------------------------------------------------------------


def test_exploration_pct():
    wm = WorldModel(tiles_revealed=98, tiles_total=3404)
    assert abs(wm.exploration_pct - 0.0288) < 0.001


def test_poor_exploration_warns_loudly():
    """实测:失败那局 T52 只探明 2.9%,13 个营地一个没发现。"""
    wm = WorldModel(tiles_revealed=98, tiles_total=3404)
    b = render_briefing(wm, 52)
    assert "🔴" in b
    assert "2.9%" in b
    assert "探图" in b


def test_healthy_exploration_no_warning():
    wm = WorldModel(tiles_revealed=1200, tiles_total=3404)
    b = render_briefing(wm, 80)
    assert "🔴" not in b
    assert "视野偏窄" not in b


def test_zero_total_does_not_crash():
    assert render_briefing(WorldModel(), 1) == ""


# --------------------------------------------------------------------------
# 简报
# --------------------------------------------------------------------------


def test_briefing_lists_camp_with_age():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.observe_camp(9, 33, turn=9, dist=6)
    b = render_briefing(wm, 52)
    assert "(9,33)" in b
    assert "距首都 6 格" in b
    assert "43 回合前" in b  # 52 - 9
    assert "未拆除" in b


def test_briefing_sorts_camps_by_distance():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.observe_camp(50, 50, turn=5, dist=40)
    wm.observe_camp(9, 33, turn=5, dist=6)
    b = render_briefing(wm, 20)
    assert b.index("(9,33)") < b.index("(50,50)")


def test_destroyed_camp_drops_off():
    """亲眼确认(可见 + 无营地)才判定拆除。"""
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.observe_camp(9, 33, turn=9, dist=6)
    gone = wm.apply_camp_checks([(9, 33, True, False)])
    assert len(gone) == 1
    assert "(9,33)" not in render_briefing(wm, 20)


def test_unseen_camp_is_not_destroyed():
    """铁律二:看不见 ≠ 拆掉了。迷雾里的营地必须维持"未拆除"。"""
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.observe_camp(9, 33, turn=9, dist=6)
    gone = wm.apply_camp_checks([(9, 33, False, False)])  # 不可见
    assert gone == []
    assert "(9,33)" in render_briefing(wm, 20)


def test_briefing_shows_stale_contact_radius():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.record_lost_contact(
        "UNIT_HORSEMAN",
        "Barbarian",
        13,
        34,
        turn=47,
        hp="80/100",
        fled_dir="西",
        moves=4,
    )
    b = render_briefing(wm, 50)
    assert "失联" in b
    assert "(13,34)" in b
    assert "半径 12" in b
    assert "向西进雾" in b


def test_briefing_warns_about_death_tiles():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.record_death(15, 34, "UNIT_BUILDER", 19)
    b = render_briefing(wm, 30)
    assert "(15,34)" in b
    assert "勿再派平民进入" in b


def test_briefing_stays_bounded():
    """情报再多也不能刷屏(方案 §13.3)。"""
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    for i in range(12):
        wm.observe_camp(i, i, turn=1, dist=i)
        wm.record_lost_contact(f"UNIT_{i}", "Barbarian", i, i, turn=40 + i)
        wm.record_death(i, i + 1, "UNIT_BUILDER", 10)
    b = render_briefing(wm, 50)
    assert b.count("👁") <= 4  # 失联最多 4 条
    assert b.count("☠️") <= 1  # 死亡格压成一行


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------


def test_roundtrip_preserves_everything(tmp_path):
    wm = WorldModel(
        game_id="rome_1", current_turn=52, tiles_revealed=98, tiles_total=3404
    )
    wm.observe_camp(9, 33, turn=9, dist=6)
    wm.record_lost_contact(
        "UNIT_HORSEMAN",
        "Barbarian",
        13,
        34,
        turn=47,
        hp="80/100",
        fled_dir="西",
        moves=4,
    )
    wm.record_death(15, 34, "UNIT_BUILDER", 19)

    p = tmp_path / "世界模型" / "rome_1.json"
    wm.save(p)
    back = WorldModel.load(p)

    assert back.current_turn == 52
    assert back.tiles_revealed == 98
    assert back.camps["9,33"].first_seen_turn == 9
    assert back.stale_contacts()[0].fled_dir == "西"
    assert back.death_tiles["15,34"].victims == ["UNIT_BUILDER"]


def test_load_missing_file_returns_empty(tmp_path):
    assert WorldModel.load(tmp_path / "nope.json").camps == {}


def test_load_corrupt_file_does_not_crash(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert WorldModel.load(p).camps == {}


# --------------------------------------------------------------------------
# 扫描解析
# --------------------------------------------------------------------------


def test_parse_world_scan():
    scan = parse_world_scan(
        [
            "CAMP|9|33|6",
            "CAMP|15|28|7",
            "EXPLORE|98|3404",
            "STATS|1|2",
            "TECH|TECH_MINING",
            "TECH|TECH_POTTERY",
        ]
    )
    assert scan.camps == [(9, 33, 6), (15, 28, 7)]
    assert (scan.revealed, scan.total) == (98, 3404)
    assert (scan.cities, scan.military) == (1, 2)
    assert scan.techs == {"TECH_MINING", "TECH_POTTERY"}


def test_parse_tolerates_garbage():
    scan = parse_world_scan(["CAMP|x|y|z", "EXPLORE|a|b", "STATS|?|?", "junk"])
    assert scan.camps == []
    assert (scan.revealed, scan.total) == (0, 0)


# --------------------------------------------------------------------------
# L2 计划链 —— 治"一个计划完成就忘记继续下一个"
# --------------------------------------------------------------------------


def test_plan_chain_auto_activates_next():
    """完成即自动激活后继 —— 这是计划链存在的全部意义。"""
    wm = WorldModel()
    p1 = wm.add_plan("造投石兵", "military_at_least", "1", turn=1)
    p2 = wm.add_plan("拆营地(9,33)", "camp_destroyed", "9,33", after=p1.id, turn=1)
    p3 = wm.add_plan("铺第二城", "cities_at_least", "2", after=p2.id, turn=1)
    assert p1.status == "active"
    assert p2.status == "pending"

    done = wm.evaluate_plans({"military": 1}, turn=5)
    assert [p.id for p in done] == [p1.id]
    assert p2.status == "active"  # 自动激活,模型不需要记得
    assert p3.status == "pending"

    done = wm.evaluate_plans({"military": 1, "destroyed_camps": {"9,33"}}, turn=12)
    assert [p.id for p in done] == [p2.id]
    assert p3.status == "active"


def test_plan_conditions_are_server_judged():
    """模型说"做完了"不算数,数字说了算。"""
    wm = WorldModel()
    wm.add_plan("铺三城", "cities_at_least", "3", turn=1)
    wm.evaluate_plans({"cities": 2}, turn=10)
    assert wm.active_plans()  # 2 < 3,没完成
    wm.evaluate_plans({"cities": 3}, turn=11)
    assert not wm.active_plans()


def test_plan_all_condition_types():
    wm = WorldModel()
    wm.add_plan("a", "exploration_at_least", "0.15", turn=1)
    wm.add_plan("b", "tech_completed", "TECH_ARCHERY", turn=1)
    wm.add_plan("c", "manual", "", turn=1)
    done = wm.evaluate_plans({"exploration": 0.2, "techs": {"TECH_ARCHERY"}}, turn=5)
    assert len(done) == 2
    assert len(wm.active_plans()) == 1  # manual 不会被自动判定


def test_plan_corrupt_args_never_complete():
    wm = WorldModel()
    wm.add_plan("坏计划", "cities_at_least", "not_a_number", turn=1)
    done = wm.evaluate_plans({"cities": 99}, turn=5)
    assert done == []  # 不崩,也不误判完成


def test_briefing_shows_plan_chain():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    p1 = wm.add_plan("拆营地(9,33)", "camp_destroyed", "9,33", turn=1)
    wm.add_plan("铺第二城", "cities_at_least", "2", after=p1.id, turn=1)
    b = render_briefing(wm, 10)
    assert "📋 计划链" in b
    assert "▶ #1 拆营地(9,33)" in b
    assert "⏸ #2 铺第二城(等待 #1 完成)" in b


def test_briefing_celebrates_completion_and_announces_successor():
    """完成 + 有后继 → 播报完成,并说明后续已自动激活。"""
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    p1 = wm.add_plan("造投石兵", "military_at_least", "1", turn=1)
    wm.add_plan("拆营地(9,33)", "camp_destroyed", "9,33", after=p1.id, turn=1)
    wm.evaluate_plans({"military": 1}, turn=10)
    b = render_briefing(wm, 10)
    assert "✔ #1 造投石兵" in b
    assert "后续计划已自动激活" in b
    assert "▶ #2 拆营地(9,33)" in b


def test_briefing_prompts_when_chain_runs_dry():
    """最后一个计划完成 → 不能静默,要提示挂新目标。

    这正是"一个计划完成就忘了下一个"最容易复发的时刻:
    链空了,如果简报什么都不说,模型就真的没有下一步了。
    """
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.add_plan("造投石兵", "military_at_least", "1", turn=1)
    wm.evaluate_plans({"military": 1}, turn=10)
    b = render_briefing(wm, 10)
    assert "✔ #1 造投石兵" in b
    assert "后续计划已自动激活" not in b  # 没有后继,不能谎称有
    assert "plan_add" in b  # 但必须提示挂新目标


# --------------------------------------------------------------------------
# 查询安全性
# --------------------------------------------------------------------------


def test_scan_query_only_reports_revealed_camps():
    """把迷雾里的营地喂给模型是作弊 —— 查询必须先判 IsRevealed。"""
    lua = wm_mod.build_world_scan_query()
    assert "IsRevealed" in lua
    assert "BarbarianCamp" in lua
    # 营地判定必须嵌在 isRev 分支内
    assert lua.index("if isRev then") < lua.index("BarbarianCamp")


# --------------------------------------------------------------------------
# 对手推演 —— "假设自己是对面"(用户 2026-07-31 提出)
# --------------------------------------------------------------------------


class _T:
    """最小 ThreatInfo 替身。"""

    def __init__(
        self, distance, moves=2, rng=0, utype="UNIT_HORSEMAN", owner="Barbarian"
    ):
        self.distance = distance
        self.base_moves = moves
        self.range = rng
        self.unit_type = utype
        self.owner_name = owner


def test_projection_flags_fast_enemy_as_immediate():
    """骑手(移动 4)在 4 格外 —— 模型看到"4 格"会以为安全,推演告诉它不是。"""
    out = wm_mod.render_enemy_projection([_T(distance=4, moves=4)])
    assert "下回合可打到我方" in out
    assert "可达 4 格" in out


def test_projection_marks_two_turn_threat():
    out = wm_mod.render_enemy_projection([_T(distance=7, moves=4)])
    assert "两回合内可到" in out


def test_projection_ignores_far_enemies():
    out = wm_mod.render_enemy_projection([_T(distance=30, moves=2)])
    assert out == ""


def test_projection_counts_range():
    """弓箭手 移动2+射程2 = 可达 4。"""
    out = wm_mod.render_enemy_projection(
        [_T(distance=4, moves=2, rng=2, utype="UNIT_ARCHER")]
    )
    assert "下回合可打到我方" in out
    assert "射程 2" in out


def test_projection_empty_without_threats():
    assert wm_mod.render_enemy_projection([]) == ""


def test_projection_is_bounded():
    many = [_T(distance=2, moves=4) for _ in range(12)]
    assert wm_mod.render_enemy_projection(many).count("⚔") <= 5


def test_briefing_includes_projection():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    b = render_briefing(wm, 20, [_T(distance=3, moves=4)])
    assert "对手推演" in b


def test_camp_check_query_gated_on_visibility():
    """拆除判定只信亲眼所见(铁律二)。"""
    lua = wm_mod.build_camp_check_query([(9, 33), (15, 28)])
    assert "IsVisible" in lua
    assert "BarbarianCamp" in lua


def test_scan_query_collects_facts_for_plans():
    """计划链的完成判定依赖这些事实,查询里必须有。"""
    lua = wm_mod.build_world_scan_query()
    assert "STATS|" in lua
    assert "TECH|" in lua


# --------------------------------------------------------------------------
# L3 威胁仲裁 —— 治"蛮族围城了还在种田"
# --------------------------------------------------------------------------


class _Threat:
    """鸭子类型,匹配 lq.ThreatInfo 用到的字段。"""

    def __init__(self, distance, cs=20, owner="Barbarian"):
        self.distance = distance
        self.combat_strength = cs
        self.owner_name = owner


def test_close_enemy_is_red():
    lvl, reason = wm_mod.compute_threat_level(WorldModel(), [_Threat(distance=2)])
    assert lvl == "RED"
    assert "3 格内" in reason or "2 格" in reason


def test_near_camp_plus_threat_is_red():
    """复盘现场:营地在 6 格外持续出兵,视野里有敌军。"""
    wm = WorldModel()
    wm.observe_camp(9, 33, turn=5, dist=6)
    lvl, reason = wm_mod.compute_threat_level(wm, [_Threat(distance=8)])
    assert lvl == "RED"
    assert "(9,33)" in reason


def test_distant_camp_alone_is_yellow():
    wm = WorldModel()
    wm.observe_camp(9, 33, turn=5, dist=6)
    lvl, _ = wm_mod.compute_threat_level(wm, [])
    assert lvl == "YELLOW"


def test_far_camp_no_threat_is_yellow_not_red():
    wm = WorldModel()
    wm.observe_camp(50, 50, turn=5, dist=40)
    lvl, _ = wm_mod.compute_threat_level(wm, [])
    assert lvl == "YELLOW"


def test_clean_board_is_green():
    lvl, reason = wm_mod.compute_threat_level(WorldModel(), [])
    assert lvl == "GREEN"
    assert reason == ""


def test_red_directive_targets_nearest_camp():
    wm = WorldModel()
    wm.observe_camp(50, 50, turn=1, dist=40)
    wm.observe_camp(9, 33, turn=1, dist=6)
    d = wm_mod.red_directive(wm)
    assert "(9,33)" in d
    assert "唯一目标" in d
    assert "不要生产开拓者" in d


def test_red_directive_without_camps():
    d = wm_mod.red_directive(WorldModel())
    assert "唯一目标" in d
    assert "敌方军事单位" in d


def test_briefing_leads_with_threat_level():
    """威胁等级永远是第一行 —— 决策入口处必须先看到它。"""
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.threat_level = "RED"
    wm.threat_reason = "蛮族已进至 2 格内"
    wm.observe_camp(9, 33, turn=1, dist=6)
    b = render_briefing(wm, 20)
    first = b.split("\n")[1]  # [0] 是 "== 世界情报 =="
    assert first.startswith("🔴 威胁等级 RED")
    assert "唯一目标" in b


def test_briefing_shows_victory_route():
    wm = WorldModel(tiles_revealed=1000, tiles_total=3404)
    wm.victory_type = "SCIENCE"
    wm.victory_locked_turn = 3
    b = render_briefing(wm, 20)
    assert "胜利路线:SCIENCE" in b
    assert "T3 锁定" in b


def test_green_empty_board_renders_nothing():
    """无威胁、无情报、无计划时不占版面。"""
    assert render_briefing(WorldModel(), 1) == ""


def test_l3_state_survives_roundtrip(tmp_path):
    wm = WorldModel(game_id="g", tiles_revealed=100, tiles_total=1000)
    wm.victory_type = "SCIENCE"
    wm.victory_reason = "朝鲜书院加成"
    wm.victory_locked_turn = 2
    wm.threat_level = "RED"
    wm.threat_reason = "测试"
    wm.city_count, wm.military_count = 3, 5
    wm.add_plan("A", "cities_at_least", "4", turn=1)
    p = tmp_path / "w.json"
    wm.save(p)
    back = WorldModel.load(p)
    assert back.victory_type == "SCIENCE"
    assert back.victory_locked_turn == 2
    assert back.threat_level == "RED"
    assert back.city_count == 3
    assert back.military_count == 5
    assert len(back.plans) == 1
    assert back.next_plan_id == 2
