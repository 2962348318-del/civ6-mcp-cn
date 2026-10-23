"""2026-08-10 罗马局 T193-T211 暴露的第二批缺陷回归测试。

用户要求「先修再打」,这四条都是当回合发现、当回合修的:

  (76) 建造者任务板不查【资源】的科技前置:改良表的 PrereqTech 查了(矿井
       远古就解锁),但煤/铀的门槛在资源自己身上(煤要工业化、铀要联合作战)。
       于是三块建不了的格子被列成 URGENT,三个建造者走过去全部 CANNOT_IMPROVE。
       修法:资源 PrereqTech 一起查,缺科技的任务降到 NORMAL 并写明缺什么。

  (77) set_research 接受被锁的市政:前置没满足时 RequestPlayerOperation
       静默不生效,而代码无条件 print("OK:PROGRESSING") —— 我把被锁的
       土木工程设成当前市政,工具回 PROGRESSING、简报还算出"23 回合",
       一回合的文化产出没有着落。修法:发操作前自查前置表并显式报错。

  (78) propose_trade 的错误被吞掉:我把俄罗斯 id 猜成 9(实际 5),lua 正确
       报了 ERR:NOT_MET,但 parse_test_trade_response 只找 CIV|/ITEM| 行,
       找不到就返回默认值 —— 渲染成 "Trade test with (player 0) / 提案为空
       / AI: ACCEPTABLE"。一个不存在的对手对空提案表示接受。

  (79) 交易缓冲区不清:
       (a) send 成交后同一笔交易又以 incoming 挂起交易出现,必须再接一次,
           而 end_turn 会被"有挂起交易"拦住(三笔奢侈品外销全走了两道手续);
       (b) mode="test" 的 EQUALIZE 会把工作交易真的提交出去 —— 给西班牙
           试价后,它 1 金/回合的还价成了真实挂起交易,拒掉还吃了
           「您违背了诺言」的外交惩罚。试价本该零副作用。
"""

from __future__ import annotations

import pytest

from civ_mcp import narrate
from civ_mcp.lua import diplomacy as lua_diplo
from civ_mcp.lua import tech as lua_tech
from civ_mcp.lua import units as lua_units
from civ_mcp.lua.models import BuilderInfo, BuilderTask


class TestResourceTechPrereqInBuilderTasks:
    """(76) 资源级科技门槛必须进任务板。"""

    def test_query_checks_resource_prereq_tech(self):
        code = lua_units.build_builder_tasks_query()
        assert "resInfo.PrereqTech" in code
        assert "需科技 " in code

    @staticmethod
    def _render(improvement: str, priority: str = "urgent") -> str:
        task = BuilderTask(
            priority=priority,
            x=66,
            y=49,
            improvement=improvement,
            resource="COAL",
            resource_class="strategic",
            city_name="奥斯蒂亚",
            nearest_builder_id=1,
            distance=2,
        )
        builder = BuilderInfo(unit_id=1, unit_index=1, x=66, y=49, charges=2, moves=2.0)
        return narrate.narrate_builder_tasks([task], [builder])

    def test_locked_task_drops_out_of_urgent(self):
        out = self._render("IMPROVEMENT_MINE_LOCKED")
        urgent_idx = out.find("URGENT")
        normal_idx = out.find("NORMAL")
        assert normal_idx != -1, "锁着的任务应落在 NORMAL 段"
        assert urgent_idx == -1 or urgent_idx > normal_idx

    def test_locked_task_says_it_cannot_be_built(self):
        out = self._render("IMPROVEMENT_MINE_LOCKED")
        assert "现在建不了" in out
        assert "MINE_LOCKED" not in out, "渲染时要去掉内部后缀"

    def test_unlocked_task_still_urgent(self):
        out = self._render("IMPROVEMENT_MINE")
        assert "URGENT" in out
        assert "现在建不了" not in out


class TestLockedCivicRejected:
    """(77) 前置没到的科技/市政必须显式报错,不能假成功。"""

    def test_civic_setter_checks_prereqs(self):
        code = lua_tech.build_set_civic("CIVIC_CIVIL_ENGINEERING")
        assert "CivicPrereqs" in code
        assert "ERR:CIVIC_LOCKED" in code
        assert code.index("missingPre") < code.index("UI.RequestPlayerOperation"), (
            "前置检查必须在发操作之前 —— 发完再验会撞上 UI 的异步结算"
        )

    def test_tech_setter_checks_prereqs(self):
        code = lua_tech.build_set_research("TECH_ROCKETRY")
        assert "TechnologyPrereqs" in code
        assert "ERR:TECH_LOCKED" in code

    def test_error_names_the_missing_prereq(self):
        code = lua_tech.build_set_civic("CIVIC_URBANIZATION")
        assert "前置还没完成" in code
        assert "table.concat(missingPre" in code


class TestTestTradeErrorsSurface:
    """(78) 错误不能退化成"空提案被接受"。"""

    def test_err_line_raises(self):
        with pytest.raises(RuntimeError) as exc:
            lua_diplo.parse_test_trade_response(
                ["ERR:NOT_MET|Have not met player 9", "---END---"]
            )
        assert "NOT_MET" in str(exc.value)

    def test_normal_response_still_parses(self):
        parsed = lua_diplo.parse_test_trade_response(
            [
                "CIV|6|挪威",
                "PROPOSED_ITEMS",
                "ITEM|US|RESOURCE|1|30|43|",
                "AI_COUNTER",
                "ITEM|THEM|GOLD|6|30||",
            ]
        )
        assert parsed.other_player_id == 6
        assert parsed.other_civ_name == "挪威"
        assert len(parsed.proposed) == 1
        assert len(parsed.counter) == 1

    def test_rejected_flag_preserved(self):
        parsed = lua_diplo.parse_test_trade_response(
            ["CIV|11|西班牙", "PROPOSED_ITEMS", "AI_COUNTER", "REJECTED"]
        )
        assert parsed.rejected is True


class TestTradeWorkingDealCleanup:
    """(79) 收发两条路径都必须清空工作交易缓冲区。"""

    def test_test_trade_clears_both_directions(self):
        code = lua_diplo.build_test_trade(11, [{"type": "GOLD", "amount": 1}], [])
        assert "ClearWorkingDeal(DealDirection.OUTGOING" in code
        assert "ClearWorkingDeal(DealDirection.INCOMING" in code
        # 清理必须发生在 EQUALIZE 之后(不然清的是还没提交的东西)
        assert code.rindex("ClearWorkingDeal(DealDirection.OUTGOING") > code.index(
            "SendWorkingDeal(DealProposalAction.EQUALIZE"
        )

    def test_propose_trade_clears_after_accept(self):
        code = lua_diplo.build_propose_trade(6, [{"type": "GOLD", "amount": 1}], [])
        assert "ClearWorkingDeal(DealDirection.INCOMING" in code
        assert code.rindex("ClearWorkingDeal(DealDirection.OUTGOING") > code.index(
            "SendWorkingDeal(DealProposalAction.PROPOSED"
        )

    def test_propose_trade_still_clears_outgoing_before_building(self):
        """T287 的旧修复(出向残留导致卖错货)不能被这次改动破坏。"""
        code = lua_diplo.build_propose_trade(6, [{"type": "GOLD", "amount": 1}], [])
        first_clear = code.index("ClearWorkingDeal(DealDirection.OUTGOING")
        assert first_clear < code.index("GetWorkingDeal(DealDirection.OUTGOING")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
