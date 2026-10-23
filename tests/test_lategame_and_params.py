"""中后期决策 / 难度参数 / 对手推演 / 政策检查 / L4 复盘 的单元测试。"""

from __future__ import annotations

from civ_mcp import debrief, lategame, opponents, params, policy


# ===========================================================================
# 四条胜利路线 —— 用户要求"不同的胜利条件怎么胜利都要会"
# ===========================================================================


def test_all_four_victory_paths_defined():
    for v in ("SCIENCE", "CULTURE", "RELIGION", "DOMINATION"):
        assert v in lategame.VICTORY_PATHS, f"{v} 缺失"
        p = lategame.VICTORY_PATHS[v]
        assert p.condition and p.key_metric and p.common_loss
        assert len(p.stages) >= 4, f"{v} 阶段太少"


def test_victory_playbook_is_actionable():
    for v in ("SCIENCE", "CULTURE", "RELIGION", "DOMINATION"):
        pb = lategame.victory_playbook(v)
        assert "判据" in pb and "阶段" in pb and "输法" in pb


def test_culture_victory_warns_about_tourism_not_culture():
    """文化胜利最常见的误解:堆文化产出而不是旅游业绩。"""
    p = lategame.VICTORY_PATHS["CULTURE"]
    assert "游客" in p.condition
    assert "旅游" in p.common_loss


def test_religion_victory_flags_great_prophet_race():
    p = lategame.VICTORY_PATHS["RELIGION"]
    assert "大预言家" in p.common_loss


def test_domination_needs_original_capitals_only():
    p = lategame.VICTORY_PATHS["DOMINATION"]
    assert "原始首都" in p.condition


def test_unknown_victory_playbook_empty():
    assert lategame.victory_playbook("BOGUS") == ""


# ===========================================================================
# 产能瓶颈 —— 用真实项目成本硬算
# ===========================================================================


def test_science_chain_total_cost():
    assert lategame.SCIENCE_TOTAL_COST == 7800  # 900+1500+1800*3


def test_low_production_science_is_flagged_as_unwinnable():
    """科技领先但产能不足 = 输。这是原方案完全没覆盖的中后期输法。"""
    ctx = lategame.LateGameContext(
        turn=200,
        era="ERA_INFORMATION",
        victory_type="SCIENCE",
        total_production=30,
        city_count=3,
        has_spaceport=True,
    )
    adv = lategame.check_lategame(ctx)
    txt = " ".join(a.text for a in adv)
    assert "产能" in txt
    assert any(a.severity == "warn" for a in adv)


def test_adequate_production_not_alarmed():
    ctx = lategame.LateGameContext(
        turn=200,
        era="ERA_INFORMATION",
        victory_type="SCIENCE",
        total_production=300,
        city_count=8,
        has_spaceport=True,
    )
    adv = lategame.check_lategame(ctx)
    assert not any("赢不了" in a.text for a in adv)


def test_missing_spaceport_is_a_hard_warning():
    ctx = lategame.LateGameContext(
        turn=200,
        era="ERA_INFORMATION",
        victory_type="SCIENCE",
        total_production=200,
        city_count=6,
        has_spaceport=False,
    )
    assert any("太空港" in a.text for a in lategame.check_lategame(ctx))


def test_early_game_stays_quiet():
    """远古期不该出现中后期建议 —— 噪音会淹没真正紧急的事。"""
    ctx = lategame.LateGameContext(
        turn=20,
        era="ERA_ANCIENT",
        victory_type="SCIENCE",
        total_production=10,
        city_count=1,
    )
    assert lategame.check_lategame(ctx) == []


def test_city_deficit_flagged():
    ctx = lategame.LateGameContext(
        turn=120,
        era="ERA_MEDIEVAL",
        victory_type="SCIENCE",
        total_production=100,
        city_count=2,
        rival_max_cities=9,
    )
    assert any("城" in a.text for a in lategame.check_lategame(ctx))


def test_wonder_race_risk_mentioned():
    ctx = lategame.LateGameContext(
        turn=120,
        era="ERA_MEDIEVAL",
        victory_type="CULTURE",
        total_production=100,
        city_count=5,
        wonder_obsessed_rivals=["秦始皇"],
    )
    assert any("奇观" in a.text for a in lategame.check_lategame(ctx))


def test_district_hint_uses_real_adjacency_rules():
    h = lategame.district_hint("DISTRICT_CAMPUS")
    assert "山脉" in h  # Adjacency_YieldChanges: Mountains_Science
    assert lategame.district_hint("DISTRICT_COMMERCIAL_HUB")
    assert lategame.district_hint("BOGUS") == ""


