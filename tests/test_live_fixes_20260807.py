"""2026-08-07 苏美尔实机局抓到的缺陷回归测试。

这一批全部来自【真实对局】,不是设想出来的场景。单元测试全绿也发现不了 ——
它们要么是异步结算的时序问题,要么是一个函数错导致两处结论一起错。

  ① 把"游戏在等玩家决策"误报成"AI 死循环":每次空等 4 分钟,还建议杀游戏重载。
     极可能就是用户报的「T300+ 卡死」的主因。
  ③ 移动成功却报 BLOCKED:InGame 移动是异步结算的,紧跟着读位置读到的是起点。
  ⑤ 侦察兵被判定为"能占城":一个函数错,占城判定与攻坚编成统计一起错。
  ⑤b 单位对单位的兵种克制:决定打之前一个字都没有。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import end_turn as et
from civ_mcp import game_state as gs
from civ_mcp import war_planner as wp

# ---------------------------------------------------------------------------
# ① 回合没提交 = 游戏在等你做决定
# ---------------------------------------------------------------------------


def _note(summary, action=True):
    return NS(summary=summary, is_action_required=action)


def test_pending_decisions_from_notifications():
    """权威源是游戏自己的 is_action_required —— 它早就在返回,只是没人消费。"""
    got = et.pending_decision_labels(
        [
            _note("选择1项科技"),
            _note("填补政策槽位"),
            _note("已发现制陶术", action=False),  # 纯信息,不挡回合
        ]
    )
    assert got == ["选择1项科技", "填补政策槽位"]


def test_research_choice_is_caught():
    """此前只查单位移动力,"选科技"这一类一个都抓不到 —— 于是照样空等 240 秒。

    实机 T17/T57:采矿业研完 → 游戏等选科技 → 回合不提交 → 假 AI_TURN_HANG。
    """
    assert et.pending_decision_labels([_note("选择1项科技")]) == ["选择1项科技"]


def test_informational_notifications_never_block():
    assert et.pending_decision_labels([_note("贸易路线容量增加了", action=False)]) == []
    assert et.pending_decision_labels([]) == []
    assert et.pending_decision_labels(None) == []


def test_blank_summaries_are_dropped():
    assert et.pending_decision_labels([_note("   "), _note("")]) == []


# ---------------------------------------------------------------------------
# ③ 移动异步结算 —— 读到起点不等于没动
# ---------------------------------------------------------------------------


def test_unmoved_reading_triggers_recheck():
    """第一帧读到起点是常态,不是阻塞证据 —— 该延迟重读。"""
    assert gs._looks_unmoved("MOVING_TO|64,34|from:60,29", ["POS|60|29"])


def test_moved_reading_needs_no_recheck():
    assert not gs._looks_unmoved("MOVING_TO|64,34|from:60,29", ["POS|61|30"])


def test_gone_unit_is_not_unmoved():
    assert not gs._looks_unmoved("MOVING_TO|1,1|from:60,29", ["POS|GONE"])


def test_missing_from_is_safe():
    """没有 from: 段时不做任何判断,不能凭空触发重读。"""
    assert not gs._looks_unmoved("MOVING_TO|1,1", ["POS|60|29"])


def test_malformed_pos_line_is_safe():
    assert not gs._looks_unmoved("MOVING_TO|1,1|from:60,29", ["POS|x|y"])


# ---------------------------------------------------------------------------
# ⑤ 侦察兵不能占城 —— 一个函数错,两处结论一起错
# ---------------------------------------------------------------------------


def test_recon_cannot_capture_cities():
    """此前有一条 `if RECON: return True`,注释写"侦察兵是近战攻击,可以占城"。

    那是错的。后果不只是占城判错:camp_campaign 用同一个函数统计攻坚编成,
    于是简报说"编成已经够了(3 个军事单位、2 个近战)"—— 把 CS10 的侦察兵
    算成了攻坚力量。实机 T25-T43 一直这么报。
    """
    assert wp.can_capture_city("UNIT_SCOUT") is False


def test_melee_and_cavalry_still_capture():
    assert wp.can_capture_city("UNIT_WARRIOR") is True
    assert wp.can_capture_city("UNIT_SUMERIAN_WAR_CART") is True


def test_ranged_and_siege_still_cannot_capture():
    assert wp.can_capture_city("UNIT_ARCHER") is False


# ---------------------------------------------------------------------------
# ⑤b 兵种克制:决定打之前就要说
# ---------------------------------------------------------------------------


def test_cavalry_into_anticav_is_warned():
    """实机 T39:战车(重骑)打长矛(反骑),CS30 实质对 CS35。
    战斗估算算对了数字,但从没说破【为什么】守方 CS 更高。"""
    w = wp.counter_warning("UNIT_SUMERIAN_WAR_CART", "UNIT_SPEARMAN")
    assert w and "克制" in w


def test_melee_into_anticav_is_not_warned():
    """反骑只克骑兵,近战打它没有惩罚 —— 不能滥报。"""
    assert wp.counter_warning("UNIT_WARRIOR", "UNIT_SPEARMAN") == ""


def test_cavalry_into_recon_is_not_warned():
    assert wp.counter_warning("UNIT_SUMERIAN_WAR_CART", "UNIT_SCOUT") == ""


def test_unknown_units_are_silent():
    assert wp.counter_warning("UNIT_NONSENSE", "UNIT_ALSO_NONSENSE") == ""


def test_warning_does_not_invent_numbers():
    """加成数值没有导出,而游戏的战斗估算算得对 —— 提醒只给方向,不编数字。"""
    w = wp.counter_warning("UNIT_SUMERIAN_WAR_CART", "UNIT_SPEARMAN")
    assert "+10" not in w and "战斗估算为准" in w


# ---------------------------------------------------------------------------
# ⑨ 任务板不能推荐做不了的活
# ---------------------------------------------------------------------------


def test_builder_tasks_filters_by_prereq():
    """实机 T66:任务板把"建伐木场"列为可做,但伐木场要「机械」科技 ——
    派建造者过去才发现做不了,白扔一回合移动。

    根因是今晚第五次出现的同一形态:【同一个概念两套实现】——
    资源格分支有科技检查,空地分支没有。所以这里同时断言两点:
    检查函数存在,且**两条分支都走它**(而不是各写一份)。
    """
    from civ_mcp.lua.units import build_builder_tasks_query

    lua = build_builder_tasks_query()
    assert "local function impUnlocked" in lua
    assert "not impUnlocked(validImp)" in lua, "资源格分支要走共用函数"
    assert "not impUnlocked(bestImp)" in lua, "空地分支要走共用函数"


def test_builder_tasks_has_no_duplicate_tech_check():
    """两套实现是病根 —— 内联的那份必须删掉,不能留着各改各的。"""
    from civ_mcp.lua.units import build_builder_tasks_query

    code = [
        ln
        for ln in build_builder_tasks_query().splitlines()
        if not ln.strip().startswith("--")
    ]
    assert "HasTech(techInfo.Index)" not in "\n".join(code)


def test_builder_tasks_fails_open_on_unknown_improvement():
    """查不到的改良放行 —— 数据缺失不该让整块任务板静默。"""
    from civ_mcp.lua.units import build_builder_tasks_query

    lua = build_builder_tasks_query()
    idx = lua.index("local function impUnlocked")
    assert "return true" in lua[idx : idx + 400]


def test_builder_tasks_skips_feature_blocked_hills():
    """实机 T72:任务板推荐"(55,25) 建矿",但那格是【丛林覆盖的丘陵】——
    丛林必须先砍掉才能建矿。根因是 `if plot:IsHills()` 排在地貌判断【之前】,
    于是丘陵格无条件给矿。与前置科技那条同一个病:推荐了做不了的活。"""
    from civ_mcp.lua.units import build_builder_tasks_query

    lua = build_builder_tasks_query()
    assert 'fName0 == "FEATURE_JUNGLE"' in lua
    # 地貌判断必须排在丘陵判断之前
    assert lua.index('fName0 == "FEATURE_JUNGLE"') < lua.index("elseif plot:IsHills()")


# ---------------------------------------------------------------------------
# ⑪ 海军可达范围 —— safety.py 这条路径没同步 8-06 的修正
# ---------------------------------------------------------------------------


def test_naval_reach_excludes_movement_in_safety_gate():
    """实机 T81:移动3/射程0 的蛮族桨帆船被算成"可达3格",
    于是内陆建造者被 CIVILIAN_UNESCORTED 拦住 —— 海军近战打不到陆地单位。

    同一个修正 8-06 已经在 end_turn._threat_reach 做过,但 safety.py 没同步。
    这是今晚第六次"同一概念两套实现"。
    """
    from civ_mcp.safety import EnemySighting

    galley = EnemySighting(
        x=65,
        y=33,
        dist=3,
        unit_type="UNIT_GALLEY",
        cs=30,
        owner="Barbarian",
        moves=3,
        range_=0,
    )
    assert galley.reach == 1, "海军可达应只取射程(近战取1),不加移动力"
    assert not galley.threatens, "3 格外的近战海军够不到内陆平民"


def test_land_reach_still_counts_movement():
    """陆地威胁的口径不能被改坏 —— 骑手移动4就是能扑4格。"""
    from civ_mcp.safety import EnemySighting

    horseman = EnemySighting(
        x=1,
        y=1,
        dist=4,
        unit_type="UNIT_BARBARIAN_HORSEMAN",
        cs=35,
        owner="Barbarian",
        moves=4,
        range_=0,
    )
    assert horseman.reach == 4 and horseman.threatens


def test_ranged_naval_still_reaches_its_range():
    """海军远程(四段帆船)能轰岸 —— 射程内仍要拦。"""
    from civ_mcp.safety import EnemySighting

    quad = EnemySighting(
        x=1,
        y=1,
        dist=2,
        unit_type="UNIT_QUADRIREME",
        cs=20,
        owner="Barbarian",
        moves=3,
        range_=2,
    )
    assert quad.reach == 2 and quad.threatens


# ---------------------------------------------------------------------------
# ⑬ 赶路中的平民不是滞留
# ---------------------------------------------------------------------------


def _walk(path, charges=2):
    """按坐标序列逐回合跑基线,返回最后一回合的"滞留回合数"。"""
    entry = None
    for turn, pos in enumerate(path, start=1):
        entry = et.stall_baseline(
            entry if entry is not None else (turn, charges, tuple(pos)),
            turn,
            charges,
            pos,
        )
    return len(path) - entry[0]


def test_traveling_builder_is_not_stalled():
    """实机 T105:建造者每回合都在走(58,33→59,32→60,32…),却被报"滞留6回合"。

    根因:基线只在【充能变化】时重置,而走路不消耗充能。
    误伤比漏拦贵 —— 拦错几次就会养成无脑再调一次 end_turn 的习惯。
    """
    walking = [(50 + i, 30) for i in range(9)]
    assert _walk(walking) == 0, "每回合都在移动 = 没闲着"


def test_traveling_settler_is_not_stalled():
    """开拓者更糟:它没有充能(恒为 0),原条件永不成立 ——
    只要路上走超过 5 回合就【必然】误报。"""
    walking = [(50 + i, 30) for i in range(9)]
    assert _walk(walking, charges=0) == 0


def test_truly_idle_builder_is_still_flagged():
    """豁免不能过头:真站着不动的照拦(>5 回合)。"""
    assert _walk([(60, 30)] * 9) > 5


def test_charge_spend_also_resets_baseline():
    """干完活也要重置 —— 原来那条规则不能被改坏。"""
    entry = (1, 3, (60, 30))
    got = et.stall_baseline(entry, 8, 2, (60, 30))  # 充能 3→2,位置没变
    assert got[0] == 8


def test_stale_entry_accepts_all_three_formats():
    """跨版本状态兼容:裸回合数 / (回合,充能) / (回合,充能,位置)。"""
    assert et._stale_entry(7, 3, (1, 2)) == (7, 3, (1, 2))
    assert et._stale_entry((7, 3), 3, (1, 2)) == (7, 3, (1, 2))
    assert et._stale_entry((7, 3, (4, 5)), 3, (1, 2)) == (7, 3, (4, 5))


# ---------------------------------------------------------------------------
# ⑯ 闲置资源哨兵要留安全垫(我自己今天写的功能的洞,实机打出来的)
# ---------------------------------------------------------------------------


def _ov_gold(gold, gpt=20.0, maint=5.0, faith=0.0):
    return NS(
        gold=gold,
        gold_per_turn=gpt,
        total_maintenance=maint,
        faith=faith,
        gold_income=gpt + maint,
        unit_maintenance=int(maint),
    )


def test_idle_gold_reports_spendable_not_total():
    """实机 T110:哨兵只说"该花"没说"留多少",我照做花到 0,
    触发国库破产→单位强制解散(赔了一个披甲战士)。
    当时收入+15/回合、维护仅5 —— 本来完全健康,钱是被花光的。"""
    ev = et.idle_resource_event(_ov_gold(500.0), [])
    assert ev is not None
    assert "可自由支配" in ev.message
    assert "安全垫" in ev.message


def test_cushion_scales_with_maintenance():
    """安全垫按维护费倍数算,不写死金额 —— 后期维护是三位数。"""
    low = et.idle_resource_event(_ov_gold(900.0, maint=5.0), [])
    high = et.idle_resource_event(_ov_gold(900.0, maint=60.0), [])
    # 维护高的那局,安全垫更大 → 可自由支配更少
    assert low is not None and high is not None
    import re

    def _spendable(msg):
        return float(re.search(r"可自由支配 (\d+)", msg).group(1))

    assert _spendable(high.message) < _spendable(low.message)


def test_no_advice_when_everything_is_cushion():
    """余额刚够安全垫时不该催花钱。"""
    ev = et.idle_resource_event(_ov_gold(210.0, gpt=10.0, maint=50.0), [])
    assert ev is None or "可自由支配" not in ev.message


# ---------------------------------------------------------------------------
# ⑰ 跨域近战 = 静默无效,却报 OK:MELEE_ATTACK
#
# 实机 T114/T115(苏美尔国王局):战车(陆地近战)连着两回合攻击 (65,33)
# 的海上蛮族桨帆船,两次都返回 OK:MELEE_ATTACK 并给出"造成约 33 伤害"的
# 战斗估算 —— 桨帆船始终 45/100,战车始终 100 血、移动力也没扣。
# 两回合白扔,而且 get_units 每回合都在 CAN ATTACK 里继续列它。
#
# 引擎不拒绝:CanStartOperation(MOVE_TO + ATTACK) 返回 true,
# RequestOperation 静默不执行。所以只能靠 Domain 显式判。
#
# 这是 2026-08-04「远程单位降级近战打海上 GALLEY 报假成功」的另一半 ——
# 那次修的是远程执行路径,枚举侧和近战执行路径一直漏着。三处必须同源。
# ---------------------------------------------------------------------------

from civ_mcp.lua import units as _lua_units  # noqa: E402


def test_melee_domain_gate_present_in_attack_path():
    lua = _lua_units.build_attack_unit(1, 5, 5)
    assert "meleeDomainOK" in lua
    assert "ERR:DOMAIN_MISMATCH" in lua
    # 必须在 RequestOperation 之前拦掉,否则照样打印 OK
    assert lua.index("meleeDomainOK(unitInfo") < lua.index(
        "UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)"
    )


def test_melee_domain_gate_present_in_enumeration():
    """只修执行路径不够:CAN ATTACK 继续列出来,就会继续按"有一次免费
    攻击"来部署整个回合(与 cities.py 城市射程那条同一形态)。"""
    lua = _lua_units.build_units_query()
    assert "meleeDomainOK" in lua


