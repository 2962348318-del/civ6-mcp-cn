"""2026-08-09 苏美尔局 T273-T301 太空冲刺段暴露的缺陷回归测试。

这批全部来自实机稳态回合循环,每一条都有明确的规划代价:

  (39) 科学胜利 ETA 分母:太空项目是【单城】建造,ETA 却拿全帝国产能
       (157/t)当分母 —— "7800/157≈50 回合"实际是乌鲁克 28/t 的 280 回合。
       T310-320 的胜利规划整段被这个数字误导,靠萨根/特斯拉两个伟人续命。

  (40) 伟人激活条件不导出:萨根的 ability 渲染成 "+3000 production toward
       current build",按文案把他挪到太空港【工地】想秒区域 —— 白跑。真实
       条件(必须站在建成的宇航中心+城市正在生产太空竞赛项目)在 DB 的
       ActionRequires* 列里,效果文案永远只说收益不说门槛。

  (41) patronize 未知参数静默吞掉:传 currency="faith"(直觉参数名)被 MCP
       层忽略,三次"失败"全在测金买断;萨根多花 620 金(信仰 430 本可搞定)。

  (42) propose_trade 卖错货:上一笔成交后的 working deal 残留没清
       (HasPendingDeal 短暂 true 时跳过 Clear),残留里躺着旧皮草 item;
       新 offer 的鲸鱼 AddItemOfType 失败被 if ri then 静默吞掉 →
       发出去的 deal 是旧皮草。offer=WHALES,成交 "We give: 1x 皮草"。

  (43) skip_remaining_units 抹掉多回合移动指令:提督/两个建造者带着挂起
       路径被 FinishMoves,连续两回合原地罚站,每回合要人肉重发 move。

  (44) 工业区计数恒 0:districts 元素带 "@x,y" 坐标后缀
       ("DISTRICT_INDUSTRIAL_ZONE@61,29"),精确相等匹配永远 False ——
       乌鲁克/阿克沙克各有 IZ+工厂,简报连喊 25 回合"一个工业区都没有"。
       连带:工厂/发电厂数硬编码 0,iz 修好后会立刻改报"有 IZ 没工厂"。

  (45) 偏离提醒把平民当军队:is_civilian_item 只认 SETTLER/BUILDER,
       排考古学家/商人都触发"再造军队会拖慢胜利进度"。

  (46) sleep 单位仍被滞留门禁点名:萨根 sleep 在太空港工地等完工(唯一
       正确用法),每 5 回合被点名,每次双调 end_turn 放行。IsReadyToSelect
       (needs_orders)是引擎权威的"刻意驻留"判据,滞留监控此前没消费它。

  (47) 资源格被地貌压住不预警:铀矿格 FEATURE_FOREST 压着,任务板照喊
       build MINE,improve 到场才报 CANNOT_IMPROVE(空地分支早有此检查,
       资源分支漏了 —— 又是"一个概念两套实现")。

  (48) 日记丢失根因(T69-104 等四段 30+ 回合):LocalSink 在 bind_game()
       前无限缓冲,bind 只挂在 get_game_overview 工具 —— 压缩续会话直接
       进回合循环不调 overview,缓冲随进程被杀蒸发。end_turn 本来就查了
       game_identity,就地绑定。

  (52) 陈年 pending marker 吞掉真提交(T373 实测):T372 AI 挂死→杀游戏→
       几小时后手动重载,存档落在 turn+1(373)。guard 见「pending marker
       from_turn=372 + 当前 373」就断定是丢响应重试,report-only 不发送
       ACTION_ENDTURN —— 返回假报告"372->373"(AI 等待仅 0.6s),agent 信
       以为真,skip 空转两轮才发现回合根本没结束。advanced marker 有 30s
       宽限窗,pending marker 却无年龄检查。修复:pending+已推进也设上限
       (30 分钟):丢响应重试都在分钟级,挂死恢复(判死 6-10min+杀+重启+
       人工装载)必然超过 30 分钟。

  (53) 太空链假 [0/0](T380 实测):火星水培 59% 在造被渲染成"链未开始"。
       ① 链表探测用不存在的 MARS_HABITAT(实名 MARS_HABITATION)落入假链;
       ② completed 判定依赖 GetScienceVictoryPoints —— GS 专属 API,
       Standard 规则集恒 0。修复:真名优先探测+链级推断(高层可造⇒低层
       必完成)+敌方如实标 progress hidden。

  (54) 假 TURN_NOT_SUBMITTED(T384 实测):宣战唤醒的 fortify 单位是软阻
       塞,第二次 ACTION_ENDTURN 被引擎放行 —— 回合已提交、AI 在跑,工具
       却按残留通知报"回合没有提交",agent 信了继续下命令全被拒。修复:
       返回前复核 IsTurnActive,已不活跃则继续轮询。

  (55) 文化胜利零监控(T384 败局根因):美国 foreign tourists 375 vs 全场
       最后门柱(希腊 domestic 381)差 7 人时,没有任何简报显示过这两个数;
       引擎通知弹出后 1 回合就输。proximity 哨兵有宗教/外交/科学(GS)线,
       唯独没有文化。修复:每回合算各对手 foreign 总量 vs 门柱,gap≤10/
       80/200 三档警报;快照加 RIVAL CULTURE PROGRESS 段;同批补 Standard
       规则集的科学哨兵(太空港数)。

  (56) 研究队列(用户点名"卡科技/路线规划不清"):科技/市政研完后
       Research: None 空转等人工接续,一局至少 5 次;路线早写在日记里,
       损失全在"计划没接进引擎"。新增 set_research_queue + end_turn 自动
       接续(仅槽位为空时消费,手动 set_research 永远优先,失败可见暂停)。
"""

from __future__ import annotations

import inspect

from civ_mcp import lategame
from civ_mcp.lua.cities import build_produce_item
from civ_mcp.lua.diplomacy import _lua_deal_item, build_propose_trade
from civ_mcp.lua.great_people import (
    build_great_people_query,
    parse_great_people_response,
)
from civ_mcp.lua.units import build_builder_tasks_query, build_skip_remaining_units
from civ_mcp.narrate import narrate_great_people


