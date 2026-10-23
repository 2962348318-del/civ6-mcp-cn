"""缺陷 116:胜利类型是猜的,而且猜错了 —— 科学胜利被报成文化胜利。

2026-08-12 罗马局 T455。工具报:

    *** GAME OVER — VICTORY ***
    You won a Culture victory!

用户当场指出不对,是科学胜利。实测:

    五个太空项目 buildable=false(T355 时全部 true,各 1800 产能可造)
      —— 项目完成后从可造列表消失,这是完成的指纹 => 5/5 全部完成

    文化进度 vs 美国 32/45、巴西 31/232、英国 38/148、印度 36/108
      —— 四家全都没过线,差 13 到 201 => 文化胜利根本没达成

三条判据同时失效,叠出一个自信的错答案:

  ① 科学:唯一判据是 GetScienceVictoryPoints() >= TotalNeeded(),而这对 API
     是 Gathering Storm 专有的 —— victory.py 的注释早就写明"absent on the
     Standard ruleset"。本机是标准规则集,这条【恒失败】,科学胜利永远检测不到。
  ② 文化:API 取不到时【跳过那一家】而 dominated 保持 true —— 读不到就当赢了。
     而这段代码下面十行的注释自己写着"EndGameMenu 的 tourism/staycationers
     不可靠"。明知不可靠,还把失败当通过。
  ③ 兜底:所有检查失败后无条件 victoryType = "VICTORY_SCORE"。

于是流程是:科学恒失败 → 掉到文化 → 文化把读不到当通过 → 报文化胜利。
三层里没有一层会说"我不确定"。

代价不止是一句话报错:我据此向用户复述了两次错误结论,还把整局的复盘方向
(以为赢在文化、是征服的副产品)带偏了。宁可报 Unknown —— 调用方会去查,
而不是照着一个错的往下走。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.overview import build_gameover_check


def _code() -> str:
    return build_gameover_check()


class TestScienceIsDetectableOnStandardRuleset:
    def test_has_a_non_gs_fallback(self):
        """sciVP 那对 API 在标准规则集不存在,不能是唯一判据。"""
        code = _code()
        i = code.index("GetScienceVictoryPoints")
        after = code[i:]
        assert "DISTRICT_SPACEPORT" in after, "要有基于太空链的标准规则集判据"

    def test_fallback_requires_a_spaceport(self):
        code = _code()
        i = code.index("DISTRICT_SPACEPORT")
        seg = code[max(0, i - 800) : i + 800]
        assert "hasPort" in seg

    def test_fallback_checks_every_chain_project(self):
        code = _code()
        assert "PROJECT_LAUNCH_EARTH_SATELLITE" in code
        assert "PROJECT_LAUNCH_MARS_REACTOR" in code

    def test_buildable_project_blocks_the_verdict(self):
        """任何一件还可造 => 没打完 => 不是科学胜利。"""
        code = _code()
        i = code.index("allDone")
        seg = code[i : i + 900]
        assert "buildable" in seg
        assert "allDone = false" in seg

    def test_gs_path_requires_positive_threshold(self):
        """needed=0 时 svp>=needed 恒真 —— 标准规则集下会假阳性。"""
        code = _code()
        i = code.index("GetScienceVictoryPointsTotalNeeded")
        seg = code[i : i + 400]
        assert "needed > 0" in seg


class TestCultureNoLongerTreatsUnreadableAsWin:
    def test_unreadable_players_block_the_verdict(self):
        code = _code()
        i = code.index("GetStaycationers")
        seg = code[i : i + 900]
        assert "unreadable" in seg, "读不到的对手必须计数,不能跳过"

    def test_verdict_requires_zero_unreadable(self):
        code = _code()
        i = code.index("unreadable == 0")
        assert i > 0
        seg = code[i : i + 200]
        assert "VICTORY_CULTURE" in seg


class TestScoreIsNoLongerABlanketFallback:
    def test_score_requires_actual_turn_limit(self):
        """原本是无条件兜底 —— 前面全失败也会给出一个确定答案。

        锚点必须是【赋值语句】而不是裸字符串 —— 解释这条缺陷的中文注释里
        也写着 VICTORY_SCORE,本局已经有四个测试栽在这上面。"""
        code = _code()
        i = code.index('victoryType = "VICTORY_SCORE"')
        seg = code[max(0, i - 400) : i + 200]
        assert "GetMaxTurns" in seg
        assert "curT >= maxT" in seg

    def test_unknown_survives_when_nothing_matches(self):
        """确认不了就说确认不了 —— 这是这条缺陷的核心教训。"""
        code = _code()
        assert 'victoryType = "Unknown"' in code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
