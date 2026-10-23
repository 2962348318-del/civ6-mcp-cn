"""2026-08-11 用户点名的两条城市建设缺陷(90-91)的回归测试。

用户原话两条:
  ·「每个城市基础设施要健全啊不能打完就什么都不管」
  ·「然后没钱为什么不直接直接买粮仓什么的」

两条指向同一个盲区:工具把数据都给了,但从没把它们**对比**出来。

  (90) set_production_queue 的逗号既是项目分隔符、又出现在区域坐标里。
       "DISTRICT_CAMPUS@71,57" 被 split(",") 拆成 "DISTRICT_CAMPUS@71"
       和 "57",两个都解析失败;报错时又用 ','.join(bad) 把拆开的拼回去,
       于是错误信息回显的是【完整且合法】的那一项,读起来像工具在说
       "这个正确的格式不对"。文档里的示例
       "BUILDING_MARKET,UNIT_TRADER,DISTRICT_CAMPUS@69,47" 因此从来没能
       用过 —— 带坐标的区域根本排不进队列。

  (91) 买价一直印在 get_city_production 的每一行里,但从没和"自己造要多少
       回合"放在一起对比。实机:产能 1 的阿尔皮努姆建谷仓要 69 回合,
       而买只要 220g,同时金库囤着 1236 金;囤积提醒每回合都在喊"金币不花
       是机会成本",却从不说该买什么。

  另加:基建缺口审计。本局两次结构性塌陷(T246 维护费 116% 因为没有商业
       建筑;T260 科研被反超因为学院建筑残缺)都是缺口攒出来的,而简报里
       只有零散的低优先级提示,从没按城汇总。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from civ_mcp import city_infra


@dataclass
class FakeCity:
    name: str
    population: int = 10
    production: float = 30.0
    districts: list[str] = field(default_factory=list)
    buildings: list[str] = field(default_factory=list)


class TestQueueCoordinateParsing:
    """(90) 区域坐标里的逗号不能被当成项目分隔符。"""

    @staticmethod
    def _split(items: str) -> list[str]:
        """复刻 server.py 里的分割逻辑,验证它保住了坐标。"""
        import re

        tokens = [s.strip() for s in items.split(",") if s.strip()]
        out: list[str] = []
        for tok in tokens:
            if (
                out
                and re.fullmatch(r"-?\d+", tok)
                and "@" in out[-1]
                and "," not in out[-1]
            ):
                out[-1] = f"{out[-1]},{tok}"
            else:
                out.append(tok)
        return out

    def test_district_coords_survive_the_split(self):
        got = self._split("BUILDING_GRANARY,DISTRICT_CAMPUS@71,57")
        assert got == ["BUILDING_GRANARY", "DISTRICT_CAMPUS@71,57"]

    def test_documented_example_actually_works(self):
        """文档写的这一串此前必然失败 —— 它是工具自己的使用示例。"""
        got = self._split("BUILDING_MARKET,UNIT_TRADER,DISTRICT_CAMPUS@69,47")
        assert got == ["BUILDING_MARKET", "UNIT_TRADER", "DISTRICT_CAMPUS@69,47"]

    def test_negative_coords_survive(self):
        got = self._split("DISTRICT_HARBOR@-3,-7")
        assert got == ["DISTRICT_HARBOR@-3,-7"]

    def test_plain_list_unaffected(self):
        got = self._split("BUILDING_GRANARY,BUILDING_MARKET,UNIT_TRADER")
        assert got == ["BUILDING_GRANARY", "BUILDING_MARKET", "UNIT_TRADER"]

    def test_server_uses_the_merge_not_bare_split(self):
        import inspect

        from civ_mcp import server

        src = inspect.getsource(getattr(server.set_production_queue, "fn", server.set_production_queue))
        assert "raw_items = [s.strip() for s in items.split" not in src, (
            "裸 split 会把区域坐标拆成两半"
        )
        assert 're.fullmatch(r"-?\\d+", tok)' in src


class TestBuyRecommendation:
    """(91) 造得慢又买得起的,要主动摆到眼前。"""

    @staticmethod
    def _opt(name, cost, turns, gold):
        from civ_mcp.lua.models import ProductionOption

        return ProductionOption(
            category="BUILDING",
            item_name=name,
            cost=cost,
            turns=turns,
            gold_cost=gold,
        )

    def test_slow_and_affordable_building_is_surfaced(self):
        from civ_mcp import narrate

        out = narrate.narrate_city_production(
            [self._opt("BUILDING_GRANARY", 65, 69, 220)]
        )
        assert "建议直接买" in out
        assert "GRANARY 自己造 69 回合 / 买 220g" in out

    def test_fast_building_is_not_nagged_about(self):
        from civ_mcp import narrate

        out = narrate.narrate_city_production(
            [self._opt("BUILDING_GRANARY", 65, 3, 220)]
        )
        assert "建议直接买" not in out

    def test_unpurchasable_is_not_recommended(self):
        from civ_mcp import narrate

        out = narrate.narrate_city_production(
            [self._opt("BUILDING_FORBIDDEN_CITY", 920, 842, -1)]
        )
        assert "建议直接买" not in out

    def test_best_value_comes_first(self):
        """回合/金币比越高越划算 —— 省的时间最多、花的钱最少。"""
        from civ_mcp import narrate

        out = narrate.narrate_city_production(
            [
                self._opt("BUILDING_SEWER", 200, 210, 680),
                self._opt("BUILDING_GRANARY", 65, 69, 220),
            ]
        )
        head = out.split("\n")[0]
        assert head.index("GRANARY") < head.index("SEWER")


class TestInfrastructureAudit:
    """基建缺口要按城汇总,而不是散落成低优先级提示。"""

    def test_new_conquest_with_only_a_monument_is_flagged(self):
        city = FakeCity(name="沃罗涅什", population=6, buildings=["MONUMENT"])
        gaps = city_infra.city_gaps(city)
        assert any("CAMPUS" in g for g in gaps)
        assert any("COMMERCIAL_HUB" in g for g in gaps)
        assert any("GRANARY" in g for g in gaps)

    def test_campus_without_university_is_flagged(self):
        """拉文那的真实形态:有学院有图书馆没大学,40 产能只出 13 科研。"""
        city = FakeCity(
            name="拉文那",
            population=10,
            districts=["CAMPUS", "COMMERCIAL_HUB", "INDUSTRIAL_ZONE"],
            buildings=[
                "MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY",
                "MARKET", "BANK", "WORKSHOP", "FACTORY",
            ],
        )
        gaps = city_infra.city_gaps(city)
        assert len(gaps) == 1
        assert "UNIVERSITY" in gaps[0]

    def test_fully_built_city_has_no_gaps(self):
        city = FakeCity(
            name="奥斯蒂亚",
            population=11,
            districts=["CAMPUS", "COMMERCIAL_HUB", "INDUSTRIAL_ZONE"],
            buildings=[
                "MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY", "UNIVERSITY",
                "MARKET", "BANK", "WORKSHOP", "FACTORY",
            ],
        )
        assert city_infra.city_gaps(city) == []

    def test_tiny_city_is_only_asked_for_basics(self):
        """人口 2 的城要求它建齐四条链只会堵死队列。"""
        city = FakeCity(name="喀山", population=2, buildings=["MONUMENT"])
        gaps = city_infra.city_gaps(city)
        assert len(gaps) == 1
        assert "GRANARY" in gaps[0]
        assert not any("CAMPUS" in g for g in gaps)

    def test_narrate_ranks_worst_first_and_stays_silent_when_clean(self):
        worst = FakeCity(name="空壳城", population=8, buildings=[])
        mild = FakeCity(
            name="小缺城",
            population=8,
            districts=["CAMPUS", "COMMERCIAL_HUB", "INDUSTRIAL_ZONE"],
            buildings=[
                "MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY", "UNIVERSITY",
                "MARKET", "BANK", "WORKSHOP",
            ],
        )
        out = city_infra.narrate([mild, worst])
        assert out.index("空壳城") < out.index("小缺城")
        assert city_infra.narrate([]) == ""

    def test_low_production_city_is_told_to_grow_not_to_build_districts(self):
        """用户点名两次:「这299回合造商业中心补脑子有问题吗」
        「那先补人口不该,你看看大神们是怎么做的一般」。

        实机:产能 1 的阿尔皮努姆,我给它设了 300 回合的商业中心。区域还
        【不能用金币买】,所以那 300 回合没有任何提前结束的可能 —— 等于
        永久空转。正确打法是反过来:先补人口,产能是人口的结果。"""
        city = FakeCity(
            name="阿尔皮努姆",
            population=4,
            production=1.0,
            districts=["HARBOR"],
            buildings=["MONUMENT", "GRANARY"],
        )
        gaps = city_infra.city_gaps(city)
        assert any("先补人口" in g for g in gaps)
        assert not any("无CAMPUS" in g or "无COMMERCIAL_HUB" in g for g in gaps), (
            "产能 1 的城催它建区域 = 让它永久空转"
        )

    def test_district_slots_respect_population(self):
        """Civ6 每 3 人口 1 个区域槽。pop 6 只能放 2 个,
        催第三个是执行不了的建议。"""
        city = FakeCity(
            name="小城",
            population=6,
            production=20.0,
            districts=["CAMPUS", "COMMERCIAL_HUB"],
            buildings=["MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY", "MARKET"],
        )
        gaps = city_infra.city_gaps(city)
        assert not any("无INDUSTRIAL_ZONE" in g for g in gaps), (
            "pop 6 只有 2 个区域槽,已经用完了"
        )
        # 但已建区域缺建筑仍然要报
        assert any("UNIVERSITY" in g for g in gaps)

    def test_healthy_big_city_still_gets_district_advice(self):
        city = FakeCity(
            name="大城",
            population=12,
            production=30.0,
            districts=["CAMPUS"],
            buildings=["MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY", "UNIVERSITY"],
        )
        gaps = city_infra.city_gaps(city)
        assert any("无COMMERCIAL_HUB" in g for g in gaps)

    def test_wired_into_the_briefing(self):
        import inspect

        from civ_mcp import end_turn as et

        assert "city_infra" in inspect.getsource(et._check_infrastructure_gaps)
        src = inspect.getsource(et)
        assert "_check_infrastructure_gaps(cities_list)" in src, (
            "审计必须真的挂进简报,否则等于没写"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