class TestScienceEtaUsesProjectCityProduction:
    """(39) ETA 分母必须是太空港城的单城产能,不是帝国总产能。"""

    def _ctx(self, **kw):
        base = dict(
            turn=300,
            era="ERA_INFORMATION",
            victory_type="SCIENCE",
            total_production=157,
            project_city_production=28,
            city_count=9,
            has_spaceport=False,
            completed_projects=set(),
            techs=set(),
        )
        base.update(kw)
        return lategame.LateGameContext(**base)

    def test_eta_divides_by_project_city(self):
        eta = lategame.my_eta_turns(self._ctx())
        remaining = sum(m.cost for m in lategame.SCIENCE_CHAIN)
        assert eta == int(round(remaining / 28)), (
            "分母必须是单城产能 28,不是帝国 157 —— 后者把 280 回合说成 50 回合"
        )

    def test_eta_falls_back_to_total_when_unknown(self):
        eta = lategame.my_eta_turns(self._ctx(project_city_production=0))
        remaining = sum(m.cost for m in lategame.SCIENCE_CHAIN)
        assert eta == int(round(remaining / 157))

    def test_advice_text_names_the_denominator(self):
        advice = lategame.check_lategame(self._ctx())
        chain_lines = [a.text for a in advice if "科学胜利链" in a.text]
        assert chain_lines, "科学链建议应存在"
        assert "太空港城产能 28" in chain_lines[0]
        assert "单城建造" in chain_lines[0]

    def test_fallback_text_flags_overestimate(self):
        advice = lategame.check_lategame(self._ctx(project_city_production=0))
        chain_lines = [a.text for a in advice if "科学胜利链" in a.text]
        assert chain_lines and "高估" in chain_lines[0], (
            "拿不到单城产能时必须标明帝国口径只是下界"
        )


class TestGreatPersonActivationRequirements:
    """(40) 伟人条目必须带激活门槛,效果文案只说收益不说门槛。"""

    def test_query_exports_action_requires_columns(self):
        q = build_great_people_query()
        assert "ActionRequiresCompletedDistrictType" in q
        assert "ActionRequiresMilitaryUnitDomain" in q

    def test_parse_reads_eleventh_column(self):
        line = (
            "GP|大科学家|卡尔·萨根|信息时代|1715|Unclaimed|1687"
            "|+3000 production toward current build"
            "|gold:620,faith:430,recruit:false|150"
            "|must stand in completed DISTRICT_SPACEPORT"
        )
        gp = parse_great_people_response([line])
        assert len(gp) == 1
        assert gp[0].activation_req == "must stand in completed DISTRICT_SPACEPORT"

    def test_parse_tolerates_legacy_ten_column_lines(self):
        line = (
            "GP|大科学家|卡尔·萨根|信息时代|1715|Unclaimed|1687"
            "|+3000 production|gold:620,faith:430,recruit:false|150"
        )
        gp = parse_great_people_response([line])
        assert gp[0].activation_req == ""

    def test_narrate_shows_activation_line(self):
        gp = parse_great_people_response(
            [
                "GP|大科学家|卡尔·萨根|信息时代|1715|Unclaimed|1687"
                "|+3000 production|gold:620,faith:430,recruit:false|150"
                "|must stand in completed DISTRICT_SPACEPORT"
            ]
        )
        text = narrate_great_people(gp)
        assert "Activation: must stand in completed DISTRICT_SPACEPORT" in text


class TestPatronizeCurrencyAlias:
    """(41) currency 别名要收编,非法值要显式报错,不再静默走默认金。"""

    def test_signature_accepts_currency(self):
        from civ_mcp import server

        src = inspect.getsource(server)
        idx = src.index("async def patronize_great_person")
        body = src[idx : idx + 2500]
        assert 'currency: str = ""' in body, "必须收 currency 别名参数"
        assert "YIELD_FAITH" in body
        assert "BAD_YIELD" in body, "非法值必须显式报错,不能静默回落到金"


class TestProposeTradeDealHygiene:
    """(42) 出向工作区无条件清空;资源 item 加入失败必须报错。"""

    def test_working_deal_cleared_unconditionally(self):
        lua = build_propose_trade(5, [], [])
        idx_clear = lua.index(
            "DealManager.ClearWorkingDeal(DealDirection.OUTGOING, me, target)"
        )
        idx_get = lua.index("DealManager.GetWorkingDeal(DealDirection.OUTGOING")
        assert idx_clear < idx_get, "Clear 必须在 GetWorkingDeal 之前且无条件执行"
        head = lua[:idx_clear].splitlines()[-3:]
        assert not any("HasPendingDeal" in l and "if" in l for l in head), (
            "不能再用 HasPendingDeal 守卫 Clear —— T287 鲸鱼变皮草的根因"
        )

    def test_deal_err_bails_before_send(self):
        lua = build_propose_trade(5, [], [])
        assert "if dealErr then" in lua
        assert lua.index("if dealErr then") < lua.index("SendWorkingDeal"), (
            "item 加入失败必须在发送前中止"
        )

    def test_resource_item_failure_sets_deal_err(self):
        snippet = _lua_deal_item(
            "me", {"type": "RESOURCE", "name": "RESOURCE_WHALES", "amount": 1}
        )
        assert "dealErr" in snippet, "加入失败要写 dealErr,不能 if ri then 静默吞"
        assert "if not ri then" in snippet


class TestSkipPreservesQueuedMoves:
    """(43) skip 不碰带挂起路径的单位 —— 引擎回合开始会自动续走。"""

    def test_skip_checks_queued_destination(self):
        lua = build_skip_remaining_units()
        assert "GetQueuedDestination" in lua
        idx_check = lua.index("GetQueuedDestination")
        idx_finish = lua.index("UnitManager.FinishMoves(unit)")
        assert idx_check < idx_finish, "查挂起路径必须在 FinishMoves 之前"
        assert "en-route" in lua, "留行单位数要出现在结果里,不能静默"


class TestIndustrialZoneCounting:
    """(44) districts 元素带 @坐标 后缀,匹配必须先剥后缀;工厂数要真算。"""

    def test_iz_match_strips_coordinate_suffix(self):
        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        idx = src.index("cities_with_industrial_zone=iz")
        head = src[max(0, idx - 3000) : idx]
        assert 'd.split("@", 1)[0]' in head, (
            "必须剥 '@x,y' 后缀再比对 —— 精确相等匹配对 "
            "'DISTRICT_INDUSTRIAL_ZONE@61,29' 恒 False"
        )

    def test_factory_and_power_plant_counted_from_buildings(self):
        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        idx = src.index("cities_with_industrial_zone=iz")
        seg = src[idx : idx + 300]
        assert "cities_with_factory=fac" in seg, "工厂数不能再硬编码 0"
        assert "cities_with_power_plant=pp" in seg