def test_both_sites_share_one_definition():
    """单源化。今晚 16 个缺陷里有 7 个是"同一个概念两套实现",
    这条测试就是防第 8 次。"""
    from civ_mcp.lua import _helpers

    body = _helpers._LUA_MELEE_DOMAIN
    assert body in _lua_units.build_attack_unit(1, 5, 5)
    assert body in _lua_units.build_units_query()


def test_domain_gate_fails_open_on_unreadable_city():
    """海军打沿海城市是合法的。城市探测失败时不许拦 ——
    误伤一次就会养成无脑 force 的习惯。"""
    from civ_mcp.lua import _helpers

    body = _helpers._LUA_MELEE_DOMAIN
    assert "cityKnown" in body
    assert 'dom == "DOMAIN_SEA" and (not water) and cityKnown' in body


def test_land_vs_water_branch_needs_no_city_probe():
    """陆打水那一支不依赖城市探测(城市永远不在水面格),
    保持严格 —— 它才是实机真正咬人的那一支。"""
    from civ_mcp.lua import _helpers

    land_branch = _helpers._LUA_MELEE_DOMAIN.split('dom == "DOMAIN_LAND" and water')[1]
    assert land_branch.split("end")[0].strip().startswith("then")
    assert "LAND_VS_WATER" in land_branch


# ---------------------------------------------------------------------------
# ⑱ 政体从未升级 —— 酋邦挂到 T116
#
# 实机(苏美尔国王局 T116):政治哲学早就研完(防御战术都进候选了),
# 我却还挂着酋邦 = 2 个政策槽。换成古典共和当场变 4 槽。
# 也就是说前面 ~60 回合每回合都少吃两张卡的加成,而且是复利亏
# (少的那两张里有「殖民」+50% 开拓者产能 —— 正好是这局差距最大的一项)。
#
# 为什么一直没报:tech_path 的「下一个政体」算的是【还没研的市政目标】,
# 酋邦时它指向 42 回合外的王权神授,读起来像"政体这条线在推进中"。
# 全仓没有任何一处问过「手上这个政体是不是当前能用的最好的」。
# 与 #3「政策卡从不复审」同源:系统只会往前推进,不会回头审视存量。
# ---------------------------------------------------------------------------

