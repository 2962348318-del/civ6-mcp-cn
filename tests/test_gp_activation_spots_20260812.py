"""缺陷 109:get_gp_advisor 把「职业 → 区域」的硬编码映射当成了激活条件。

2026-08-12 罗马局 T355。手上攒了六个没用掉的伟人,挨个问 advisor:

    get_gp_advisor(8)  ->  查尔斯·达尔文 (Scientist -> Campus):
      1. 亚琛 (65,49) — needs move (dist 22), yield 35
      2. 奥斯蒂亚 (69,47) — needs move (dist 17), yield 34
      ... 一共 13 个学院区城市,排得整整齐齐

而引擎自己说的是:

    CANNOT_ACTIVATE|查尔斯·达尔文 ... Requirements: 必须位于或邻近一个自然奇观。
    Valid tiles: 34,6 / 35,6 / 36,6 / 33,7 / 36,7

那 13 个学院区【一格都不合法】,真正的合法格一个都没显示。阿尔瓦·阿尔托
同样:advisor 说工业区,引擎说 Valid tiles 全是 DISTRICT_CITY_CENTER。

刺眼的是查询里【早就】读了引擎的权威合法格 —— GetActivationHighlightPlots()
的结果存进 validPlotSet,却只拿去给硬编码映射出来的城市打个 canAct 标记。
枚举范围仍然是猜的。于是对条件特殊的伟人:合法的不显示,显示的不合法。

两层伤害:

  · 渲染层把 canAct=false 写成 "needs move (dist 22)" —— 把"引擎说这里不能
    激活"翻译成了"走过去就行"。照着做就是走 22 格再吃一次 CANNOT_ACTIVATE。
  · "CAN ACTIVATE" 的真实语义是"这格在合法集合里",不是"现在就能按"。瓦特
    显示 CAN ACTIVATE,而他站在三格外,activate 当场被拒。

和缺陷 105(伦敦 🟢 但陆路走不到)同族:自信的排序 + 不成立的前置条件。
models.py 里那条 "萨根按 +3000 production 被挪去太空港工地白跑一趟
(2026-08-09 T288)" 记的是同一件事的另一条路径 —— 那次修的是
get_great_people 的 activation_req,这条路径漏下了。
"""

from __future__ import annotations

import pytest

from civ_mcp import narrate as nr
from civ_mcp.lua import great_people as lua_gp
from civ_mcp.lua.models import GPActivationSpot, GPAdvisorResult


def strip_lua_comments(code: str) -> str:
    """断言要看代码,不能被解释这条缺陷的中文注释喂饱 —— 本局已有三个测试
    栽在这上面。"""
    return "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("--"))


def _code() -> str:
    return strip_lua_comments(lua_gp.build_gp_advisor_query(8))


class TestEnumerationSourceIsTheEngine:
    def test_highlight_plots_drive_the_listing(self):
        code = _code()
        i = code.index("GetActivationHighlightPlots")
        # 合法格集合必须被真的遍历,而不是只拿来打标记
        assert "for _, pIdx in ipairs(plots)" in code[i:], "必须以引擎的合法格为枚举源"

    def test_no_longer_walks_cities_by_guessed_district(self):
        """旧枚举:遍历所有城市找 classToDistrict 猜出的那种区域。"""
        code = _code()
        assert "HasDistrict" not in code, (
            "按职业猜出来的区域不能当枚举源 —— 达尔文要的是自然奇观"
        )
        assert "GP_CITY|" not in code

    def test_class_map_survives_only_as_a_hint(self):
        """映射本身没错,错的是拿它当条件。留着给"通常去哪"用。"""
        code = _code()
        assert "usualDist" in code
        assert "GREAT_PERSON_CLASS_SCIENTIST" in code

    def test_empty_plot_set_is_reported_explicitly(self):
        """玛丽·利基实机就是空集合 —— 必须和"没有对口区域"区分开。"""
        assert "GP_NO_PLOTS" in _code()

    def test_gp_class_still_read_from_the_great_person_object(self):
        """缺陷87 的修复不能在这次重构里丢掉。"""
        code = _code()
        assert "GameInfo.GreatPersonClasses[gpObj:GetClass()]" in code
        assert "uInfo.GreatPersonClass" not in code

    def test_reports_natural_wonder_kind(self):
        code = _code()
        assert "NATURAL_WONDER" in code
        assert "NaturalWonder" in code

    def test_labels_tiles_adjacent_to_a_wonder(self):
        """达尔文的合法格多半是奇观【旁边】的空地,只报坐标看不出去干嘛。"""
        assert "邻近" in lua_gp.build_gp_advisor_query(8)

    def test_district_plots_also_get_the_adjacency_reason(self):
        """实机 T355:(92,38) 只报了"圣地" —— 它合法是因为隔壁的珠穆朗玛峰。
        区域名回答了"这是什么",没回答"为什么能在这儿激活"。"""
        code = _code()
        assert 'if kind ~= "NATURAL_WONDER" then' in code, (
            "邻接扫描不能只对空地做 —— 区域格同样需要说明合法的原因"
        )

    def test_marks_the_plot_the_gp_is_standing_on(self):
        code = _code()
        assert "onIt" in code

    def test_spot_count_is_capped_and_reported(self):
        code = _code()
        assert str(lua_gp.GP_SPOT_LIMIT) in code
        assert "GP_TOTAL|" in code


