"""缺陷 103:「断生产」门禁从写出来就没触发过一次。

2026-08-11 罗马局 T294-T301。连续七个回合有 4 到 6 座城空转,end_turn 的
回合完成度门禁一声不吭,直到用户第三次点名「生产别停」。

判据只差一个拼写:

    building.upper() in ("", "NONE")     # 门禁(end_turn.py)
    c.currently_building not in ("NONE", "nothing")   # 渲染层(narrate.py)

空转城市的实际值是 "nothing","nothing".upper() 是 "NOTHING",和 "NONE"
永不相等。同一个字段、同一个含义,渲染层判对了,门禁判错了。

这道门本身是用户前两次点名「生产断了」换来的,注释里写着"修复成本只是
一次 set_city_production,所以列阻断而非提醒"——立意完全正确,只是那个
阻断从来没发生过。用户不得不第三次说同一件事。

教训和缺陷 99(建筑前缀)、101(份数后缀)是同一族:同一个值在两处用两种
写法表示,只在一处做了归一。区别在于这一个的代价是【一道安全门静默失效】,
而不是一条建议出错——门禁失效不会有任何症状,直到你回头去数损失。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from civ_mcp.end_turn import check_turn_completion


@dataclass
class FakeCity:
    name: str
    currently_building: str
    x: int = 10
    y: int = 10


def _blockers(*cities) -> str:
    blockers, _ = check_turn_completion([], [], list(cities))
    return " ".join(blockers)


class TestIdleDetectionCoversEverySpelling:
    def test_lowercase_nothing_is_caught(self):
        """实机 Lua 侧发的就是这个值 —— 旧判据漏的正是它。"""
        out = _blockers(FakeCity("奥斯蒂亚", "nothing"))
        assert "奥斯蒂亚" in out
        assert "没有在生产" in out

    def test_uppercase_none_is_caught(self):
        assert "罗马" in _blockers(FakeCity("罗马", "NONE"))

    def test_empty_string_is_caught(self):
        assert "拉文那" in _blockers(FakeCity("拉文那", ""))

    def test_whitespace_only_is_caught(self):
        assert "亚琛" in _blockers(FakeCity("亚琛", "   "))

    def test_mixed_case_is_caught(self):
        assert "巴黎" in _blockers(FakeCity("巴黎", "Nothing"))

    def test_busy_city_is_not_flagged(self):
        assert _blockers(FakeCity("拉文那", "UNIT_ARTILLERY")) == ""

    def test_all_idle_cities_are_listed(self):
        """T301 实况:六座城同时空转,要全部点名而不是只报一座。"""
        out = _blockers(
            FakeCity("奥斯蒂亚", "nothing"),
            FakeCity("克里斯蒂安桑", "nothing"),
            FakeCity("普提奥利", "nothing"),
            FakeCity("圣彼得堡", "nothing"),
            FakeCity("斯摩棱斯克", "nothing"),
            FakeCity("科斯特罗姆斯卡亚", "nothing"),
        )
        for name in ("奥斯蒂亚", "克里斯蒂安桑", "普提奥利", "圣彼得堡"):
            assert name in out
        assert "6 座城" in out

    def test_mixed_busy_and_idle(self):
        out = _blockers(
            FakeCity("忙碌城", "BUILDING_SEWER"),
            FakeCity("空转城", "nothing"),
        )
        assert "空转城" in out
        assert "忙碌城" not in out


class TestItIsABlockerNotAWarning:
    def test_idle_production_blocks(self):
        """空转的产能是当场蒸发,不是可以押后的提醒。"""
        blockers, warnings = check_turn_completion(
            [], [], [FakeCity("空转城", "nothing")]
        )
        assert any("没有在生产" in b for b in blockers)
        assert not any("没有在生产" in w for w in warnings)


class TestSharedConstant:
    def test_values_are_centralised(self):
        from civ_mcp.end_turn import _IDLE_PRODUCTION_VALUES

        assert "NOTHING" in _IDLE_PRODUCTION_VALUES, (
            "实机值是 nothing —— 漏掉它整道门就是哑的"
        )
        assert "NONE" in _IDLE_PRODUCTION_VALUES
        assert "" in _IDLE_PRODUCTION_VALUES

    def test_gate_uses_the_constant_not_a_literal(self):
        import inspect

        src = inspect.getsource(check_turn_completion)
        assert "_IDLE_PRODUCTION_VALUES" in src
        assert 'in ("", "NONE")' not in src, "写死的字面量正是这个缺陷的根因"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
