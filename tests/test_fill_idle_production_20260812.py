"""帝国规模上去之后,逐城设生产的接口不够用了。

2026-08-12 罗马局 T457-458,帝国 93 座城。end_turn 的门禁一次点名:

    🔴 11 座城没有在生产:拉文那、阿尔皮努姆、沃罗涅什、斯塔万格、哈图沙、
       加丹加、姆班赞刚果、穆比、巴特那、金奈、洛杉矶
       —— 空转的产能不会攒着,是当场蒸发。

而 set_city_production 是【逐城】接口:补一轮要先查 11 个 city_id、再看每座城
的可造列表(打下来的城常常连纪念碑都没有、或者已经有了)、然后 11 次往返。
那两个回合是手写 run_lua 遍历兜住的,连着写了两次一模一样的脚本。

这个动作没有决策含量 —— 挑一个这座城造得出的基础建筑而已。真正需要决策的城
(太空港城、前线补兵城)本来就不会空转,不会被它碰到。所以它适合工具化,
而不是每次让人现写脚本。

顺序上先试纪念碑/谷仓/水磨坊/下水道这些"几乎每座城都缺且立刻见效"的,
最后用建造者兜底 —— 建造者在任何城市都能造,保证不会有城市白白蒸发一回合产能。
"""

from __future__ import annotations

import inspect

import pytest

from civ_mcp.lua.cities import (
    IDLE_FILL_CANDIDATES,
    build_fill_idle_production,
    parse_fill_idle_production,
)


class TestCandidateOrder:
    def test_builder_is_the_last_resort(self):
        """建造者兜底必须在最后 —— 它总能造,放前面会盖掉更有价值的建筑。"""
        assert IDLE_FILL_CANDIDATES[-1] == ("UNIT", "UNIT_BUILDER")

    def test_basics_come_first(self):
        names = [n for _, n in IDLE_FILL_CANDIDATES]
        assert names[0] == "BUILDING_MONUMENT"
        assert "BUILDING_GRANARY" in names
        assert "BUILDING_WATER_MILL" in names

    def test_every_candidate_has_a_kind(self):
        for kind, name in IDLE_FILL_CANDIDATES:
            assert kind in ("BUILDING", "UNIT")
            assert name.startswith(kind + "_") or name.startswith("BUILDING_")


class TestQuery:
    def test_only_touches_idle_cities(self):
        """在造东西的城不能被改 —— 那会打断太空港项目之类的关键生产。"""
        code = build_fill_idle_production()
        assert "CurrentlyBuilding()" in code
        i = code.index("CurrentlyBuilding()")
        seg = code[i : i + 200]
        assert 'cur == "NONE"' in seg or 'cur == ""' in seg

    def test_prechecks_each_candidate(self):
        code = build_fill_idle_production()
        assert "CanStartOperation" in code
        i = code.index("CanStartOperation")
        j = code.index("RequestOperation")
        assert i < j

    def test_stops_at_first_success(self):
        """一座城只下一单 —— 否则后面的候选会覆盖前面的。"""
        code = build_fill_idle_production()
        assert "done = true" in code
        assert "if not done" in code

    def test_reports_cities_it_could_not_fill(self):
        code = build_fill_idle_production()
        assert "NOFILL|" in code

    def test_emits_a_summary_line(self):
        code = build_fill_idle_production()
        assert "IDLE_FILL|" in code

    def test_city_names_are_pipe_escaped(self):
        """城市名里带 | 会撕裂管道分隔的输出。"""
        code = build_fill_idle_production()
        assert 'gsub("|", "/")' in code


class TestParser:
    def test_counts_and_details(self):
        filled, failed, detail = parse_fill_idle_production(
            [
                "FILLED|罗马|UNIT_BUILDER",
                "FILLED|马德里|BUILDING_WATER_MILL",
                "NOFILL|某小城|1310739",
                "IDLE_FILL|2|1",
            ]
        )
        assert filled == 2
        assert failed == 1
        assert "罗马 → UNIT_BUILDER" in detail
        assert any("无可造项" in d for d in detail)

    def test_empty_input_is_zero(self):
        assert parse_fill_idle_production([]) == (0, 0, [])


class TestToolSurface:
    def test_tool_exists(self):
        from civ_mcp import server

        assert hasattr(server, "fill_idle_production")

    def test_docstring_explains_why_it_is_safe(self):
        """必须写明它不碰非空转城 —— 否则没人敢在有太空港项目时调用。"""
        from civ_mcp import server

        fn = getattr(server.fill_idle_production, "fn", server.fill_idle_production)
        doc = inspect.getdoc(fn) or ""
        assert "idle" in doc.lower()
        assert "never touches" in doc or "never idle" in doc

    def test_game_state_method_wired(self):
        from civ_mcp.game_state import GameState

        assert hasattr(GameState, "fill_idle_production")
        src = inspect.getsource(GameState.fill_idle_production)
        assert "build_fill_idle_production" in src
        assert "parse_fill_idle_production" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
