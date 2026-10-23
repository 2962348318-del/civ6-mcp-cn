"""任务 AE 间谍后期运营 —— 单元测试。"""

from __future__ import annotations

from pathlib import Path

from civ_mcp import spy_strategy as ss


def _ctx(**kw):
    base = dict(
        turn=180,
        victory_type="SCIENCE",
        spies=[],
        rivals=[],
        my_districts={},
        counterspy_cities=(),
    )
    base.update(kw)
    return ss.SpyContext(**base)


def _spy(uid=1, **kw):
    base = dict(
        unit_id=uid,
        name="阿尔忒弥斯",
        rank=2,
        city_name="none",
        city_owner=-1,
        status="idle",
        is_escaping=False,
        current_mission="none",
        available_ops=(),
    )
    base.update(kw)
    return ss.Spy(**base)


def _rival(name="英国", pid=1, **kw):
    base = dict(
        player_id=pid,
        civ_name=name,
        techs_i_lack=(),
        threat_type="",
        capital_name="伦敦",
        is_at_war=False,
    )
    base.update(kw)
    return ss.RivalIntel(**base)


# --- 任务标识必须和 Lua 侧一致,否则调用必然失败 ---


def test_op_names_match_the_lua_hash_table():
    lua = (Path(ss.__file__).resolve().parents[0] / "lua" / "espionage.py").read_text(
        encoding="utf-8"
    )
    for op in (*ss.OFFENSIVE_OPS, ss.DEFENSIVE_OP):
        assert f'"{op}"' in lua, f"{op} 在 espionage.py 的 op 表里不存在"


# --- 数据来自 gamedata ---


def test_spy_promotions_loaded_from_unitpromotions():
    assert len(ss.SPY_PROMOTIONS) == 11
    assert ss.SPY_PROMOTIONS["PROMOTION_SPY_TECHNOLOGIST"] == "技术专家"


def test_every_mapped_promotion_actually_exists():
    for promo in ss._PROMOTION_FOR_OP.values():
        assert promo in ss.SPY_PROMOTIONS


def test_ambiguous_promotions_are_not_mapped():
    """名字看不出用途的晋升不许硬塞一个任务上去。"""
    mapped = set(ss._PROMOTION_FOR_OP.values())
    assert "PROMOTION_SPY_LINGUIST" not in mapped
    assert "PROMOTION_SPY_QUARTERMASTER" not in mapped
    assert "PROMOTION_SPY_ACE_DRIVER" not in mapped


def test_districts_loaded_and_named():
    assert ss.DISTRICTS["DISTRICT_SPACEPORT"] == "宇航中心"
    assert ss.DISTRICTS["DISTRICT_CAMPUS"] == "学院"


def test_tech_cost_comes_from_technologies_table():
    assert ss.tech_cost("TECH_POTTERY") == 25
    assert ss.tech_cn("TECH_POTTERY") == "制陶术"
    assert ss.tech_cost("BOGUS") == 0


# --- 偷科技排序 ---


def test_steal_targets_sorted_by_real_cost_desc():
    got = ss.rank_steal_targets(("TECH_POTTERY", "TECH_ROCKETRY", "TECH_WRITING"))
    assert got[0] == "TECH_ROCKETRY"
    assert ss.tech_cost(got[0]) > ss.tech_cost(got[-1])


def test_unknown_techs_are_dropped_not_ranked():
    assert ss.rank_steal_targets(("BOGUS_TECH",)) == []


def test_steal_targets_are_capped():
    assert len(ss.rank_steal_targets(tuple(ss.TECHS), top_n=3)) == 3


# --- 防御排序 ---


def test_spaceport_outranks_campus():
    ctx = _ctx(
        my_districts={"罗马": ["DISTRICT_CAMPUS"], "安条克": ["DISTRICT_SPACEPORT"]}
    )
    assert ss.defense_priority(ctx)[0][1] == "DISTRICT_SPACEPORT"


