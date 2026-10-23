"""lategame.py 的三个扩展函数 —— 任务 Q(伟人博弈)/ AA(太空竞赛收尾)/
AF(外交 DVP 追踪)的单元测试。

原有的 lategame 测试在 test_lategame_and_params.py,那边一行没动 ——
这里只测新追加的三个函数。
"""

from __future__ import annotations

import json
from civ_mcp.resources import gamedata_path

from civ_mcp import lategame as lg

_GAMEDATA = gamedata_path()


def _tables():
    return json.loads((_GAMEDATA / "_all.json").read_text(encoding="utf-8"))["tables"]


# ===========================================================================
# 不回归:原有 API 一个都不能少、不能变
# ===========================================================================


def test_existing_api_untouched():
    for name in (
        "check_lategame",
        "district_hint",
        "victory_playbook",
        "render_lategame",
        "LateGameContext",
        "LateGameAdvice",
        "VICTORY_PATHS",
        "SCIENCE_CHAIN",
        "SCIENCE_TOTAL_COST",
    ):
        assert hasattr(lg, name), f"原有 API {name} 丢失"
    assert lg.SCIENCE_TOTAL_COST == 7800


def test_science_chain_still_matches_projects_json():
    """新函数依赖 SCIENCE_CHAIN,先确认它和导出表仍然对得上。"""
    projects = {r["ProjectType"]: r for r in _tables()["Projects"]["rows"]}
    for m in lg.SCIENCE_CHAIN:
        assert m.key in projects, f"{m.key} 不在 Projects.json 里"
        assert int(projects[m.key]["Cost"]) == m.cost
        assert projects[m.key]["PrereqTech"] == m.prereq_tech
        assert projects[m.key]["PrereqDistrict"] == m.prereq_district


# ===========================================================================
# 任务 Q:伟人博弈
# ===========================================================================


def _gp_ctx(**kw):
    base = dict(victory_type="SCIENCE", gold=0, faith=0, offers=[])
    base.update(kw)
    return lg.GreatPersonContext(**base)


def _offer(**kw):
    return lg.GreatPersonOffer(**kw)


def test_great_person_tables_loaded_from_gamedata():
    assert len(lg.GP_INDIVIDUALS) == 177
    assert len(lg.GP_CLASSES) == 9
    assert lg.gp_class_cn("GREAT_PERSON_CLASS_SCIENTIST") == "大科学家"


def test_lookup_accepts_type_or_chinese_name():
    """计划书点名的希帕蒂娅 —— 名字必须从导出表校验,不是记忆。"""
    by_type = lg.gp_lookup("GREAT_PERSON_INDIVIDUAL_HYPATIA")
    by_name = lg.gp_lookup("希帕蒂娅")
    assert by_type == by_name
    assert by_type["GreatPersonClassType"] == "GREAT_PERSON_CLASS_SCIENTIST"
    assert by_type["EraType"] == "ERA_CLASSICAL"


def test_lookup_rejects_invented_names():
    assert lg.gp_lookup("爱因斯坦二世") is None
    assert lg.gp_lookup("") is None


def test_unknown_great_person_is_dropped_not_scored():
    """名字对不上导出表的一律不评分 —— 宁可少说,不能编。"""
    assert (
        lg.evaluate_great_person(_gp_ctx(offers=[_offer(individual_name="虚构伟人")]))
        == []
    )


def test_class_fit_is_route_dependent():
    assert lg.gp_fit_score("GREAT_PERSON_CLASS_SCIENTIST", "SCIENCE") == 3
    assert lg.gp_fit_score("GREAT_PERSON_CLASS_SCIENTIST", "RELIGION") == 0
    assert lg.gp_fit_score("GREAT_PERSON_CLASS_GENERAL", "DOMINATION") == 3
    assert lg.gp_fit_score("GREAT_PERSON_CLASS_PROPHET", "RELIGION") == 3
    assert lg.gp_fit_score("BOGUS_CLASS", "SCIENCE") == 0