class TestParser:
    def test_parses_spots(self):
        res = lua_gp.parse_gp_advisor_response(
            [
                "GP_INFO|查尔斯·达尔文|GREAT_PERSON_CLASS_SCIENTIST|DISTRICT_CAMPUS|81|37|1",
                "GP_SPOT|34|6|22|0|NATURAL_WONDER|乞力马扎罗|-|-1|0|-1|-1",
                "GP_SPOT|35|6|23|0|TILE|邻近乞力马扎罗|-|-1|0|-1|-1",
                "GP_TOTAL|7",
            ]
        )
        assert res is not None
        assert len(res.spots) == 2
        assert res.total_spots == 7
        first = res.spots[0]
        assert (first.x, first.y) == (34, 6)
        assert first.kind == "NATURAL_WONDER"
        assert first.detail == "乞力马扎罗"
        assert first.on_plot is False
        assert first.city_name == ""

    def test_parses_on_plot_and_city(self):
        res = lua_gp.parse_gp_advisor_response(
            [
                "GP_INFO|詹姆斯·瓦特|GREAT_PERSON_CLASS_ENGINEER|DISTRICT_INDUSTRIAL_ZONE|72|51|1",
                "GP_SPOT|72|51|0|1|DISTRICT_INDUSTRIAL_ZONE|工业区|拉文那|10|67|-1|-1",
                "GP_TOTAL|5",
            ]
        )
        assert res is not None
        spot = res.spots[0]
        assert spot.on_plot is True
        assert spot.city_name == "拉文那"
        assert spot.city_yield == 67

    def test_no_plots_line_yields_empty_spots(self):
        res = lua_gp.parse_gp_advisor_response(
            [
                "GP_INFO|玛丽·利基|GREAT_PERSON_CLASS_SCIENTIST|DISTRICT_CAMPUS|72|50|1",
                "GP_NO_PLOTS",
            ]
        )
        assert res is not None
        assert res.spots == []
        assert res.total_spots == 0


def _result(
    spots: list[GPActivationSpot],
    name="查尔斯·达尔文",
    can_now: bool | None = None,
) -> GPAdvisorResult:
    return GPAdvisorResult(
        gp_name=name,
        gp_class="GREAT_PERSON_CLASS_SCIENTIST",
        target_district="DISTRICT_CAMPUS",
        gp_x=81,
        gp_y=37,
        charges=1,
        cities=[],
        spots=spots,
        total_spots=len(spots),
        can_activate_now=can_now,
    )