def test_victory_route_raises_its_own_district():
    """走文化路线时剧院广场应该压过工业区。"""
    districts = {"罗马": ["DISTRICT_INDUSTRIAL_ZONE", "DISTRICT_THEATER"]}
    assert (
        ss.defense_priority(_ctx(victory_type="CULTURE", my_districts=districts))[0][1]
        == "DISTRICT_THEATER"
    )
    assert (
        ss.defense_priority(_ctx(victory_type="SCIENCE", my_districts=districts))[0][1]
        == "DISTRICT_INDUSTRIAL_ZONE"
    )


def test_worthless_districts_are_not_defended():
    assert ss.defense_priority(_ctx(my_districts={"罗马": ["DISTRICT_AQUEDUCT"]})) == []


# --- 建议输出 ---


def test_escaping_spy_is_top_priority():
    out = ss.get_spy_advice(_ctx(spies=[_spy(is_escaping=True, city_name="伦敦")]))
    assert out[1].startswith("🔴")
    assert "逃脱" in out[1]


def test_science_rival_gets_sabotage_not_tech_steal():
    """太空竞赛卡的是产能 —— 破坏产能比偷科技更直接。"""
    out = ss.get_spy_advice(
        _ctx(spies=[_spy()], rivals=[_rival(threat_type="SCIENCE")])
    )
    assert any("SABOTAGE_PRODUCTION" in line for line in out)


def test_culture_rival_gets_great_work_heist():
    out = ss.get_spy_advice(
        _ctx(spies=[_spy()], rivals=[_rival("法国", threat_type="CULTURE")])
    )
    assert any("GREAT_WORK_HEIST" in line for line in out)


def test_promotion_hint_is_labelled_as_inferred():
    out = ss.get_spy_advice(
        _ctx(spies=[_spy()], rivals=[_rival(threat_type="SCIENCE")])
    )
    assert any("按晋升名推断" in line for line in out)


def test_op_not_available_to_this_spy_is_not_suggested():
    out = ss.get_spy_advice(
        _ctx(
            spies=[_spy(available_ops=("COUNTERSPY",))],
            rivals=[_rival(threat_type="SCIENCE")],
        )
    )
    assert not any("SABOTAGE_PRODUCTION" in line for line in out)


def test_steal_tech_list_shows_costs():
    out = ss.get_spy_advice(
        _ctx(
            spies=[_spy()],
            rivals=[_rival(techs_i_lack=("TECH_ROCKETRY", "TECH_POTTERY"))],
        )
    )
    line = next(x for x in out if "已研究但你没有" in x)
    assert "火箭研究(1410 科研)" in line  # 名字与成本都来自 Technologies.json
    assert line.index("火箭研究") < line.index("制陶术")  # 贵的排前面


def test_counterspy_suggested_for_undefended_high_value_district():
    out = ss.get_spy_advice(
        _ctx(spies=[_spy(1), _spy(2)], my_districts={"罗马": ["DISTRICT_SPACEPORT"]})
    )
    assert any("COUNTERSPY" in line and "罗马" in line for line in out)


def test_already_defended_city_is_not_suggested_again():
    out = ss.get_spy_advice(
        _ctx(
            spies=[_spy(1), _spy(2)],
            my_districts={"罗马": ["DISTRICT_SPACEPORT"]},
            counterspy_cities=("罗马",),
        )
    )
    assert not any("COUNTERSPY" in line for line in out)


def test_spy_on_a_mission_is_left_alone():
    assert (
        ss.get_spy_advice(
            _ctx(
                spies=[_spy(status="on_mission", current_mission="COUNTERSPY")],
                rivals=[_rival(threat_type="SCIENCE")],
            )
        )
        == []
    )


def test_rival_at_war_is_skipped():
    assert (
        ss.get_spy_advice(
            _ctx(spies=[_spy()], rivals=[_rival(threat_type="SCIENCE", is_at_war=True)])
        )
        == []
    )


def test_no_spies_is_silent():
    assert ss.get_spy_advice(_ctx(rivals=[_rival(threat_type="SCIENCE")])) == []


def test_output_is_capped():
    out = ss.get_spy_advice(
        _ctx(spies=[_spy(i, is_escaping=True) for i in range(6)]), max_items=3
    )
    assert any("已省略" in line for line in out)
