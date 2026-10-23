"""2026-08-08 查尔斯顿围城战(T193-T213)抓到的缺陷回归测试。

这批全部来自实机 20 回合战争,每一条都有明确的战场代价:

  (34) 攻击路径城血读法:City 对象上 GetMaxDamage/GetDamage 不存在,pcall 吞掉
       读成 0/0,触发假 CITY_WILL_RAZE("远程攻击 0 血城市将夷平") —— 查尔斯顿
       墙 273/300 时连报三次,每次都要 force 才能继续攻城。同文件 :768 的战报
       路径早就用 district 读法(正确),又是「一个概念两套实现」。

  (36) 政策卡幽灵抹除(T193-T216 五次入槽五次消失):政体与政策共用一个待定
       变更集(fix 21 已证)。新政体解锁后 ENDTURN_BLOCKING_CONSIDER_GOVERNMENT_
       CHANGE 每回合出现,end_turn 自动调 SetGovernmentChangeConsidered(true)
       关掉它 —— 这个调用把待定集整个丢弃,本回合排的卡一并回滚。受控实验:
       set→在,get_policies→在,skip→在,end_turn→没了,三轮复现。
       修法与 fix(21) 同款:标记前先无参 RequestPolicyChanges({},{}) 提交。

  (37) NO_CITY 错误消息占位符:_bail() 参数是普通字符串,f-string 不展开,
       玩家看到字面量 "No city at ({target_x},{target_y})"。
"""

from __future__ import annotations

from civ_mcp.lua.economy import build_make_trade_route
from civ_mcp.lua.units import build_attack_unit


class TestCityHPReadsViaDistrict:
    """(34) 攻击路径的城血必须走 city center district,City 对象读不到。"""

    def test_pre_attack_hp_probe_iterates_districts(self):
        lua = build_attack_unit(0, 61, 23)
        # 攻击前读城血的分支必须遍历 districts 找 CITY_CENTER
        assert "cityTarget:GetDistricts():Members()" in lua
        assert "DISTRICT_CITY_CENTER" in lua

    def test_no_direct_city_object_getmaxdamage_remains(self):
        """City 对象直调 GetMaxDamage 的旧写法不能再出现。"""
        lua = build_attack_unit(0, 61, 23)
        assert "cityTarget:GetMaxDamage(" not in lua
        assert "cityTarget:GetDamage(" not in lua


class TestGovernmentConsideredCommitsFirst:
    """(36) SetGovernmentChangeConsidered 之前必须提交待定政策集。"""

    def test_end_turn_source_commits_before_marking_considered(self):
        import inspect

        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        # 锚定 Lua 调用形式(pc: 前缀),避开中文注释里的同名词(T190 的教训:
        # 测试断言撞注释,匹配到的是文档不是代码)
        idx_mark = src.index("pc:SetGovernmentChangeConsidered(true)")
        # 同一段 Lua 里,提交调用必须出现在标记之前
        head = src[:idx_mark]
        idx_commit = head.rfind("RequestPolicyChanges({{}}, {{}})")
        assert idx_commit != -1, (
            "SetGovernmentChangeConsidered 前必须先 RequestPolicyChanges({},{}) "
            "提交待定集,否则本回合排的政策卡会被整体回滚(T193-T216 五次实机复现)"
        )
        # 两者应在同一个 execute_write 块里(间距很近)
        assert idx_mark - idx_commit < 500, "提交与标记应在同一段 Lua 中"


class TestNoCityMessageInterpolates:
    """(37) NO_CITY 错误必须带真实坐标,不能输出字面量占位符。"""

    def test_coordinates_are_interpolated(self):
        lua = build_make_trade_route(0, 60, 29)
        assert "No city at (60,29)" in lua
        assert "{target_x}" not in lua.split("NO_CITY")[1][:80]


class TestAquaticDistrictPlacement:
    """(38) 2026-08-08 T216:港口的合法地块全在水上,顾问的 `not IsWater()`
    过滤把引擎返回的 PLOTS 全灭 —— 乌尔港口引擎给 plot 2347,工具报
    "No valid placement tiles"。水陆判定必须跟区域的 Aquatic 字段走。
    """

    def test_filter_branches_on_aquatic(self):
        from civ_mcp.lua.map import build_district_advisor_query

        q = build_district_advisor_query(393221, "DISTRICT_HARBOR")
        assert "dist.Coast" in q, "水陆过滤必须读区域的 Coast 字段(本版本无 Aquatic)"
        assert "if isAquatic then terrainOK = plot:IsWater()" in q

    def test_unconditional_water_reject_is_gone(self):
        from civ_mcp.lua.map import build_district_advisor_query

        q = build_district_advisor_query(393221, "DISTRICT_CAMPUS")
        assert "if plot and not plot:IsWater() and not plot:IsImpassable()" not in q
