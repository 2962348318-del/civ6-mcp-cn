"""缺陷 113:空军在工具层完全不能用 —— attack 报 NO_LOS,move 报 invalid state。

2026-08-12 罗马局 T367。花 2715 金在克里斯蒂安桑买了喷气式轰炸机
(CS90 RS120,航空港就在斯基泰首都 5 格外),然后:

    unit_action(attack, 85,49)
      -> ERR:NO_LOS|Ranged unit cannot melee-attack. RANGE_ATTACK failed …
         LOS blocked (cliff/terrain). Move to a tile with line of sight …
    unit_action(move, 84,48)
      -> ERR:CANNOT_MOVE|Unit cannot move (invalid state)

两条路同时堵死,用户只好离开 MCP 去游戏界面手动点轰炸机 ——
工具把人赶回了 UI,这是最彻底的失效。

而引擎侧一切正常,run_lua 实测:

    CAN_OP|UNITOPERATION_AIR_ATTACK        <- 对 (85,49) 校验通过
    CAN_OP|UNITOPERATION_SKIP_TURN / SLEEP / WAIT_FOR
    CAN_CMD|UNITCOMMAND_DELETE

根因是一行的判定顺序:

    local isRanged = RangedCombat > 0 or Bombard > 0
    local isAir = (not isRanged) and CanStartOperation(AIR_ATTACK, ...)

空军【当然】有远程攻击力(喷气式轰炸机 RangedCombat=120),所以 isRanged 恒为真、
isAir 恒为假 —— 文件里那段写得好好的 AIR_ATTACK 分支【从来没有被执行过】。
空军一律走地面远程路径,被 LOS 检查毙掉;而空军根本不受地形视线约束。

move 那半边同理:空军不走 MOVE_TO,只能在航空港/航母之间 REBASE,旧文案却回
"invalid state",读起来像单位坏了 —— 于是人会去试换位置、等下回合,全是白试。

这条的代价不止 2715 金:八座敌方首都里伦敦(陆路 BFS 已证伪)和华盛顿只能
靠海空军拿,空军不可用等于统治路线少两座首都。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.units import build_attack_unit, build_move_unit


def _attack() -> str:
    return build_attack_unit(55, 85, 49)


class TestAirIsDetectedBeforeRanged:
    def test_air_check_comes_first(self):
        """判定顺序就是这条缺陷本身 —— isAir 必须在 isRanged 之前定。"""
        code = _attack()
        assert code.index("local isAir") < code.index("local isRanged"), (
            "先判空军再判远程,否则空军的远程攻击力会把它归成地面远程单位"
        )

    def test_isair_no_longer_requires_not_ranged(self):
        code = _attack()
        assert (
            "(not isRanged) and UnitManager.CanStartOperation(unit, UnitOperationTypes.AIR_ATTACK"
            not in code
        ), "这正是恒假的那行"

    def test_isranged_excludes_air(self):
        """反过来要挡住:空军不能再落进远程分支。"""
        code = _attack()
        i = code.index("local isRanged")
        seg = code[i : i + 260]
        assert "not isAir" in seg

    def test_domain_is_the_primary_signal(self):
        code = _attack()
        assert "DOMAIN_AIR" in code

    def test_can_start_operation_is_the_fallback(self):
        """Domain 取不到时还有一层 —— 实测轰炸机对 AIR_ATTACK 返回 true,
        地面远程单位返回 false,两者都不会误判。"""
        code = _attack()
        i = code.index("DOMAIN_AIR")
        seg = code[i : i + 500]
        assert "UnitOperationTypes.AIR_ATTACK" in seg
        assert "pcall" in seg

    def test_air_branch_still_exists_and_fires(self):
        code = _attack()
        assert "if isAir then" in code
        i = code.index("if isAir then")
        # 窗口必须够宽:isAir 分支里有一段很长的 AIR_ATTACK_REFUSED 兜底信息,
        # 把真正执行的 RequestOperation 推到 1700 字符之后(2026-08-26 实测)。
        seg = code[i : i + 1800]
        assert "UnitManager.RequestOperation(unit, UnitOperationTypes.AIR_ATTACK" in seg
        assert "OK:AIR_ATTACK|" in seg


class TestAirCannotMoveMessage:
    def test_move_explains_rebase(self):
        code = build_move_unit(55, 84, 48)
        assert "AIR_UNIT_CANNOT_MOVE" in code
        assert "REBASE" in code or "rebase" in code

    def test_message_says_attack_ignores_los(self):
        """给出可执行的下一步 —— 否则读的人还是会去试换位置。"""
        code = build_move_unit(55, 84, 48)
        i = code.index("AIR_UNIT_CANNOT_MOVE")
        seg = code[i : i + 400]
        assert "attack" in seg
        assert "视线" in seg

    def test_generic_invalid_state_survives_for_others(self):
        """非空军单位仍然走原来的兜底,不能被这条改掉。"""
        code = build_move_unit(55, 84, 48)
        assert "ERR:CANNOT_MOVE|Unit cannot move (invalid state)" in code

    def test_air_check_guards_the_generic_bail(self):
        code = build_move_unit(55, 84, 48)
        i = code.index("AIR_UNIT_CANNOT_MOVE")
        j = code.index("ERR:CANNOT_MOVE|Unit cannot move (invalid state)")
        assert i < j, "空军分支必须在通用兜底之前"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