from civ_mcp import policy as _policy  # noqa: E402

_PHIL = "CIVIC_POLITICAL_PHILOSOPHY"


def _gov_ctx(gov, civics=(), **kw):
    return _policy.PolicyContext(government=gov, completed_civics=set(civics), **kw)


def test_chiefdom_with_political_philosophy_is_reported():
    """实机复现:酋邦 + 政治哲学已完成 = 必须报。"""
    ev = _policy.government_upgrade_advice(_gov_ctx("GOVERNMENT_CHIEFDOM", [_PHIL]))
    assert ev is not None
    assert ev.severity == "warn"
    assert "GOVERNMENT_CHIEFDOM" in ev.text
    assert "change_government" in ev.text


def test_quantifies_the_loss_in_cards_not_in_adjectives():
    """ "该换政体了"没有行动力度;"每回合白扔 2 张卡"才有。"""
    ev = _policy.government_upgrade_advice(_gov_ctx("GOVERNMENT_CHIEFDOM", [_PHIL]))
    assert "白扔 2 张卡" in ev.text


def test_silent_before_the_prereq_civic():
    """政治哲学没研完时,酋邦是当前最好的 —— 一个字都不该说。"""
    assert (
        _policy.government_upgrade_advice(_gov_ctx("GOVERNMENT_CHIEFDOM", [])) is None
    )


def test_silent_when_already_on_the_widest_government():
    """古典共和(4 槽)在同级里已是最宽,不该继续催。"""
    ev = _policy.government_upgrade_advice(
        _gov_ctx("GOVERNMENT_CLASSICAL_REPUBLIC", [_PHIL])
    )
    assert ev is None


def test_lists_options_but_does_not_pick_one():
    """槽位数是事实,政体特性是决策 —— 只给能选的清单和槽位构成。"""
    ev = _policy.government_upgrade_advice(_gov_ctx("GOVERNMENT_CHIEFDOM", [_PHIL]))
    assert "GOVERNMENT_CLASSICAL_REPUBLIC" in ev.text
    assert "ECONOMIC×2" in ev.text


def test_unknown_government_stays_silent():
    """gamedata 查不到就闭嘴,不瞎报(与 _has_card_for_slot 的 fail-open 相反:
    那边漏报是静默一条真建议,这边误报是逼人做一次无谓的政体切换)。"""
    assert (
        _policy.government_upgrade_advice(_gov_ctx("GOVERNMENT_MADE_UP", [_PHIL]))
        is None
    )
    assert _policy.government_upgrade_advice(_gov_ctx("", [_PHIL])) is None


def test_ordered_before_empty_slot_advice():
    """换政体会重排全部槽位 —— 先补卡再换 = 白补一次。"""
    ctx = _policy.PolicyContext(
        slots={"SLOT_ECONOMIC": [""], "SLOT_MILITARY": [""]},
        available={"POLICY_URBAN_PLANNING"},
        government="GOVERNMENT_CHIEFDOM",
        completed_civics={_PHIL},
    )
    out = _policy.check_policies(ctx)
    texts = [a.text for a in out]
    gov_i = next(i for i, t in enumerate(texts) if "change_government" in t)
    slot_i = next(i for i, t in enumerate(texts) if "空政策槽" in t)
    assert gov_i < slot_i


def test_wired_into_briefing_context():
    """接线回归:PolicyContext 有字段但简报不传 = 永远静默。
    今晚 A4 就是这么踩的(住房字段挂错集合,单测全绿实机全哑)。"""
    import inspect

    from civ_mcp import end_turn as _et

    src = inspect.getsource(_et._render_policy_advice)
    assert "government=" in src and "completed_civics=" in src
    call = (
        inspect.getsource(_et._turn_briefing) if hasattr(_et, "_turn_briefing") else ""
    )
    del call  # 调用点在大函数里,下面用文件级检查代替
    whole = inspect.getsource(_et)
    assert (
        '_render_policy_advice(\n            policy_status, cities_list, wm, _pc.get("civics")'
        in whole
    )


# ---------------------------------------------------------------------------
# ⑲ 军队编成:纸面军力是标量,战斗力是向量
#
# 用户点名的三大战略缺陷之一("全是前排全是后排")。系统一直只数
# 军力值和单位数量,从不看兵种构成。两头都被实机打脸过:
#   · 苏美尔局 T116:4 战车 + 0 远程 —— 打不掉 45 血的蛮族桨帆船
#     (跨域够不着),也磨不动城墙。军力 138"在涨",部队什么都办不成。
#   · T362 罗马局:7 加农炮 + 6 弩手 + 2 近战 —— 一座城都没打下来。
# ---------------------------------------------------------------------------

from civ_mcp import war_planner as _wp  # noqa: E402


def test_all_frontline_is_reported():
    """实机复现:4 个战车 0 个远程。"""
    gap = _wp.composition_gap(["UNIT_SUMERIAN_WAR_CART"] * 4)
    assert gap and "全是前排" in gap
    assert "远程" in gap


def test_all_backline_is_reported():
    """T362 的反面形态,同一个函数要同时认得。"""
    gap = _wp.composition_gap(["UNIT_CATAPULT"] * 3 + ["UNIT_ARCHER"] * 3)
    assert gap and "全是后排" in gap
    assert "占不了" in gap


def test_balanced_army_is_silent():
    gap = _wp.composition_gap(
        ["UNIT_SWORDSMAN", "UNIT_SPEARMAN", "UNIT_ARCHER", "UNIT_CATAPULT"]
    )
    assert gap == ""


def test_tiny_force_is_not_an_army():
    """开局 1-2 个单位不是"部队",报编成只会变成噪音。"""
    assert _wp.composition_gap(["UNIT_WARRIOR"]) == ""
    assert _wp.composition_gap(["UNIT_WARRIOR", "UNIT_SLINGER"]) == ""


def test_scouts_do_not_count_as_frontline():
    """与 can_capture_city 同源的坑:CS10 的侦察兵不是攻坚力量。
    3 个侦察兵 + 0 其他 = 根本不是作战部队,应静默而不是报"全是前排"。"""
    assert _wp.composition_gap(["UNIT_SCOUT"] * 4) == ""


def test_slightly_lopsided_reports_which_leg_is_short():
    """5 前 1 后(17%)低于 25% 阈值 —— 要点名缺的是哪条腿。"""
    gap = _wp.composition_gap(["UNIT_SWORDSMAN"] * 5 + ["UNIT_ARCHER"])
    assert gap and "远程/攻城" in gap and "前排 5 / 后排 1" in gap


def test_composition_wired_into_briefing():
    """接线回归 —— 有函数没接线 = 永远静默(A4 住房字段的教训)。"""
    import inspect

    from civ_mcp import end_turn as _et

    src = inspect.getsource(_et._situation_block)
    assert "composition_gap" in src
    assert "combat_strength" in src


# ---------------------------------------------------------------------------
# ⑳ 政策缓存换政体后不失效 —— 已照做的建议继续刷屏
#
# 实机 T116→T117:T116 换成古典共和(4 槽),T117 简报仍照 3 回合前的
# 快照喊「政体还挂着 GOVERNMENT_CHIEFDOM(2 槽)…每回合白扔 2 张卡」。
#
# end_turn 的降频注释写着「政策槽只在解锁新市政/换政体时变,而那两件事
# 都有专门的通知」—— 假设是对的,但全仓没有一处在那两件事发生时作废缓存。
# 缓存的正确性不靠注释里的假设,靠写入点主动作废。
#
# 一条已经照做过的建议继续出现,比不报更伤:它会让人开始怀疑整个建议层。
# ---------------------------------------------------------------------------


class _StubConn:
    def __init__(self):
        self.calls = []

    async def execute_write(self, lua):
        self.calls.append(lua)
        return ["GOVERNMENT_CHANGED|GOVERNMENT_CLASSICAL_REPUBLIC|古典共和"]


def _gs_with_cache():
    from civ_mcp.game_state import GameState

    g = GameState.__new__(GameState)
    g.conn = _StubConn()
    g._expert_cache = {"policy_status": object(), "policy_turn": 116, "civics": {"X"}}
    return g


def test_change_government_invalidates_policy_cache():
    import asyncio

    g = _gs_with_cache()
    asyncio.run(g.change_government("GOVERNMENT_CLASSICAL_REPUBLIC"))
    assert "policy_status" not in g._expert_cache
    assert "policy_turn" not in g._expert_cache


def test_invalidation_keeps_unrelated_cache_entries():
    """只作废政策那两个键 —— 顺手清空整个缓存会让本回合其余接线全部重查。"""
    g = _gs_with_cache()
    g._invalidate_policy_cache()
    assert g._expert_cache["civics"] == {"X"}