class TestCivilianClassification:
    """(45) 平民名单要列全 —— 考古学家/商人不是军队。"""

    def test_marker_list_covers_trader_and_archaeologist(self):
        from civ_mcp import game_state as gsm

        src = inspect.getsource(gsm)
        idx = src.index("_CIVILIAN_MARKERS")
        seg = src[idx : idx + 500]
        for marker in ("TRADER", "ARCHAEOLOGIST", "MISSIONARY", "SPY"):
            assert f'"{marker}"' in seg, f"平民名单缺 {marker}"


class TestStallGateRespectsSleep:
    """(46) needs_orders=False(sleep/fortify/自动化)的平民不进滞留名单。"""

    def test_stall_loop_consumes_needs_orders(self):
        from civ_mcp import end_turn as et

        src = inspect.getsource(et)
        idx = src.index("滞留超过 5 回合")
        head = src[max(0, idx - 4000) : idx]
        assert 'getattr(u, "needs_orders", True)' in head, (
            "滞留监控必须消费 IsReadyToSelect 判据 —— 萨根 sleep 等太空港"
            "完工被反复点名(T288-T300)"
        )


class TestBuilderResourceTaskWarnsBlockingFeature:
    """(47) 资源格被可移除地貌压住时,任务描述必须预警先砍。"""

    def test_resource_branch_checks_removable_feature(self):
        q = build_builder_tasks_query()
        idx_res = q.index("RESOURCECLASS_STRATEGIC")
        seg = q[idx_res : idx_res + 4000]
        assert "removableR" in seg, "资源分支必须做与空地分支相同的可移除地貌检查"
        assert "remove_feature" in seg, "预警文案要指名工具"


class TestDiaryBindsOnEndTurn:
    """(48) end_turn 已查 game_identity,就地绑定遥测 —— 不再依赖 overview。"""

    def test_end_turn_binds_after_identity(self):
        from civ_mcp import server

        src = inspect.getsource(server)
        idx = src.index("async def end_turn")
        body = src[idx : idx + 6000]
        idx_ident = body.index("get_game_identity")
        seg = body[idx_ident : idx_ident + 1200]
        assert "bind_game(_diary_civ_type, _diary_seed)" in seg, (
            "identity 到手就必须 bind —— 否则压缩续会话的日记全在内存缓冲里"
        )


class TestProduceItemSuggestsNearNames:
    """(⑨) ITEM_NOT_FOUND 要报近似名 —— 内部名不可猜。"""

    def test_not_found_path_builds_suggestions(self):
        lua = build_produce_item(65536, "BUILDING", "BUILDING_ARCHAEOLOGICAL_MUSEUM")
        assert "Did you mean" in lua
        assert "get_city_production" in lua, "报错要指路到权威列表工具"


class TestExcavateAction:
    """(51) 考古学家核心操作 excavate 没有工具入口 —— T304 只能 run_lua
    手发 UNITOPERATION_EXCAVATE。工具化补齐。"""

    def test_unit_action_dispatches_excavate(self):
        from civ_mcp import server

        src = inspect.getsource(server)
        idx = src.index('case "excavate"')
        assert "gs.excavate(unit_id)" in src[idx : idx + 300]

    def test_lua_builder_uses_excavate_operation(self):
        from civ_mcp.lua.units import build_excavate

        lua = build_excavate(123)
        assert "UNITOPERATION_EXCAVATE" in lua
        assert "CanStartOperation" in lua, "开挖前必须预检,否则静默无效"


class TestStoppedShortEvidenceReread:
    """(35/49) STOPPED_SHORT 是同帧误报高发区 —— 必须证据重读改写结论。

    T204 战车打长枪报 stopped short 实已命中;T205 战车攻城报 stopped short
    实际单位当场阵亡(按'没打到'重试 = 对已死单位下令+误判战场)。
    """

    def test_lua_message_states_async_uncertainty(self):
        from civ_mcp.lua.units import build_attack_unit

        lua = build_attack_unit(0, 61, 23)
        assert "resolve ASYNC" in lua, "STOPPED_SHORT 文案必须承认异步不确定性"
        assert "Do NOT re-attack" in lua

    def test_python_side_rereads_unit_survival(self):
        from civ_mcp import game_state as gsm

        src = inspect.getsource(gsm)
        idx = src.index('"STOPPED_SHORT" in result')
        seg = src[idx : idx + 3000]
        assert "ATTACK_RESOLVED_UNIT_LOST" in seg, (
            "单位没了必须改写为'攻击已结算且阵亡',不能保留'没打到'的原文"
        )
        assert "build_attack_followup_query" in seg, "目标格状态必须一并重读"


class TestUnimprovedResourceCoordinates:
    """(50) 'Needs builder' 元素自带逗号坐标,分隔符必须用分号,坐标必须保留。

    实机显示 "ANTIQUITY_SITE, 28 (unimproved)" —— "URANIUM@50,32" 被逗号
    分隔劈成 ["URANIUM@50","32"],坐标残缺到没法派建造者。
    """

    def test_lua_joins_with_semicolon(self):
        from civ_mcp.lua.cities import build_cities_query

        q = build_cities_query()
        assert 'table.concat(unimproved, ";")' in q
        assert 'table.concat(pillImprov, ";")' in q

    def test_parse_splits_citytiles_on_semicolon(self):
        import re

        from civ_mcp.lua import cities as lc

        src = inspect.getsource(lc)
        idx = src.index('line.startswith("CITYTILES|")')
        seg = src[idx : idx + 800]
        assert re.search(r'p\[2\]\.split\(";"\)', seg), "parse 必须按分号劈"

    def test_narrate_keeps_coordinates(self):
        from civ_mcp import narrate as nr

        src = inspect.getsource(nr)
        idx = src.index("Needs builder")
        head = src[max(0, idx - 600) : idx]
        assert 'split("@")[0]' not in head, (
            "不能再剥掉 @x,y —— 这行的用途就是告诉建造者去哪"
        )


