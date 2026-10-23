"""缺陷 92:陆战单位被寻路"抄近道"抄进海里,只有事后警告。

2026-08-11 罗马局 T267 实锤。一条两格调动:线列步兵 (63,48) → (63,50)。
两格之间 (63,49) 是自家商业中心,陆路完全通。引擎嫌绕远,从西侧海湾抄了
近道,单位登船,停在 (61,49) —— 英国领海边上,战力归零。

工具当时给了 ⚠️ EMBARKED 警告。但那是【事后】的:移动已经发生,移动力
已经耗尽,这一回合撤不回来。警告本身没有阻止任何事。复盘记录里这是第 7 次
(前 6 次记在 game_state.py 的登船警告注释里),说明"把警告写得更响"这个
处置从一开始就不够。

修复分两半:
  · 有陆路 → 工具自己沿陆路【逐格】走。每步都是相邻格,引擎没有抄近道的
    余地。能替调用方走对的事不该变成一次拒绝。
  · 没陆路 → 这是真跨海投送,拦下来要 force —— 登船的代价必须是明知故犯。

判据走过一段弯路,单独记在 TestPathfinderLesson 里:第一版去问引擎
UnitManager.GetMoveToPath"你打算走哪条路"。该函数在 GameCore 里存在且
可调用,但签名是 (unit, plotIndex) 而非 (unit, x, y),于是相邻格移动返回了
一条 51 格、含 33 个水格、绕经地图最北端的路径。数字看着像真的。
戳破它的是:换个目标再问,水格数【一模一样】还是 33 —— 不随输入变化的
测量值不是测量值。
"""

from __future__ import annotations

import re

import pytest

from civ_mcp import safety


def _ctx(**kw) -> safety.MoveContext:
    ctx = safety.MoveContext()
    ctx.found = True
    ctx.unit_type = "UNIT_LINE_INFANTRY"
    ctx.formation = "FORMATION_CLASS_LAND_COMBAT"
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


# T267 实测的真实陆路:BFS 七次展开就找到了
T267_PATH = [(62, 48), (62, 49), (63, 50), (63, 51)]


class TestSeaCrossingGate:
    def test_no_land_route_is_blocked(self):
        v = safety.check_embark(_ctx(sea_only=True), 40, 40)
        assert v.blocked
        assert v.code == "EMBARK_REQUIRED"
        assert "护航" in v.message
        assert "战力归零" in v.message

    def test_land_route_available_is_not_blocked(self):
        """有陆路就不该拒绝 —— 那种情况由 plan_land_hops 直接走对。"""
        v = safety.check_embark(_ctx(land_path=T267_PATH), 63, 51)
        assert not v.blocked

    def test_truncated_search_does_not_block(self):
        """搜索被上限截断时 sea_only 不置位 —— 缺数据不等于"只能渡海"。"""
        assert not safety.check_embark(_ctx(), 63, 51).blocked

    def test_force_overrides(self):
        assert not safety.check_embark(_ctx(sea_only=True), 40, 40, force=True).blocked

    def test_unfound_unit_passes(self):
        ctx = safety.MoveContext()
        ctx.sea_only = True
        assert not safety.check_embark(ctx, 1, 1).blocked


class TestWaterTargetGate:
    """目标格本身是水 —— 旧代码在这里直接放行,门等于不存在。

    2026-08-11 T270 实机:建造者任务板把 OIL@69,55 和陆地资源并排列出,
    看不出那一格是【海上】油田。我照着下令,建造者一头扎进水里,战力归零
    漂在 (70,55),而且它就算到了也改良不了 —— 海上资源要海军。
    """

    def test_water_target_is_blocked(self):
        v = safety.check_embark(_ctx(target_is_water=True), 69, 55)
        assert v.blocked
        assert v.code == "TARGET_IS_WATER"
        assert "水面格" in v.message

    def test_civilian_gets_the_extra_warning(self):
        ctx = _ctx(target_is_water=True)
        ctx.formation = safety.CIVILIAN
        ctx.unit_type = "UNIT_BUILDER"
        v = safety.check_embark(ctx, 69, 55)
        assert "改良不了" in v.message

    def test_water_target_outranks_sea_only(self):
        """两个条件同时成立时,先说"那格是水" —— 那是更直接的原因。"""
        v = safety.check_embark(_ctx(target_is_water=True, sea_only=True), 69, 55)
        assert v.code == "TARGET_IS_WATER"

    def test_force_overrides(self):
        assert not safety.check_embark(
            _ctx(target_is_water=True), 69, 55, force=True
        ).blocked

    def test_dry_target_unaffected(self):
        assert not safety.check_embark(_ctx(land_path=T267_PATH), 63, 51).blocked

    def test_lua_emits_the_wet_marker(self):
        src = safety.build_move_safety_query(1, 5, 5)
        assert 'print("LANDPATH|WET")' in src

    def test_parser_reads_the_wet_marker(self):
        ctx = safety.parse_move_safety(["LANDPATH|WET"])
        assert ctx.target_is_water
        assert not ctx.sea_only

    def test_city_needs_builder_list_excludes_water_tiles(self):
        """根因修在数据源:「Needs builder」不该列建造者去不了的海上资源。

        get_builder_tasks 早就过滤了水格,get_cities 这一处漏了 —— 同一个
        事实两个数据源给出不同答案,错的那个照样会被照着执行。"""
        from civ_mcp.lua import cities

        src = cities.build_cities_query()
        i = src.index("table.insert(unimproved,")
        guard = src[max(0, i - 400) : i]
        assert "not plot:IsWater()" in guard