def test_affordable_high_fit_gets_patronize():
    """计划书原始场景:希帕蒂娅对科学胜利价值极高且金币够 → 买断。"""
    out = lg.evaluate_great_person(
        _gp_ctx(gold=800, offers=[_offer(individual_name="希帕蒂娅", gold_cost=600)])
    )
    assert f"[{lg.GP_PATRONIZE}]" in out[1]
    assert "零和" in out[1]


def test_enough_points_prefers_recruit_over_paying():
    out = lg.evaluate_great_person(
        _gp_ctx(
            gold=800,
            offers=[
                _offer(individual_name="希帕蒂娅", gold_cost=600, can_recruit=True)
            ],
        )
    )
    assert f"[{lg.GP_RECRUIT}]" in out[1]
    assert "不必花钱" in out[1]


def test_faith_patronage_is_used_when_gold_is_short():
    out = lg.evaluate_great_person(
        _gp_ctx(
            gold=10,
            faith=900,
            offers=[_offer(individual_name="希帕蒂娅", gold_cost=600, faith_cost=800)],
        )
    )
    assert f"[{lg.GP_PATRONIZE}]" in out[1]
    assert "信仰够" in out[1]


def test_unaffordable_high_fit_states_the_shortfall():
    out = lg.evaluate_great_person(
        _gp_ctx(gold=100, offers=[_offer(individual_name="希帕蒂娅", gold_cost=600)])
    )
    assert "金币差 500" in out[1]


def test_off_route_great_person_is_skipped():
    out = lg.evaluate_great_person(
        _gp_ctx(offers=[_offer(individual_name="阿尔特米西亚")])
    )  # 海军统帅
    assert f"[{lg.GP_SKIP}]" in out[1]


def test_ability_text_is_quoted_never_invented():
    """个体效果不在导出数据里 —— 有实时原文就引用,没有就明说查不到。"""
    with_ability = lg.evaluate_great_person(
        _gp_ctx(offers=[_offer(individual_name="希帕蒂娅", ability="学院 +1 科技")])
    )
    assert "效果:学院 +1 科技" in with_ability[1]

    without = lg.evaluate_great_person(
        _gp_ctx(offers=[_offer(individual_name="希帕蒂娅")])
    )
    assert "效果不在导出数据里" in without[1]


def test_claimed_great_person_is_flagged():
    out = lg.evaluate_great_person(
        _gp_ctx(offers=[_offer(individual_name="希帕蒂娅", claimant="英国")])
    )
    assert "已被 英国 预订" in out[1]


def test_rival_points_leader_is_flagged():
    out = lg.evaluate_great_person(
        _gp_ctx(offers=[_offer(individual_name="希帕蒂娅", rival_points_leader="法国")])
    )
    assert "法国" in out[1] and "再拖就没了" in out[1]


def test_offers_sorted_by_fit_and_capped():
    offers = [
        _offer(individual_name="阿尔特米西亚"),  # 海军统帅 fit 0
        _offer(individual_name="希帕蒂娅"),  # 大科学家 fit 3
        _offer(individual_name="谢尔盖·科罗廖夫"),  # 大工程师 fit 3
        _offer(individual_name="盖尤斯·杜伊路斯"),  # 海军统帅 fit 0
    ]
    out = lg.evaluate_great_person(_gp_ctx(offers=offers), max_items=2)
    assert "希帕蒂娅" in out[1] or "科罗廖夫" in out[1]
    assert "阿尔特米西亚" not in "\n".join(out)
    assert any("未列出" in line for line in out)


def test_no_offers_is_silent():
    assert lg.evaluate_great_person(_gp_ctx()) == []


# ===========================================================================
# 任务 AA:科学胜利收尾
# ===========================================================================


def _sci_ctx(**kw):
    base = dict(turn=200)
    base.update(kw)
    return lg.ScienceEndgameContext(**base)


_ALL_SPACE_TECHS = {
    "TECH_ROCKETRY",
    "TECH_SATELLITES",
    "TECH_NUCLEAR_FUSION",
    "TECH_ROBOTICS",
    "TECH_NANOTECHNOLOGY",
}