class TestPendingMarkerAgeGuard:
    """(52) 挂死重载后的陈年 pending marker 不得再吞提交。"""

    def test_stale_constant_covers_hang_recovery_not_retry(self):
        from civ_mcp import end_turn_guard as etg

        # 丢响应重试在分钟级(self-heal ~15s),挂死恢复 30 分钟起 ——
        # 分界必须落在两者之间,且严于其他两个窗口常量。
        assert etg.PENDING_ADVANCE_STALE_SECONDS >= 600
        assert etg.PENDING_ADVANCE_STALE_SECONDS > etg.STALE_AFTER_SECONDS
        assert etg.PENDING_ADVANCE_STALE_SECONDS > etg.ADVANCED_GRACE_SECONDS

    def test_guard_branch_checks_pending_marker_age(self):
        from civ_mcp import end_turn as et

        src = inspect.getsource(et.execute_end_turn)
        idx = src.index("turn_at_entry > guard_from")
        seg = src[idx : idx + 900]
        # 放行(clear+正常发送)条件必须包含 pending 超龄分支;
        # 只认 advanced 的旧逻辑会把挂死重载误判成丢响应重试。
        assert "PENDING_ADVANCE_STALE_SECONDS" in seg, (
            "pending marker 无年龄检查 —— T373 假报告回归"
        )

    def test_marker_roundtrip_preserves_pending_state(self, tmp_path, monkeypatch):
        from civ_mcp import end_turn_guard as etg

        monkeypatch.setattr(etg, "_GUARD_PATH", tmp_path / "guard.json")
        etg.write_pending(372)
        m = etg.read_marker()
        assert m["from_turn"] == 372
        assert m["advanced"] is False
        assert m["written_at"] > 0
        etg.clear()
        assert etg.read_marker() == {}


class TestSpaceChainStandardRuleset:
    """(53) Standard 规则集下太空链不得再靠 GS 专属 VP API 渲染假 [0/0]。

    T380 实测:火星水培 1064/1800 在造,快照却写"链未开始需先建太空港"——
    ① 链表探测用不存在的 MARS_HABITAT(实名 MARS_HABITATION)落入假链;
    ② completed 判定依赖 GetScienceVictoryPoints(Standard 恒 0)。
    """

    def test_chain_probes_real_project_name(self):
        from civ_mcp.lua import victory as v

        q = v.build_victory_progress_query()
        assert "PROJECT_LAUNCH_MARS_HABITATION" in q, (
            "必须优先探测本体真实项目名 MARS_HABITATION"
        )
        idx_new = q.index("PROJECT_LAUNCH_MARS_HABITATION")
        idx_old = q.index('PROJECT_LAUNCH_MARS_HABITAT"')
        assert idx_new < idx_old, "HABITATION 探测必须先于旧 HABITAT 名"

    def test_completed_inference_not_vp_only(self):
        from civ_mcp.lua import victory as v

        q = v.build_victory_progress_query()
        assert "reachedTier" in q, (
            "Standard 下必须用链级推断(高层可造=>低层已完成),不能只信 sciVP"
        )
        assert "SPACEDONE|" in q

    def test_parse_reads_spacedone_and_culthreat(self):
        from civ_mcp.lua.victory import parse_victory_progress_response

        vp = parse_victory_progress_response(
            [
                "SPACEDONE|2|5",
                "CULTHREAT|美国|375|381|希腊",
            ]
        )
        assert vp.space_completed == 2
        assert vp.space_chain_total == 5
        assert vp.culture_threats["美国"] == (375, 381, "希腊")

    def test_narrate_standard_shows_inferred_not_zero_vp(self):
        from civ_mcp.lua.models import VictoryPlayerProgress, VictoryProgress
        from civ_mcp.narrate import narrate_victory_progress

        us = VictoryPlayerProgress(
            player_id=0,
            name="苏美尔",
            score=441,
            science_vp=0,
            science_vp_needed=0,
            diplomatic_vp=0,
            tourism=0,
            military_strength=1158,
            techs_researched=61,
            civics_completed=36,
            religion_cities=0,
            staycationers=52,
        )
        us.spaceports = 2
        them = VictoryPlayerProgress(
            player_id=2,
            name="美国",
            score=628,
            science_vp=0,
            science_vp_needed=0,
            diplomatic_vp=0,
            tourism=0,
            military_strength=904,
            techs_researched=68,
            civics_completed=50,
            religion_cities=1,
            staycationers=521,
        )
        them.spaceports = 1
        vp = VictoryProgress(
            players=[us, them],
            space_completed=2,
            space_chain_total=5,
            culture_threats={"美国": (375, 381, "希腊")},
        )
        text = narrate_victory_progress(vp)
        assert "0/0 VP" not in text, "Standard 下不得渲染假 [0/0] VP"
        assert "~2/5 projects done" in text
        assert "progress hidden" in text, "敌方进度必须如实标 hidden"


class TestCultureVictoryThreatSentinel:
    """(55, 败局根因) 对手文化胜利临近度必须被监控和渲染。

    T384 实录:美国 foreign tourists 375,全场最后门柱是希腊 domestic 381
    —— 差 7 人。此前没有任何简报/快照显示过这两个数;引擎"即将获得文化
    胜利"通知弹出后 1 回合他就赢了,T447 的发射计划直接作废。
    """

    def test_proximity_query_emits_culture_threat(self):
        from civ_mcp.lua import victory as v

        q = v.build_victory_proximity_query()
        assert "CUL_THREAT|" in q
        assert "GetTouristsFrom" in q
        assert "GetStaycationers" in q

    def test_proximity_query_std_science_sentinel(self):
        from civ_mcp.lua import victory as v

        q = v.build_victory_proximity_query()
        assert "SCI_THREAT_STD|" in q, (
            "Standard 下 GS 的 SCI_THREAT 永远沉默,必须有太空港数退化哨兵"
        )

    def test_end_turn_consumes_culture_threat(self):
        import inspect as _i

        from civ_mcp import end_turn as et

        src = _i.getsource(et._check_victory_proximity)
        assert "CUL_THREAT|" in src
        assert "CULTURE VICTORY IMMINENT" in src

    def test_snapshot_renders_rival_culture_progress(self):
        from civ_mcp.lua.models import VictoryPlayerProgress, VictoryProgress
        from civ_mcp.narrate import narrate_victory_progress

        us = VictoryPlayerProgress(
            player_id=0,
            name="苏美尔",
            score=441,
            science_vp=0,
            science_vp_needed=0,
            diplomatic_vp=0,
            tourism=0,
            military_strength=1158,
            techs_researched=61,
            civics_completed=36,
            religion_cities=0,
            staycationers=52,
        )
        vp = VictoryProgress(
            players=[us],
            culture_threats={"美国": (375, 381, "希腊"), "印度": (73, 521, "美国")},
        )
        text = narrate_victory_progress(vp)
        assert "RIVAL CULTURE PROGRESS" in text
        assert "美国: 375 foreign tourists, gap 6" in text
        assert "WINS IN TURNS" in text, "缺口≤10 必须是最高级警报"


