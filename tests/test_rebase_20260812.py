"""缺陷 113 的另一半:空军转场(rebase)没有工具入口。

2026-08-12 罗马局 T455-458 收官清场。空军的射程是 15 格,超出【只能转场】,
而 unit_action 里没有这个动作 —— 四次转场全靠 run_lua 手发:

    UnitManager.RequestOperation(u, GameInfo.UnitOperations["UNITOPERATION_REBASE"].Hash, p)

没有它的后果不是"少个便利功能",而是空军只能打眼前这一片:
轰炸机在 (60,51),印度海得拉巴在 (108,45) —— 44 格,打不到也过不去,
而它的全部价值恰恰是机动。这一局的目标散在 (17,46) 到 (108,45) 之间,
不转场就有一半够不着。

实测要点(T458):
  · Hash -1054550409,参数 PARAM_X/PARAM_Y 指向目标航空港/航母所在格
  · 转场有距离上限:(60,51)→(104,47) 44 格被拒,分段到 (85,44) 就成 ——
    所以拒绝时必须提示"分段跳",否则调用方会以为目标非法而放弃
  · CanStartOperation 可预检,不必发出去才知道行不行

先前那半(attack 走 AIR_ATTACK、move 报 AIR_UNIT_CANNOT_MOVE)见
tests/test_air_units_20260812.py。两半合起来空军才算真的可用。
"""

from __future__ import annotations

import inspect

import pytest

from civ_mcp.lua.units import build_move_unit, build_rebase


def _code() -> str:
    return build_rebase(55, 73, 30)


class TestRebaseQuery:
    def test_uses_the_rebase_operation(self):
        code = _code()
        assert "UNITOPERATION_REBASE" in code

    def test_passes_target_coordinates(self):
        code = _code()
        assert "PARAM_X" in code and "PARAM_Y" in code
        assert "73" in code and "30" in code

    def test_prechecks_before_sending(self):
        """CanStartOperation 能预检 —— 不必发出去才知道被拒。"""
        code = _code()
        assert "CanStartOperation" in code
        i = code.index("CanStartOperation")
        j = code.index("RequestOperation")
        assert i < j, "预检必须在请求之前"

    def test_rejects_non_air_units(self):
        """rebase 只对 DOMAIN_AIR 有效,地面单位走 move、商人走 teleport。"""
        code = _code()
        assert "DOMAIN_AIR" in code
        assert "ERR:NOT_AIR_UNIT" in code

    def test_failure_message_teaches_the_two_hop(self):
        """距离上限是实测出来的坑:44 格拒、20 格成。不写清楚就会以为目标非法。"""
        code = _code()
        i = code.index("ERR:CANNOT_REBASE")
        seg = code[i : i + 500]
        assert "分段" in seg
        assert "44" in seg, "把实测数字写进去,读的人才知道该跳几段"

    def test_failure_message_names_the_other_cause(self):
        code = _code()
        i = code.index("ERR:CANNOT_REBASE")
        seg = code[i : i + 500]
        assert "航空港" in seg and "航母" in seg

    def test_failure_reports_actual_distance(self):
        code = _code()
        assert "GetPlotDistance" in code


class TestMoveErrorPointsAtRebase:
    def test_air_move_error_names_the_tool_call(self):
        """空军用 move 时的报错要给出可直接照抄的调用,而不是只说"能转场"。"""
        code = build_move_unit(55, 84, 48)
        i = code.index("AIR_UNIT_CANNOT_MOVE")
        seg = code[i : i + 400]
        assert "rebase" in seg
        assert "unit_action" in seg


class TestWiring:
    def test_tool_dispatches_rebase(self):
        from civ_mcp import server

        fn = getattr(server.unit_action, "fn", server.unit_action)
        src = inspect.getsource(fn)
        assert 'case "rebase"' in src
        assert "gs.rebase_unit(" in src

    def test_tool_requires_coordinates(self):
        from civ_mcp import server

        fn = getattr(server.unit_action, "fn", server.unit_action)
        src = inspect.getsource(fn)
        i = src.index('case "rebase"')
        seg = src[i : i + 700]
        assert "target_x is None or target_y is None" in seg

    def test_action_is_listed_for_callers(self):
        """不在动作清单里的动作等于不存在 —— 调用方不会去猜。"""
        from civ_mcp import server

        fn = getattr(server.unit_action, "fn", server.unit_action)
        doc = inspect.getdoc(fn) or ""
        assert "rebase" in doc

    def test_game_state_method_exists(self):
        from civ_mcp.game_state import GameState

        assert hasattr(GameState, "rebase_unit")
        src = inspect.getsource(GameState.rebase_unit)
        assert "build_rebase" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