def test_royal_society_does_not_exist_in_this_ruleset():
    """计划书要求检查皇家学会。数据说这个建筑在原版规则集里不存在。"""
    assert lg.RULESET == "RULESET_STANDARD"
    assert lg.ROYAL_SOCIETY_AVAILABLE is False
    buildings = {r["BuildingType"] for r in _tables()["Buildings"]["rows"]}
    assert lg.ROYAL_SOCIETY_BUILDING not in buildings


def test_idle_builders_are_told_royal_society_is_not_available():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY"},
            spaceport_cities=("罗马",),
            counterspy_spaceport_cities=("罗马",),
            idle_builders=3,
        )
    )
    assert any("没有皇家学会" in line for line in out)
    assert any("Gathering Storm" in line for line in out)


def test_space_projects_are_all_single_instance():
    """并行建议的前提:同一个项目不能重复建。这是导出值。"""
    projects = {r["ProjectType"]: r for r in _tables()["Projects"]["rows"]}
    for m in lg.SCIENCE_CHAIN:
        assert projects[m.key]["MaxPlayerInstances"] == "1"


def test_parallel_spaceports_are_recommended():
    out = lg.science_endgame(
        _sci_ctx(
            techs=_ALL_SPACE_TECHS,
            completed_projects={"PROJECT_LAUNCH_EARTH_SATELLITE"},
            spaceport_cities=("罗马", "安条克"),
            spaceports_running_project=1,
            counterspy_spaceport_cities=("罗马", "安条克"),
            aluminum_stock=5,
            total_production=200,
        )
    )
    line = next(x for x in out if x.startswith("⚡"))
    assert "还能再开 1 条线" in line
    assert "MaxPlayerInstances=1" in line


def test_single_spaceport_gets_a_second_port_hint_with_real_cost():
    out = lg.science_endgame(
        _sci_ctx(
            techs=_ALL_SPACE_TECHS,
            spaceport_cities=("罗马",),
            counterspy_spaceport_cities=("罗马",),
            aluminum_stock=5,
        )
    )
    assert lg.DISTRICT_SPACEPORT_COST == 1800
    assert any("1800" in line for line in out)


def test_no_parallel_advice_when_only_one_project_is_runnable():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY"},
            spaceport_cities=("罗马", "安条克"),
            counterspy_spaceport_cities=("罗马", "安条克"),
            aluminum_stock=5,
        )
    )
    assert not any(line.startswith("⚡") for line in out)


def test_aluminum_reveal_tech_comes_from_resources_json():
    assert lg.ALUMINUM_REVEAL_TECH == "TECH_RADIO"
    assert lg.ALUMINUM_CN == "铝"


def test_missing_radio_blocks_aluminum_visibility():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY", "TECH_NUCLEAR_FUSION"},
            spaceport_cities=("罗马",),
            counterspy_spaceport_cities=("罗马",),
        )
    )
    assert any("TECH_RADIO" in line and "看不见" in line for line in out)


def test_zero_aluminum_with_radio_researched_is_a_stock_warning():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY", "TECH_RADIO", "TECH_NUCLEAR_FUSION"},
            spaceport_cities=("罗马",),
            counterspy_spaceport_cities=("罗马",),
            aluminum_stock=0,
        )
    )
    line = next(x for x in out if "库存为 0" in x)
    assert "资源需求列不在导出表里" in line  # 不许承诺"需要 N 个铝"


def test_aluminum_quiet_when_stocked():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY", "TECH_RADIO", "TECH_NUCLEAR_FUSION"},
            spaceport_cities=("罗马",),
            counterspy_spaceport_cities=("罗马",),
            aluminum_stock=8,
        )
    )
    assert not any("铝" in line for line in out)


def test_undefended_spaceport_is_flagged():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY"},
            spaceport_cities=("罗马", "安条克"),
            counterspy_spaceport_cities=("罗马",),
            aluminum_stock=5,
        )
    )
    line = next(x for x in out if "反间谍" in x)
    assert "安条克" in line and "罗马" not in line