class TestTurnNotSubmittedRechecksActive:
    """(54) TURN_NOT_SUBMITTED 返回前必须复核 IsTurnActive。

    T384 实录:宣战唤醒的 fortify 单位是软阻塞,第二次 ACTION_ENDTURN 被
    引擎放行 —— 回合已提交、AI 在跑,工具却按残留通知报"回合没有提交",
    agent 信了继续下命令,全被 CanStartOperation=false 拒绝。
    """

    def test_recheck_guards_the_report(self):
        import inspect as _i

        from civ_mcp import end_turn as et

        src = _i.getsource(et.execute_end_turn)
        idx = src.index("TURN_NOT_SUBMITTED|")
        head = src[max(0, idx - 3000) : idx]
        assert "IsTurnActive" in head, (
            "报'回合没提交'之前必须查引擎的 IsTurnActive —— T384 假 blocker 回归"
        )


class TestResearchQueue:
    """(56, 用户点名"卡科技/路线规划不清") 研究队列:规划一次,自动接续。

    本局科技/市政研完后 Research: None 空转至少 5 次;核聚变路线早在
    T342 就写进日记(核裂变→激光→核聚变),损失全在"计划没接进引擎"。
    队列是 fallback:只在槽位为空时消费,手动 set_research 永远优先。
    """

    def test_roundtrip_and_pop(self, tmp_path, monkeypatch):
        from civ_mcp import research_queue as rqm

        monkeypatch.setattr(rqm, "_QUEUE_PATH", tmp_path / "rq.json")
        rqm.set_queue("sumer_42", "tech", ["TECH_LASERS", "TECH_NUCLEAR_FUSION"])
        assert rqm.get_queue("sumer_42", "tech") == [
            "TECH_LASERS",
            "TECH_NUCLEAR_FUSION",
        ]
        assert rqm.pop_next("sumer_42", "tech") == "TECH_LASERS"
        assert rqm.get_queue("sumer_42", "tech") == ["TECH_NUCLEAR_FUSION"]

    def test_queue_never_bleeds_across_games(self, tmp_path, monkeypatch):
        from civ_mcp import research_queue as rqm

        monkeypatch.setattr(rqm, "_QUEUE_PATH", tmp_path / "rq.json")
        rqm.set_queue("sumer_42", "tech", ["TECH_LASERS"])
        assert rqm.get_queue("rome_7", "tech") == []
        assert rqm.pop_next("rome_7", "tech") is None
        rqm.set_queue("rome_7", "tech", ["TECH_POTTERY"])
        assert rqm.get_queue("sumer_42", "tech") == [], "换局必须丢弃旧局队列"

    def test_end_turn_consumes_only_when_slot_empty(self):
        import inspect as _i

        from civ_mcp import end_turn as et

        src = _i.getsource(et.execute_end_turn)
        idx = src.index("research_queue")
        seg = src[idx : idx + 2500]
        assert "cur_tech < 0" in seg, "只在研究槽为空(-1)时消费队列"
        assert "cur_civic < 0" in seg
        assert "FAILED" in seg, "引擎拒绝的队列项必须可见地报失败"

    def test_server_tool_validates_prefix(self):
        import inspect as _i

        from civ_mcp import server

        src = _i.getsource(server)
        idx = src.index("async def set_research_queue")
        body = src[idx : idx + 3000]
        assert "BAD_NAMES" in body, "错误前缀的名字必须显式拒绝,不能静默入队"
        assert "get_game_identity" in body, "队列必须按游戏 identity 键控"


class TestEngineBlockerBeatsCpuWatchdog:
    """(57) 报 AI_TURN_HANG 前必须先问引擎回合在谁手里。

    T11 实录(希腊局):法典研完 → 引擎挂起「填补政策槽位」阻塞,
    IsTurnActive=true / GetFirstEndTurnBlocking!=0 —— 回合根本没提交。
    工具却按 CPU 满载判据(这台机器空闲也满,6 秒烧 57 核秒)报"AI 无限
    循环",并建议杀游戏重载存档。填上政策卡后同一个回合立刻走。
    上一轮修的 pending_decision_labels 走解析器的 is_action_required
    启发式,认不出这类阻塞;引擎自己的 GetFirstEndTurnBlocking 从没被消费。
    """

    def test_helper_queries_engine_authority(self):
        import inspect as _i

        from civ_mcp import end_turn as et

        src = _i.getsource(et._engine_turn_blocker)
        assert "GetFirstEndTurnBlocking" in src, "必须查引擎权威阻塞源"
        assert "IsTurnActive" in src, "必须查回合是否仍在我方手里"

    def test_watchdog_defers_to_engine(self):
        import inspect as _i

        from civ_mcp import end_turn as et

        src = _i.getsource(et.execute_end_turn)
        idx = src.index("AI_TURN_HANG|")
        head = src[max(0, idx - 2000) : idx]
        assert "_engine_turn_blocker" in head, (
            "CPU 看门狗下结论前必须先问引擎 —— T11 假死循环回归"
        )
        assert "TURN_NOT_SUBMITTED|" in head, "我方回合仍活跃时要改报未提交"
        assert "不要杀游戏" in head, "误诊代价最大的是那句'杀游戏重载',必须反转"

    def test_pending_decisions_path_also_consumes_engine(self):
        import inspect as _i

        from civ_mcp import end_turn as et

        src = _i.getsource(et.execute_end_turn)
        idx = src.index("stuck = await _pending_decisions(gs)")
        seg = src[idx : idx + 800]
        assert "_engine_turn_blocker" in seg, (
            "启发式清单要用引擎阻塞项补齐,不能漏掉政策槽这类"
        )


class TestCultureSentinelGateFloor:
    """(58) 文化哨兵在门柱是个位数时必须闭嘴。

    T18-T37 实测(希腊局):全场游客总数个位数,哨兵每回合刷两条
    "!!! CULTURE VICTORY IMMINENT: 苏美尔 0 游客,距门柱仅差 2" ——
    技术上 gap<=10 成立,语义上荒谬。文化胜利要几百个游客(上局输
    的那次门柱是 381)。绝对差判据缺了量级下限,真警报的可信度被
    假警报一起烧掉。
    """

    def test_floor_constant_is_meaningful(self):
        from civ_mcp import end_turn as et

        assert et.CULTURE_GATE_FLOOR >= 20
        assert et.CULTURE_GATE_FLOOR < 381, "上局输局时门柱 381,下限必须远低于它"

    def test_consumer_skips_tiny_gates(self):
        """量级门槛必须在三档警报【之前】判断。

        2026-08-10:门槛判断搬进了 culture_threat_is_live(同时加了对手
        游客下限,见 test_london_siege_fixes_20260810),这里跟着改成
        断言"消费端先问它、再分档",行为断言在那个文件里。
        """
        import inspect as _i

        from civ_mcp import end_turn as et

        assert not et.culture_threat_is_live(0, 2), "开局个位数门柱必须静默"
        src = _i.getsource(et._check_victory_proximity)
        idx = src.index("CUL_THREAT|")
        seg = src[idx : idx + 1600]
        assert "culture_threat_is_live" in seg
        assert seg.index("culture_threat_is_live") < seg.index("gap <= 10"), (
            "量级门槛必须在三档警报之前判断"
        )