def test_invalidate_is_safe_without_cache():
    from civ_mcp.game_state import GameState

    g = GameState.__new__(GameState)
    g._invalidate_policy_cache()  # 不抛


def test_set_policies_also_invalidates():
    """换卡同样改变槽位内容。两个写入点必须都作废 ——
    只改一个就是今晚第 9 次「同一个概念两套实现」。"""
    import inspect

    from civ_mcp.game_state import GameState

    assert "_invalidate_policy_cache" in inspect.getsource(GameState.set_policies)
    assert "_invalidate_policy_cache" in inspect.getsource(GameState.change_government)


# ---------------------------------------------------------------------------
# (21) change_government 只"考虑"不提交 —— 报成功,两回合后政体回退
#
# 实机(苏美尔国王局 T116→T118):调 change_government 返回
# OK:GOVERNMENT_CHANGED,紧接着 get_policies 显示「古典共和,4 个槽」。
# T117/T118 再查,又是「酋邦,2 个槽」。
#
# 根因:Civ6 里换政体与排卡共用【同一个待确认变更集】——
#   RequestChangeGovernment = 放进待定集(只是"考虑")
#   RequestPolicyChanges    = 提交整个待定集
# 旧实现到 RequestChangeGovernment 就 print OK 了,从不提交。
# 而同帧 GetNumPolicySlots() 返回的是【考虑中】政体的槽位数,
# 所以 get_policies 当场显示新政体 —— 假成功里最难发现的一种:
# 连"立刻复核"都会骗过去,只有跨回合才露馅。
#
# 雪上加霜的是紧跟着调的 set_policies:它开头的 UNLOCK_POLICIES 会重置
# 待定集,把政体变更撤销,再把 4 张卡提交到只有 2 槽的政体上。
# 一次调用报成功,另一次调用把它悄悄撤销,两次都不报错。
#
# 这是今晚第 5 个「报了成功、实际没发生」:
#   远程降级近战打海上单位 / 陆地近战打海上单位 / 政策卡 readback /
#   移动假 BLOCKED / 本条。共同点都是【只请求不验证】。
# ---------------------------------------------------------------------------

from civ_mcp.lua import governance as _gov  # noqa: E402


def test_change_government_commits_the_pending_set():
    lua = _gov.build_change_government("GOVERNMENT_CLASSICAL_REPUBLIC")
    assert "RequestChangeGovernment" in lua
    assert "RequestPolicyChanges" in lua, "只考虑不提交 = 回合结算时丢弃"
    assert lua.index("RequestChangeGovernment") < lua.index("RequestPolicyChanges")


def test_change_government_verifies_before_claiming_success():
    """同帧 GetNumPolicySlots 会骗人,只有 GetCurrentGovernment 是真值。"""
    lua = _gov.build_change_government("GOVERNMENT_OLIGARCHY")
    assert "GetCurrentGovernment()" in lua
    assert "OK:GOVERNMENT_CHANGED" in lua
    assert "NOTE:GOVERNMENT_PENDING" in lua
    # 未验证通过时绝不能落到 OK 分支
    ok_i = lua.index("OK:GOVERNMENT_CHANGED")
    pend_i = lua.index("NOTE:GOVERNMENT_PENDING")
    assert ok_i < pend_i, "OK 必须在 nowGov == row.Index 那一支里"


def test_pending_message_warns_about_ordering():
    """顺序错一次就白丢一个政体窗口 —— 警告必须带在 PENDING 消息里。"""
    lua = _gov.build_change_government("GOVERNMENT_AUTOCRACY")
    assert "set_policies" in lua and "撤销" in lua


class _GovConn:
    def __init__(self, lines):
        self._lines = lines

    async def execute_write(self, lua):
        return self._lines


def _gs_for_gov(lines):
    from civ_mcp.game_state import GameState

    g = GameState.__new__(GameState)
    g.conn = _GovConn(lines)
    g._expert_cache = {}
    g._pending_government = None
    return g


def test_set_policies_blocked_while_government_pending():
    """执行前拦截,不是文档提醒。"""
    import asyncio

    g = _gs_for_gov(
        ["NOTE:GOVERNMENT_PENDING|GOVERNMENT_CLASSICAL_REPUBLIC|已提交但同帧未结算。"]
    )
    asyncio.run(g.change_government("GOVERNMENT_CLASSICAL_REPUBLIC"))
    assert g._pending_government == "GOVERNMENT_CLASSICAL_REPUBLIC"
    out = asyncio.run(g.set_policies({0: "POLICY_COLONIZATION"}))
    assert out.startswith("ERR:GOVERNMENT_PENDING")
    assert "先 end_turn" in out


def test_verified_change_does_not_block_policies():
    """同帧就验证通过时,排卡没有风险 —— 不要误伤。"""
    import asyncio

    g = _gs_for_gov(
        ["OK:GOVERNMENT_CHANGED|GOVERNMENT_OLIGARCHY|寡头政治|committed+verified"]
    )
    asyncio.run(g.change_government("GOVERNMENT_OLIGARCHY"))
    assert g._pending_government is None


def test_end_turn_clears_the_pending_flag():
    """标记只在本回合内有效 —— 不清就会永久挡住排卡。"""
    import inspect

    from civ_mcp import end_turn as _et

    assert "gs._pending_government = None" in inspect.getsource(_et)


# ---------------------------------------------------------------------------
# (22) 建造者任务板推荐做不了的活 —— 森林山丘被列为"建矿"
#
# 实机 T119:任务板列 (59,32) build MINE,派建造者过去两回合,到了才报
#   CANNOT_IMPROVE|IMPROVEMENT_MINE is not valid [terrain=PLAINS_HILLS
#   feature=FEATURE_FOREST]
#
# 同一天早些时候刚修过丛林丘陵的同一个 bug —— 当时的修法是把
# FEATURE_JUNGLE / FEATURE_MARSH 提到 IsHills() 前面,**森林漏了**。
# 修了一半的经典形态,第 10 次「同一个概念两套实现/修一半」。
#
# 这次不再逐个列举地貌名,改成从属性判:【可移除的地貌】挡住除其专属
# 改良之外的一切改良。而且不静默跳过 —— 砍地貌本身就是该做的活,
# 列成 REMOVE_FEATURE 任务,建造者才知道该来干什么。
#
# fail 方向:这里必须 fail-closed(宁可漏列一个任务),与政策空槽提醒
# 的 fail-open 相反 —— 建造者是全局最稀缺资源,推荐一个做不了的活
# 直接扣掉两回合。
# ---------------------------------------------------------------------------


def test_feature_check_precedes_hills():
    """森林/丛林/沼泽都要在 IsHills() 之前判掉。"""
    lua = _lua_units.build_builder_tasks_query()
    rem_i = lua.index("elseif removable then")
    hills_i = lua.index("elseif plot:IsHills() then")
    assert rem_i < hills_i


def test_no_hardcoded_feature_list_in_the_gate():
    """判据是 Removable 属性,不是地貌名单 —— 名单永远会漏下一个。"""
    lua = _lua_units.build_builder_tasks_query()
    assert "fInfo0.Removable" in lua


def test_hardcoded_list_survives_only_as_fallback():
    """Removable 读不到时才退回三名单(Civ6 可移除地表就这三个)。"""
    lua = _lua_units.build_builder_tasks_query()
    fallback = lua[lua.index("if not okR") : lua.index("local isWonder")]
    for f in ("FEATURE_FOREST", "FEATURE_JUNGLE", "FEATURE_MARSH"):
        assert f in fallback


def test_blocked_tile_becomes_a_remove_task_not_silence():
    """静默跳过 = 建造者既不来也不知道该来干嘛。"""
    lua = _lua_units.build_builder_tasks_query()
    assert "REMOVE_FEATURE" in lua
    assert "needRemove" in lua


def test_lumber_mill_still_allowed_on_forest():
    """伐木场是森林的专属改良 —— 不能被一刀切拦掉。"""
    lua = _lua_units.build_builder_tasks_query()
    assert (
        'fName0 == "FEATURE_FOREST" and impUnlocked("IMPROVEMENT_LUMBER_MILL")' in lua
    )


def test_natural_wonder_tiles_are_skipped():
    lua = _lua_units.build_builder_tasks_query()
    assert "isWonder" in lua and "NaturalWonder" in lua


def test_remove_task_renders_with_the_right_tool_name():
    """渲染成 'build REMOVE_FEATURE' 会被当成改良名再调一次 improve ——
    又一次白跑。措辞必须点名 remove_feature 工具。"""
    from civ_mcp import narrate
    from civ_mcp.lua.models import BuilderTask

    t = BuilderTask(
        priority="normal",
        x=59,
        y=32,
        improvement="REMOVE_FEATURE",
        resource="FOREST",
        resource_class="remove",
        city_name="乌鲁克",
        nearest_builder_id=1,
        distance=2,
    )
    from civ_mcp.lua.models import BuilderInfo

    b = BuilderInfo(unit_id=1, unit_index=1, x=57, y=31, charges=3, moves=2.0)
    out = narrate.narrate_builder_tasks([t], [b])
    assert "remove_feature" in out
    assert "build REMOVE_FEATURE" not in out


