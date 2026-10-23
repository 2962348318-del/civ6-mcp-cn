"""任务 N 总督策略 —— 单元测试。

核心约束:本机是 RULESET_STANDARD,游戏库里没有 Governors 表。
所以这些测试重点验证【没有总督系统时完全静默】和【不硬编码总督知识】。
"""

from __future__ import annotations

import json
from civ_mcp.resources import gamedata_path

from civ_mcp import governor_advisor as ga

_GAMEDATA = gamedata_path()


def _ctx(**kw):
    return ga.GovernorContext(**kw)


def _gov(gtype="GOVERNOR_THE_EDUCATOR", **kw):
    base = dict(
        governor_type=gtype,
        name="平伽拉",
        assigned_city_id=-1,
        assigned_city_name="",
        is_established=False,
        turns_to_establish=0,
        available_promotions=(),
    )
    base.update(kw)
    return ga.Governor(**base)


# --- 这条测试记录的是一个事实,不是一个断言偏好 ---


def test_standard_ruleset_has_no_governors_table():
    """总督是 Rise & Fall 的系统。本机规则集里根本没这张表 ——
    所以本模块绝不能硬编码总督类型或数值。"""
    tables = json.loads((_GAMEDATA / "_all.json").read_text(encoding="utf-8"))
    assert tables["ruleset"] == "RULESET_STANDARD"
    assert "Governors" not in tables["tables"]
    assert "GovernorPromotions" not in tables["tables"]


def test_no_governor_system_is_completely_silent():
    """实时返回全空 = 本局没有总督。一个字都不该输出。"""
    assert ga.get_governor_advice(_ctx()) == []
    assert ga.get_governor_advice(_ctx(victory_type="SCIENCE")) == []


# --- 点数闲置 ---


def test_unspent_points_are_flagged():
    out = ga.get_governor_advice(
        _ctx(points_available=2, available_types=("GOVERNOR_THE_EDUCATOR",))
    )
    assert any("没花" in line for line in out)
    assert any("appoint_governor" in line for line in out)


def test_unspent_points_without_options_still_warns():
    out = ga.get_governor_advice(_ctx(points_available=1))
    assert any("没花" in line for line in out)
    assert any("get_governors" in line for line in out)


# --- 按胜利路线排序 ---


def test_science_victory_prefers_educator():
    ranked = ga.rank_for_victory(
        ("GOVERNOR_THE_MERCHANT", "GOVERNOR_THE_EDUCATOR"), "SCIENCE"
    )
    assert ranked[0] == "GOVERNOR_THE_EDUCATOR"


def test_domination_prefers_defender():
    ranked = ga.rank_for_victory(
        ("GOVERNOR_THE_EDUCATOR", "GOVERNOR_THE_DEFENDER"), "DOMINATION"
    )
    assert ranked[0] == "GOVERNOR_THE_DEFENDER"


def test_unrecognised_governor_type_is_not_commented_on():
    """匹配不上的类型排在最后,且不编造职能描述。"""
    assert ga._role("GOVERNOR_SOMETHING_NEW") is None
    ranked = ga.rank_for_victory(
        ("GOVERNOR_SOMETHING_NEW", "GOVERNOR_THE_EDUCATOR"), "SCIENCE"
    )
    assert ranked == ["GOVERNOR_THE_EDUCATOR", "GOVERNOR_SOMETHING_NEW"]


def test_unknown_governor_gets_no_invented_role_text():
    out = ga.get_governor_advice(
        _ctx(points_available=1, available_types=("GOVERNOR_SOMETHING_NEW",))
    )
    line = next(x for x in out if "GOVERNOR_SOMETHING_NEW" in x)
    assert "(" not in line.split("GOVERNOR_SOMETHING_NEW")[1]


def test_no_victory_type_still_ranks_deterministically():
    types = ("GOVERNOR_THE_MERCHANT", "GOVERNOR_THE_EDUCATOR")
    assert ga.rank_for_victory(types, "") == list(types)


# --- 派驻状态 ---


def test_appointed_but_unassigned_is_flagged():
    out = ga.get_governor_advice(_ctx(appointed=[_gov()]))
    assert any("未派驻" in line and "assign_governor" in line for line in out)


def test_threatened_city_is_named_for_defensive_governor():
    out = ga.get_governor_advice(
        _ctx(
            appointed=[_gov("GOVERNOR_THE_DEFENDER", name="维克多")],
            threatened_cities=("安条克",),
        )
    )
    assert any("安条克" in line for line in out)


def test_establishing_governor_is_a_hint_not_a_warning():
    out = ga.get_governor_advice(
        _ctx(
            appointed=[
                _gov(
                    assigned_city_id=1, assigned_city_name="罗马", turns_to_establish=3
                )
            ]
        )
    )
    line = next(x for x in out if "就任中" in x)
    assert line.startswith("💡")
    assert "还需 3 回合" in line


def test_established_and_assigned_governor_is_silent():
    assert (
        ga.get_governor_advice(
            _ctx(
                appointed=[
                    _gov(
                        assigned_city_id=1,
                        assigned_city_name="罗马",
                        is_established=True,
                    )
                ]
            )
        )
        == []
    )


def test_available_promotion_with_points_is_surfaced():
    out = ga.get_governor_advice(
        _ctx(
            points_available=1,
            appointed=[
                _gov(
                    assigned_city_id=1,
                    assigned_city_name="罗马",
                    is_established=True,
                    available_promotions=("PROMOTION_X",),
                )
            ],
        )
    )
    assert any("promote_governor" in line for line in out)


def test_promotion_not_pushed_without_points():
    out = ga.get_governor_advice(
        _ctx(
            appointed=[
                _gov(
                    assigned_city_id=1,
                    assigned_city_name="罗马",
                    is_established=True,
                    available_promotions=("PROMOTION_X",),
                )
            ]
        )
    )
    assert out == []


def test_output_is_capped():
    out = ga.get_governor_advice(
        _ctx(
            points_available=1,
            available_types=("GOVERNOR_THE_EDUCATOR",),
            appointed=[_gov(name=f"总督{i}") for i in range(6)],
        ),
        max_items=3,
    )
    assert any("已省略" in line for line in out)