class TestUnfavorableExchangeGate:
    """(59) 亏本近战必须拦下来,估算不能只印不拦。

    T39 实录(希腊局):75 血勇士打丘陵上满血蛮族勇士,估算白纸黑字
    "我打16 / 我吃35"(1:2.2)且打不死 —— 攻击照常执行,打完 43 血,
    两回合后被收人头。LOW_HP 门禁没拦:打完还剩 40>20,它管的是
    "会不会死",不管"划不划算"。更要命的是估算与攻击在同一次调用里,
    看到数字时攻击已经结算,人根本没有中止的机会。
    """

    def test_ratio_constant_sane(self):
        from civ_mcp import game_state as gs

        assert 1.2 <= gs.UNFAVORABLE_EXCHANGE_RATIO <= 2.0, (
            "1:1 对拼有接应时合理,1:1.5 起才算纯亏"
        )

    def test_gate_present_before_attack_lua(self):
        import inspect as _i

        from civ_mcp import game_state as gs

        src = _i.getsource(gs.GameState.attack_unit)
        assert "UNFAVORABLE_EXCHANGE" in src
        assert src.index("UNFAVORABLE_EXCHANGE") < src.index("build_attack_unit"), (
            "拦截必须发生在真正发出攻击之前"
        )

    def test_gate_skips_kills_and_ranged(self):
        import inspect as _i

        from civ_mcp import game_state as gs

        src = _i.getsource(gs.GameState.attack_unit)
        idx = src.index("ERR:UNFAVORABLE_EXCHANGE")
        head = src[max(0, idx - 1200) : idx]
        assert "_kills" in head, "能击杀的攻击不该被拦"
        assert "is_ranged" in head, "远程无反击,不适用"
        assert "not force" in head, "必须留 force 逃生口"


class TestCultureGateFloorAppliedInSnapshot:
    """(58 第二处站点) 快照的 RIVAL CULTURE PROGRESS 段同口径静默。

    只修 end_turn 的哨兵不够:T40 快照仍在喊
    "苏美尔: 0 foreign tourists, gap 5 !!! WINS IN TURNS"。
    """

    def test_snapshot_silences_tiny_gates(self):
        from civ_mcp.lua.models import VictoryPlayerProgress, VictoryProgress
        from civ_mcp.narrate import narrate_victory_progress

        us = VictoryPlayerProgress(
            player_id=0,
            name="希腊",
            score=25,
            science_vp=0,
            science_vp_needed=0,
            diplomatic_vp=0,
            tourism=0,
            military_strength=117,
            techs_researched=4,
            civics_completed=3,
            religion_cities=0,
            staycationers=2,
        )
        vp = VictoryProgress(
            players=[us], culture_threats={"苏美尔": (0, 5, "unmet civ")}
        )
        text = narrate_victory_progress(vp)
        assert "WINS IN TURNS" not in text, "门柱 5 的时候不许喊即将胜利"
        assert "race not started" in text

    def test_snapshot_still_alarms_on_real_gates(self):
        from civ_mcp.lua.models import VictoryPlayerProgress, VictoryProgress
        from civ_mcp.narrate import narrate_victory_progress

        us = VictoryPlayerProgress(
            player_id=0,
            name="希腊",
            score=25,
            science_vp=0,
            science_vp_needed=0,
            diplomatic_vp=0,
            tourism=0,
            military_strength=117,
            techs_researched=4,
            civics_completed=3,
            religion_cities=0,
            staycationers=2,
        )
        vp = VictoryProgress(players=[us], culture_threats={"美国": (375, 381, "希腊")})
        text = narrate_victory_progress(vp)
        assert "WINS IN TURNS" in text, "真临门(上局 375/381)必须照喊"


class TestBarbarianRedDoesNotParalyzeExpansion:
    """(60) 蛮族RED不得硬拦平民生产 —— 希腊局T43-51被瘫痪八回合的根因。

    编成 CS28重装步兵×2+弓箭手×2、城防18-28,对面CS20散兵 —— 零风险,
    但门禁按"敌军距离"一票否决,开拓者T64才出(对手T50已4城),六项
    经济全场垫底。蛮族不围城,平民出生在城里无风险;出城另有
    CIVILIAN_UNESCORTED 把守。判据:蛮族来源+军事单位≥4 → 放行附日志。
    """

    def test_barb_waiver_exists_in_produce_gate(self):
        import inspect as _i

        from civ_mcp import game_state as gsm

        src = _i.getsource(gsm)
        idx = src.index("RED_ALERT_CIVILIAN|")
        head = src[max(0, idx - 3000) : idx]
        assert "barb_red" in head, "必须有蛮族豁免分支"
        assert "military_count" in head, "豁免判据必须看编成规模"
        assert ">= 4" in head