# ===========================================================================
# 难度参数表
# ===========================================================================


def test_all_eight_difficulties_have_params():
    for d in params.DIFFICULTY_ORDER:
        p = params.difficulty_params(d)
        assert p.cities_by_t50 > 0


def test_harder_difficulty_demands_more():
    easy = params.difficulty_params("DIFFICULTY_SETTLER")
    hard = params.difficulty_params("DIFFICULTY_DEITY")
    assert hard.cities_by_t50 > easy.cities_by_t50
    assert hard.first_military_by < easy.first_military_by
    assert hard.barb_factor > easy.barb_factor


def test_unknown_difficulty_falls_back_to_prince():
    assert params.difficulty_params(None) == params.difficulty_params(
        "DIFFICULTY_PRINCE"
    )
    assert params.difficulty_params("BOGUS") == params.difficulty_params(
        "DIFFICULTY_PRINCE"
    )


def test_pace_check_catches_the_52_turn_failure():
    """复盘那局:T52、1 城、军阀难度 —— 当时没有任何机制提醒。"""
    out = params.check_pace(52, cities=1, military=1, difficulty="DIFFICULTY_WARLORD")
    assert out
    assert any("座城" in o for o in out)


def test_pace_check_flags_no_military():
    out = params.check_pace(20, cities=1, military=0, difficulty="DIFFICULTY_PRINCE")
    assert any("无军事单位" in o for o in out)


def test_healthy_pace_is_silent():
    assert (
        params.check_pace(50, cities=5, military=5, difficulty="DIFFICULTY_PRINCE")
        == []
    )


def test_map_params_cover_main_types():
    assert params.map_params("ARCHIPELAGO").naval_priority == "高"
    assert params.map_params("PANGAEA").naval_priority == "低"
    assert params.map_params(None).note


# ===========================================================================
# 对手推演 —— 用户的"假设自己是对面"想法(意图层)
# ===========================================================================


class _Agenda:
    def __init__(self, name):
        self.name = name
        self.category = "HISTORICAL"
        self.description = ""


class _Civ:
    def __init__(self, **kw):
        self.player_id = kw.get("player_id", 1)
        self.civ_name = kw.get("civ_name", "英国")
        self.leader_name = kw.get("leader_name", "维多利亚")
        self.is_at_war = kw.get("is_at_war", False)
        self.has_met = True
        self.diplomatic_state = kw.get("diplomatic_state", "NEUTRAL")
        self.military_strength = kw.get("military_strength", 0)
        self.agendas = kw.get("agendas", [])


def test_military_gap_raises_war_risk():
    """军力悬殊本身就是开战信号 —— 这是能用数字算的,不用猜意图。"""
    r = opponents.read_opponent(_Civ(military_strength=200), my_military=50)
    assert r.military_ratio == 4.0
    assert r.triggers
    assert r.war_risk in ("中", "高")


def test_zero_own_military_is_max_risk():
    r = opponents.read_opponent(_Civ(military_strength=100), my_military=0)
    assert r.military_ratio > 10


def test_agenda_profiles_cover_all_historical_leaders():
    """20 位领袖的固定议程都要有画像,否则推演会有盲区。"""
    historical = [
        "AGENDA_IRON_CROWN",
        "AGENDA_BLACK_QUEEN",
        "AGENDA_QUEEN_OF_NILE",
        "AGENDA_PEACEKEEPER",
        "AGENDA_ALLY_OF_ENKIDU",
        "AGENDA_WITH_YOUR_SHIELD_OR_ON_IT",
        "AGENDA_LAST_VIKING_KING",
        "AGENDA_BUSHIDO",
        "AGENDA_ENTHUSIASTIC_DISCIPLE",
        "AGENDA_GREAT_PERSON_OBSESSED",
        "AGENDA_DELIAN_LEAGUE",
        "AGENDA_WESTERNIZER",
        "AGENDA_COUNTER_REFORMER",
        "AGENDA_WONDER_OBSESSED",
        "AGENDA_AYYUBID_DYNASTY",
        "AGENDA_BACKSTAB_AVERSE",
        "AGENDA_OPTIMUS_PRINCEPS",
        "AGENDA_BIG_STICK_POLICY",
        "AGENDA_SUN_NEVER_SETS",
        "AGENDA_TLATOANI",
    ]
    missing = [a for a in historical if a not in opponents.AGENDA_PROFILES]
    assert not missing, f"缺少议程画像: {missing}"


