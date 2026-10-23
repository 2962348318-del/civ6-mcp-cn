"""缺陷 100:试价把"价钱公道"报成"会成交"。

2026-08-11 罗马局 T286 实锤,而且可复现:

    propose_trade(印度, request=COTTON, offer_gold=300, mode="test")
        → "AI response: ACCEPTABLE — this deal would be accepted as-is"
    propose_trade(同一笔, mode="send")
        → "REJECTED|Trade rejected with 印度"
    再 test 一次 → 仍然 ACCEPTABLE

试价走的是 DealManager.SendWorkingDeal(EQUALIZE),它回答的是【这笔交易
数值上公不公平】,不是【对方愿不愿意跟你做生意】。印度对我是 DENOUNCED
−53 —— Civ6 里关系差到一定程度的 AI 会拒绝一切交易,价格再好看也拒。

旧文案 "this deal would be accepted as-is" 是一句无条件的成交预测,而
底层根本没测过意愿。照着它下单就是白跑一趟,更糟的是它会让人反复加价
去解决一个跟价格无关的问题。

修法不是换算法(引擎没暴露"意愿"接口),是把话说准:报 PRICE_FAIR,
并点明 send 被拒时该往关系上找原因,而不是往出价上找。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.models import TestTradeItem, TestTradeResult
from civ_mcp.narrate import narrate_test_trade


def _result(proposed, counter, rejected=False) -> TestTradeResult:
    return TestTradeResult(
        other_player_id=9,
        other_civ_name="印度",
        proposed=proposed,
        counter=counter,
        rejected=rejected,
    )


def _item(side, itype="GOLD", amount=300, duration=0) -> TestTradeItem:
    return TestTradeItem(
        side=side,
        item_type=itype,
        amount=amount,
        duration=duration,
        value_id="",
        subtype_id="",
    )


class TestEqualMeansPriceNotConsent:
    def test_matching_counter_is_not_called_accepted(self):
        """还价和提案一致,只说明价钱谈拢了。"""
        same = [_item("US"), _item("THEM", "RESOURCE", 1, 30)]
        out = narrate_test_trade(_result(same, list(same)))
        assert "would be accepted as-is" not in out, (
            "EQUALIZE 没测过意愿,不能做无条件的成交预测"
        )
        assert "PRICE_FAIR" in out

    def test_says_price_is_fair(self):
        same = [_item("US")]
        out = narrate_test_trade(_result(same, list(same)))
        assert "等价" in out or "公平" in out

    def test_warns_that_relations_can_still_block(self):
        same = [_item("US")]
        out = narrate_test_trade(_result(same, list(same)))
        assert "关系" in out
        assert "不等于" in out

    def test_points_the_caller_at_the_right_fix(self):
        """send 被拒时,该改关系不该改价格 —— 这句必须写出来,
        否则会有人反复加价去解决一个跟价格无关的问题。"""
        same = [_item("US")]
        out = narrate_test_trade(_result(same, list(same)))
        assert "不是出价问题" in out


class TestOtherBranchesUnchanged:
    def test_outright_rejection_still_reported(self):
        out = narrate_test_trade(_result([_item("US")], [], rejected=True))
        assert "REJECTED" in out

    def test_counter_offer_still_shown(self):
        proposed = [_item("US", "GOLD", 100)]
        counter = [_item("US", "GOLD", 500)]
        out = narrate_test_trade(_result(proposed, counter))
        assert "counter-offer" in out or "还价" in out
        assert "PRICE_FAIR" not in out, "还价不一致时不是价钱谈拢"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
