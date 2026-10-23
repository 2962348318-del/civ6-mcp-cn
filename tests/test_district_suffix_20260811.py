"""缺陷 106 + 107:两个"已经有了/永远做不到"却照样推荐的判断。

== 缺陷 107:districts 的坐标后缀让成员判断恒假 ==

CityInfo.districts 的元素【自带坐标后缀】。Lua 侧 distLocs 拼的是

    DistrictType .. mark .. "@" .. x .. "," .. y

也就是 "DISTRICT_INDUSTRIAL_ZONE@66,47"。于是

    "DISTRICT_INDUSTRIAL_ZONE" in city.districts     # 永远 False

两处中招,第二处贵得多:

  ① get_area_effect_advice 的 has_iz。罗马和奥斯蒂亚各有工业区+工厂+
     发电厂,简报连着两回合说"目前群内还没有工业区——这是后期产能的
     地基",劝我去建一个已经存在的东西。

  ② end_turn 的 spaceport_cities。科学胜利收尾模块靠它判断太空港建了没,
     而它恒为空元组 —— 整条科学收尾链的核心输入一直是空的。T311 刚锁定
     科学路线,几回合后就要建宇航中心,这个是在造成损失【之前】抓到的。

和缺陷 99(BUILDING_ 前缀)、101(奢侈品 xN 后缀)同族:同一个值两种写法,
只在一处归一。区别是这次的归一从来没做过,而且错的方向是"恒假" ——
恒假的检查不会报错,只会让一条永远为真的建议一直刷。

== 缺陷 106:区域格上的资源被报成 needs builder ==

区域盖上去之后 GetImprovementType() 仍然返回 -1,和"没改良的空地"读数
完全一样。T315 实探三处:

    (69,48) 铝  districtType=15 DISTRICT_BATH     improvement=-1
    (76,55) 铝  districtType=1  DISTRICT_HOLY_SITE improvement=-1
    (84,45) 油  districtType=13 DISTRICT_WONDER    improvement=-1
    (81,49) 铀  districtType=-1 none               improvement=2   <- 正常

前三个每回合都刷 "needs builder",我照着派了两个建造者过去,白跑一趟。

刺眼的是 get_builder_tasks 【早就】写了 `if distIdx < 0 then` 正确跳过
区域格 —— 同一个事实,三个数据源,两个错一个对。而紧挨着出错那一行的
注释里,记的正是 T274 同一位置因为漏判"海上资源"栽的跟头,末尾写着
"同一个事实两个数据源给出不同答案,错的那个照样会被照着执行"。
同一行、同一形状,第二次。
"""

from __future__ import annotations

import pytest

from civ_mcp.district_planner import (
    district_types,
    get_area_effect_advice,
    has_district,
)

# T317 实机格式:带 "@x,y",在建的还带 "*"
ROME = {
    "name": "罗马",
    "x": 65,
    "y": 45,
    "districts": [
        "DISTRICT_CAMPUS@67,44",
        "DISTRICT_COMMERCIAL_HUB@66,44",
        "DISTRICT_INDUSTRIAL_ZONE@66,47",
    ],
}
OSTIA = {
    "name": "奥斯蒂亚",
    "x": 68,
    "y": 48,
    "districts": [
        "DISTRICT_CAMPUS@69,47",
        "DISTRICT_INDUSTRIAL_ZONE@68,49",
        "DISTRICT_BATH@69,48",
    ],
}
AACHEN = {"name": "亚琛", "x": 65, "y": 50, "districts": ["DISTRICT_CAMPUS@65,49"]}
PARIS = {"name": "巴黎", "x": 62, "y": 48, "districts": ["DISTRICT_HOLY_SITE@63,48"]}


class TestNormalisation:
    def test_strips_coordinate_suffix(self):
        assert district_types(["DISTRICT_INDUSTRIAL_ZONE@66,47"]) == {
            "DISTRICT_INDUSTRIAL_ZONE"
        }

    def test_strips_incomplete_marker(self):
        """Lua 用 "*" 标记未完工 —— "DISTRICT_CAMPUS*@62,42"。"""
        assert district_types(["DISTRICT_CAMPUS*@62,42"]) == {"DISTRICT_CAMPUS"}

    def test_bare_type_still_works(self):
        """别的调用点可能传干净的类型名,不能因为归一反而认不出。"""
        assert district_types(["DISTRICT_CAMPUS"]) == {"DISTRICT_CAMPUS"}

    def test_empty_and_none(self):
        assert district_types(None) == set()
        assert district_types([]) == set()
        assert district_types(["", None]) == set()

    def test_has_district_matches_suffixed(self):
        assert has_district(ROME["districts"], "DISTRICT_INDUSTRIAL_ZONE") is True

    def test_has_district_rejects_absent(self):
        assert has_district(ROME["districts"], "DISTRICT_SPACEPORT") is False

    def test_has_district_any_of(self):
        assert has_district(ROME["districts"], "DISTRICT_HANSA", "DISTRICT_CAMPUS")

    def test_has_district_no_args_is_false(self):
        assert has_district(ROME["districts"]) is False

    def test_naive_membership_is_the_bug(self):
        """把缺陷本身钉住:直接 `in` 判断在实机数据上就是 False。
        以后谁再这么写,这条会提醒他为什么不行。"""
        assert "DISTRICT_INDUSTRIAL_ZONE" not in ROME["districts"]


