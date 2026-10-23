"""缺陷 98 → 缺陷 104:换卡失败的根因订正。

== 缺陷 98(T278-T280)==
紫禁城给了第 8 个政策槽,我连着两回合往里插「楼房」,两次都收到
"已提交,下一帧生效",两次都没进槽。当时的结论是:前面那道
`if unlockCost > 0 and gold < unlockCost` 的门依赖
GetCostToUnlockPolicies,而该方法"在这个上下文里根本不存在",
pcall 静默失败使门恒开。于是把文案改成"窗口关着会被静默回滚"。

== 缺陷 104(T304-T305)实测订正 ==
上面那个根因是【错的】,而且错得有代价:它把一条正确的 API 写进了
测试黑名单(`assert "GetCostToUnlockPolicies" not in src`),等于用
测试禁止了修复。

T305 用 run_lua 在 InGame 上下文直接探,结果是:

    METHOD GetCostToUnlockPolicies=true   UNLOCK_COST ok=true cost=0
    METHOD PolicyChangeMade=true          IsPolicyObsolete=true

四个方法全部存在可调。T280 那次探不到,多半是探在 GameCore 上下文 ——
CanSlotPolicy 在 GameCore 确实不存在(T305 亲测报 "function expected
instead of nil")。同一个名字在两个上下文里一个有一个没有,这就是
整条错误结论的来源。

真正的根因是【过时卡】。T304 提交六张进三张、T305 提交三张进零张,
分界线不是免费窗口,是 IsPolicyObsolete,无一例外:

    POLICY_CONSCRIPTION   unlocked=true  obsolete=true   -> 进不去
    POLICY_RETAINERS      unlocked=true  obsolete=true   -> 进不去
    POLICY_URBAN_PLANNING unlocked=true  obsolete=true   -> 进不去
    POLICY_LEVEE_EN_MASSE unlocked=true  obsolete=false  -> 进去了
    POLICY_FIVE_YEAR_PLAN unlocked=true  obsolete=false  -> 进去了
    POLICY_SIMULTANEUM    unlocked=true  obsolete=false  -> 进去了

过时卡一路绿灯:IsPolicyUnlocked=true、CanSlotPolicy=true、槽位类型
也对得上,直到 RequestPolicyChanges 被引擎静默丢弃 —— 不抛错、不改槽、
连 PolicyChangeMade 都不置位。

两处都要修,而读取侧是首要的:get_policies 把过时卡列进了「可用政策」
清单,我是【照着工具给的清单】挑的这三张。工具推荐了不可能装备的东西,
然后在失败时告诉我"等市政完成那回合再改"—— 照做的话,那一回合会再
失败一次。

教训与缺陷 102 同族:一个可用性判据只做了一半。区别是这次的错误结论
被写进了测试,所以它不但没被发现,还被保护了两天。断言要盯行为
(过时卡必须拦下),不要盯"某个 API 名字不许出现"。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.governance import build_policies_query, build_set_policies


def _src(policy: str = "POLICY_INSULAE", slot: int = 7) -> str:
    """去掉 Lua 注释行 —— 断言查的是【代码】用了什么,
    不该被解释这段历史的注释文字撞上。"""
    raw = build_set_policies({slot: policy})
    return "\n".join(ln for ln in raw.split("\n") if not ln.strip().startswith("--"))


def _read_src() -> str:
    raw = build_policies_query()
    return "\n".join(ln for ln in raw.split("\n") if not ln.strip().startswith("--"))


class TestObsoleteIsTheActualGate:
    def test_write_path_checks_obsolete(self):
        """过时卡必须在发请求【之前】拦下 —— 发出去只会被静默丢弃。"""
        src = _src()
        assert "IsPolicyObsolete" in src

    def test_obsolete_check_runs_before_the_request(self):
        src = _src()
        assert src.index("IsPolicyObsolete") < src.index("RequestPolicyChanges")

    def test_obsolete_has_its_own_error_code(self):
        """和 CANNOT_SLOT / SLOT_MISMATCH 分开 —— 它们的修法完全不同:
        槽位错了换个槽就行,过时了这张卡这辈子都插不进去。"""
        src = _src()
        assert "ERR:POLICY_OBSOLETE" in src

    def test_obsolete_message_says_the_card_is_dead_not_retryable(self):
        src = _src()
        i = src.index("ERR:POLICY_OBSOLETE")
        tail = src[i : i + 400]
        assert "取代" in tail
        assert "get_policies" in tail, "要告诉调用方去哪儿拿新清单"

    def test_read_path_filters_obsolete_out_of_the_available_list(self):
        """首要修复:清单里根本不该出现过时卡。我就是照这个清单挑的。"""
        src = _read_src()
        i = src.index("IsPolicyUnlocked")
        assert "IsPolicyObsolete" in src[i : i + 300]

    def test_read_path_unlocked_alone_is_not_enough(self):
        """IsPolicyUnlocked 对过时卡照样返回 true —— 单独用它就是这个缺陷。"""
        src = _read_src()
        assert "not pCulture:IsPolicyObsolete" in src


class TestApiAvailabilityClaimCorrected:
    def test_cost_api_is_usable_again(self):
        """旧测试断言这个名字【不许出现】,把正确 API 关在门外。
        实测 InGame 下它存在且返回 0。"""
        src = _src()
        assert "GetCostToUnlockPolicies" in src

    def test_cost_is_read_not_guessed(self):
        """成本实读之后,就不必再靠"窗口开没开"猜结算结果。"""
        src = _src()
        assert "unlockCost" in src

    def test_free_window_api_still_used(self):
        assert "CivicCompletedThisTurn()" in _src()


class TestHonestReporting:
    def test_three_distinct_outcomes(self):
        """成功 / 已提交待结算 / 需付费 —— 三条路必须能分辨。"""
        src = _src()
        for marker in (
            "OK:POLICIES_SET",
            "NOTE:POLICIES_SUBMITTED",
            "WARN:POLICIES_NEED_GOLD",
        ):
            assert src.count(marker) == 1

    def test_zero_cost_is_not_reported_as_a_failure(self):
        """cost==0 就是免费,这时候报"会被回滚"是在制造假警报。"""
        src = _src()
        assert "freeWindow or unlockCost == 0" in src

    def test_paid_branch_names_the_actual_cost(self):
        src = _src()
        i = src.index("WARN:POLICIES_NEED_GOLD")
        assert "unlockCost" in src[i : i + 300]

    def test_no_longer_blames_the_free_window_for_everything(self):
        """旧文案把每一次失败都归给"窗口没开",于是真正的原因
        (过时卡)三个回合没被发现。"""
        src = _src()
        assert "POLICIES_LIKELY_ROLLED_BACK" not in src

    def test_success_path_unchanged(self):
        assert "OK:POLICIES_SET" in _src()


class TestStillCatchesTheOriginalDefect98Case:
    def test_slot_and_type_prechecks_survive(self):
        """订正根因不等于删掉旧的门 —— 槽位类型错仍要拦。"""
        src = _src()
        assert "CanSlotPolicy" in src
        assert "ERR:CANNOT_SLOT" in src
        assert "ERR:SLOT_MISMATCH" in src

    def test_readback_still_present(self):
        assert "missing" in _src()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
