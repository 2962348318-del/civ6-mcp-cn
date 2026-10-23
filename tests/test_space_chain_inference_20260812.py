"""缺陷 112:刚解锁的两件火星项目被报成 [DONE],进度虚报到 ~4/5。

2026-08-12 罗马局 T360。核聚变【就在这一回合】研究完,T360 的十回合快照:

    罗马: 63 techs | 1 spaceport(s) | ~4/5 projects done (inferred)
      [DONE] 发射地球卫星
      [DONE] 启动登月计划
      [DONE] 发射火星溶液栽培     <- 这回合才解锁
      [>>>] 发射火星住所 -- 0% (22 turns) in 拉文那
      [DONE] 发射火星反应堆       <- 这回合才解锁

同一回合直接查拉文那的可造列表:

    PROJECT_LAUNCH_MARS_REACTOR      (cost 1800, 26 turns)
    PROJECT_LAUNCH_MARS_HABITATION   (cost 1800, 22 turns)
    PROJECT_LAUNCH_MARS_HYDROPONICS  (cost 1800, 26 turns)

三件全在,一件没做。真实进度 2/5,剩 5400 产能。

根因:同层曾有一条推断 ——「有科技 + 既不在造也不可造 => 已完成」。核聚变
当回合完成,快照跑的那一帧引擎还没刷新可造列表,GetProductionCost 返回 0,
canBuild 为假,于是两件刚解锁的项目被判成已完成。和缺陷108 同族:把异步的
"尚未结算"读成了结论。

修法不是补一次重读,而是【删掉这条推断】。"不可造"的成因太多(刚解锁未刷新、
该城正在造别的、资源不足),拿它反推"已完成"本来就不成立;同层三件互不构成
前置,引擎不提供区分信息,那就不猜。方向也不对称:少报一件只是保守,多报一件
会让人以为胜利在望 —— 而这条恰好在科学胜利收官阶段虚报,是最贵的时刻。

层级之间那条推断保留:引擎强制前置,高层在造就意味着低层必然完成,那是硬规则。
真正的完成集合由 world_model.completed_projects 维护(缺陷110),走"实际建造过"
的路径,单调递增。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.victory import build_victory_progress_query


def _code() -> str:
    return build_victory_progress_query()


class TestSameTierInferenceHasTimingGuard:
    """T455 反转:这一组原本钉的是「删掉同层推断」,现在钉的是「保留推断 + 时序保护」。

    上一版修 112 时我选了删掉整条推断,理由写的是"少报只是保守,多报会让人以为
    胜利在望"。T455 推翻了它:五件太空项目全部完成、科学胜利已达成,而工具报
    0/5 全部 unlocked —— 少报把【已经赢了】显示成"一件没做",连带把胜利类型
    也认错成文化胜利(缺陷116)。少报和多报一样有害,只是错的方向不同。

    而且当时的测试把那个错误修法【钉死了】—— test_the_bad_branch_is_gone
    断言推断必须不存在。这正是本局反复出现的"测试冻结错误行为":测试写的是
    当时的决定,而不是不变的事实。所以这一组重写成钉住真正不变的东西 ——
    刚解锁那一帧不能用来推断完成。
    """

    def test_same_tier_inference_is_restored(self):
        code = _code()
        assert "not s.canBuild and not s.techJustIn" in code, (
            "同层推断要回来 —— 没有它,已完成的项目会一直显示成未解锁"
        )

    def test_timing_guard_exists(self):
        """T360 的误判根因是时序(科技刚到手、引擎未刷新),不是判据本身。"""
        code = _code()
        assert "techJustIn" in code
        assert "GetLastResearchCompleted" in code

    def test_guard_is_computed_per_project(self):
        code = _code()
        i = code.index("local techJustIn")
        seg = code[i : i + 400]
        assert "techRow" in seg, "要对该项目自己的前置科技判定,不是全局标志"

    def test_cross_tier_inference_survives(self):
        """层级之间是引擎强制的前置,这条必须留着 —— 修的是同层那条。"""
        code = _code()
        assert "s.tier < reachedTier" in code
        i = code.index("s.tier < reachedTier")
        assert "completed" in code[i : i + 120]

    def test_unlocked_but_unbuilt_reports_as_ready_or_unlocked(self):
        """可造的项目必须报 ready/unlocked —— 那是"还没做",不是"做完了"。"""
        code = _code()
        i = code.index("s.tier < reachedTier")
        seg = code[i : i + 900]
        assert 's.canBuild and "ready" or "unlocked"' in seg, (
            "有科技但还可造的,应该报 ready/unlocked,不是 completed"
        )

    def test_buildable_project_is_never_completed(self):
        """canBuild 为真时绝不能判完成 —— 这是 T360 误报的直接防线。"""
        code = _code()
        i = code.index("not s.canBuild and not s.techJustIn")
        seg = code[max(0, i - 200) : i + 200]
        assert "s.hasTech and not s.canBuild" in seg

    def test_gathering_storm_path_untouched(self):
        """GS 有真正的 sciVP,那条路径不受影响。"""
        code = _code()
        assert "sciNeeded > 0" in code
        assert "completedCount < sciVP" in code


class TestParserStillReadsTheChain:
    def test_ready_status_round_trips(self):
        from civ_mcp.lua.victory import parse_victory_progress_response

        vp = parse_victory_progress_response(
            [
                "SPACEPROJ|PROJECT_LAUNCH_MARS_REACTOR|发射火星反应堆|ready|0|26|1800|TECH_NUCLEAR_FUSION|true|",
                "SPACEPROJ|PROJECT_LAUNCH_MARS_HABITATION|发射火星住所|building|0|22|1800|TECH_ROBOTICS|true|拉文那",
                "SPACEDONE|2|5",
            ]
        )
        assert vp is not None
        by_type = {sp.project_type: sp for sp in vp.space_projects}
        assert by_type["PROJECT_LAUNCH_MARS_REACTOR"].status == "ready"
        assert by_type["PROJECT_LAUNCH_MARS_HABITATION"].status == "building"
        assert vp.space_completed == 2, "T360 的真实进度是 2/5,不是 4/5"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