class TestLandHopPlanning:
    def test_t267_route_is_walked_tile_by_tile(self):
        hops = safety.plan_land_hops(_ctx(land_path=T267_PATH), 63, 51)
        assert hops == [(62, 49), (63, 50), (63, 51)]

    def test_start_tile_is_not_re_walked(self):
        hops = safety.plan_land_hops(_ctx(land_path=T267_PATH), 63, 51)
        assert (62, 48) not in hops

    def test_every_hop_is_adjacent_to_the_previous(self):
        """逐格的全部意义在于每步都相邻 —— 一旦跳格,引擎又有了绕路的自由。"""
        hops = safety.plan_land_hops(_ctx(land_path=T267_PATH), 63, 51)
        prev = (62, 48)
        for hx, hy in hops:
            assert max(abs(hx - prev[0]), abs(hy - prev[1])) <= 1
            prev = (hx, hy)

    def test_adjacent_target_is_left_to_the_engine(self):
        """隔壁格引擎绕不出花样,不必接管。"""
        assert safety.plan_land_hops(_ctx(land_path=[(1, 1), (1, 2)]), 1, 2) == []

    def test_no_path_means_no_takeover(self):
        assert safety.plan_land_hops(_ctx(), 63, 51) == []

    def test_path_to_a_different_target_is_ignored(self):
        """路径终点和请求的目标对不上时不能拿来用。"""
        assert safety.plan_land_hops(_ctx(land_path=T267_PATH), 99, 99) == []

    def test_long_route_is_capped(self):
        long_path = [(0, i) for i in range(40)]
        hops = safety.plan_land_hops(_ctx(land_path=long_path), 0, 39)
        assert len(hops) == safety.MAX_LAND_HOPS


class TestLandPathParsing:
    def test_steps_are_ordered_by_index_not_output_order(self):
        ctx = safety.parse_move_safety(
            [
                "LANDSTEP|3|63|50",
                "LANDSTEP|1|62|48",
                "LANDSTEP|4|63|51",
                "LANDSTEP|2|62|49",
            ]
        )
        assert ctx.land_path == T267_PATH

    def test_sea_only_flag(self):
        ctx = safety.parse_move_safety(["LANDPATH|NONE"])
        assert ctx.sea_only
        assert ctx.land_path == []

    def test_malformed_step_is_skipped_not_fatal(self):
        ctx = safety.parse_move_safety(["LANDSTEP|x|y|z", "LANDSTEP|1|5|5"])
        assert ctx.land_path == [(5, 5)]

    def test_absence_of_both_means_no_conclusion(self):
        ctx = safety.parse_move_safety(["UNIT|UNIT_SCOUT|F|P|2|2|1|1"])
        assert ctx.land_path == []
        assert not ctx.sea_only