# ---------------------------------------------------------------------------
# (23) 回合完成度门禁改用游戏自己的「待指令」判据
#
# ⚠️ 这一条不是缺陷修复,是一次**我读错了实机现象**之后的改进,记下来
# 免得下次再走同一条弯路。
#
# T121 门禁报「建造者(59,32) 整回合未动」,我第一反应是误报(它刚被下过
# improve 指令)。实地查证:(59,32) 的矿【已经建好了】,建造者确实闲着 ——
# 门禁是对的。Civ6 的建造者改良是**瞬时**完成的(消耗一个充能立即落地),
# 根本不存在"多回合作业中"的建造者。
#
# 走过的死路,别再试:
#   · ActivityTypes.ACTIVITY_OPERATION —— 正在"建矿"的建造者实测是
#     ACTIVITY_AWAKE,这个枚举对建造者不成立。
#   · GetQueuedOperationsCount —— 方法不存在。
#   · HasPendingOperations —— 对改良中的建造者返回 false。
#
# 但查证过程里挖到了真东西:Unit:IsReadyToSelect() 就是游戏 UI
# 「下一个待指令单位」按钮的判据。实机一次读回 9 个单位,
# 已 fortify/已 skip/自动化/商路运行中的一律 false,精确命中唯一那个
# 闲置建造者。门禁此前用「移动力满」近似它,把驻守和遗忘混为一谈 ——
# 那段代码的注释甚至专门写了「数据层分不出驻守与遗忘」。分得出,
# 只是没人去读。与「回合没提交=游戏在等你做决定」同一形态。
# ---------------------------------------------------------------------------


def _u(**kw):
    from civ_mcp.lua.models import UnitInfo

    base = dict(
        unit_id=1,
        unit_index=1,
        name="建造者",
        unit_type="UNIT_BUILDER",
        x=59,
        y=32,
        moves_remaining=2.0,
        max_moves=2.0,
        health=100,
        max_health=100,
        combat_strength=0,
        build_charges=2,
    )
    base.update(kw)
    return UnitInfo(**base)


def test_unit_awaiting_orders_is_reported():
    blockers, _ = et.check_turn_completion(units=[_u(needs_orders=True)])
    assert any("平民整回合未动" in b for b in blockers)


def test_unit_not_awaiting_orders_is_silent():
    """已 fortify / 已 skip / 自动化的单位 —— 移动力仍是满的,但不该报。"""
    blockers, _ = et.check_turn_completion(units=[_u(needs_orders=False)])
    assert not any("平民整回合未动" in b for b in blockers)


def test_fortified_military_no_longer_noise():
    """简报里那条「4 个军事单位整回合未动(若为刻意驻守请忽略)」的来源。
    驻守单位 IsReadyToSelect=false,现在能分辨了。"""
    cart = _u(
        unit_type="UNIT_SUMERIAN_WAR_CART",
        name="战车",
        combat_strength=30,
        build_charges=0,
        moves_remaining=4.0,
        max_moves=4.0,
        needs_orders=False,
    )
    blockers, warnings = et.check_turn_completion(units=[cart])
    assert not any("军事单位整回合未动" in w for w in warnings)


def test_missing_field_defaults_to_needing_orders():
    """老快照/解析失败 → 按"需要指令"处理。宁可多提醒一次,
    也不要静默放过真正被遗忘的平民(与建造者任务板的 fail-closed 同向)。"""
    from types import SimpleNamespace

    stale = SimpleNamespace(
        unit_type="UNIT_BUILDER",
        name="建造者",
        x=1,
        y=1,
        moves_remaining=2.0,
        max_moves=2.0,
        combat_strength=0,
        build_charges=2,
    )
    blockers, _ = et.check_turn_completion(units=[stale])
    assert any("平民整回合未动" in b for b in blockers)


def test_needs_orders_round_trips_through_lua_and_parser():
    lua = _lua_units.build_units_query()
    assert "IsReadyToSelect()" in lua
    assert '.. "|" .. needsOrders)' in lua
    # 死路不要再写进【代码】—— 注释里留着是故意的(防止下次重走)
    code = "\n".join(ln for ln in lua.splitlines() if not ln.strip().startswith("--"))
    assert "ACTIVITY_OPERATION" not in code
    assert "GetQueuedOperationsCount" not in code

    line = "1|1|建造者|UNIT_BUILDER|59,32|2/2|100/100|0|0|2||0|0||0|||1"
    assert _lua_units.parse_units_response([line])[0].needs_orders is True
    assert _lua_units.parse_units_response([line[:-1] + "0"])[0].needs_orders is False


def test_parser_defaults_true_on_short_line():
    """旧格式(17 段)的行没有这一列 —— 必须默认 True,不能默认 False,
    否则一次解析退化就把整个门禁静默掉。"""
    short = "1|1|建造者|UNIT_BUILDER|59,32|2/2|100/100|0|0|2||0|0||0||"
    assert _lua_units.parse_units_response([short])[0].needs_orders is True


# ---------------------------------------------------------------------------
# (22) 任务板推荐做不了的活 —— 森林山丘被报成"建矿"(修了一半的经典)
#
# 实机 T119:任务板 `(59,32): build MINE [city: 乌鲁克]`,派建造者过去,
# improve 返回 CANNOT_IMPROVE|terrain=TERRAIN_PLAINS_HILLS
# feature=FEATURE_FOREST。建造者白跑两回合(它是全局最稀缺的资源)。
#
# 同一天早些时候我修过一次这个 bug:【丛林丘陵】被推荐建矿,修法是把
# 丛林/沼泽提到 IsHills() 前面判。**森林没提上来** —— 三个可移除地貌
# 只搬了两个。修了一半的经典形态,和今晚另外 8 次同源。
#
# 这次不再逐个列举地貌名(列举就会漏),改成 gamedata 的 Removable 判定:
# 可移除地貌挡住除其专属改良之外的一切改良。
#
# 另外发现渲染层【早就有】resource_class == "remove" 的分支,而 Lua 侧
# 从来不产生这类任务 —— 接收端准备好了,发送端没接上。同样补齐。
# ---------------------------------------------------------------------------


def test_features_judged_before_hills():
    """铁律:地貌一律先判,判完再看丘陵。顺序反了就会推荐做不了的活。"""
    lua = _lua_units.build_builder_tasks_query()
    i_rem = lua.index("elseif removable then")
    i_hills = lua.index("elseif plot:IsHills() then")
    assert i_rem < i_hills, "可移除地貌必须排在 IsHills() 之前"


def test_no_hardcoded_feature_list_in_the_main_branch():
    """列举地貌名就会漏(森林那次就是漏的)。主判据必须是 Removable。"""
    lua = _lua_units.build_builder_tasks_query()
    assert "fInfo0.Removable" in lua


def test_hardcoded_list_survives_only_as_fallback():
    """Removable 读不到时才退回三名单 —— Civ6 可移除地表只有这三个。"""
    lua = _lua_units.build_builder_tasks_query()
    fb = lua[lua.index("fInfo0.Removable == nil") :]
    head = fb[: fb.index("end")]
    for f in ("FEATURE_FOREST", "FEATURE_JUNGLE", "FEATURE_MARSH"):
        assert f in head


def test_forest_offers_lumber_mill_only_when_unlocked():
    """伐木场要机械科技。没研就不该推荐 —— 要推的是"先砍掉"。"""
    lua = _lua_units.build_builder_tasks_query()
    assert (
        'fName0 == "FEATURE_FOREST" and impUnlocked("IMPROVEMENT_LUMBER_MILL")' in lua
    )


def test_blocked_tile_becomes_a_remove_task_not_silence():
    """静默跳过 = 建造者既不来也不知道该来干什么。砍地貌本身是该做的活。"""
    lua = _lua_units.build_builder_tasks_query()
    assert "REMOVE_FEATURE" in lua
    assert "needRemove" in lua


def test_remove_task_round_trips_to_the_renderer():
    """发送端与接收端必须对齐 —— 渲染层早就有 remove 分支,Lua 侧从不产生。"""
    from civ_mcp import narrate
    from civ_mcp.lua import units as lu

    tasks, _ = lu.parse_builder_tasks(
        ["TASK|normal|59,32|REMOVE_FEATURE|FOREST|remove|乌鲁克|1638408|2"]
    )
    assert len(tasks) == 1 and tasks[0].resource_class == "remove"
    b = lu.BuilderInfo(unit_id=1638408, unit_index=8, x=59, y=32, charges=3, moves=2.0)
    out = narrate.narrate_builder_tasks(tasks, [b])
    assert "remove_feature" in out and "FOREST" in out
    assert "build MINE" not in out


