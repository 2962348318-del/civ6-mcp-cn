"""任务 O 城邦策略 —— 单元测试。"""

from __future__ import annotations

from pathlib import Path

from civ_mcp import city_state_advisor as csa


def _ctx(**kw):
    base = dict(tokens_available=0, city_states=[], victory_type="", my_player_id=0)
    base.update(kw)
    return csa.CityStateContext(**base)


def _cs(name="布鲁塞尔", pid=10, cstype="Industrial", **kw):
    base = dict(
        player_id=pid,
        name=name,
        city_state_type=cstype,
        envoys_sent=0,
        suzerain_id=-1,
        suzerain_name="None",
        can_send_envoy=True,
        suzerain_envoys=0,
        has_quest=False,
        quest_text="",
    )
    base.update(kw)
    return csa.CityState(**base)


# --- 类型名必须和 Lua 侧的 csTypeMap 一致,不然全盘对不上 ---


def test_city_state_types_match_lua_query():
    lua = (Path(csa.__file__).resolve().parents[0] / "lua" / "governance.py").read_text(
        encoding="utf-8"
    )
    for cs_type in csa.CITY_STATE_YIELD:
        assert f'"{cs_type}"' in lua, f"{cs_type} 在 Lua 查询里不存在"


def test_all_six_types_are_covered_by_every_victory_priority():
    for vt in ("SCIENCE", "CULTURE", "RELIGION", "DOMINATION", ""):
        assert set(csa.priority_order(vt)) == set(csa.CITY_STATE_YIELD)


# --- 优先级 ---


def test_science_victory_ranks_scientific_first():
    assert csa.priority_order("SCIENCE")[0] == "Scientific"
    assert csa.type_rank("Scientific", "SCIENCE") < csa.type_rank(
        "Religious", "SCIENCE"
    )


def test_domination_ranks_militaristic_first():
    assert csa.priority_order("DOMINATION")[0] == "Militaristic"


def test_unknown_type_ranks_last():
    assert csa.type_rank("Bogus", "SCIENCE") == len(csa.priority_order("SCIENCE"))


# --- 档位算术 ---


def test_envoy_tiers_are_1_3_6():
    assert csa.ENVOY_TIERS == (1, 3, 6)
    assert csa.next_tier(0) == 1
    assert csa.next_tier(1) == 3
    assert csa.next_tier(3) == 6
    assert csa.next_tier(6) is None
    assert csa.next_tier(9) is None


# --- 宗主争夺算术 ---


def test_no_suzerain_needs_three_envoys():
    assert csa.envoys_to_suzerain(_cs(), my_id=0) == 3
    assert csa.envoys_to_suzerain(_cs(envoys_sent=2), my_id=0) == 1


def test_already_suzerain_needs_nothing():
    assert (
        csa.envoys_to_suzerain(
            _cs(suzerain_id=0, suzerain_name="罗马", envoys_sent=4), my_id=0
        )
        == 0
    )


def test_must_exceed_incumbent_not_tie():
    """平手时在位者保留宗主权 —— 必须多投一个。"""
    cs = _cs(suzerain_id=1, suzerain_name="英国", suzerain_envoys=4, envoys_sent=4)
    assert csa.envoys_to_suzerain(cs, my_id=0) == 1


def test_unknown_incumbent_count_is_not_guessed():
    """不知道在位者投了几个就返回 None,不能瞎猜一个数。"""
    cs = _cs(suzerain_id=1, suzerain_name="英国", suzerain_envoys=0, envoys_sent=1)
    assert csa.envoys_to_suzerain(cs, my_id=0) is None


# --- 建议输出 ---


def test_quest_is_surfaced_as_free_envoy():
    out = csa.get_city_state_advice(
        _ctx(city_states=[_cs(has_quest=True, quest_text="清除一个蛮族营地")])
    )
    assert any("免费使者" in line and "蛮族营地" in line for line in out)


def test_idle_envoys_are_directed_to_the_priority_type():
    out = csa.get_city_state_advice(
        _ctx(
            tokens_available=3,
            victory_type="SCIENCE",
            city_states=[
                _cs("日内瓦", 10, "Scientific"),
                _cs("布鲁塞尔", 11, "Industrial"),
            ],
        )
    )
    text = "\n".join(out)
    assert "日内瓦" in text
    assert "集中投到跨档" in text


def test_suzerain_contest_is_computed():
    out = csa.get_city_state_advice(
        _ctx(
            tokens_available=2,
            city_states=[
                _cs(
                    "日内瓦",
                    10,
                    "Scientific",
                    envoys_sent=2,
                    suzerain_id=1,
                    suzerain_name="英国",
                    suzerain_envoys=3,
                )
            ],
        )
    )
    assert any("英国" in line and "再投 2 个可夺过来" in line for line in out)


def test_far_out_of_reach_suzerain_is_not_mentioned():
    out = csa.get_city_state_advice(
        _ctx(
            tokens_available=0,
            city_states=[
                _cs(
                    "日内瓦",
                    10,
                    "Scientific",
                    envoys_sent=0,
                    suzerain_id=1,
                    suzerain_name="英国",
                    suzerain_envoys=6,
                )
            ],
        )
    )
    assert out == []


def test_header_shows_priority_for_locked_victory():
    out = csa.get_city_state_advice(
        _ctx(
            tokens_available=1,
            victory_type="CULTURE",
            city_states=[_cs("那不勒斯", 10, "Cultural")],
        )
    )
    assert "Cultural" in out[0]


def test_no_city_states_is_silent():
    assert csa.get_city_state_advice(_ctx()) == []


def test_maxed_city_state_with_no_action_is_silent():
    out = csa.get_city_state_advice(
        _ctx(city_states=[_cs(envoys_sent=6, suzerain_id=0, suzerain_name="罗马")])
    )
    assert out == []


def test_output_is_capped():
    states = [
        _cs(f"城邦{i}", 10 + i, "Scientific", has_quest=True, quest_text="任务")
        for i in range(8)
    ]
    out = csa.get_city_state_advice(
        _ctx(city_states=states, tokens_available=3), max_items=3
    )
    assert any("已省略" in line for line in out)


def test_suzerain_bonuses_are_out_of_scope():
    """具体宗主加成是任务 W,本模块不许承诺任何加成数值。"""
    out = csa.get_city_state_advice(
        _ctx(tokens_available=3, city_states=[_cs("日内瓦", 10, "Scientific")])
    )
    text = "\n".join(out)
    assert "%" not in text