class TestPathfinderLesson:
    """第一版判据的教训,冻结成断言,免得后人再去问引擎要路径。"""

    @staticmethod
    def _probe_src() -> str:
        src = safety.build_move_safety_query(1, 5, 5)
        return "\n".join(
            ln for ln in src.split("\n") if not ln.strip().startswith("--")
        )

    def test_does_not_call_the_engine_pathfinder(self):
        assert "GetMoveToPath" not in self._probe_src(), (
            "GetMoveToPath 在 GameCore 里签名是 (unit, plotIndex);"
            "传 x,y 会得到一条绕经地图北端的假路径"
        )

    def test_probe_is_wrapped_in_pcall(self):
        src = self._probe_src()
        assert "pcall(function()" in src[: src.index("LANDSTEP")]

    def test_probe_rides_the_existing_query(self):
        """不新增 Lua 往返 —— 移动前查询实测约 324ms,再加一次会拖垮每回合。"""
        assert (
            safety.build_move_safety_query(1, 5, 5).count("Game.GetLocalPlayer()") == 1
        )

    def test_search_limit_is_bounded(self):
        assert safety.LAND_PROBE_LIMIT > 0
        assert str(safety.LAND_PROBE_LIMIT) in self._probe_src()

    def test_search_limit_covers_a_whole_continent(self):
        """上限太小 = 每次都截断 = 永远给不出"没有陆路"的结论 = 门白写。

        2026-08-11 T268 实测:搜完本局我方大陆用了 1603 格,而初版上限是
        400 —— 这张图上的探测从来没有跑到过结论。保守默认值在这里不是
        安全,是静默失效。"""
        assert safety.LAND_PROBE_LIMIT >= 2000, (
            "要够搜完一整个大陆连通分量,否则 sea_only 永远不会置位"
        )

    def test_skips_non_land_domain_and_water_targets(self):
        src = self._probe_src()
        assert "DOMAIN_LAND" in src
        assert "tPlot:IsWater()" in src

    def test_bfs_only_expands_dry_passable_tiles(self):
        src = self._probe_src()
        assert "not adj:IsWater()" in src
        assert "not adj:IsImpassable()" in src

    def test_bfs_routes_around_foreign_units(self):
        """外国单位占的格也走不进去 —— move 到那儿会被拒。

        2026-08-11 T277 实机:大科学家被普雷斯拉夫的装甲剑士挡住、大作家
        被维尔纽斯的长矛兵挡住,同一回合两次。城邦单位在我自己领土里
        游荡不是偶发,是常态。"""
        src = self._probe_src()
        assert "pv:IsVisible(fx, fy)" in src, "只算可见单位 —— 看不见的挡不到规划头上"
        assert "blocked[fx * 1000 + fy] = true" in src

    def test_bfs_respects_closed_borders(self):
        """没有开放边境的外国领土进不去 —— 规划到那儿同样是走不通的路。

        2026-08-11 T278 实机:大科学家去 (75,26) 的自然奇观,被俄罗斯领土
        挡住报 need Open Borders。交战中反而能进 —— 战争本身就是通行证。"""
        src = self._probe_src()
        assert "HasOpenBordersFrom(pid)" in src
        assert "IsAtWarWith(pid)" in src
        assert "passable[owner]" in src

    def test_city_states_gate_on_suzerainty_not_open_borders(self):
        """城邦不走开放边境那套,只认宗主。

        2026-08-11 T278 实机:大科学家被桑给巴尔挡住,报的是
        need suzerainty 而不是 need Open Borders —— 两套规则,
        套错一个就还是算出走不通的路。"""
        src = self._probe_src()
        assert "GetSuzerain()" in src
        assert "IsMajor()" in src

    def test_border_lookup_is_precomputed(self):
        """归属可通行性要先算成表 —— BFS 里每格重算外交状态会拖垮这段。"""
        src = self._probe_src()
        assert src.index("passable[pid] = ok") < src.index("local function bfs(")

    def test_own_and_unowned_tiles_are_always_passable(self):
        src = self._probe_src()
        assert "passable[me] = true" in src
        assert "passable[-1] = true" in src

    def test_terrain_and_unit_blocking_are_judged_separately(self):
        """两遍 BFS:带单位的铺路径,只看地形的判 sea_only。

        混成一遍会把"被城邦单位堵了一格"误报成"只能渡海",然后拦下一次
        完全正常的调动。单位每回合都在动,地形不动。"""
        src = self._probe_src()
        assert "local function bfs(useUnits)" in src
        assert "bfs(true)" in src
        assert "bfs(false)" in src
        # sea_only 只能由不带单位的那一遍得出
        i = src.index('print("LANDPATH|NONE")')
        window = src[max(0, i - 300) : i]
        assert "bfs(false)" in window

    def test_bfs_routes_around_own_units(self):
        """文明6 不允许同编队类堆叠 —— 把路径规划到自己单位头上,第一步
        就撞 STACKING_CONFLICT。T267 首次实测就撞上了 (62,49)。"""
        src = self._probe_src()
        assert "blocked[k] or not mayEnter" in src
        assert "oi.FormationClass == myClass" in src

    def test_own_unit_lookup_is_precomputed(self):
        """占位表要一次遍历建好 —— 在 BFS 里逐格 GetUnitsAt 会把这段拖垮。"""
        src = self._probe_src()
        assert src.index("blocked[ox * 1000 + oy] = true") < src.index("while head <=")

    def test_no_unconsumed_format_placeholders(self):
        """f-string 里的 Lua 表字面量要写成 {{}} —— 漏一层就是运行时炸。"""
        assert not re.search(r"\{[a-z_]+\}", safety.build_move_safety_query(7, 11, 13))