def test_space_race_great_people_names_are_validated():
    assert lg.SPACE_RACE_GREAT_PEOPLE == (
        "卡尔·萨根",
        "谢尔盖·科罗廖夫",
        "沃纳·冯·布劳恩",
    )
    individuals = {r["Name"] for r in _tables()["GreatPersonIndividuals"]["rows"]}
    for name in lg.SPACE_RACE_GREAT_PEOPLE:
        assert name in individuals


def test_available_space_great_person_is_surfaced_without_numbers():
    out = lg.science_endgame(
        _sci_ctx(
            techs={"TECH_ROCKETRY"},
            spaceport_cities=("罗马",),
            counterspy_spaceport_cities=("罗马",),
            aluminum_stock=5,
            available_great_people=("卡尔·萨根", "希帕蒂娅"),
        )
    )
    line = next(x for x in out if "关键伟人" in x)
    assert "卡尔·萨根" in line
    assert "希帕蒂娅" not in line
    assert "不在导出数据里" in line  # 不许报 +3000 锤这类数字


def test_completed_chain_is_silent():
    assert (
        lg.science_endgame(
            _sci_ctx(
                techs=_ALL_SPACE_TECHS,
                completed_projects={m.key for m in lg.SCIENCE_CHAIN},
                spaceport_cities=("罗马",),
            )
        )
        == []
    )


def test_empty_context_is_silent():
    assert lg.science_endgame(_sci_ctx()) == []


def test_science_endgame_output_is_capped():
    out = lg.science_endgame(
        _sci_ctx(
            techs=_ALL_SPACE_TECHS,
            spaceport_cities=("罗马", "安条克", "库马"),
            spaceports_running_project=0,
            aluminum_stock=0,
            available_great_people=("卡尔·萨根",),
            idle_builders=2,
            total_production=200,
        ),
        max_items=2,
    )
    assert any("已省略" in line for line in out)


# ===========================================================================
# 任务 AF:外交胜利 DVP 追踪
# ===========================================================================


def test_diplomatic_victory_is_absent_from_this_ruleset():
    """这条测试记录的是一个事实:原版规则集没有外交胜利。
    计划书任务 AF 的四个 DVP 来源在本局全部不存在。"""
    victories = {r["VictoryType"] for r in _tables()["Victories"]["rows"]}
    assert victories == {
        "VICTORY_SCORE",
        "VICTORY_DEFAULT",
        "VICTORY_CONQUEST",
        "VICTORY_CULTURE",
        "VICTORY_RELIGIOUS",
        "VICTORY_TECHNOLOGY",
    }
    assert lg.DIPLOMATIC_VICTORY_AVAILABLE is False
    assert lg.AVAILABLE_VICTORY_TYPES == victories


def test_none_of_the_planned_dvp_sources_exist():
    blob = json.dumps(_tables(), ensure_ascii=False).upper()
    for token in (
        "VICTORY_DIPLOMATIC",
        "WORLD_CONGRESS",
        "STATUE_OF_LIBERTY",
        "CARBON",
        "SEASTEAD",
    ):
        assert token not in blob, f"{token} 居然存在 —— 结论要重新核"


def test_availability_note_explains_the_silence():
    note = lg.dvp_availability_note()
    assert "RULESET_STANDARD" in note
    assert "VICTORY_DIPLOMATIC" in note


def test_silent_when_ruleset_lacks_diplomatic_victory():
    """本规则集下不许凭空生成一整套无法执行的建议。"""
    assert lg.track_dvp_progress(lg.DVPContext(turn=200)) == []
    assert lg.track_dvp_progress(lg.DVPContext()) == []


def test_activates_when_caller_supplies_real_dvp_data():
    """换到有外交胜利的规则集后,算术部分不用改一行就能用。"""
    out = lg.track_dvp_progress(lg.DVPContext(turn=200, current_dvp=12))
    assert out and "12/20 DVP" in out[0]


