"""P6 简报重构 —— 常驻上下文豁免降噪 + 受益对象存在性校验。

两条都来自校准局实测:
  · T15:8 条建议休眠后,简报里整个「主线」段消失了
  · 我方零骑兵时推荐「演习」(加骑乘产能);使者恒为 0 却连推 60 回合
"""

from __future__ import annotations

from dataclasses import dataclass

from civ_mcp import advice as advice_mod
from civ_mcp import end_turn
from civ_mcp.advice import Advice, Confidence, Priority


@dataclass
class U:
    unit_type: str
    name: str = "单位"


def _mk(
    module="policy",
    action="换上演习",
    priority=Priority.OPTIMIZE,
    standing=False,
    reason="",
):
    return Advice(
        module=module,
        priority=priority,
        action=action,
        reason=reason,
        standing=standing,
        confidence=Confidence.RECOMMENDED,
    )


# ===========================================================================
# 常驻上下文不参与降噪
# ===========================================================================


class TestStandingContext:
    def _run(self, item, turns=6):
        st = {"items": {}, "era": None, "expl_last": -999}
        survived = 0
        for t in range(1, turns + 1):
            kept, _slept = end_turn._filter_noise([item], st, t)
            survived += len(kept)
        return survived

    def test_ordinary_advice_gets_suppressed_after_repeats(self):
        assert self._run(_mk()) < 6

    def test_standing_context_is_never_suppressed(self):
        """科技链每回合长一样,那不是"又提醒了一次",是坐标系。"""
        assert self._run(_mk(module="tech_path", standing=True)) == 6

    def test_survival_still_exempt(self):
        assert self._run(_mk(priority=Priority.SURVIVAL)) == 6

    def test_mainline_modules_are_marked_standing_in_the_registry(self):
        for module in (
            "tech_path",
            "pace",
            "lategame",
            "dvp",
            "science_endgame",
            "culture_endgame",
        ):
            assert advice_mod.LEGACY_REGISTRY[module].standing, module

    def test_from_legacy_propagates_the_flag(self):
        items = advice_mod.from_legacy("tech_path", ["⚠️ 下一步研究 采矿"])
        assert items and items[0].standing
        noisy = advice_mod.from_legacy("resource_trade", ["💡 卖丝绸"])
        assert noisy and not noisy[0].standing


# ===========================================================================
# 受益对象存在性校验
# ===========================================================================


class TestBeneficiaryVerification:
    def test_policy_card_dropped_when_no_cavalry(self):
        kept, dropped = advice_mod.verify_beneficiaries(
            [_mk(action="换上「演习」政策卡")], {"cavalry": 0}
        )
        assert kept == [] and len(dropped) == 1

    def test_policy_card_kept_when_cavalry_exists(self):
        items = [_mk(action="换上「演习」政策卡")]
        kept, dropped = advice_mod.verify_beneficiaries(items, {"cavalry": 3})
        assert kept == items and not dropped

    def test_unobserved_fact_never_drops_anything(self):
        """没观测到 ≠ 不存在。缺 key 一律放行。"""
        items = [_mk(action="换上「演习」政策卡")]
        kept, dropped = advice_mod.verify_beneficiaries(items, {"naval": 0})
        assert kept == items and not dropped

    def test_no_facts_at_all_is_a_passthrough(self):
        items = [_mk(action="换上「演习」政策卡")]
        assert advice_mod.verify_beneficiaries(items, None)[0] == items

    def test_envoy_advice_is_annotated_not_dropped(self):
        """使者建议本身没错,错在从不说使者从哪来。"""
        kept, dropped = advice_mod.verify_beneficiaries(
            [_mk(module="city_state_advisor", action="给普雷斯拉夫投 2 个使者")],
            {"envoys": 0},
        )
        assert not dropped and len(kept) == 1
        assert any("使者来源" in d for d in kept[0].detail)

    def test_annotation_does_not_duplicate_when_supply_exists(self):
        kept, _ = advice_mod.verify_beneficiaries(
            [_mk(action="给普雷斯拉夫投 2 个使者")], {"envoys": 4}
        )
        assert kept[0].detail == ()

    def test_reason_text_also_counts_as_a_hit(self):
        kept, dropped = advice_mod.verify_beneficiaries(
            [_mk(action="切政策卡", reason="演习能加骑乘单位产能")], {"cavalry": 0}
        )
        assert dropped and not kept

    def test_with_detail_does_not_mutate_the_original(self):
        original = _mk()
        copy = original.with_detail("补一行")
        assert original.detail == () and copy.detail == ("补一行",)


# ===========================================================================
# 事实采集
# ===========================================================================


class TestBeneficiaryFacts:
    def test_counts_cavalry_siege_naval_and_religious(self):
        facts = end_turn._beneficiary_facts(
            [
                U("UNIT_HORSEMAN"),
                U("UNIT_KNIGHT"),
                U("UNIT_CATAPULT"),
                U("UNIT_GALLEY"),
                U("UNIT_MISSIONARY"),
                U("UNIT_WARRIOR"),
            ]
        )
        assert facts["cavalry"] == 2
        assert facts["siege"] == 1
        assert facts["naval"] == 1
        assert facts["religion_units"] == 1

    def test_empty_army_reports_zeros_not_missing_keys(self):
        """空军队是【观测到 0】,不是【没观测】—— 这时该删的就要删。"""
        facts = end_turn._beneficiary_facts([])
        assert facts["cavalry"] == 0

    def test_unobservable_quantities_are_absent(self):
        """使者/间谍/巨作没有低成本的每回合观测,不许出现在事实表里。"""
        facts = end_turn._beneficiary_facts([U("UNIT_WARRIOR")])
        for key in ("envoys", "spies", "great_works"):
            assert key not in facts