class TestWiredIntoMovePath:
    def test_guard_and_hops_run_before_the_move_is_issued(self):
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState.move_unit)
        assert "check_embark(" in src, "写了门不挂上等于没写"
        assert "plan_land_hops(" in src
        assert src.index("check_embark(") < src.index("build_move_unit"), (
            "拦截要在下指令之前 —— 事后警告救不了已经耗尽移动力的单位"
        )
        assert src.index("plan_land_hops(") < src.index("build_move_unit")

    def test_hop_walker_reuses_the_full_safety_path(self):
        """中途格的威胁和终点格一样要看 —— 替人走路不等于替人担风险。"""
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState._walk_land_hops)
        assert "self.move_unit(unit_index, hx, hy)" in src
        assert "force=True" not in src, "逐格移动不能绕过安全检查"

    def test_blocked_route_is_announced_not_silently_retried(self):
        """算出走不通就要说 —— 静默放行等于让调用方去撞墙。

        2026-08-11 T278:我对着同一个目标连发三次 move,每次引擎都一头撞进
        桑给巴尔边境,而探测早就知道这条路被拦死了,只是没讲。撞一次墙的
        代价是一整回合移动力。"""
        import inspect

        from civ_mcp import game_state, safety

        assert "LANDPATH|BLOCKED" in safety.build_move_safety_query(1, 5, 5)
        ctx = safety.parse_move_safety(["LANDPATH|BLOCKED"])
        assert ctx.route_blocked
        assert not ctx.sea_only, "被挡不等于没有陆路,不能触发渡海拦截"

        src = inspect.getsource(game_state.GameState.move_unit)
        assert "ROUTE_BLOCKED" in src

    def test_blocked_route_is_gated_not_waved_through(self):
        """这一条最初只报不拦,理由是"挡路的下回合就走开了"。

        2026-08-11 T287 实测推翻了这个权衡:放行之后引擎不会原地不动,
        它会绕水路走 —— 一个建造者当场从 (69,57) 漂到 (69,59),战力归零。
        此时又【没有陆路 hops 可以接管】,放行等于把方向盘交还给刚被证明
        会开进海里的那个寻路器。拦一回合可逆,单位下水不可逆。"""
        from civ_mcp import safety

        ctx = safety.parse_move_safety(["LANDPATH|BLOCKED"])
        ctx.found = True
        ctx.unit_type = "UNIT_BUILDER"
        v = safety.check_embark(ctx, 5, 5)
        assert v.blocked
        assert v.code == "ROUTE_BLOCKED"
        assert "登船" in v.message

    def test_blocked_route_is_forceable(self):
        from civ_mcp import safety

        ctx = safety.parse_move_safety(["LANDPATH|BLOCKED"])
        ctx.found = True
        ctx.unit_type = "UNIT_BUILDER"
        assert not safety.check_embark(ctx, 5, 5, force=True).blocked

    def test_water_target_still_takes_precedence(self):
        """两者同时成立时先说"那格是水" —— 更直接的原因。"""
        from civ_mcp import safety

        ctx = safety.parse_move_safety(["LANDPATH|WET"])
        ctx.route_blocked = True
        ctx.found = True
        ctx.unit_type = "UNIT_BUILDER"
        assert safety.check_embark(ctx, 5, 5).code == "TARGET_IS_WATER"

    def test_takeover_is_reported_not_silent(self):
        import inspect

        from civ_mcp import game_state

        src = inspect.getsource(game_state.GameState._walk_land_hops)
        assert "LAND_ROUTE" in src, "工具改变了移动路线就必须说出来"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