def test_progress_breaks_down_by_source():
    out = lg.track_dvp_progress(
        lg.DVPContext(
            turn=200,
            current_dvp=12,
            wonder_dvp=4,
            tech_dvp=1,
            congress_dvp=6,
            competition_dvp=1,
        )
    )
    assert "奇观 4" in out[0] and "世界议会决议 6" in out[0]
    assert "还差 8" in out[0]


def test_eta_is_computed_from_wonder_and_congress_cadence():
    out = lg.track_dvp_progress(
        lg.DVPContext(
            turn=200,
            current_dvp=12,
            wonder_turns_left=6,
            wonder_name="自由女神像",
            wonder_dvp_value=4,
            turns_between_congress=30,
            dvp_per_congress=2,
        )
    )
    line = next(x for x in out if x.startswith("⏳"))
    assert "T260" in line  # 200 + 2 届 × 30
    assert "自由女神像" in line


def test_no_pipeline_says_so_instead_of_faking_an_eta():
    out = lg.track_dvp_progress(lg.DVPContext(turn=200, current_dvp=5))
    assert any("没有任何在途来源" in line for line in out)


def test_target_reached_is_reported():
    out = lg.track_dvp_progress(lg.DVPContext(turn=200, current_dvp=20))
    assert "已达标" in out[0]
    assert not any(x.startswith("⏳") for x in out)


def test_custom_target_is_honoured():
    out = lg.track_dvp_progress(lg.DVPContext(turn=200, current_dvp=6, target_dvp=10))
    assert "6/10 DVP" in out[0]


def test_zero_target_falls_back_to_default():
    out = lg.track_dvp_progress(lg.DVPContext(turn=200, current_dvp=6, target_dvp=0))
    assert f"6/{lg.DEFAULT_DVP_TARGET} DVP" in out[0]


def test_dvp_stripping_resolution_is_top_severity():
    out = lg.track_dvp_progress(
        lg.DVPContext(
            turn=200, current_dvp=12, dvp_stripping_resolution=True, my_votes=5
        )
    )
    assert any(line.startswith("🔴") for line in out)


def test_rival_lead_is_flagged():
    out = lg.track_dvp_progress(
        lg.DVPContext(
            turn=200, current_dvp=12, rival_max_dvp=17, rival_max_dvp_name="西班牙"
        )
    )
    assert any("西班牙" in line and "领先你 5" in line for line in out)


def test_rival_behind_is_not_flagged():
    out = lg.track_dvp_progress(
        lg.DVPContext(
            turn=200, current_dvp=12, rival_max_dvp=3, rival_max_dvp_name="西班牙"
        )
    )
    assert not any("西班牙" in line for line in out)


def test_dvp_output_is_capped():
    out = lg.track_dvp_progress(
        lg.DVPContext(
            turn=200,
            current_dvp=12,
            wonder_dvp=4,
            congress_dvp=8,
            wonder_turns_left=6,
            wonder_dvp_value=4,
            turns_between_congress=30,
            dvp_per_congress=2,
            dvp_stripping_resolution=True,
            my_votes=5,
            rival_max_dvp=14,
            rival_max_dvp_name="西班牙",
        ),
        max_items=2,
    )
    assert any("已省略" in line for line in out)


# ===========================================================================
# 三个函数互相独立,且都不抛异常
# ===========================================================================


def test_all_three_return_lists_of_str():
    for result in (
        lg.evaluate_great_person(_gp_ctx()),
        lg.science_endgame(_sci_ctx()),
        lg.track_dvp_progress(lg.DVPContext()),
    ):
        assert isinstance(result, list)
        assert all(isinstance(x, str) for x in result)


def test_garbage_input_does_not_raise():
    assert (
        lg.evaluate_great_person(
            _gp_ctx(victory_type="BOGUS", offers=[_offer(individual_name="")])
        )
        == []
    )
    assert (
        lg.science_endgame(
            _sci_ctx(techs={"BOGUS"}, spaceport_cities=(), completed_projects={"BOGUS"})
        )
        == []
    )
    assert (
        lg.track_dvp_progress(lg.DVPContext(current_dvp=-5, target_dvp=-1)) is not None
    )
