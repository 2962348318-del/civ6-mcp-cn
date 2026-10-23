"""缺陷 108:生产设置的验证只读一次,把「还没结算」当成了「引擎拒绝」。

2026-08-12 罗马局 T355。第一个太空项目刚完成,给拉文那设第二个
(登月计划),连着两次都收到:

    Error: SILENT_FAILURE|PROJECT_LAUNCH_MOON_LANDING appeared to set but
    the game engine did not persist it (NOT_SET|current=NONE|expected=...)

下一次调用直接读引擎:

    CurrentlyBuilding = PROJECT_LAUNCH_MOON_LANDING
    GetCurrentProductionTypeHash = -473089769   (= 登月计划的 Hash)

【第一次就设置成功了】。两次"失败"报告全是假的,我还照着重试了一遍。

根因是时序,不是 API 选错:CityManager.RequestOperation 是 fire-and-forget,
请求发出后同一批读回时引擎可能还没结算,CurrentlyBuilding() 返回 "NONE"。
build_verify_production 的注释解释过为什么用 CurrentlyBuilding() 而不是
GetCurrentProductionTypeHash()(后者在 GameCore 返回 nil)—— 那个选择是
对的,错的是只读一次就下定论。

这个假失败特别贵:

  · 它出现在科学胜利的核心操作上。五个太空项目每个都要 set 一次,
    每次都可能报错;调用方如果信了,就会以为整条发射链推不动。
  · 它给的补救建议是"用 purchase_item 强制提交",而【项目不能购买】。
    照做只会撞第二堵墙。

和缺陷 104 同一族:把异步 API 的"尚未结算"读成"被拒绝"。区别是 104 的
结论错在根因(过时卡),这一个错在时机 —— 判据本身没问题,只是问早了。
修法相应也不同:不改判据,只是失败时跨帧再读一次,两次都失败才算数。
"""

from __future__ import annotations

import inspect

import pytest

from civ_mcp import game_state
from civ_mcp.lua.cities import build_verify_production


def _src() -> str:
    return inspect.getsource(game_state.GameState.set_city_production)


class TestVerifyRetriesBeforeDeclaringFailure:
    def test_reads_more_than_once(self):
        """单次读回撞上异步窗口就会误判 —— 必须给引擎第二次机会。"""
        src = _src()
        i = src.index("OK-path verification")
        seg = src[i : i + 2500]
        assert "for _ in range(2)" in seg, (
            "NOT_SET 之后要跨帧重读一次,不能一次就下结论"
        )

    def test_confirmed_returns_immediately(self):
        """第一次就确认的情况不该多花一次往返。"""
        src = _src()
        i = src.index("for _ in range(2)")
        seg = src[i : i + 600]
        assert 'if any("CONFIRMED" in vl for vl in verify_lines):' in seg
        assert seg.index("CONFIRMED") < seg.index("NOT_SET")

    def test_failure_message_says_two_reads(self):
        """报错要说清是重试过的,否则读的人不知道这是不是又一次早读。"""
        src = _src()
        assert "after two reads" in src

    def test_no_longer_tells_projects_to_use_purchase(self):
        """项目不能购买 —— 旧文案把调用方推向第二堵墙。

        注意从【返回语句】那处找,不要撞上解释这段历史的中文注释 ——
        本局已经有三个测试栽在这上面。"""
        src = _src()
        i = src.index('f"Error: SILENT_FAILURE|')
        seg = src[i : i + 600]
        assert "projects cannot be purchased" in seg


class TestVerifyQueryItself:
    """判据本身是对的,钉住它免得被"顺手改成 hash 比较"。"""

    def test_uses_currently_building(self):
        src = build_verify_production(196610, "PROJECT_LAUNCH_MOON_LANDING")
        assert "CurrentlyBuilding()" in src

    def test_does_not_use_ingame_only_api(self):
        """GetCurrentProductionTypeHash 在 GameCore 返回 nil —— 实测确认。"""
        src = build_verify_production(196610, "PROJECT_LAUNCH_MOON_LANDING")
        assert "GetCurrentProductionTypeHash" not in src

    def test_reports_both_sides_on_mismatch(self):
        src = build_verify_production(196610, "PROJECT_LAUNCH_MOON_LANDING")
        assert "current=" in src
        assert "expected=" in src

    def test_confirmed_carries_turns_left(self):
        src = build_verify_production(196610, "PROJECT_LAUNCH_MOON_LANDING")
        i = src.index("CONFIRMED")
        assert "GetTurnsLeft" in src[i : i + 200]

    def test_works_for_projects_not_just_buildings(self):
        """实测:GameCore 下 CurrentlyBuilding() 对项目返回的就是
        "PROJECT_LAUNCH_MOON_LANDING",所以查询没问题。"""
        src = build_verify_production(196610, "PROJECT_LAUNCH_MOON_LANDING")
        assert 'cur == "PROJECT_LAUNCH_MOON_LANDING"' in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
