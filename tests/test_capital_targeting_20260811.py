"""缺陷 93:统治路线下的军力窗口把目标定在【不计分的城】上。

2026-08-11 罗马局 T268 实锤。同一份简报里并排出现两句话:

  「统治胜利判据:占领所有其它文明的【原始首都】(不是所有城市)」
  「军力窗口 2.54× —— 目标定 挪威 的 奥勒松(82,61),人口 1,无城墙」

奥勒松不是挪威首都(尼德罗斯才是)。打下来统治计数一个不涨。旧排序键是
(有无城墙, 人口),挑的是【最好打的】城,不是【计分的】城。

这比"拖着不兑现"更贵:窗口是有限的,拿它去打一座不计分的城,窗口照样
关闭、战争疲劳照样累积、外交惩罚照样付,统治进度纹丝不动。复盘一直把
两局失败归因于犹豫,现在看目标选错同样有份。

数据侧也缺一半:VisibleCity 根本没有首都字段,所以排序就算想考虑也无从
考虑。城市对象上没有 IsCapital/IsOriginalCapital(实测两个 pcall 都拿不到),
可用的只有 GetCities():GetCapitalCity() 与 city:GetOriginalOwner() ——
两者相交才是"还没失守的原始首都",也就是打下来能让计数 +1 的那一座。
"""

from __future__ import annotations

import pytest

from civ_mcp import playbook
from civ_mcp.lua.models import VisibleCity


def _city(name, x, y, pop, walls=False, cap=False) -> VisibleCity:
    return VisibleCity(
        name=name,
        x=x,
        y=y,
        population=pop,
        has_walls=walls,
        is_original_capital=cap,
    )


class _Civ:
    def __init__(self, name, cities, at_war=False):
        self.civ_name = name
        self.visible_cities = cities
        self.is_at_war = at_war


# T268 的真实盘面:挪威一座 pop 1 的无墙小城,英国一座 pop 13 的无墙首都
AALESUND = _city("奥勒松", 82, 61, 1)
LONDON = _city("伦敦", 60, 51, 13, cap=True)


class TestTargetOrdering:
    def test_domination_prefers_the_scoring_capital(self):
        ranked = playbook._rank_war_targets(
            [_Civ("挪威", [AALESUND]), _Civ("英国", [LONDON])], domination=True
        )
        assert ranked[0][4] is LONDON, (
            "统治路线下 pop 1 的无墙小城不该压过 pop 13 的原始首都 —— "
            "前者打下来计数不涨"
        )

    def test_non_domination_keeps_the_easy_target(self):
        """非统治路线下首都只是"又硬又远的城",没有额外价值。"""
        ranked = playbook._rank_war_targets(
            [_Civ("挪威", [AALESUND]), _Civ("英国", [LONDON])], domination=False
        )
        assert ranked[0][4] is AALESUND

    def test_unwalled_capital_beats_walled_capital(self):
        walled_cap = _city("莫斯科", 77, 41, 8, walls=True, cap=True)
        ranked = playbook._rank_war_targets(
            [_Civ("俄罗斯", [walled_cap]), _Civ("英国", [LONDON])], domination=True
        )
        assert ranked[0][4] is LONDON

    def test_capitals_outrank_even_easier_cities(self):
        """首都键压过城墙键 —— 一座有墙的首都仍然比无墙普通城优先。"""
        walled_cap = _city("德里", 90, 38, 6, walls=True, cap=True)
        easy = _city("小城", 1, 1, 1)
        ranked = playbook._rank_war_targets(
            [_Civ("印度", [walled_cap]), _Civ("某国", [easy])], domination=True
        )
        assert ranked[0][4] is walled_cap

    def test_cities_at_war_are_excluded(self):
        ranked = playbook._rank_war_targets(
            [_Civ("挪威", [AALESUND], at_war=True)], domination=True
        )
        assert ranked == []

    def test_empty_input(self):
        assert playbook._rank_war_targets([], domination=True) == []


class TestVisibleCityModel:
    def test_field_exists_and_defaults_false(self):
        """默认 False —— 缺数据时不能谎报某城是首都。"""
        assert _city("x", 0, 0, 1).is_original_capital is False

    def test_parser_reads_the_capital_flag(self):
        from civ_mcp.lua.diplomacy import parse_diplomacy_response

        lines = [
            "CIV|7|英国|维多利亚|1|0|PEACE|0|1|0|0|0|0",
            "ECITY|7|伦敦|60,51|13|100|0.0|100|60|1",
            "ECITY|7|利物浦|60,55|6|100|0.0|0|40|0",
        ]
        civs = parse_diplomacy_response(lines)
        cities = {c.name: c for c in civs[0].visible_cities}
        assert cities["伦敦"].is_original_capital
        assert not cities["利物浦"].is_original_capital

    def test_legacy_nine_field_line_does_not_claim_capital(self):
        """旧格式没有首都位 —— 必须默认 False,不能猜。"""
        from civ_mcp.lua.diplomacy import parse_diplomacy_response

        civs = parse_diplomacy_response(
            [
                "CIV|7|英国|维多利亚|1|0|PEACE|0|1|0|0|0|0",
                "ECITY|7|伦敦|60,51|13|100|0.0|100|60",
            ]
        )
        assert not civs[0].visible_cities[0].is_original_capital


class TestLuaProbe:
    @staticmethod
    def _src() -> str:
        from civ_mcp.lua import diplomacy

        src = diplomacy.build_diplomacy_query()
        return "\n".join(
            ln for ln in src.split("\n") if not ln.strip().startswith("--")
        )

    def test_uses_the_apis_that_actually_exist(self):
        src = self._src()
        assert "GetCapitalCity()" in src
        assert "GetOriginalOwner()" in src

    def test_does_not_use_the_nonexistent_city_methods(self):
        """实测:City 上没有 IsCapital / IsOriginalCapital,两个 pcall 都拿不到。"""
        src = self._src()
        assert "IsOriginalCapital" not in src
        assert "ec:IsCapital" not in src

    def test_capital_probe_is_pcall_wrapped(self):
        src = self._src()
        assert "pcall(function()" in src[: src.index("GetCapitalCity()")]

    def test_capital_lookup_is_hoisted_out_of_the_city_loop(self):
        """每座城查一次首都是浪费 —— 一个文明只有一个首都。"""
        src = self._src()
        assert src.index("GetCapitalCity()") < src.index(
            "for _, ec in Players[i]:GetCities():Members() do"
        )


class TestBriefingHonesty:
    def test_non_capital_target_is_flagged_under_domination(self):
        """挑不到首都时必须明说这一仗不计分 —— 否则读简报的人会以为它计分。"""
        import inspect

        src = inspect.getsource(playbook.war_campaign)
        assert "统治计数不涨" in src

    def test_capital_target_is_labelled(self):
        import inspect

        src = inspect.getsource(playbook.war_campaign)
        assert "计统治 +1" in src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
