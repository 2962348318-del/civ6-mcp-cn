"""缺陷 115:选了「再玩一回合」之后,end_turn 永远回 GAME OVER,一局就此卡死。

2026-08-12 罗马局 T455,文化胜利达成。用户在胜利弹窗上点了「再玩一回合」,
要求继续把剩下 13 座敌城清完。结果 end_turn 一律回:

    GAME OVER — VICTORY! You won a Culture victory! The game has ended.

而引擎侧三个信号里有两个说"能打":

    WINNING_TEAM | 0            <- 仍然是我的队伍(胜利标记永不清除)
    ENDGAMEMENU  | hidden=true  <- 弹窗已关闭,玩家选了继续
    TURN_ACTIVE  | true         <- 本方回合活跃,引擎完全允许行动

实测佐证:同一时刻单位可以移动、可以攻击、可以 rebase 转场,唯独 end_turn
被自己拦死。连直接调引擎的 UI.RequestAction(ACTION_ENDTURN) 都返回 true
(只是回合数不动,因为 AI 那边也在等)。

根因是判据只读了第一个信号。build_gameover_check 的结构是:

    if winTeam < 0 then
        local egm = ContextPtr:LookUpControl("/InGame/EndGameMenu")
        ...                       <- EndGameMenu 检查【只在这个分支里】
    end
    -- winTeam >= 0 直接落到下面,一路判定 GAME_OVER

也就是说 EndGameMenu 那道检查只在"没有获胜者"时才跑,而"有获胜者但弹窗
已关闭"——正是「再玩一回合」的特征——从来没被考虑过。GetWinningTeam()
一旦置位就不再清除,于是这个状态是【永久】的:这一局再也 end 不了回合。

和缺陷 109/110/112 同一族:拿一个信号当全部真相。区别在后果 ——
那几个是给出错误建议,这个是把整局游戏锁死,而且没有任何绕过路径
(工具、引擎 API 两条路都试过)。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.overview import build_gameover_check, build_gameover_check_gamecore


def _code() -> str:
    return build_gameover_check()


class TestOneMoreTurnIsNotGameOver:
    def test_winning_team_alone_no_longer_ends_it(self):
        """胜利标记必须和弹窗状态一起看 —— 它自己永不清除。"""
        code = _code()
        i = code.index("if winTeam >= 0 then")
        seg = code[i : i + 600]
        assert "EndGameMenu" in seg, "有获胜者时也要看弹窗是否还开着"
        assert "IsHidden()" in seg

    def test_hidden_menu_plus_active_turn_reports_active(self):
        code = _code()
        i = code.index("if winTeam >= 0 then")
        seg = code[i : i + 600]
        assert "IsTurnActive" in seg, "回合活跃是「还能打」的第二个证据"
        assert "GAME_ACTIVE" in seg

    def test_the_one_more_turn_branch_comes_before_the_verdict(self):
        """放行分支必须在判定 GAME_OVER 之前,否则永远走不到。"""
        code = _code()
        assert code.index("if winTeam >= 0 then") < code.index("if winTeam < 0 then")

    def test_missing_control_still_reports_game_over(self):
        """弹窗查不到时保守按结束处理 —— 查不到既可能是继续,
        也可能是控件路径变了,不能因为找不到就假定还能打。"""
        code = _code()
        i = code.index("if winTeam >= 0 then")
        seg = code[i : i + 600]
        assert "egmOMT and egmOMT:IsHidden()" in seg, (
            "必须是「拿到控件 且 它是隐藏的」才放行"
        )

    def test_real_victory_still_detected(self):
        """修的是误判,不是功能 —— 弹窗还开着时必须照旧报结束。"""
        code = _code()
        assert "GAME_OVER|" in code
        assert "Game.TestVictory" in code or "TestVictory" in code

    def test_defeat_and_turn_limit_paths_untouched(self):
        code = _code()
        assert "Elimination" in code
        assert "TurnLimit" in code


class TestGamecoreFallbackDocumentsItsLimit:
    def test_gamecore_cannot_see_the_popup(self):
        """GameCore 没有 ContextPtr,认不出「再玩一回合」—— 这是有意的,
        但必须写明,否则下一个人会以为两版判据一致。"""
        code = build_gameover_check_gamecore()
        assert "ContextPtr" not in code

    def test_limitation_is_documented(self):
        import inspect

        doc = inspect.getdoc(build_gameover_check_gamecore) or ""
        assert "再玩一回合" in doc
        assert "build_gameover_check" in doc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