def test_natural_wonder_tiles_are_skipped():
    lua = _lua_units.build_builder_tasks_query()
    assert "NaturalWonder" in lua


def test_builder_board_fails_closed_on_missing_data():
    """方向与政策空槽提醒相反:那边 fail-open,这边 fail-closed。
    建造者是最稀缺资源,推荐一个做不了的活 = 直接扣两回合。"""
    lua = _lua_units.build_builder_tasks_query()
    assert "fail-closed" in lua


# ---------------------------------------------------------------------------
# (23) 移动 BLOCKED 的诊断只看目标格,不看真正挡路的那一格
#
# 实机 T131:开拓者在 (66,24),去 (65,25)/(65,27)/(67,26)/(70,27) 全部
# BLOCKED,每次都只说 "tile appears passable - path may be blocked by
# intermediate tiles"。我照着"路径问题"连试 4 个方向,全废。
# 真因是【美国领土未开放边境】—— 靠"宣战后同一格立刻能走"才反推出来。
# T142 又复现一次(开拓者去 (51,30))。累计浪费 6+ 次调用。
#
# 诊断逻辑本身其实很完善(水/山/不可通行/他国领土/城邦都分了),
# 问题是它诊断的对象错了:目标格 8 格远,障碍在路上,于是永远落到
# UNKNOWN 兜底分支。**诊断的对象必须是真正挡路的那一格。**
#
# 对照组:同一份代码里的 CIVILIAN_UNESCORTED 拦截会点名"哪个敌军、
# 在哪、机动多少、下回合可达几格",这条差距明显。
# ---------------------------------------------------------------------------


def test_blocked_move_walks_the_path():
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "GetMoveToPath" in lua
    assert "PATH_BLOCKED" in lua
    assert "真正挡路的是" in lua


def test_blocked_move_separates_no_moves_from_no_path():
    """ "没移动力"和"没有路径"是两种完全不同的处置:
    前者等一回合,后者换目标。混在一起报等于没报。"""
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "DIAG|NO_MOVES" in lua
    assert "DIAG|NO_PATH" in lua
    assert "下回合再走" in lua


def test_border_case_says_change_direction_wont_help():
    """实机就是在这里反复试方向的 —— 消息必须直说换方向没用。"""
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "未开放边境" in lua and "换方向没用" in lua


def test_uses_the_right_unit_variable():
    """本查询里单位变量叫 u 不是 unit。写错名字 pcall 会静默失败,
    诊断永远落到 UNKNOWN —— 又一个"看起来在工作、其实什么都没做"。"""
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "unit:GetMovesRemaining" not in lua
    assert "GetMoveToPath(u, plot" in lua


def test_nil_unit_is_guarded():
    """单位可能已阵亡(POS|GONE 分支),诊断不能因此抛错。"""
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "if u == nil then return end" in lua


def test_fallback_names_zoc_and_friendly_blocking():
    """扫不到静态障碍时,剩下的可能性是 ZOC 或己方单位堵路 —— 说出来,
    而不是回到那句无信息的"可能被中间格挡住"。"""
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "控制区(ZOC)" in lua and "己方单位堵路" in lua
    # 那句无信息的旧文案不能再作为【输出】出现(注释里引用它是可以的)
    assert 'print("DIAG|UNKNOWN|tile appears passable' not in lua


def test_diag_omitted_when_no_move_target():
    """只读位置时不该带诊断块。"""
    assert "PATH_BLOCKED" not in _lua_units.build_unit_position_query(1)


# ---------------------------------------------------------------------------
# (24) 目标格被敌方单位占据 —— 最直接的原因,却一个字都没查
#
# 实机 T142:开拓者去 (51,30) 反复 BLOCKED。修好路径扫描后它说
# "扫不到静态障碍,多半是 ZOC" —— 但返回标签一直是 CAPTURE_MOVE,
# 而那个标签只在【目标格有敌方单位】时才会用。也就是说答案一直印在
# 返回值里,诊断却从水/山/领土一路查到 ZOC,唯独漏了"格子上站着人"。
#
# 而且军事单位和平民的处置完全不同:
#   军事单位撞上去 = 攻击(要用 attack,move 不会自动开打)
#   平民撞上去     = 永远进不去
# 报同一句话对两者都没用。
# ---------------------------------------------------------------------------


def test_occupied_target_is_diagnosed_first():
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "DIAG|OCCUPIED" in lua
    # 必须排在水/山/领土之前 —— 它是最直接的原因
    assert lua.index("DIAG|OCCUPIED") < lua.index("plot:IsWater()")


def test_occupied_message_branches_on_combat_strength():
    """军事单位要 attack,平民是根本进不去 —— 两种处置不能报同一句。"""
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    assert "move 不会自动开打" in lua
    assert "平民单位永远进不去敌占格" in lua


def test_occupied_probe_is_guarded():
    lua = _lua_units.build_unit_position_query(1, 51, 30)
    seg = lua[lua.index("local occWhy") : lua.index("DIAG|OCCUPIED")]
    assert "pcall(" in seg
    assert "if not pu then return end" in seg


# ---------------------------------------------------------------------------
# (25) 外交/交易弹窗挡路时,没有任何工具会告诉你 —— 用户实机报告
#
# 用户描述:"经常会直接跳出来一个国家首领的头像加上交易内容,这个东西
# 识别不到,识别不到的话就一直卡在那里,后面的任务也进行不下去。"
#
# 检测能力一直是有的(get_diplomacy_sessions / get_pending_deals /
# dismiss_popup 都在),问题是**检测只挂在 end_turn 的超时分支上**:
#   if not advanced:      <- 只有回合推不动了才查
#       mid_deals = await gs.get_pending_deals()
# 回合【中途】弹出来的窗口,到下一次 end_turn 超时之前无人过问。
# 这中间发出去的 unit_action / city_action 全部白扔,而且每一条都
# 返回"成功" —— 又是今晚的主线病灶:静默失效 + 假成功。
#
# 修法:挂到 get_units 上。回合完成度门禁强制每回合必调它,
# 所以探测必然发生,且零额外往返。
# ---------------------------------------------------------------------------


def test_units_query_probes_both_channels():
    """外交会话和交易提案是两套 API,只查一个会漏。"""
    lua = _lua_units.build_units_query()
    assert "FindOpenSessionID" in lua
    assert "HasPendingDeal" in lua
    assert "BLOCKER|" in lua


def test_blocker_line_is_not_parsed_as_a_unit():
    """BLOCKER| 行有 5 段,会被旧的 len(parts)>=7 判据放过 —— 但要显式跳过,
    免得将来字段增减时它突然变成一个幽灵单位。"""
    units = _lua_units.parse_units_response(
        [
            "BLOCKER|1|美国#2|0|",
            "1|1|战士|UNIT_WARRIOR|5,5|2/2|100/100|20|0|0||0|0||0||",
        ]
    )
    assert len(units) == 1
    assert units[0].unit_type == "UNIT_WARRIOR"


def test_blocker_message_names_who_and_how():
    """光说"有弹窗"没用 —— 要给出是谁、用哪个工具、参数从哪来。"""
    msg = _lua_units.parse_blocking_sessions(["BLOCKER|1|美国#2|1|印度#3"])
    assert "美国#2" in msg and "印度#3" in msg
    assert "respond_to_diplomacy" in msg
    assert "respond_to_trade" in msg
    assert "dismiss_popup" in msg


def test_blocker_explains_the_silent_failure():
    """必须说明后果 —— 否则会被当成一条可以稍后再看的提示。"""
    msg = _lua_units.parse_blocking_sessions(["BLOCKER|2|美国#2|0|"])
    assert "静默失效" in msg and "照样返回成功" in msg


def test_no_blocker_line_means_empty():
    assert (
        _lua_units.parse_blocking_sessions(["1|1|x|UNIT_WARRIOR|5,5|2/2|100/100"]) == ""
    )
    assert _lua_units.parse_blocking_sessions([]) == ""


def test_malformed_blocker_is_ignored():
    assert _lua_units.parse_blocking_sessions(["BLOCKER|abc|x|1|y"]) == ""
    assert _lua_units.parse_blocking_sessions(["BLOCKER|1"]) == ""


def test_blocker_is_rendered_first():
    """排在 16 个单位后面等于没报。"""
    import inspect

    from civ_mcp import server

    src = inspect.getsource(server)
    i = src.index("_blocking_sessions")
    seg = src[i : i + 400]
    assert "blocker" in seg and "body" in seg
    assert seg.index("{blocker}") < seg.index("{body}")


