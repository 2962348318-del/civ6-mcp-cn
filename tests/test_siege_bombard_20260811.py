"""缺陷 102:攻城单位的主武器被读成 0 —— 远程战力在 Bombard 列,不在 RangedCombat。

2026-08-11 罗马局 T291 实锤。围攻斯基泰首都时让大炮开火,得到:

    Combat Estimate (Melee)
      UNIT_ARTILLERY (CS:57) vs CITY (CS:62)
      Est damage to defender: ~16
      Est damage to attacker: ~35
    ERR:UNFAVORABLE_EXCHANGE|造成 ~16 却要承受 ~35(1:2.2)

大炮是射程 2 的【攻城单位】,零反伤,而且专克城墙。它被当成近战冲脸了。

查表查出根因:

    UNIT_ARTILLERY    Combat=60  RangedCombat=0   Bombard=80  Range=2  SIEGE
    UNIT_CATAPULT     Combat=25  RangedCombat=0   Bombard=35  Range=2  SIEGE
    UNIT_BOMBARD      Combat=45  RangedCombat=0   Bombard=55  Range=2  SIEGE
    UNIT_FIELD_CANNON Combat=50  RangedCombat=60  Bombard=0   Range=2  RANGED

攻城线的远程战力全在 Bombard 列。工具只读 RangedCombat,于是投石机、
攻城炮、大炮这一整类的主武器读成 0:get_units 不显示 RS、伤害估算按近战
算、白吃反击、还被亏本交易门拦下。

代价特别大,因为攻城单位是【啃城墙唯一高效的手段】——统治路线的核心
兵种一直是废的。而我一直以为"3 门大炮 CS80"是能用的火力,其实那个 80
是 Bombard,工具读的是 Combat 60。

值得单独记一笔:执行侧的远程门(build_attack_unit)【早就】同时看
RangedCombat 和 Bombard,注释里写得清清楚楚"Catapult/Trebuchet 远程靠
Bombard"。漏的是估算侧和显示侧。于是"能不能打"和"值不值得打"两个判断
用了不同的兵种模型,前者说能、后者说亏 —— 一致的数据源比两处各自正确
更重要。
"""

from __future__ import annotations

import pytest

from civ_mcp import civ_kit

# T291 实测值
ARTILLERY = {"Combat": "60", "RangedCombat": "0", "Bombard": "80"}
CATAPULT = {"Combat": "25", "RangedCombat": "0", "Bombard": "35"}
FIELD_CANNON = {"Combat": "50", "RangedCombat": "60", "Bombard": "0"}
WARRIOR = {"Combat": "20", "RangedCombat": "0", "Bombard": "0"}


class TestRangedHelper:
    def test_siege_uses_bombard(self):
        assert civ_kit._ranged(ARTILLERY) == 80
        assert civ_kit._ranged(CATAPULT) == 35

    def test_ranged_class_still_uses_ranged_combat(self):
        assert civ_kit._ranged(FIELD_CANNON) == 60

    def test_melee_has_no_ranged(self):
        assert civ_kit._ranged(WARRIOR) == 0

    def test_missing_columns_do_not_crash(self):
        assert civ_kit._ranged({}) == 0

    def test_real_game_data(self):
        """对着真实导出表验证,不只是构造的字典。"""
        rows = civ_kit._units()
        art = rows.get("UNIT_ARTILLERY")
        if art:  # 表存在时才断言
            assert civ_kit._ranged(art) == 80, "大炮的 80 在 Bombard 列"


class TestLuaPathsReadBombard:
    def test_unit_list_shows_siege_ranged_strength(self):
        from civ_mcp.lua.units import build_units_query

        src = build_units_query()
        i = src.index("local rs =")
        assert "Bombard" in src[i : i + 240], "单位列表里攻城单位也要显示 RS"

    def test_damage_estimate_reads_bombard(self):
        from civ_mcp.lua.units import build_combat_estimate_query

        src = build_combat_estimate_query(1, 5, 5)
        i = src.index("local attRS =")
        assert "Bombard" in src[i : i + 200], (
            "估算侧漏掉 Bombard 就会把攻城单位算成近战冲脸"
        )

    def test_threat_scan_counts_siege_units(self):
        from civ_mcp.lua.units import build_threat_scan_query

        src = build_threat_scan_query()
        assert "bombardVal" in src, "敌方攻城单位不能因为 RangedCombat=0 就不算威胁"

    def test_move_safety_counts_siege_as_military(self):
        from civ_mcp import safety

        src = safety.build_move_safety_query(1, 5, 5)
        i = src.index("local urs =")
        assert "Bombard" in src[i : i + 200]


class TestPythonPathsReadBombard:
    def test_threat_info_ranged_strength(self):
        import inspect

        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        i = src.index("self.ranged_strength =")
        assert "Bombard" in src[i : i + 300]

    def test_estimate_and_execution_agree(self):
        """执行门早就看 Bombard,估算侧漏了 —— 两处必须用同一个兵种模型。

        不一致的后果不是"某一处不准",而是"能打"和"值不值得打"给出
        互相矛盾的答案,调用方无所适从。"""
        from civ_mcp.lua.units import build_combat_estimate_query, build_attack_unit

        exec_src = build_attack_unit(1, 5, 5)
        est_src = build_combat_estimate_query(1, 5, 5)
        assert "Bombard" in exec_src
        assert "Bombard" in est_src


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