class TestPolicyUnlockGate:
    """(61) 「换卡被回合结算回滚」根因:解锁前置没被满足。

    vanilla 规则:政策重排只在市政完成的当回合免费,平时要花金解锁。
    上局 T193-T223 十轮"全部被抹"都发生在窗口关着的时候;T7 空槽填卡存活
    恰因法典当回合研完=免费窗口。

    2026-08-11 订正(缺陷 98):本类原先断言那道门必须调用
    GetCostToUnlockPolicies 并在金不够时报 POLICIES_LOCKED —— 当时判定该方法
    在这个上下文不存在,pcall 静默失败后 unlockCost 恒为 -1,判据
    `-1 > 0` 恒假,门一次也没关上过。

    2026-08-11 二次订正(缺陷 104):上面那条"方法不存在"也是错的,而这次
    的错法更糟 —— 它以 `assert "GetCostToUnlockPolicies" not in lua` 的形式
    把一条【正确的 API】写进了黑名单,等于用测试禁止修复。T305 在 InGame
    上下文实探:GetCostToUnlockPolicies 存在且返回 0,PolicyChangeMade、
    IsPolicyObsolete 同样存在。T280 探不到多半是探在 GameCore —— 同一个名字
    在两个上下文里一个有一个没有。

    真正的根因是过时卡(IsPolicyObsolete),与免费窗口无关,详见
    test_policy_window_20260811.py。这里只保留"不许把失败说成成功"这一条
    行为断言,不再对具体 API 名字做存在性/缺席性判断 —— 那正是本类栽过
    两次的地方。
    """

    def test_gate_uses_a_real_window_signal(self):
        from civ_mcp.lua.governance import build_set_policies

        lua = "\n".join(
            ln
            for ln in build_set_policies({0: "POLICY_DISCIPLINE"}).split("\n")
            if not ln.strip().startswith("--")
        )
        assert "CivicCompletedThisTurn" in lua, "免费窗口判据"
        assert "IsPolicyObsolete" in lua, (
            "过时卡才是换卡失败的实际原因 —— 必须在发请求前拦下"
        )

    def test_failure_is_reported_honestly(self):
        from civ_mcp.lua.governance import build_set_policies

        lua = build_set_policies({0: "POLICY_DISCIPLINE"})
        assert "WARN:POLICIES_NEED_GOLD" in lua, (
            "付费换卡而金不够时必须说出来,不能断言下一帧生效"
        )

    def test_async_readback_no_longer_claims_rollback(self):
        from civ_mcp.lua.governance import build_set_policies

        lua = build_set_policies({0: "POLICY_DISCIPLINE"})
        assert "POLICIES_PENDING" not in lua, (
            "同帧读旧值≠失败(异步请求下一帧结算),旧文案把成功说成引擎回滚"
        )
        assert "POLICIES_SUBMITTED" in lua


class TestPantheonDeadCivBeliefLock:
    """(63) 万神殿信仰被【已灭亡文明】锁定时工具全盲 —— 罗马局 T114-118 实测。
    法国 T105 灭亡,其万神殿信仰(DIVINE_SPARK)仍被引擎全局锁定;可选列表和
    choose_pantheon 的独占检查都只扫【存活玩家】的 GetPantheon,漏掉死国 →
    列表错列可选、选择后 FOUND_PANTHEON 被引擎静默拒绝,连报 3 回合 PENDING。
    权威判定 = Game.GetReligion():IsInSomePantheon(belief.Index)(死活都算,
    实测 ingame/gamecore 双上下文可用)。
    """

    def test_status_query_uses_engine_authoritative_check(self):
        from civ_mcp.lua.religion import build_pantheon_status_query

        lua = build_pantheon_status_query()
        assert "IsInSomePantheon" in lua, "可选列表必须用引擎权威判定,不能只扫存活玩家"

    def test_choose_pantheon_uses_engine_authoritative_check(self):
        from civ_mcp.lua.religion import build_choose_pantheon

        lua = build_choose_pantheon("BELIEF_DIVINE_SPARK")
        assert "IsInSomePantheon" in lua
        # 权威检查必须先于 FOUND_PANTHEON 请求
        assert lua.index("IsInSomePantheon") < lua.index(
            "PlayerOperations.FOUND_PANTHEON"
        ), "独占检查必须先于请求发出"

    def test_choose_pantheon_names_dead_civ_case(self):
        from civ_mcp.lua.religion import build_choose_pantheon

        lua = build_choose_pantheon("BELIEF_DIVINE_SPARK")
        assert "已灭亡" in lua, "占有者可能已灭亡(存活扫描找不到名字),文案必须说明"
        assert "BELIEF_TAKEN" in lua


class TestCityCanAttackOnlyListsHostiles:
    """get_cities 的 CAN ATTACK 列表混入友方城邦单位 —— 罗马局 T103/T113 实测。
    只判 owner~=me 会把普雷斯拉夫剑士/耶路撒冷桨帆船列为城市攻击目标,
    city_action attack 随即 CANNOT_ATTACK(unknown reason)。列出打不到的目标
    会让人以为有一次免费攻击可用。城市远程只能打交战方(蛮族恒交战,
    IsAtWarWith 覆盖)。
    """

    def test_city_targets_filtered_by_war_state(self):
        from civ_mcp.lua.cities import build_cities_query

        lua = build_cities_query()
        assert "IsAtWarWith" in lua, "CAN ATTACK 枚举必须按交战状态过滤"
        idx = lua.index("cityTargets")
        segment = lua[idx : idx + 2000]
        assert "hostile" in segment, "过滤逻辑必须落在 cityTargets 收集段内"


class TestProductionQueue:
    """(65) 城市生产空转 —— 罗马局用户点名优先。完工后 Building: nothing
    只靠简报提示,亚琛/奥斯蒂亚/拉文那/法兰克福/巴黎/阿奎莱亚各空转过
    1-2 回合。修复 = set_production_queue 工具 + end_turn pre-send 消费
    (仿研究队列:槽位空才消费、手动优先、按局键控落盘)。
    """

    def test_parse_item_infers_type_from_prefix(self):
        from civ_mcp.production_queue import parse_item

        assert parse_item("BUILDING_MARKET") == {
            "item_type": "BUILDING",
            "item_name": "BUILDING_MARKET",
        }
        assert parse_item("UNIT_TRADER")["item_type"] == "UNIT"
        assert parse_item("PROJECT_ENHANCE_DISTRICT_CAMPUS")["item_type"] == "PROJECT"

    def test_parse_item_district_requires_coords(self):
        from civ_mcp.production_queue import parse_item

        assert parse_item("DISTRICT_CAMPUS") is None, "区域无坐标必须在入队时拒绝"
        entry = parse_item("DISTRICT_CAMPUS@69,47")
        assert entry == {
            "item_type": "DISTRICT",
            "item_name": "DISTRICT_CAMPUS",
            "target_x": 69,
            "target_y": 47,
        }

    def test_parse_item_rejects_unknown_prefix(self):
        from civ_mcp.production_queue import parse_item

        assert parse_item("WONDER_PYRAMIDS") is None
        assert parse_item("") is None

    def test_queue_roundtrip_and_game_isolation(self, tmp_path, monkeypatch):
        from civ_mcp import production_queue as pqm

        monkeypatch.setattr(pqm, "_QUEUE_PATH", tmp_path / "pq.json")
        items = [
            {"item_type": "BUILDING", "item_name": "BUILDING_MARKET"},
            {"item_type": "UNIT", "item_name": "UNIT_TRADER"},
        ]
        pqm.set_queue("ROME_1", 131073, items)
        assert pqm.get_queue("ROME_1", 131073) == items
        assert pqm.get_all("ROME_1") == {131073: items}
        # 换局丢弃旧队列 —— 跨局污染比空白更糟
        assert pqm.get_queue("GREECE_2", 131073) == []
        head = pqm.pop_next("ROME_1", 131073)
        assert head["item_name"] == "BUILDING_MARKET"
        assert len(pqm.get_queue("ROME_1", 131073)) == 1
        # 弹空后城条目清除
        pqm.pop_next("ROME_1", 131073)
        assert pqm.get_all("ROME_1") == {}

    def test_end_turn_consumes_on_empty_slot_only(self):
        import inspect as _i

        from civ_mcp import end_turn as etm

        src = _i.getsource(etm)
        assert "production_queue" in src, "end_turn 必须接生产队列消费"
        idx = src.index("PQIDLE|")
        segment = src[max(0, idx - 3000) : idx + 3000]
        assert "GetCurrentProductionTypeHash" in segment, "空转判定必须问引擎生产槽"
        assert "pop_next" in segment
        assert "FAILED" in segment, "被拒项必须可见地暂停队列,不许静默"

    def test_idle_probe_runs_ingame_with_success_sentinel(self):
        """上线首回合事故:探测跑在 GameCore(BuildQueue 方法不存在)→ pcall
        全失败 → 全城误判空转 → 队列头覆盖罗马在建的大学。修复 = 探测走
        execute_write(InGame)+ PQPROBE_OK 成功哨兵,探测失败宁可不消费。
        """
        import inspect as _i

        from civ_mcp import end_turn as etm

        src = _i.getsource(etm)
        idx = src.index("PQIDLE|")
        segment = src[max(0, idx - 3000) : idx + 3000]
        assert "execute_write" in segment, "空转探测必须走 InGame 上下文"
        probe_call = segment[
            segment.index("PQPROBE_OK") - 500 : segment.index("PQPROBE_OK")
        ]
        assert "execute_read" not in probe_call, "探测不得走 GameCore(方法不存在)"
        assert "PQPROBE_OK" in segment, "必须有探测成功哨兵"
        assert "not consuming" in segment, "探测失败必须放弃消费而非全量消费"


