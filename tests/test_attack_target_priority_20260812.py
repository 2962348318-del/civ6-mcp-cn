"""缺陷 117:城格上的一个幽灵商人把六发轰炸全吸走了,城市毫发无伤。

2026-08-12 罗马局 T455-459,打美国最后一座城旧金山 (17,46)。连续六发:

    AIR_ATTACK|target:UNIT_TRADER at (17,46)|damage dealt:100
      city walls: 100/100, garrison: 200/200
    Post-combat: UNIT_TRADER 100/100

每一发都"打中"、每一发都报 100 伤害,而城墙和守军六回合一动没动,那个商人
也始终满血。同一格换个 API 查:

    Units.GetUnitsInPlot(plot) -> COUNT|0      <- 一个单位都没有
    Map.GetUnitsAt(17,46)      -> UNIT_TRADER  <- 贸易路线上路过的商人

两个 API 对同一格给出不同答案。Map.GetUnitsAt 会把贸易路线上经过的商人算
进来,而它既不驻扎也打不到。

而攻击代码的目标优先级是【军事单位 → 平民 → 城市】:平民 fallback 排在城市
检查【之前】,一旦命中就 break 掉整个城市分支。于是打一座城时,只要那一格上
有任何平民(哪怕是路过的),攻击就全部导向平民,城市根本挨不到。

规则上也该改:Civ6 里城市格只有【驻军】参与防御,平民不参战 —— 城里的平民
受城防保护,是打不到的。所以正确顺序是【军事单位 → 城市 → 平民】。

这条和缺陷105/109 同族:一个自信的目标选择,建立在一个不成立的前提上。
区别是它连报文都在骗人 —— "damage dealt:100" 让六个回合看起来都在推进。
"""

from __future__ import annotations

import pytest

from civ_mcp.lua.units import build_attack_unit


def _code() -> str:
    return build_attack_unit(55, 17, 46)


class TestTargetPriority:
    def test_civilian_no_longer_preempts_city(self):
        """平民 fallback 不能在城市检查之前接管 —— 这正是这条缺陷。"""
        code = _code()
        assert "if enemy == nil and fallback then enemy = fallback" not in code

    def test_city_check_comes_before_civilian_fallback(self):
        code = _code()
        i_city = code.index("cityTarget = pC")
        i_civ = code.index("enemy = civilianFallback")
        assert i_city < i_civ, "城市必须先于平民成为目标"

    def test_civilian_only_when_no_city(self):
        """野外的平民照旧能打(俘获)—— 修的是城格上的误导向,不是功能。"""
        code = _code()
        i = code.index("enemy = civilianFallback")
        seg = code[max(0, i - 200) : i]
        assert "cityTarget == nil" in seg

    def test_military_still_wins_over_everything(self):
        """有驻军时先打驻军 —— 这条本来就是对的,不能被改掉。"""
        code = _code()
        i = code.index("eCombat > 0")
        seg = code[i : i + 200]
        assert "enemy = other" in seg
        assert "break" in seg

    def test_civilian_fallback_still_collected(self):
        code = _code()
        assert "civilianFallback = fallback" in code

    def test_no_enemy_bail_still_reachable(self):
        """既无单位又无城市时仍要报 NO_ENEMY,不能因为重排而丢掉。"""
        code = _code()
        assert "ERR:NO_ENEMY" in code
        i_civ = code.index("enemy = civilianFallback")
        i_bail = code.index("ERR:NO_ENEMY")
        assert i_civ < i_bail, "兜底要在平民赋值之后判断"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