class TestAreaEffectAdviceKnowsWhatAlreadyExists:
    def test_existing_industrial_zone_is_recognised(self):
        """T317 实况:罗马和奥斯蒂亚都有工业区,不该再说"群内还没有"。"""
        out = "\n".join(get_area_effect_advice([ROME, OSTIA, AACHEN, PARIS]))
        if out:  # 只有成群时才出建议
            assert "还没有工业区" not in out

    def test_says_add_factory_when_zone_exists(self):
        out = "\n".join(get_area_effect_advice([ROME, OSTIA, AACHEN, PARIS]))
        if out:
            assert "已有工业区" in out

    def test_group_without_zone_still_gets_the_warning(self):
        """真的没有工业区时,那条建议必须还在 —— 修的是误报不是功能。"""
        bare = [
            {"name": f"城{i}", "x": 60 + i, "y": 50, "districts": []} for i in range(4)
        ]
        out = "\n".join(get_area_effect_advice(bare))
        if out:
            assert "还没有工业区" in out


class TestSpaceportDetection:
    """②号中招点:科学胜利收尾模块的核心输入。"""

    def test_spaceport_with_suffix_is_found(self):
        city = {"districts": ["DISTRICT_SPACEPORT@72,49", "DISTRICT_CAMPUS@70,49"]}
        assert has_district(city["districts"], "DISTRICT_SPACEPORT") is True

    def test_end_turn_uses_the_helper_not_raw_membership(self):
        import inspect

        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        i = src.index("spaceport_cities=ports")
        head = src[max(0, i - 900) : i]
        assert "has_district" in head, (
            "spaceport_cities 必须走归一 —— 直接 in 判断恒假,"
            "整个科学收尾模块会以为太空港没建"
        )

    def test_district_planner_uses_the_helper(self):
        import inspect

        from civ_mcp import district_planner as dp

        src = inspect.getsource(dp.get_area_effect_advice)
        assert "has_district" in src
        assert '"DISTRICT_INDUSTRIAL_ZONE" in' not in src


class TestResourceOnDistrictTile:
    """缺陷106:区域压住的资源不是"待改良"。"""

    def test_city_scan_checks_district_type(self):
        from civ_mcp.lua.cities import build_cities_query

        src = build_cities_query()
        i = src.index("local unimproved = {}")
        seg = src[i : i + 2000]
        assert "GetDistrictType" in seg, (
            "区域格上 GetImprovementType 恒为 -1,不查区域就会报 needs builder"
        )
        assert "distT < 0" in seg

    def test_empire_resources_marks_district_tiles(self):
        from civ_mcp.lua.map import build_empire_resources_query

        src = build_empire_resources_query()
        assert "GetDistrictType" in src
        i = src.index("local improved =")
        assert '"2"' in src[i : i + 400], "要有第三态,不能在已改良/未改良里二选一"

    def test_owned_resource_has_third_state(self):
        from civ_mcp.lua.models import OwnedResource

        r = OwnedResource(
            name="ALUMINUM", resource_class="strategic", improved=False, x=69, y=48
        )
        assert r.on_district is False, "默认不能是 True,否则真正待改良的会被吞掉"

    def test_parser_reads_the_third_state(self):
        from civ_mcp.lua.map import parse_empire_resources_response

        _stock, owned, _nearby, _lux = parse_empire_resources_response(
            [
                "OWNED|ALUMINUM|strategic|2|69,48",
                "OWNED|URANIUM|strategic|1|81,49",
                "OWNED|OIL|strategic|0|57,49",
            ]
        )
        by_name = {r.name: r for r in owned}
        assert by_name["ALUMINUM"].on_district is True
        assert by_name["ALUMINUM"].improved is False
        assert by_name["URANIUM"].improved is True
        assert by_name["URANIUM"].on_district is False
        assert by_name["OIL"].improved is False
        assert by_name["OIL"].on_district is False

    def test_render_does_not_call_for_a_builder(self):
        from civ_mcp.lua.models import OwnedResource
        from civ_mcp.narrate import narrate_empire_resources

        out = narrate_empire_resources(
            [],
            [
                OwnedResource(
                    name="ALUMINUM",
                    resource_class="strategic",
                    improved=False,
                    x=69,
                    y=48,
                    on_district=True,
                )
            ],
            [],
            {},
        )
        assert "needs builder" not in out
        assert "区域占用" in out

    def test_genuinely_unimproved_still_calls_for_a_builder(self):
        from civ_mcp.lua.models import OwnedResource
        from civ_mcp.narrate import narrate_empire_resources

        out = narrate_empire_resources(
            [],
            [
                OwnedResource(
                    name="OIL",
                    resource_class="strategic",
                    improved=False,
                    x=57,
                    y=49,
                )
            ],
            [],
            {},
        )
        assert "needs builder" in out


class TestConsistencyAcrossDataSources:
    def test_builder_tasks_already_skipped_districts(self):
        """get_builder_tasks 一直是对的 —— 这条钉住它,免得有人"统一"成错的那版。"""
        from civ_mcp.lua.units import build_builder_tasks_query

        src = build_builder_tasks_query()
        assert "GetDistrictType" in src
        assert "distIdx < 0" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