def test_every_profile_is_complete():
    for key, p in opponents.AGENDA_PROFILES.items():
        assert p.cares and p.angers and p.predicts, key
        assert p.war_risk in ("高", "中", "低"), key


def test_render_shows_war_and_military_gap():
    reads = [
        opponents.read_opponent(
            _Civ(civ_name="斯基泰", is_at_war=True, military_strength=300),
            my_military=60,
        )
    ]
    out = opponents.render_opponents(reads)
    assert "交战中" in out
    assert "军力" in out


def test_render_empty_when_no_contact():
    assert opponents.render_opponents([]) == ""


def test_barbarian_doctrine_is_deterministic():
    """蛮族不用猜 —— 规则已知,行为可算。"""
    assert any("平民" in d for d in opponents.BARBARIAN_DOCTRINE)
    out = opponents.render_opponents([], has_barbarians=True)
    assert "蛮族" in out


# ===========================================================================
# 政策卡自动检查
# ===========================================================================


def _pctx(**kw):
    base = dict(
        slots={"SLOT_MILITARY": ["POLICY_X"], "SLOT_ECONOMIC": ["POLICY_Y"]},
        available=set(),
        producing=[],
        threat_level="GREEN",
        victory_type="",
        exploration=1.0,
    )
    base.update(kw)
    return policy.PolicyContext(**base)


def test_empty_slot_is_top_priority():
    """空槽是最高优先 —— 但前提是【有卡能插】。"""
    ctx = _pctx(
        slots={"SLOT_MILITARY": ["", ""], "SLOT_ECONOMIC": ["POLICY_Y"]},
        available={"POLICY_DISCIPLINE"},  # 军事卡,填得上那两个空槽
    )
    adv = policy.check_policies(ctx)
    assert adv
    assert "空政策槽" in adv[0].text


def test_empty_slot_silent_when_no_card_unlocked():
    """2026-08-07 苏美尔局 T5 实测:法典未研完、零卡解锁,简报却在喊
    "2 个空政策槽,用 set_policies 补上" —— 一条看起来可执行、实际做不了的
    建议。比纯噪音更伤:agent 真的会去调 set_policies,失败后开始怀疑整个建议层。"""
    ctx = _pctx(
        slots={"SLOT_MILITARY": ["", ""], "SLOT_ECONOMIC": [""]}, available=set()
    )
    assert not [a for a in policy.check_policies(ctx) if "空政策槽" in a.text]


def test_empty_slot_only_reports_fillable_types():
    """只有军事卡时,不该去催填经济槽。"""
    ctx = _pctx(
        slots={"SLOT_MILITARY": [""], "SLOT_ECONOMIC": [""]},
        available={"POLICY_DISCIPLINE"},  # gamedata: SLOT_MILITARY
    )
    line = next(a.text for a in policy.check_policies(ctx) if "空政策槽" in a.text)
    assert "SLOT_MILITARY" in line and "SLOT_ECONOMIC" not in line


def test_settler_production_suggests_colonization():
    ctx = _pctx(
        slots={"SLOT_ECONOMIC": [""]},
        available={"POLICY_COLONIZATION"},
        producing=["UNIT_SETTLER"],
    )
    assert any("殖民" in a.text for a in policy.check_policies(ctx))


def test_red_alert_suggests_discipline():
    ctx = _pctx(
        slots={"SLOT_MILITARY": [""]},
        available={"POLICY_DISCIPLINE"},
        threat_level="RED",
    )
    assert any("纪律" in a.text for a in policy.check_policies(ctx))


def test_science_victory_suggests_natural_philosophy():
    ctx = _pctx(
        slots={"SLOT_ECONOMIC": [""]},
        available={"POLICY_NATURAL_PHILOSOPHY"},
        victory_type="SCIENCE",
    )
    assert any("自然哲学" in a.text for a in policy.check_policies(ctx))


def test_locked_card_not_suggested():
    """还没解锁的卡不能建议 —— 会让模型去调一个必然失败的操作。"""
    ctx = _pctx(
        slots={"SLOT_ECONOMIC": [""]}, available=set(), producing=["UNIT_SETTLER"]
    )
    assert not any("殖民" in a.text for a in policy.check_policies(ctx))


def test_already_inserted_card_not_suggested():
    ctx = _pctx(
        slots={"SLOT_ECONOMIC": ["POLICY_COLONIZATION"]},
        available={"POLICY_COLONIZATION"},
        producing=["UNIT_SETTLER"],
    )
    assert not any("殖民" in a.text for a in policy.check_policies(ctx))


def test_government_change_forces_reshuffle():
    ctx = _pctx(government_changed=True)
    assert any("政体" in a.text for a in policy.check_policies(ctx))


