"""任务 T 贸易路线精确优化 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import trade_optimizer as to


def _ctx(**kw):
    base = dict(idle_traders=1, origin_city="罗马", destinations=[], victory_type="")
    base.update(kw)
    return to.TradeContext(**base)


# --- 产出串解析:格式由 lua/economy.py 定义,解析错了后面全是错的 ---


def test_parse_yields_matches_economy_format():
    assert to.parse_yields("Food:3 Prod:2 Gold:4") == {
        "food": 3.0,
        "prod": 2.0,
        "gold": 4.0,
    }


def test_parse_yields_handles_floats_and_all_six_keys():
    got = to.parse_yields("Food:1.5 Prod:2 Gold:3 Sci:4 Cul:5 Faith:6")
    assert got["food"] == 1.5
    assert set(got) == {"food", "prod", "gold", "science", "culture", "faith"}


def test_parse_yields_survives_garbage():
    assert to.parse_yields("") == {}
    assert to.parse_yields("完全不是产出串") == {}
    assert to.parse_yields("Bogus:9") == {}


# --- 排序 ---


def test_science_victory_prefers_science_route():
    sci = to.Destination("巴比伦", "苏美尔", origin_yields="Sci:4")
    gold = to.Destination("伦敦", "英国", origin_yields="Gold:5")
    out = to.get_trade_advice(_ctx(destinations=[gold, sci], victory_type="SCIENCE"))
    assert "巴比伦" in out[1]


def test_domination_prefers_production_route():
    prod = to.Destination("南特", is_domestic=True, origin_yields="Prod:4")
    gold = to.Destination("伦敦", "英国", origin_yields="Gold:5")
    out = to.get_trade_advice(
        _ctx(destinations=[gold, prod], victory_type="DOMINATION")
    )
    assert "南特" in out[1]


def test_city_state_quest_is_surfaced():
    cs = to.Destination(
        "布鲁塞尔",
        "布鲁塞尔",
        is_city_state=True,
        has_quest=True,
        origin_yields="Gold:3",
    )
    out = to.get_trade_advice(_ctx(destinations=[cs]))
    assert any("使者" in line for line in out)
    assert any("城邦" in line for line in out)


def test_domestic_route_mentions_road():
    d = to.Destination(
        "南特",
        is_domestic=True,
        origin_yields="Food:3 Prod:2",
        dest_yields="Food:1 Prod:1",
    )
    out = to.get_trade_advice(_ctx(destinations=[d]))
    assert any("修路" in line for line in out)
    assert any("目的地另得" in line for line in out)


def test_hostile_owner_is_flagged_and_penalised():
    hostile = to.Destination("伦敦", "英国", origin_yields="Gold:9")
    safe = to.Destination("南特", is_domestic=True, origin_yields="Prod:3 Food:3")
    ctx = _ctx(destinations=[hostile, safe], hostile_civs=("英国",))
    out = to.get_trade_advice(ctx)
    assert any("潜在敌人" in line for line in out)
    assert "南特" in out[1]  # 打折后国内路线应排到第一


def test_religion_route_mentions_pressure():
    d = to.Destination(
        "特诺奇提特兰",
        "阿兹特克",
        origin_yields="Faith:2",
        pressure_out=8.0,
        religion_out="天主教",
    )
    out = to.get_trade_advice(_ctx(destinations=[d], victory_type="RELIGION"))
    assert any("宗教压力" in line for line in out)


def test_no_idle_trader_is_silent():
    d = to.Destination("南特", is_domestic=True, origin_yields="Food:3")
    assert to.get_trade_advice(_ctx(idle_traders=0, destinations=[d])) == []


def test_no_destinations_is_silent():
    assert to.get_trade_advice(_ctx()) == []


def test_zero_value_destinations_are_silent():
    """全是 0 产出的目的地不该硬凑出建议。"""
    d = to.Destination("废城", "英国", origin_yields="")
    assert to.get_trade_advice(_ctx(destinations=[d])) == []


def test_advice_is_capped_to_top_n():
    dests = [
        to.Destination(f"城{i}", "英国", origin_yields=f"Gold:{i + 1}")
        for i in range(8)
    ]
    out = to.get_trade_advice(_ctx(destinations=dests), top_n=3)
    assert sum(1 for line in out if line.startswith("· ")) == 3
