"""2026-08-08 苏美尔实机局 T190:区域/奇观顾问误读 GetOperationTargets 返回结构。

发现经过(真实对局):
  巴地比拉的生产列表明明写着 DISTRICT_INDUSTRIAL_ZONE(cost 54, 4 turns),
  get_district_advisor 却回"No valid placement tiles for
  DISTRICT_INDUSTRIAL_ZONE." 两个工具对同一件事给出相反答案。

  直接在游戏里 dump 返回表才看清:CityManager.GetOperationTargets 的结果是按
  CityOperationResults.* 分键的字典,同时存在
      PLOTS / BLOCKED_PLOTS / MOUNTAIN_PLOTS / FAILURE_REASONS / ...
  当时唯一的键是 FAILURE_REASONS,值是 {"[ICON_Citizen] 人口需要达到7或更多。"}
  (巴地比拉 pop 6,已有 2 个区域,第 3 个要 pop 7)。

根因:两个顾问各写了一份 `for k,v in pairs(targets) do if type(v)=="table"`,
不看 key,把每个 table 值都当地块索引。中文错误字符串被喂给
Map.GetPlotByIndex → 退化成 0 号地块(0,0 是水格)→ 被水格过滤 → 空结果。
游戏给出的确切原因被丢掉,agent 只看到"没地方放",会直接放弃这个区域。

反向后果同样存在:BLOCKED_PLOTS / MOUNTAIN_PLOTS 也是 table,会被当成合法
推荐位吐出去。

修法:
  1. 抽出 _lua_op_target_plots() 单一实现,按 CityOperationResults.PLOTS 取
     地块、按 FAILURE_REASONS 取原因,两个顾问共用(此前是两套拷贝)。
  2. 无地块时 bail 带上游戏自己的原因。
  3. get_wonder_advisor 补上 ERR: 透传 —— 区域顾问早有,奇观顾问漏了。
"""

from __future__ import annotations

import re

from civ_mcp.lua._helpers import _lua_op_target_plots
from civ_mcp.lua.map import build_district_advisor_query, build_wonder_advisor_query


class TestSharedSnippet:
    def test_keys_on_plots_not_on_value_type(self):
        lua = _lua_op_target_plots()
        assert "RES.PLOTS" in lua, "必须按 CityOperationResults.PLOTS 取地块"
        assert "RES.FAILURE_REASONS" in lua, "必须读出游戏给的失败原因"

    def test_blocked_and_mountain_plots_are_not_collected(self):
        """只认 PLOTS —— BLOCKED_PLOTS / MOUNTAIN_PLOTS 不能混进推荐位。"""
        lua = _lua_op_target_plots()
        assert "BLOCKED_PLOTS" not in lua
        assert "MOUNTAIN_PLOTS" not in lua
        # 唯一往 plotIndices 插值的分支必须挂在 PLOTS 键或数字退化路径下
        inserts = [ln for ln in lua.splitlines() if "table.insert(plotIndices" in ln]
        assert len(inserts) == 2, f"预期 PLOTS 分支 + 数字退化分支各一处: {inserts}"

    def test_fallback_never_accepts_strings_as_plot_indices(self):
        """常量表缺席时的退化路径必须挡住字符串 —— 这正是原 bug 的形状。"""
        lua = _lua_op_target_plots()
        fallback = lua.split("elseif RES == nil then", 1)[1]
        assert 'type(idx) == "number"' in fallback

    def test_strips_icon_markup_from_reason(self):
        assert "ICON_" in _lua_op_target_plots()  # gsub 模式里出现
        assert "gsub" in _lua_op_target_plots()

    def test_defines_both_names_callers_rely_on(self):
        lua = _lua_op_target_plots()
        assert "local plotIndices = {}" in lua
        assert 'local failReason = ""' in lua


class TestBothAdvisorsUseIt:
    """一个概念一套实现 —— 本项目反复栽在"修了一套忘了另一套"。"""

    def test_district_advisor_uses_shared_snippet(self):
        q = build_district_advisor_query(65536, "DISTRICT_CAMPUS")
        assert "RES.PLOTS" in q
        assert "RES.FAILURE_REASONS" in q

    def test_wonder_advisor_uses_shared_snippet(self):
        q = build_wonder_advisor_query(65536, "BUILDING_GREAT_LIBRARY")
        assert "RES.PLOTS" in q
        assert "RES.FAILURE_REASONS" in q

    def test_no_advisor_still_scoops_every_table_value(self):
        """旧写法 `if type(v) == "table" then` 直接插值,不能再出现。"""
        bad = re.compile(
            r'if type\(v\) == "table" then\s*\n\s*for _, idx in ipairs\(v\) do\s*'
            r"table\.insert\(plotIndices"
        )
        for q in (
            build_district_advisor_query(65536, "DISTRICT_CAMPUS"),
            build_wonder_advisor_query(65536, "BUILDING_GREAT_LIBRARY"),
        ):
            assert not bad.search(q), "旧的不看 key 的写法又回来了"


class TestReasonSurfaces:
    def test_district_bail_carries_the_game_reason(self):
        q = build_district_advisor_query(65536, "DISTRICT_CAMPUS")
        assert "ERR:NO_TILES|" in q
        assert "failReason" in q.split("ERR:NO_TILES|")[1][:120]

    def test_wonder_bail_carries_the_game_reason(self):
        q = build_wonder_advisor_query(65536, "BUILDING_GREAT_LIBRARY")
        assert "ERR:NO_TILES|" in q
        assert "failReason" in q.split("ERR:NO_TILES|")[1][:120]


class TestWonderAdvisorPropagatesErrors:
    """区域顾问一直在透传 ERR:,奇观顾问漏了同一段。"""

    def test_returns_err_line_instead_of_empty_list(self):
        import asyncio
        from types import SimpleNamespace as NS

        from civ_mcp.game_state import GameState

        gs = GameState.__new__(GameState)
        gs._advisor_calls_this_turn = 0
        gs._advisor_budget_warning = None
        gs._advisor_budget_check = lambda: (None, None)
        gs.conn = NS(
            execute_write=lambda lua: _async(["ERR:NO_TILES|人口需要达到7或更多。"])
        )

        out = asyncio.run(gs.get_wonder_advisor(65536, "BUILDING_COLOSSUS"))
        assert isinstance(out, str)
        assert "人口需要达到7或更多" in out


async def _async(value):
    return value
