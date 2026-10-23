"""缺陷 114:和平谈判要不到城市 —— 交战中连提案都发不出去。

2026-08-12 罗马局。用户提出「和他谈判,用和解换所有城市和资源」,这是 Civ6 的
标准玩法:劣势方会拿城市求停战。查下来这条路在工具层是断的:

  · propose_trade 第一行 `if IsAtWarWith(target) then bail AT_WAR` —— 交战中
    直接拒绝。而「用和解换城」【只能】在交战时谈,停战本身就是我方的对价。
  · propose_peace 只有白和平(单纯停战),没有任何索要参数。
  · 工具签名里根本没有城市相关的参数。

而引擎和 Lua 层都是齐的:

    DealItemTypes.CITIES = -746262857
    DealAgreementTypes.MAKE_PEACE = 547027585
    _lua_deal_item 的 CITY 分支:AddItemOfType(CITIES) + SetValueType(city_id)

也就是说底座早就铺好,只差工具签名没把它接出来。

代价在 T456 看得最清楚:刚果、英国、巴西三家【主动】提的和约里都带着 City
条目(刚果那份列了 12 个),我方一律收得下 —— 却一次也提不出同样的条件。
整局的和平谈判因此只有"对方给什么就拿什么",没有"我要什么"。
"""

from __future__ import annotations

import inspect

import pytest

from civ_mcp.lua.diplomacy import (
    _deal_has_peace,
    build_propose_trade,
    build_test_trade,
)


PEACE = {"type": "AGREEMENT", "subtype": "MAKE_PEACE"}
CITY = {"type": "CITY", "city_id": 1310739}
GOLD = {"type": "GOLD", "amount": 100, "duration": 0}


class TestWarGate:
    def test_peace_deal_is_allowed_at_war(self):
        """带停战条款的提案必须能在交战中发出 —— 这正是和约的用途。"""
        code = build_propose_trade(10, [PEACE], [PEACE, CITY])
        assert "ERR:AT_WAR" not in code

    def test_plain_trade_still_blocked_at_war(self):
        """不带停战的普通交易照旧拦 —— 引擎本来也不会受理。"""
        code = build_propose_trade(10, [GOLD], [])
        assert "ERR:AT_WAR" in code

    def test_block_message_names_the_parameter(self):
        code = build_propose_trade(10, [GOLD], [])
        i = code.index("ERR:AT_WAR")
        seg = code[i : i + 300]
        assert "make_peace" in seg, "拦截要给出解法,不能只说不行"

    def test_test_mode_follows_the_same_rule(self):
        """试价尤其要能用 —— 不然没法在提之前知道 AI 肯不肯割地。"""
        assert "ERR:AT_WAR" not in build_test_trade(10, [PEACE], [PEACE, CITY])
        assert "ERR:AT_WAR" in build_test_trade(10, [GOLD], [])

    def test_helper_detects_peace_on_either_side(self):
        assert _deal_has_peace([PEACE], []) is True
        assert _deal_has_peace([], [PEACE]) is True
        assert _deal_has_peace([GOLD], [CITY]) is False

    def test_helper_ignores_other_agreements(self):
        ob = {"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}
        assert _deal_has_peace([ob], [ob]) is False


class TestCityItemReachesTheDeal:
    def test_city_item_emits_cities_type(self):
        code = build_propose_trade(10, [], [CITY])
        assert "DealItemTypes.CITIES" in code
        assert "1310739" in code

    def test_city_uses_set_value_type(self):
        """城市靠 SetValueType(cityID) 指定,不是 SetAmount。"""
        code = build_propose_trade(10, [], [CITY])
        i = code.index("DealItemTypes.CITIES")
        seg = code[i : i + 200]
        assert "SetValueType" in seg


class TestToolSurface:
    def _src(self):
        from civ_mcp import server

        fn = getattr(server.propose_trade, "fn", server.propose_trade)
        return inspect.getsource(fn)

    def test_request_cities_parameter_exists(self):
        assert "request_cities: str" in self._src()

    def test_offer_cities_parameter_exists(self):
        assert "offer_cities: str" in self._src()

    def test_make_peace_parameter_exists(self):
        assert "make_peace: bool" in self._src()

    def test_peace_is_attached_to_both_sides(self):
        """停战是双方条款,只挂一侧引擎不认。"""
        src = self._src()
        i = src.index("if make_peace:")
        seg = src[i : i + 400]
        assert seg.count("MAKE_PEACE") >= 2
        assert "offer_items.append" in seg and "request_items.append" in seg

    def test_bad_city_id_is_rejected_with_a_readable_error(self):
        src = self._src()
        assert "不是城市 ID" in src

    def test_docstring_tells_you_to_test_first(self):
        from civ_mcp import server

        fn = getattr(server.propose_trade, "fn", server.propose_trade)
        doc = inspect.getdoc(fn) or ""
        assert "make_peace" in doc
        assert "request_cities" in doc
        assert 'mode="test"' in doc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