def test_advice_is_capped():
    ctx = _pctx(
        slots={"SLOT_MILITARY": ["", ""], "SLOT_ECONOMIC": ["", ""]},
        available={
            "POLICY_DISCIPLINE",
            "POLICY_BASTIONS",
            "POLICY_COLONIZATION",
            "POLICY_ILKUM",
            "POLICY_NATURAL_PHILOSOPHY",
        },
        producing=["UNIT_SETTLER", "UNIT_BUILDER"],
        threat_level="RED",
        victory_type="SCIENCE",
    )
    rendered = policy.render_policy_advice(policy.check_policies(ctx))
    assert rendered.count("⚠️") + rendered.count("💡") <= 4


def test_no_advice_renders_empty():
    assert policy.render_policy_advice([]) == ""


# ===========================================================================
# L4 复盘自动化
# ===========================================================================


def test_debrief_records_and_summarises():
    log = debrief.DebriefLog(civ="rome", difficulty="DIFFICULTY_WARLORD")
    for t in range(1, 53):
        log.record(
            debrief.TurnFact(
                turn=t,
                cities=1,
                military=1,
                exploration=0.029,
                active_camps=0,
                threat_level="RED" if t > 14 else "GREEN",
                units_lost=["UNIT_BUILDER"] if t in (19, 27, 45) else [],
            )
        )
    s = log.summary()
    assert s["损失单位"] == 3
    assert s["最终探索度"] == "2.9%"
    assert "红色占比" in s


def test_debrief_contradiction_detection():
    """T28 那句"蛮族威胁已解除"必须被事实打脸。"""
    log = debrief.DebriefLog()
    for t in range(25, 40):
        log.record(debrief.TurnFact(turn=t, active_camps=1, threat_level="RED"))
    log.add_judgement(28, "蛮族威胁已解除,转入发展期")
    con = log.contradictions()
    assert con
    assert any("营地" in c or "红" in c for c in con)


def test_debrief_no_false_positive():
    """判断正确时不该报冲突 —— 误报会让机制失去信任。"""
    log = debrief.DebriefLog()
    for t in range(25, 40):
        log.record(debrief.TurnFact(turn=t, active_camps=0, threat_level="GREEN"))
    log.add_judgement(28, "蛮族威胁已解除")
    assert log.contradictions() == []


def test_debrief_lessons_from_the_real_failure():
    log = debrief.DebriefLog()
    for t in range(1, 53):
        log.record(
            debrief.TurnFact(
                turn=t,
                cities=1,
                exploration=0.029,
                active_camps=2,
                threat_level="RED" if t > 14 else "GREEN",
                units_lost=["UNIT_BUILDER"] if t in (19, 27, 45) else [],
            )
        )
    les = log.lessons()
    joined = " ".join(les)
    assert "探索" in joined
    assert "营地" in joined
    assert "座城" in joined


def test_debrief_markdown_renders():
    log = debrief.DebriefLog(
        civ="rome", difficulty="DIFFICULTY_WARLORD", victory_type="SCIENCE"
    )
    log.record(debrief.TurnFact(turn=1, cities=1))
    log.record(
        debrief.TurnFact(turn=52, cities=1, active_camps=2, units_lost=["UNIT_SETTLER"])
    )
    md = log.to_markdown()
    assert "# 对局复盘" in md
    assert "SCIENCE" in md
    assert "UNIT_SETTLER" in md


def test_debrief_roundtrip(tmp_path):
    log = debrief.DebriefLog(civ="rome", victory_type="SCIENCE")
    log.record(debrief.TurnFact(turn=5, cities=2, units_lost=["UNIT_BUILDER"]))
    log.add_judgement(5, "一切顺利")
    jp = tmp_path / "d.json"
    log.save(jp, tmp_path / "d.md")
    back = debrief.DebriefLog.load(jp)
    assert back.civ == "rome"
    assert back.facts[0].units_lost == ["UNIT_BUILDER"]
    assert back.judgements[0].text == "一切顺利"
    assert (tmp_path / "d.md").exists()


def test_debrief_record_is_idempotent_per_turn():
    """end_turn 可能重试,同回合重复写入不能产生重复行。"""
    log = debrief.DebriefLog()
    log.record(debrief.TurnFact(turn=5, cities=1))
    log.record(debrief.TurnFact(turn=5, cities=2))
    assert len(log.facts) == 1
    assert log.facts[0].cities == 2


def test_debrief_load_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    assert debrief.DebriefLog.load(p).facts == []
