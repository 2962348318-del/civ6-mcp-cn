"""缺陷 101:份数后缀让奢侈品建议同时误报和漏报,而且把结论指反。

2026-08-11 罗马局 T286 实锤。同一份 get_trade_options 输出自相矛盾:

    Our tradeable resources:
      Luxuries: 柑橘 x3, 水银 x5, 丝绸 x2, 茶叶 x2, 松露 x6, 葡萄酒 x3
    ...
    · 你完全没有柑橘(可供 4 座城 各 +1 宜居度),西班牙 有 —— 值得开价买。

上半区说我有 3 份柑橘,下半区说我一份都没有。同时西班牙的棉花 x2、
熏香 x2 —— 我真正缺的那两种 —— 一个字都没提。

根因是一行标签拼接:

    label = f"{name} x{our_amt}" if our_amt > 1 else name

持有 1 份发裸名,2 份以上带 " xN" 后缀。normalize 只认裸名,带后缀就返回
None,调用方退回用原字符串当键,于是【持有越多越匹配不上】。误报和漏报
是同一个原因的两面:我方多份的被当成"没有"(该卖的说成该买),对方多份的
落不进 missing(该买的一个字没提)。

顺带修掉一个一直静默的功能:server.py 的注释写着"我方奢侈份数在这里未知,
全记 1 份,盈余段自然静默",而份数其实就印在标签里,只是没人去读。本模块
收益最高的一段(重复奢侈只能卖,第 2 份起对宜居度零贡献)因此从来没有过
数据、一次也没出过声。
"""

from __future__ import annotations

import pytest

from civ_mcp import resource_trade as rt


class TestNormalizeStripsCountSuffix:
    def test_chinese_name_with_count(self):
        assert rt.normalize("柑橘 x3") == "RESOURCE_CITRUS"

    def test_bare_name_still_works(self):
        assert rt.normalize("柑橘") == "RESOURCE_CITRUS"

    def test_english_name_with_count(self):
        assert rt.normalize("Citrus x2") == "RESOURCE_CITRUS"

    def test_resource_type_with_count(self):
        assert rt.normalize("RESOURCE_COTTON x2") == "RESOURCE_COTTON"

    def test_the_two_i_actually_lacked(self):
        """T286 真正缺的两种,当时因为带 x2 后缀被整个漏掉。"""
        assert rt.normalize("棉花 x2") == "RESOURCE_COTTON"
        assert rt.normalize("熏香 x2") == "RESOURCE_INCENSE"

    def test_non_luxury_still_none(self):
        assert rt.normalize("铁 x2") is None

    def test_empty_and_junk(self):
        assert rt.normalize("") is None
        assert rt.normalize("x3") is None


class TestParseCount:
    def test_reads_the_number(self):
        assert rt.parse_count("柑橘 x3") == 3
        assert rt.parse_count("松露 x6") == 6

    def test_bare_name_is_one(self):
        assert rt.parse_count("柑橘") == 1

    def test_tolerates_spacing_and_case(self):
        assert rt.parse_count("Citrus X2") == 2
        assert rt.parse_count("柑橘x4") == 4

    def test_empty_is_one(self):
        assert rt.parse_count("") == 1


class TestAdviceGetsItRight:
    def _ctx(self):
        return rt.ResourceTradeContext(
            my_luxuries={
                (rt.normalize(x) or x): rt.parse_count(x)
                for x in ["柑橘 x3", "水银 x5", "松露 x6"]
            },
            rivals=[
                rt.RivalHoldings(
                    player_id=11,
                    civ_name="西班牙",
                    luxuries=("柑橘", "棉花 x2", "熏香 x2", "葡萄酒 x2"),
                )
            ],
        )

    def test_does_not_tell_me_to_buy_what_i_have_three_of(self):
        out = " ".join(rt.get_resource_trade_advice(self._ctx()))
        assert "你完全没有柑橘" not in out

    def test_surfaces_what_i_actually_lack(self):
        out = " ".join(rt.get_resource_trade_advice(self._ctx()))
        assert "棉花" in out or "熏香" in out, "真正缺的品种必须出现在建议里"

    def test_surplus_advice_finally_has_data(self):
        """卖富余是本模块收益最高的一段,此前因为份数恒为 1 而永远静默。"""
        out = " ".join(rt.get_resource_trade_advice(self._ctx()))
        assert "零贡献" in out, "手里 3 份柑橘 5 份水银 6 份松露,该提示卖掉"


class TestServerWiring:
    def test_server_parses_counts_instead_of_hardcoding_one(self):
        import inspect

        from civ_mcp import server

        fn = getattr(server.get_trade_options, "fn", server.get_trade_options)
        src = inspect.getsource(fn)
        assert "rt.parse_count(x)" in src
        assert "or x): 1 for x in" not in src, "份数就印在标签里,不该硬编码成 1"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
