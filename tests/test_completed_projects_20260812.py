"""缺陷 110:WorldModel.completed_projects 有存盘、读盘和三个消费者,没有写入者。

2026-08-12 罗马局 T356。同一回合的两个数据源:

    end_turn 简报    : 科学胜利链还剩 7800 产能(0/5 个项目已完成)…还需约 111 回合
    get_victory_progress : 罗马: 1 spaceport | ~2/5 projects done
                           [DONE] 发射地球卫星  [DONE] 启动登月计划

7800 正是五件的总和 —— 也就是"一件都没做"的数字。而地球卫星在 T350 前后
就完成了,登月计划这一回合刚由卡尔·萨根的 +3000 产能一次性推完。

根因不是算错,是【这个字段从来没有人写】:

    world_model.py:214   completed_projects: list[str] = field(default_factory=list)
    world_model.py:499   存盘
    world_model.py:534   读盘
    end_turn.py:3312     消费(ETA 与剩余产能)
    end_turn.py:4602     消费(科学收尾建议)
    lategame.py:204/248/283  消费(剩余成本/进度计数/里程碑跳过)

三个消费者、一个存盘、一个读盘、零个写入者。于是 remaining_cost 恒为
7800,进度恒为 0/5,而 lategame.py:283 那句 `if m.key in ctx.completed_projects`
本来是用来跳过已完成里程碑的 —— 恒假,所以它会一直推荐已经做完的项目。

和缺陷 107 同一个模块、同一个形状:107 是 spaceport_cities 恒为空元组,
这条是 completed_projects 恒为空列表。更刺眼的是位置 —— 修 107 时写下的
那句注释("这一行曾经让 spaceport_cities 永远是空元组")就在 4602 行
`completed_projects=set(wm.completed_projects)` 的上面几行,当时盯着 ports
改完就走了,没看紧挨着的下一行。恒空的输入不会报错,只会让一条永远为真的
建议一直刷 —— 这正是 107 的教训原文。
"""

from __future__ import annotations

import inspect

import pytest

from civ_mcp import end_turn as et
from civ_mcp import lategame
from civ_mcp.world_model import WorldModel


class TestTierInference:
    def test_building_moon_landing_implies_satellite_done(self):
        done = lategame.infer_completed_projects("PROJECT_LAUNCH_MOON_LANDING")
        assert done == {"PROJECT_LAUNCH_EARTH_SATELLITE"}

    def test_building_a_mars_project_implies_both_earlier_tiers(self):
        """T356 实况:拉文那在造火星住所 —— 卫星和登月必然都完成了。"""
        done = lategame.infer_completed_projects("PROJECT_LAUNCH_MARS_HABITATION")
        assert done == {
            "PROJECT_LAUNCH_EARTH_SATELLITE",
            "PROJECT_LAUNCH_MOON_LANDING",
        }

    def test_same_tier_siblings_are_not_inferred(self):
        """同层三件互不构成前置 —— 在造其中一件推不出另外两件。"""
        done = lategame.infer_completed_projects("PROJECT_LAUNCH_MARS_HABITATION")
        assert "PROJECT_LAUNCH_MARS_REACTOR" not in done
        assert "PROJECT_LAUNCH_MARS_HYDROPONICS" not in done

    def test_first_project_infers_nothing(self):
        assert (
            lategame.infer_completed_projects("PROJECT_LAUNCH_EARTH_SATELLITE") == set()
        )

    def test_non_space_build_infers_nothing(self):
        assert lategame.infer_completed_projects("BUILDING_FACTORY") == set()
        assert lategame.infer_completed_projects("") == set()
        assert lategame.infer_completed_projects("NONE") == set()

    def test_known_set_is_preserved(self):
        """必须单调递增 —— 这回合没在造项目不能把已完成的清零。"""
        known = {"PROJECT_LAUNCH_EARTH_SATELLITE", "PROJECT_LAUNCH_MOON_LANDING"}
        assert lategame.infer_completed_projects("", known) == known
        assert lategame.infer_completed_projects("BUILDING_FACTORY", known) == known

    def test_union_never_shrinks(self):
        known = {"PROJECT_LAUNCH_MARS_REACTOR"}
        out = lategame.infer_completed_projects("PROJECT_LAUNCH_MOON_LANDING", known)
        assert known <= out

    def test_every_chain_entry_has_a_tier(self):
        """漏填 tier 的那件会被推断静默跳过。"""
        for m in lategame.SCIENCE_CHAIN:
            assert m.tier > 0, f"{m.key} 没有层级"

    def test_tiers_are_ordered_as_the_engine_forces(self):
        tiers = [m.tier for m in lategame.SCIENCE_CHAIN]
        assert tiers == sorted(tiers), "链条顺序必须与层级一致"
        assert tiers[0] < tiers[1], "卫星和登月是两个独立的层"
        assert tiers.count(3) == 3, "三件火星项目同层"


class TestRemainingCostReactsToCompletion:
    def test_remaining_drops_when_projects_are_done(self):
        ctx = lategame.LateGameContext(
            turn=356,
            era="ERA_INFORMATION",
            victory_type="SCIENCE",
            completed_projects={
                "PROJECT_LAUNCH_EARTH_SATELLITE",
                "PROJECT_LAUNCH_MOON_LANDING",
            },
        )
        remaining = lategame._remaining_science_cost(ctx)
        assert remaining == lategame.SCIENCE_TOTAL_COST - 900 - 1500
        assert remaining < lategame.SCIENCE_TOTAL_COST

    def test_empty_set_still_reports_the_full_chain(self):
        ctx = lategame.LateGameContext(turn=300, era="", victory_type="SCIENCE")
        assert lategame._remaining_science_cost(ctx) == lategame.SCIENCE_TOTAL_COST


class TestWiring:
    """缺陷的本体是接线,所以接线本身要有岗哨。"""

    def test_end_turn_writes_the_field(self):
        src = inspect.getsource(et)
        assert "wm.completed_projects = sorted(" in src, (
            "completed_projects 必须有写入者 —— 这正是缺陷110"
        )

    def test_inference_uses_the_spaceport_city_build(self):
        src = inspect.getsource(et)
        i = src.index("infer_completed_projects")
        seg = src[max(0, i - 1500) : i + 300]
        assert "_sp_building" in seg
        assert "currently_building" in seg

    def test_victory_progress_calibrates_the_set(self):
        """逐回合推断只能推到层,同层三件要靠 vp 的权威判定补齐。"""
        src = inspect.getsource(et)
        i = src.index("_vp_done")
        seg = src[i : i + 900]
        assert 'getattr(sp, "status", "") == "completed"' in seg
        assert "|" in seg, "必须取并集,不能覆盖"

    def test_world_model_round_trips_the_field(self, tmp_path):
        wm = WorldModel()
        wm.completed_projects = [
            "PROJECT_LAUNCH_EARTH_SATELLITE",
            "PROJECT_LAUNCH_MOON_LANDING",
        ]
        path = tmp_path / "世界模型" / "rome_1.json"
        wm.save(path)
        back = WorldModel.load(path)
        assert back.completed_projects == wm.completed_projects


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