def test_probe_wired_into_get_units():
    """接线回归 —— 有 Lua 有 parser 但 game_state 不存 = 永远静默。"""
    import inspect

    from civ_mcp.game_state import GameState

    src = inspect.getsource(GameState.get_units)
    assert "parse_blocking_sessions" in src
    assert "_blocking_sessions" in src


def test_blocker_parser_is_exported_from_the_package():
    """实机踩到:测试全绿但 get_units 报 AttributeError。

    原因是 test_probe_wired_into_get_units 只检查源码里【有没有那个字符串】,
    而 game_state 走的是包级别的 lq.parse_blocking_sessions —— 新函数没加进
    lua/__init__.py 的再导出。字符串在、符号不在,测试照样通过。
    接线测试必须验证【符号可解析】,不能只验证源码文本。
    """
    from civ_mcp import lua as lq

    assert callable(lq.parse_blocking_sessions)
    assert lq.parse_blocking_sessions(["BLOCKER|1|X#2|0|"]) != ""


# ---------------------------------------------------------------------------
# (26) 平民滞留门禁把【伟人】报成"建造者"
#
# 实机 T151:门禁说 "建造者(56,25) 滞留 6 回合",我去那个坐标找建造者 ——
# 站在那儿的是大商人柯莱欧司。
#
# 根因是二分法:kind = "开拓者" if is_settler else "建造者"
# 判据用的是"有没有充能"(伟人也有充能),而不是单位类型。
# 连带解法句也错:"建造者:get_builder_tasks 派活" 对伟人完全无效,
# 照做只会白跑一次 —— 报错的类别 + 错误的解法,双份误导。
# ---------------------------------------------------------------------------


def _stale_unit(utype, x=56, y=25, name="", charges=1, uid=1):
    return NS(
        unit_type=utype,
        x=x,
        y=y,
        name=name,
        combat_strength=0,
        build_charges=charges,
        unit_id=uid,
    )


def _run_stale_gate(units, turn=151, first_seen_turn=140):
    """直接驱动 end_turn 里那段滞留检查的逻辑等价物。"""
    st = {"settler_first_seen": {}}
    out = []
    for u in units:
        utype = str(u.unit_type).upper()
        is_settler = "SETTLER" in utype
        if is_settler:
            kind = "开拓者"
        elif "GREAT_" in utype:
            kind = "伟人"
        else:
            kind = "建造者"
        name = str(getattr(u, "name", "") or "").strip()
        label = kind + (f"·{name}" if kind == "伟人" and name else "")
        out.append(f"{label}({u.x},{u.y}) 滞留 {turn - first_seen_turn} 回合")
    del st
    return out


def test_great_person_is_not_called_a_builder():
    lines = _run_stale_gate([_stale_unit("UNIT_GREAT_MERCHANT", name="柯莱欧司")])
    assert "建造者" not in lines[0]
    assert "伟人" in lines[0] and "柯莱欧司" in lines[0]


def test_settler_and_builder_still_classified_right():
    lines = _run_stale_gate(
        [_stale_unit("UNIT_SETTLER"), _stale_unit("UNIT_BUILDER", charges=3)]
    )
    assert "开拓者" in lines[0]
    assert "建造者" in lines[1]


def test_gate_source_classifies_by_unit_type_not_charges():
    """判据必须是 UnitType,不能靠"有没有充能"反推 —— 伟人也有充能。"""
    import inspect

    from civ_mcp import end_turn as _et

    src = inspect.getsource(_et)
    # 注意:注释里保留了旧的二分法作为病历,所以不能用 index 找第一次出现。
    # 直接断言真正的分支存在,并且它排在 is_settler 之后、兜底之前。
    assert 'elif "GREAT_" in utype:' in src
    i_great = src.index('elif "GREAT_" in utype:')
    i_settler = src.index("if is_settler:" + chr(10) + "                    kind =")
    assert i_settler < i_great


def test_gate_gives_great_person_its_own_remedy():
    """解法句也要分类 —— get_builder_tasks 对伟人无效,照做就是白跑。"""
    import inspect

    from civ_mcp import end_turn as _et

    src = inspect.getsource(_et)
    assert "伟人:unit_action(activate)" in src
    assert "在本局已经废了" in src


# ---------------------------------------------------------------------------
# (27) 残血单位发起近战 —— 提醒挡不住惯性,得拦
#
# 实机 T159:33 血战车打 36 血蛮族弩手。估算白纸黑字写着
# "Est damage to attacker: ~24" —— 我读了这个数字,还是按"再补一下它
# 就死了"的惯性打了。打完剩 6 血,下回合被反杀,损失一辆战车。
# 而那个被打到 10 血的敌人,下回合城市射击(35伤害)就能免费点掉。
#
# 简报里一直有"把低于60%的单位撤出接触线"(T108 校准局的教训),但它是
# 【提醒】不是【拦截】。这个项目的原则是"执行前拦截优先于文档提醒",
# 攻击路径上一直漏着这一条。
#
# 判据全部取自估算,不猜;远程不拦(无反击,est_damage_to_attacker 恒为0)。
# ---------------------------------------------------------------------------


class _EstConn:
    def __init__(self, est_lines, attack_lines=None):
        self.est_lines = est_lines
        self.attack_lines = attack_lines or ["OK:MELEE_ATTACK|done"]
        self.calls = []

    async def execute_write(self, lua):
        self.calls.append(lua)
        if "combat" in lua.lower() or "CombatEstimate" in lua:
            return self.est_lines
        return self.attack_lines


def test_low_hp_floor_is_a_named_constant():
    from civ_mcp import game_state as _gs

    assert isinstance(_gs.LOW_HP_ATTACK_FLOOR, int)
    assert 0 < _gs.LOW_HP_ATTACK_FLOOR < 50


def test_gate_present_on_attack_path():
    import inspect

    from civ_mcp.game_state import GameState

    src = inspect.getsource(GameState.attack_unit)
    assert "LOW_HP_ATTACK" in src
    assert "LOW_HP_ATTACK_FLOOR" in src
    # 必须在实际发起攻击之前
    assert src.index("LOW_HP_ATTACK_FLOOR") < src.index("build_attack_unit")


def test_gate_skips_ranged():
    """远程无反击,est_damage_to_attacker 恒为 0,拦它就是纯误伤。"""
    import inspect

    from civ_mcp.game_state import GameState

    src = inspect.getsource(GameState.attack_unit)
    i = src.index("LOW_HP_ATTACK_FLOOR")
    seg = src[max(0, i - 900) : i]
    assert "is_ranged" in seg


def test_gate_is_forceable_with_reason():
    """拦截不是禁止 —— 有时这一击就是值得的。但要显式声明。"""
    import inspect

    from civ_mcp.game_state import GameState

    src = inspect.getsource(GameState.attack_unit)
    assert "not force" in src
    assert "force=True" in src


def test_gate_message_offers_the_alternatives():
    """只说"别打"没用 —— 要给出当时真实存在的三个替代选项。"""
    import inspect

    from civ_mcp.game_state import GameState

    src = inspect.getsource(GameState.attack_unit)
    assert "城市射击" in src  # 目标下回合会不会被免费点掉
    assert "满血单位" in src  # 有没有别的单位能上
    assert "撤回" in src  # 撤下来回血还是完整战力


# ---------------------------------------------------------------------------
# (28) 空区域:区域盖好了却不放建筑,全仓没有一处检查
#
# 用户问"你这科技是不是太慢了",查出来的根因不是"没建学院":
#   乌鲁克  学院+图书馆+大学 → 科研 18
#   西巴尔  学院,里面空的    → 科研 7
#   埃利都  学院,里面空的    → 科研 6
# 科研 48/回合 vs 美国 82,差距的一大半在这两个空壳上。
# 区域是容器,建筑才是产出;花 54 锤立起来不放建筑 = 把锤子埋地里。
#
# district_planner 里原有一句"别停在空区域",但那是【建议建区域】时
# 说的,对已经建好的空壳一个字都没有。
#
# 第二点来自用户同一轮的追问:"文化宗教呢但是不能一起都建要看你想
# 怎么胜利"。无差别催建会把人推去补圣地 —— 而这局宗教路线 T130 就
# 关闭了(4 个宗教位全满)。那不是提醒,是噪音加误导。所以按胜利路线
# 加权,权重 0 的直接不进建议。
# ---------------------------------------------------------------------------

from civ_mcp import district_planner as _dp  # noqa: E402

_TECHS = {"TECH_WRITING", "TECH_EDUCATION", "TECH_ASTROLOGY", "TECH_CURRENCY"}


def _city(name, districts, buildings=()):
    return {"name": name, "districts": list(districts), "buildings": list(buildings)}


def test_empty_campus_is_reported():
    out = _dp.empty_district_advice(
        [_city("西巴尔", ["DISTRICT_CAMPUS@62,27"])], _TECHS, set(), "DOMINATION"
    )
    assert out and any("CAMPUS 是空的" in line for line in out)
    assert any("LIBRARY" in line for line in out)


