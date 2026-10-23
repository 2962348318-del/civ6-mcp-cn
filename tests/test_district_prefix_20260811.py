"""缺陷 99:已建的建筑被判成缺失 —— 比较时前缀不统一。

2026-08-11 罗马局 T284 实锤。简报打出两条【核心】建议:

    【核心】罗马 的 INDUSTRIAL_ZONE 是空的 —— 缺 WORKSHOP、FACTORY、POWER_PLANT
    【核心】奥斯蒂亚 的 INDUSTRIAL_ZONE 是空的 —— 缺 WORKSHOP、...

而同一回合 get_cities 白纸黑字列着两座城的 Buildings 里都有 WORKSHOP 和
FACTORY。两条都是假的。

根因在一行:

    have = {str(b).split("@")[0] for b in c.get("buildings")}   # {"WORKSHOP", ...}
    ...
    if btype in have:      # btype 是 "BUILDING_WORKSHOP" —— 永不相等

city 的 buildings 列表在 Lua 侧就把 BUILDING_ 前缀剥掉了,而 chain 里存的是
全名。于是【每一栋已建的建筑都被判成缺失】,这个模块从写出来就一直在报
假缺口。最能说明问题的是下面那行输出:

    missing.append(btype.replace("BUILDING_", ""))

作者知道两边格式不同 —— 只是在【输出】那行统一了,没在【比较】那行统一。

为什么拖到 T284 才发现:这条建议长期落在"低优先级建议已省略"里,直到
锁定 SCIENCE 路线把工业区权重提到【核心】,才被顶到简报正文。
"""

from __future__ import annotations

import pytest

from civ_mcp.district_planner import empty_district_advice

TECHS = {
    "TECH_APPRENTICESHIP",
    "TECH_INDUSTRIALIZATION",
    "TECH_ELECTRICITY",
    "TECH_WRITING",
    "TECH_EDUCATION",
}


def _rome() -> dict:
    """T284 的真实罗马:工业区里工坊和工厂都已建成。"""
    return {
        "name": "罗马",
        "districts": ["DISTRICT_INDUSTRIAL_ZONE@66,47"],
        "buildings": ["MONUMENT", "PALACE", "WORKSHOP", "FACTORY", "SEWER"],
    }


class TestBuiltBuildingsAreNotReportedMissing:
    def test_workshop_and_factory_are_recognised(self):
        out = empty_district_advice(
            [_rome()], researched_techs=TECHS, victory_type="SCIENCE"
        )
        joined = " ".join(out)
        assert "WORKSHOP" not in joined, "工坊已建成,不该报缺"
        assert "FACTORY" not in joined or "POWER" in joined, (
            "工厂已建成 —— 只有真正缺的发电厂可以出现"
        )

    def test_fully_built_district_produces_no_row(self):
        city = {
            "name": "满配城",
            "districts": ["DISTRICT_INDUSTRIAL_ZONE@1,1"],
            "buildings": ["WORKSHOP", "ELECTRONICS_FACTORY", "FACTORY", "POWER_PLANT"],
        }
        assert (
            empty_district_advice(
                [city], researched_techs=TECHS, victory_type="SCIENCE"
            )
            == []
        )

    def test_genuinely_empty_district_still_reported(self):
        """修复不能把正确的行为一起关掉 —— 真空的区域照旧要报。"""
        city = {
            "name": "空壳城",
            "districts": ["DISTRICT_INDUSTRIAL_ZONE@1,1"],
            "buildings": ["MONUMENT"],
        }
        out = empty_district_advice(
            [city], researched_techs=TECHS, victory_type="SCIENCE"
        )
        assert out and "WORKSHOP" in " ".join(out)

    def test_partially_built_reports_only_the_gap(self):
        city = {
            "name": "半配城",
            "districts": ["DISTRICT_INDUSTRIAL_ZONE@1,1"],
            "buildings": ["WORKSHOP"],
        }
        out = empty_district_advice(
            [city], researched_techs=TECHS, victory_type="SCIENCE"
        )
        joined = " ".join(out)
        assert "FACTORY" in joined
        assert "缺 WORKSHOP" not in joined

    def test_full_prefix_form_also_matches(self):
        """有些数据源可能带全名 —— 两种写法都该认。"""
        city = {
            "name": "全名城",
            "districts": ["DISTRICT_INDUSTRIAL_ZONE@1,1"],
            "buildings": ["BUILDING_WORKSHOP", "BUILDING_FACTORY"],
        }
        out = empty_district_advice(
            [city], researched_techs=TECHS, victory_type="SCIENCE"
        )
        assert "WORKSHOP" not in " ".join(out)

    def test_coordinate_suffix_is_stripped_too(self):
        city = {
            "name": "带坐标城",
            "districts": ["DISTRICT_INDUSTRIAL_ZONE@1,1"],
            "buildings": ["WORKSHOP@2,2", "FACTORY@3,3"],
        }
        out = empty_district_advice(
            [city], researched_techs=TECHS, victory_type="SCIENCE"
        )
        assert "WORKSHOP" not in " ".join(out)


class TestComparisonNormalisesBothSides:
    def test_source_compares_short_names(self):
        import inspect

        src = inspect.getsource(empty_district_advice)
        assert "short = btype.replace" in src
        assert "if short in have:" in src, (
            "比较必须用统一后的短名 —— 拿全名去比短名集合是恒不相等"
        )

    def test_have_set_is_normalised(self):
        import inspect

        src = inspect.getsource(empty_district_advice)
        i = src.index("have = {")
        assert 'replace("BUILDING_", "")' in src[i : i + 200]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
