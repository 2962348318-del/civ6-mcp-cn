"""缺陷 95:在建的区域被当成已建成,基建审计据此给出办不到的建议。

2026-08-11 罗马局 T270 实锤。get_cities 把阿奎莱亚的区域列成
BATH / HARBOR / CAMPUS,读起来是三个都盖好了。实测 d:IsComplete():

    DISTRICT_BATH    complete=true
    DISTRICT_HARBOR  complete=false      <- 在建
    DISTRICT_CAMPUS  complete=false      <- 在建

于是基建审计报「阿奎莱亚:CAMPUS缺LIBRARY/UNIVERSITY」—— 而学院本身还没
封顶,图书馆那时【根本排不进生产列表】。这条建议不是次优,是执行不了。

反向的错也同时存在:在建区域被算进"已建成"的数量,区域槽位提前判满,
真正该建的区域反而不报。

所以两个数要分开:
  · districts          —— 已建成。回答"这座城有没有学院"
  · districts_building —— 在建。它【占槽位】但还不能盖建筑
槽位占用 = 两者之和;"有没有" = 只看前者。
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
    districts_building: list[str] = field(default_factory=list)
    buildings: list[str] = field(default_factory=list)


class TestAuditRespectsCompletion:
    def test_unfinished_district_is_not_asked_for_its_buildings(self):
        """阿奎莱亚的原形:学院在建,不该报它缺图书馆。"""
        city = FakeCity(
            name="阿奎莱亚",
            population=8,
            production=14.0,
            districts=["BATH@64,42"],
            districts_building=["HARBOR@63,43", "CAMPUS@62,42"],
            buildings=["MONUMENT", "GRANARY"],
        )
        gaps = city_infra.city_gaps(city)
        assert not any("LIBRARY" in g for g in gaps), (
            "学院还没封顶,图书馆排不进生产列表 —— 这条建议执行不了"
        )

    def test_unfinished_district_is_not_re_recommended(self):
        """已经在盖了就别再催建一个。"""
        city = FakeCity(
            name="普提奥利",
            population=6,
            production=10.0,
            districts=[],
            districts_building=["CAMPUS@67,39"],
            buildings=["MONUMENT", "GRANARY", "WATER_MILL"],
        )
        gaps = city_infra.city_gaps(city)
        assert not any("无CAMPUS" in g for g in gaps)

    def test_unfinished_district_still_consumes_a_slot(self):
        """反向的错也要防:在建区域占着槽位,不能当它不存在。

        pop 6 = 2 个槽,已建 1 个 + 在建 1 个 = 满,不该再催第三个。"""
        city = FakeCity(
            name="小城",
            population=6,
            production=20.0,
            districts=["CAMPUS@1,1"],
            districts_building=["COMMERCIAL_HUB@1,2"],
            buildings=["MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY"],
        )
        gaps = city_infra.city_gaps(city)
        assert not any("无INDUSTRIAL_ZONE" in g for g in gaps), (
            "两个槽已被已建+在建占满"
        )

    def test_completed_district_still_reports_missing_buildings(self):
        """建成的区域照旧要报缺建筑 —— 修复不能把正确的行为一起关掉。"""
        city = FakeCity(
            name="拉文那",
            population=10,
            districts=["CAMPUS@70,49"],
            buildings=["MONUMENT", "GRANARY", "WATER_MILL", "LIBRARY"],
        )
        gaps = city_infra.city_gaps(city)
        assert any("UNIVERSITY" in g for g in gaps)

    def test_missing_field_defaults_to_empty(self):
        """老对象没有这个字段时不能炸 —— 审计每回合都跑。"""

        @dataclass
        class Legacy:
            name: str = "旧城"
            population: int = 10
            production: float = 30.0
            districts: list = field(default_factory=list)
            buildings: list = field(default_factory=list)

        assert isinstance(city_infra.city_gaps(Legacy()), list)


class TestParserSplitsByCompletion:
    def test_star_marks_go_to_the_building_list(self):
        from civ_mcp.lua.cities import parse_cities_response

        line = (
            "1|阿奎莱亚|63,42|8|16|14|10|7|5|0|10|6|99|nothing|0|63|200/200|400/400|"
            "|"
            "|BATH@64,42;HARBOR*@63,43;CAMPUS*@62,42|"
            "100|100|0|0|0.0|0|33|"
        )
        cities, _ = parse_cities_response([line])
        c = cities[0]
        assert c.districts == ["BATH@64,42"]
        assert c.districts_building == ["HARBOR@63,43", "CAMPUS@62,42"]

    def test_star_is_stripped_from_the_stored_name(self):
        """存进来的名字不该带标记 —— 下游按子串匹配区域类型。"""
        from civ_mcp.lua.cities import parse_cities_response

        line = (
            "1|城|1,1|8|10|10|10|5|5|0|10|6|99|nothing|0|60|200/200|400/400|"
            "||CAMPUS*@1,1|100|100|0|0|0.0|0|33|"
        )
        c = parse_cities_response([line])[0][0]
        assert c.districts_building == ["CAMPUS@1,1"]
        assert "*" not in c.districts_building[0]

    def test_all_complete_leaves_building_list_empty(self):
        from civ_mcp.lua.cities import parse_cities_response

        line = (
            "1|城|1,1|8|10|10|10|5|5|0|10|6|99|nothing|0|60|200/200|400/400|"
            "||CAMPUS@1,1;BATH@1,2|100|100|0|0|0.0|0|33|"
        )
        c = parse_cities_response([line])[0][0]
        assert c.districts == ["CAMPUS@1,1", "BATH@1,2"]
        assert c.districts_building == []


class TestLuaMarksCompletion:
    @staticmethod
    def _src() -> str:
        from civ_mcp.lua import cities

        src = cities.build_cities_query()
        return "\n".join(
            ln for ln in src.split("\n") if not ln.strip().startswith("--")
        )

    def test_queries_is_complete(self):
        assert "d:IsComplete()" in self._src()

    def test_completion_check_is_pcall_wrapped(self):
        """IsComplete 在某些对象上可能缺失 —— 不能让它带崩整个城市查询。"""
        src = self._src()
        i = src.index("d:IsComplete()")
        assert "pcall(function()" in src[max(0, i - 200) : i]

    def test_defaults_to_complete_when_unreadable(self):
        """读不到就按【已建成】处理:那是修复前的行为,不会凭空多报缺口。"""
        src = self._src()
        assert "local built = true" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