def test_filled_district_is_silent():
    out = _dp.empty_district_advice(
        [
            _city(
                "乌鲁克",
                ["DISTRICT_CAMPUS@59,29"],
                ["BUILDING_LIBRARY", "BUILDING_UNIVERSITY"],
            )
        ],
        _TECHS,
        set(),
        "DOMINATION",
    )
    assert out == []


def test_unresearched_buildings_are_not_debt():
    """前置科技没到 = 还没轮到,不是欠账。报它只会制造做不了的待办。"""
    out = _dp.empty_district_advice(
        [_city("X", ["DISTRICT_CAMPUS@1,1"], ["BUILDING_LIBRARY"])],
        {"TECH_WRITING"},
        set(),
        "DOMINATION",  # 没有 TECH_EDUCATION
    )
    assert out == []


def test_holy_site_hidden_on_domination():
    """用户点名的那条:走统治时不该催你补圣地。"""
    out = _dp.empty_district_advice(
        [_city("X", ["DISTRICT_HOLY_SITE@1,1"])], _TECHS, set(), "DOMINATION"
    )
    assert out == []


def test_holy_site_is_top_on_religion():
    """同一个空圣地,走宗教时必须排第一 —— 权重是路线相关的,不是绝对的。"""
    out = _dp.empty_district_advice(
        [_city("X", ["DISTRICT_CAMPUS@1,1", "DISTRICT_HOLY_SITE@2,2"])],
        _TECHS,
        set(),
        "RELIGION",
    )
    body = [line for line in out if "是空的" in line]
    assert "HOLY_SITE" in body[0]
    assert "【核心】" in body[0]


def test_core_districts_sort_first():
    out = _dp.empty_district_advice(
        [_city("A", ["DISTRICT_THEATER@1,1"]), _city("B", ["DISTRICT_CAMPUS@2,2"])],
        _TECHS,
        set(),
        "DOMINATION",
    )
    body = [line for line in out if "是空的" in line]
    assert "CAMPUS" in body[0]  # 权重2 排在权重1 的 THEATER 前面


def test_no_victory_type_means_no_filtering():
    """还没锁定路线时不替人做取舍 —— 全部同权重列出。"""
    out = _dp.empty_district_advice(
        [_city("X", ["DISTRICT_HOLY_SITE@1,1"])], _TECHS, set(), ""
    )
    assert out and any("HOLY_SITE" in line for line in out)


def test_weight_table_covers_all_four_paths():
    for vt in ("DOMINATION", "SCIENCE", "CULTURE", "RELIGION"):
        assert _dp.district_weight("DISTRICT_CAMPUS", vt) >= 1
    # 统治不需要圣地,宗教必须要
    assert _dp.district_weight("DISTRICT_HOLY_SITE", "DOMINATION") == 0
    assert _dp.district_weight("DISTRICT_HOLY_SITE", "RELIGION") == 2


def test_mapping_comes_from_gamedata_not_hardcoded():
    """建筑-区域映射必须来自 Buildings.PrereqDistrict,不写死建筑名。"""
    table = _dp._district_buildings()
    assert "DISTRICT_CAMPUS" in table
    names = [b for b, _, _ in table["DISTRICT_CAMPUS"]]
    assert "BUILDING_LIBRARY" in names and "BUILDING_UNIVERSITY" in names
    # 奇观不算区域建筑欠账
    assert not any("WONDER" in b for b in names)


def test_wired_into_briefing():
    import inspect

    from civ_mcp import end_turn as _et

    src = inspect.getsource(_et)
    assert "empty_district_advice" in src
    assert "victory_type=wm.victory_type" in src


# ---------------------------------------------------------------------------
# (29) 攻城单位被当成近战 —— 门禁把自己推荐的做法也拦了
#
# 实机 T173:投石机对京都远程齐射,被 STAGGERED_ASSAULT 拦下。
# 而那条门禁的选项③自己写着"用远程打一发(零反伤)而不是近战" ——
# 投石机的 Bombard 攻击正是零反伤的远程打击。
#
# 根因:MoveContext.is_ranged 只认 PROMOTION_CLASS_RANGED,
# 投石机是 PROMOTION_CLASS_SIEGE。这正是 2026-08-04 在攻击执行路径
# 修过的判据(isRanged = RangedCombat>0 或 Bombard>0),discipline 的
# MoveContext 没跟上 —— 今晚第 N 例「同一个概念两套实现」。
#
# 顺带的正确性收益:RANGED_ALREADY_MOVED 现在也覆盖攻城单位
# (攻城单位移动后同样不能攻击,之前这条对它漏网)。
# ---------------------------------------------------------------------------

from civ_mcp import discipline as _disc  # noqa: E402
from civ_mcp import safety as _safety  # noqa: E402


def _siege_ctx(**kw):
    ctx = _safety.MoveContext()
    ctx.found = True
    ctx.unit_type = "UNIT_TREBUCHET"
    ctx.formation = "FORMATION_CLASS_LAND_COMBAT"
    ctx.promotion = "PROMOTION_CLASS_SIEGE"
    ctx.target_is_enemy_city = True
    ctx.enemy_city_name = "京都"
    ctx.adjacent_military = 0
    ctx.nearby_military = 3
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def test_siege_counts_as_standoff():
    assert _siege_ctx().is_ranged is True


def test_siege_not_blocked_by_staggered_assault():
    """T173 实机场景原样复刻:投石机独自轰城,3 个友军还在路上 —— 放行。"""
    v = _disc.check_staggered_assault(_siege_ctx())
    assert v.blocked is False


def test_siege_not_blocked_by_low_hp_assault():
    """远程攻城零反伤,残血也能安全输出 —— 不拦。"""
    v = _disc.check_low_hp_assault(_siege_ctx(hp=30, max_hp=100))
    assert v.blocked is False


def test_melee_still_blocked():
    """豁免只给远距类 —— 近战单打独斗照拦(T69 的教训不能松)。"""
    ctx = _siege_ctx()
    ctx.unit_type = "UNIT_SWORDSMAN"
    ctx.promotion = "PROMOTION_CLASS_MELEE"
    v = _disc.check_staggered_assault(ctx)
    assert v.blocked is True and v.code == "STAGGERED_ASSAULT"


def test_naval_ranged_and_air_also_standoff():
    for cls in (
        "PROMOTION_CLASS_NAVAL_RANGED",
        "PROMOTION_CLASS_AIR_BOMBER",
    ):
        assert _siege_ctx(promotion=cls).is_ranged is True


def test_ranged_already_moved_now_covers_siege():
    """收益面:攻城单位移动后不能攻击,这条门禁现在管得到它了。"""
    ctx = _siege_ctx(moves_remaining=0.0, max_moves=2.0)
    assert ctx.is_ranged and ctx.has_moved


# ---------------------------------------------------------------------------
# (30) choose_pantheon 假成功 —— 选了个已被抢走的信仰,照报 FOUNDED
#
# 实机 T179:choose_pantheon(锻造之神) 返回 PANTHEON_FOUNDED|锻造之神。
# T180 复核:"No pantheon selected",且锻造之神已从可选列表消失 ——
# 早被别家选走了(万神殿信仰全局唯一)。引擎静默拒绝,工具照报成功,
# 50 信仰也没扣。今晚第 6 例「只请求不验证」。
#
# 修法两层:①执行前查独占性(谁抢的点名谁);②请求后同帧重读,
# 结算了才报 OK+verified,没结算报 PENDING —— 不谎报。
# ---------------------------------------------------------------------------

from civ_mcp.lua import religion as _rel  # noqa: E402


def test_pantheon_checks_exclusivity_before_requesting():
    lua = _rel.build_choose_pantheon("BELIEF_GOD_OF_THE_FORGE")
    assert "ERR:BELIEF_TAKEN" in lua
    assert "已被" in lua and "选走" in lua
    # 独占性检查必须在 RequestPlayerOperation 之前
    # 注意找真调用 UI.RequestPlayerOperation —— 病历注释里也提到这个词
    assert lua.index("ERR:BELIEF_TAKEN") < lua.index("UI.RequestPlayerOperation")


def test_pantheon_verifies_after_requesting():
    lua = _rel.build_choose_pantheon("BELIEF_STONE_CIRCLES")
    assert "GetPantheon()" in lua[lua.index("UI.RequestPlayerOperation") :]
    assert "verified" in lua
    assert "NOTE:PANTHEON_PENDING" in lua


def test_pantheon_never_prints_bare_ok_without_settlement_branch():
    """OK 只能出现在重读命中的分支里 —— 不结算就 PENDING,不谎报。"""
    lua = _rel.build_choose_pantheon("BELIEF_STONE_CIRCLES")
    i_req = lua.index("UI.RequestPlayerOperation")
    tail = lua[i_req:]
    assert "if nowPan == belief.Index then" in tail
