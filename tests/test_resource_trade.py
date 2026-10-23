"""任务 E 奢侈资源自动交易建议 —— 单元测试。"""

from __future__ import annotations

from civ_mcp import resource_trade as rt


def _ctx(**kw):
    base = dict(my_luxuries={}, rivals=[], city_count=4, amenity_shortfall=0)
    base.update(kw)
    return rt.ResourceTradeContext(**base)


def _rival(name="英国", pid=1, lux=(), risk="低", war=False):
    return rt.RivalHoldings(pid, name, tuple(lux), risk, war)


# --- 数据来自 Resources.json,不是记忆 ---


def test_luxuries_loaded_from_gamedata():
    assert rt.LUXURIES, "Resources.json 没读到 —— 整个模块会静默失效"
    assert "RESOURCE_SILK" in rt.LUXURIES
    assert rt.luxury_cn("RESOURCE_SILK") == "丝绸"


def test_happiness_column_is_cities_served():
    """绝大多数奢侈供 4 城;香水/肉桂/丁香供 6 —— 这是导出值不是记忆。"""
    assert rt.cities_served("RESOURCE_SILK") == 4
    assert rt.cities_served("RESOURCE_PERFUME") == 6
    assert rt.cities_served("RESOURCE_CINNAMON") == 6


def test_strategic_and_bonus_resources_are_not_luxuries():
    assert "RESOURCE_IRON" not in rt.LUXURIES
    assert "RESOURCE_STONE" not in rt.LUXURIES
    assert rt.normalize("RESOURCE_IRON") is None


def test_normalize_accepts_chinese_and_type_names():
    assert rt.normalize("丝绸") == "RESOURCE_SILK"
    assert rt.normalize("RESOURCE_SILK") == "RESOURCE_SILK"
    assert rt.normalize("Silk") == "RESOURCE_SILK"
    assert rt.normalize("") is None
    assert rt.normalize("不存在的东西") is None


# --- 冗余检测 ---


def test_duplicate_luxury_is_flagged_as_worthless():
    ctx = _ctx(
        my_luxuries={"RESOURCE_SILK": 2},
        rivals=[_rival("巴西", lux=("RESOURCE_DYES",))],
    )
    out = rt.get_resource_trade_advice(ctx)
    assert any("丝绸" in line and "零贡献" in line for line in out)


def test_single_copy_is_not_surplus():
    ctx = _ctx(my_luxuries={"RESOURCE_SILK": 1})
    assert rt.surplus_luxuries(ctx) == []


def test_buyer_list_excludes_civs_that_already_have_it():
    ctx = _ctx(
        my_luxuries={"RESOURCE_SILK": 2},
        rivals=[
            _rival("英国", lux=("RESOURCE_SILK",)),
            _rival("巴西", 2, lux=("RESOURCE_DYES",)),
        ],
    )
    line = next(x for x in rt.get_resource_trade_advice(ctx) if "丝绸" in x)
    assert "巴西" in line
    assert "英国" not in line


def test_high_war_risk_buyer_is_warned_about():
    ctx = _ctx(
        my_luxuries={"RESOURCE_SILK": 2},
        rivals=[_rival("阿兹特克", lux=("RESOURCE_DYES",), risk="高")],
    )
    out = rt.get_resource_trade_advice(ctx)
    assert any("高宣战风险" in line for line in out)


# --- 缺口检测 ---


def test_missing_luxury_is_suggested_for_purchase():
    ctx = _ctx(
        my_luxuries={"RESOURCE_SILK": 1}, rivals=[_rival("埃及", lux=("RESOURCE_TEA",))]
    )
    out = rt.get_resource_trade_advice(ctx)
    assert any("茶叶" in line and "埃及" in line for line in out)


def test_luxury_i_already_own_is_not_suggested():
    ctx = _ctx(
        my_luxuries={"RESOURCE_TEA": 1}, rivals=[_rival("埃及", lux=("RESOURCE_TEA",))]
    )
    assert rt.missing_luxuries(ctx) == {}


def test_civs_at_war_are_skipped():
    ctx = _ctx(rivals=[_rival("埃及", lux=("RESOURCE_TEA",), war=True)])
    assert rt.missing_luxuries(ctx) == {}
    assert rt.get_resource_trade_advice(ctx) == []


def test_amenity_shortfall_is_surfaced():
    ctx = _ctx(
        my_luxuries={"RESOURCE_SILK": 1},
        amenity_shortfall=3,
        rivals=[_rival("埃及", lux=("RESOURCE_TEA",))],
    )
    assert any("缺 3 点宜居度" in line for line in rt.get_resource_trade_advice(ctx))


# --- 静默与诚实 ---


def test_nothing_to_trade_is_silent():
    assert rt.get_resource_trade_advice(_ctx()) == []
    assert rt.get_resource_trade_advice(_ctx(my_luxuries={"RESOURCE_SILK": 1})) == []


def test_never_predicts_a_price():
    """AI 估价受关系/议程/金库影响,预测价格必然是编的。"""
    ctx = _ctx(
        my_luxuries={"RESOURCE_SILK": 2},
        rivals=[_rival("巴西", lux=("RESOURCE_DYES",))],
    )
    out = rt.get_resource_trade_advice(ctx)
    assert not any("GPT" in line or "金/回合" in line for line in out)
    assert any('mode="test"' in line for line in out)


def test_advice_is_capped():
    ctx = _ctx(
        my_luxuries={r: 2 for r in list(rt.LUXURIES)[:8]},
        rivals=[_rival("巴西", lux=tuple(list(rt.LUXURIES)[8:12]))],
    )
    out = rt.get_resource_trade_advice(ctx, max_items=4)
    assert sum(1 for line in out if line.startswith("· ")) == 4