class TestHighlightIsCandidateNotPermission:
    """修枚举源之后剩下的第三层:高亮格【不等于】许可。

    2026-08-12 实测,阿尔瓦·阿尔托站在拉文那市中心(它就在自己的 16 个
    高亮格里),UnitManager.CanStartCommand 返回 false,failTable 是 nil ——
    引擎拒绝且不给理由。传 PARAM_X/PARAM_Y 也不改变结果,所以逐格预测做
    不到,只能如实报告"脚下这格现在放不放行"。

    这条测试是给修复本身设的岗:换掉错误的枚举源之后,新列表的第一行会
    带 ★ 标记,如果照旧写"现在就能激活",等于用一个新的假绿灯换掉了旧的。
    """

    def test_query_asks_can_start_command(self):
        code = _code()
        assert "CanStartCommand" in code
        assert "GP_CAN_NOW|" in code

    def test_prophets_use_the_religion_operation(self):
        """大预言家走的是另一条命令 —— 用通用命令测会恒为 false。"""
        code = _code()
        i = code.index("GP_CAN_NOW")
        seg = code[max(0, i - 1200) : i]
        assert "UNITOPERATION_FOUND_RELIGION" in seg
        assert "GREAT_PERSON_CLASS_PROPHET" in seg

    def test_parser_reads_the_flag(self):
        res = lua_gp.parse_gp_advisor_response(
            [
                "GP_INFO|阿尔瓦·阿尔托|GREAT_PERSON_CLASS_ENGINEER|DISTRICT_INDUSTRIAL_ZONE|72|50|1",
                "GP_CAN_NOW|false",
                "GP_SPOT|72|50|0|1|DISTRICT_CITY_CENTER|市中心|拉文那|10|67|-1|-1",
                "GP_TOTAL|16",
            ]
        )
        assert res is not None
        assert res.can_activate_now is False

    def test_unknown_flag_stays_none(self):
        res = lua_gp.parse_gp_advisor_response(
            [
                "GP_INFO|詹姆斯·瓦特|GREAT_PERSON_CLASS_ENGINEER|DISTRICT_INDUSTRIAL_ZONE|72|51|1",
                "GP_SPOT|72|51|0|1|DISTRICT_INDUSTRIAL_ZONE|工业区|拉文那|10|67|-1|-1",
            ]
        )
        assert res is not None
        assert res.can_activate_now is None

    def test_refused_plot_does_not_claim_ready(self):
        spot = GPActivationSpot(
            x=72,
            y=50,
            distance=0,
            on_plot=True,
            kind="DISTRICT_CITY_CENTER",
            detail="市中心",
            city_name="拉文那",
            city_yield=67,
        )
        out = nr.narrate_gp_advisor(_result([spot], can_now=False))
        assert "拒绝" in out
        assert "现在就能激活" not in out, "引擎拒绝的格不能再标成可以激活"

    def test_allowed_plot_says_so(self):
        spot = GPActivationSpot(
            x=72, y=51, distance=0, on_plot=True, kind="DISTRICT_INDUSTRIAL_ZONE"
        )
        out = nr.narrate_gp_advisor(_result([spot], can_now=True))
        assert "现在就能激活" in out

    def test_listing_discloses_candidate_semantics(self):
        spot = GPActivationSpot(x=76, y=27, distance=10, on_plot=False, kind="TILE")
        out = nr.narrate_gp_advisor(_result([spot]))
        assert "候选" in out


class TestNarration:
    def test_no_spots_says_nowhere_not_needs_move(self):
        out = nr.narrate_gp_advisor(_result([], name="玛丽·利基"))
        assert "无处可激活" in out
        assert "needs move" not in out
        assert "unit_action" in out, "要给出读引擎原文的下一步"

    def test_never_says_needs_move(self):
        """把误导文案钉死:"距离 N 格"是事实,"needs move"是承诺。"""
        out = nr.narrate_gp_advisor(
            _result(
                [
                    GPActivationSpot(
                        x=34, y=6, distance=22, on_plot=False, kind="NATURAL_WONDER"
                    )
                ]
            )
        )
        assert "needs move" not in out
        assert "距离 22 格" in out

    def test_standing_on_the_plot_is_called_out(self):
        """★ 只说"人在这儿",不说"能按" —— 放不放行看 can_activate_now。"""
        out = nr.narrate_gp_advisor(
            _result(
                [
                    GPActivationSpot(
                        x=72,
                        y=51,
                        distance=0,
                        on_plot=True,
                        kind="DISTRICT_INDUSTRIAL_ZONE",
                        detail="工业区",
                        city_name="拉文那",
                        city_yield=67,
                    )
                ]
            )
        )
        assert "★ 已站在这格" in out
        assert "现在就能激活" not in out, "判定未知时不能承诺"

    def test_distance_outranks_yield(self):
        """22 格外的高产城市不如 1 格外的普通城市 —— 旧版按 yield 排序。"""
        far_rich = GPActivationSpot(
            x=65,
            y=49,
            distance=22,
            on_plot=False,
            kind="DISTRICT_CAMPUS",
            city_name="亚琛",
            city_yield=35,
        )
        near_poor = GPActivationSpot(
            x=86,
            y=44,
            distance=8,
            on_plot=False,
            kind="DISTRICT_CAMPUS",
            city_name="科斯特罗姆斯卡亚",
            city_yield=24,
        )
        out = nr.narrate_gp_advisor(_result([far_rich, near_poor]))
        assert out.index("科斯特罗姆斯卡亚") < out.index("亚琛")

    def test_wonder_detail_is_shown(self):
        out = nr.narrate_gp_advisor(
            _result(
                [
                    GPActivationSpot(
                        x=35,
                        y=6,
                        distance=23,
                        on_plot=False,
                        kind="TILE",
                        detail="邻近乞力马扎罗",
                    )
                ]
            )
        )
        assert "邻近乞力马扎罗" in out

    def test_truncation_is_disclosed(self):
        res = _result(
            [
                GPActivationSpot(x=34, y=6, distance=22, on_plot=False, kind="TILE")
                for _ in range(3)
            ]
        )
        res.total_spots = 40
        out = nr.narrate_gp_advisor(res)
        assert "40" in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