class TestCityRangedFakeKillDisplay:
    """城市远程攻击假击杀显示 —— 罗马局 T157-159 实机:弩手打克里斯蒂安桑,
    城血 187→166 立着,结果连报 'damage dealt:200 (killed) / Target
    eliminated'。根因:eliminated 判定只看城格上有无敌方 UNIT 行,而城格
    本来就没有独立单位。城市目标的存亡必须看 CITY_DEF 行。
    """

    def test_city_target_elimination_checks_city_def(self):
        import inspect as _i

        from civ_mcp import game_state as gsm

        src = _i.getsource(gsm)
        idx = src.index('is_city_target = "target:CITY" in result')
        segment = src[idx : idx + 500]
        assert "city_def is None" in segment, (
            "城市目标的 eliminated 必须以 CITY_DEF 消失为准,不能只看单位行"
        )


class TestMoveDiagnosticsTerritoryTruth:
    """move 诊断误导修复 —— T136/T146 实机:开放边界已生效仍报 'need Open
    Borders';城邦领海被报成 'can embark'。诊断必须先问引擎的真实归属/
    边界状态再下结论。
    """

    def test_border_branch_checks_open_borders(self):
        from civ_mcp.lua import units as lu

        src = lu.build_unit_position_query(1, 2, 3)
        assert "HasOpenBordersFrom" in src
        assert "BORDER_OPEN" in src, "已开放边界时必须明说障碍另有其因"

    def test_water_branch_checks_owner(self):
        from civ_mcp.lua import units as lu

        src = lu.build_unit_position_query(1, 2, 3)
        assert "WATER_BORDER_CS" in src, "城邦领海必须点名,不能只报 can embark"


class TestTradeAvailableNotificationSurfaced:
    """用户反馈:「交易界面你经常无视很多次了」—— "有新的可用交易"通知
    T106-T160 反复被当噪音。修复 = 报告组装时检测该通知并升为独立事件行,
    附 get_pending_trades / propose_trade(test) 的可执行引导。
    """

    def test_trade_notification_becomes_event(self):
        import inspect as _i

        from civ_mcp import end_turn as etm

        src = _i.getsource(etm)
        assert "可用交易" in src, "必须检测交易可用通知"
        idx = src.index("别再跳过")
        segment = src[max(0, idx - 1500) : idx + 800]
        assert "get_pending_trades" in segment
        assert "propose_trade" in segment, "必须给到可执行的卖出引导"


class TestGPActivateMovesHint:
    """GP 激活报错自相矛盾 —— T139-140 实机:站在有效格上 CANNOT_ACTIVATE,
    Valid tiles 列着当前格,两回合试错才发现根因是当回合已移动。失败信息
    必须显式点出移动力根因。
    """

    def test_activate_error_includes_moves_hint(self):
        from civ_mcp.lua.great_people import build_activate_great_person

        lua = build_activate_great_person(0)
        assert "GetMovesRemaining" in lua
        assert "移动力" in lua, "报错必须点出移动力根因"


class TestAgreementDealItemsCarryDuration:
    """(64) 协议类交易项不设时长 → 结算为空操作 —— 罗马局 T130-T135 实机。
    开放边界 deal 三次 ACCEPTED(含 100 金代价)、成交条款正常显示,但
    HasOpenBordersFrom 恒 false、领土照样封锁。根因:AddItemOfType(AGREEMENTS)
    后只 SetSubType 不 SetDuration,0 时长协议通过 AI 评估却在结算时被丢弃。
    实机验证:补 SetDuration(30) 后同款交易当帧生效,pending 展示也从
    「Open Borders」变为「Open Borders for 30 turns」。
    """

    def test_open_borders_item_sets_duration(self):
        from civ_mcp.lua.diplomacy import _lua_deal_item

        lua = _lua_deal_item("me", {"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
        assert "SetDuration(30)" in lua, "开放边界默认 30 回合时长,缺了就是空操作"
        assert "DealAgreementTypes.OPEN_BORDERS" in lua

    def test_agreement_duration_overridable(self):
        from civ_mcp.lua.diplomacy import _lua_deal_item

        lua = _lua_deal_item(
            "target", {"type": "AGREEMENT", "subtype": "OPEN_BORDERS", "duration": 45}
        )
        assert "SetDuration(45)" in lua

    def test_alliance_path_sets_duration(self):
        from civ_mcp.lua.diplomacy import build_form_alliance

        lua = build_form_alliance(5, "ALLIANCE_MILITARY")
        agreements_idx = lua.index("DealAgreementTypes.ALLIANCE")
        segment = lua[agreements_idx : agreements_idx + 300]
        assert "SetDuration(30)" in segment, "联盟协议项同样必须带时长"
