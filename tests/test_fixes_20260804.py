"""2026-08-04 缺陷修复回归测试(9 条实机缺陷)。

P0-1 攻击分派降级 bug(远程禁止降级近战)
P0-2 城市被远程夷平警告(执行前 + 打穿后)
P0-3 金币审计(gold_audit 自动归因)
P1-4 respond_to_trade 后 INCOMING working deal 清理
P1-5 set_policies readback 验证 + clearList 语义修正
P1-6 settle 扫描过滤已有城市/废墟
P1-7 海军威胁等级建模(GALLEY 误报 RED)
P2-8 缺战略资源提示(骑士缺铁等)
P2-9 交易通知探测(通知存在但读不到交易内容)
"""

from __future__ import annotations

import asyncio
import json

import pytest

from civ_mcp import discipline, gold_audit, world_model as wm_mod
from civ_mcp.end_turn import _AiHangWatchdog
from civ_mcp.game_state import GameState
from civ_mcp.lua import diplomacy, governance, map as map_lua, units as units_lua
from civ_mcp.lua.models import ThreatInfo


# ---------------------------------------------------------------------------
# P1-10 AI 端回合死循环:外交会话残留(宣战/议和会话清理失效)
# ---------------------------------------------------------------------------


def test_war_declare_two_phase_lua():
    """宣战拆两阶段:Phase1 只请求会话,Phase2 同调用确认+关闭。

    旧流程:RequestSession 后同帧 FindOpenSessionID(必然 -1,会话异步建立),
    AddResponse/verify 全部落空,会话留给 8s 后台任务——该任务在
    mcp_call.py 每次新进程下从不执行,残留会话卡死 DiplomacyActionView,
    进而引发 AI 回合 CPU 满载死循环(实机 5 次)。
    """
    war_lua = diplomacy.build_send_diplo_action(5, "DECLARE_SURPRISE_WAR")
    # Phase 1 只请求,不响应、不验证、不关闭
    assert 'local isWar = action:find("_WAR") ~= nil' in war_lua
    assert "OK:WAR_REQUESTED" in war_lua
    # war 分支提前 return,AddResponse/verify 只存在于非 war 分支(运行时跳过)
    assert "if not isWar then" in war_lua
    else_branch = war_lua.split("if not isWar then")[1].split("else")[1]
    assert "OK:WAR_REQUESTED" in else_branch
    assert 'return ""' in else_branch

    respond = diplomacy.build_war_respond(5)
    assert respond.count("AddResponse") == 1  # 每帧一条,连发会被吞
    assert "OK:RESPONDED" in respond

    verify_close = diplomacy.build_war_verify_close(5)
    assert "IsAtWarWith" in verify_close
    assert "CloseSession" in verify_close
    assert "ShowIngameUI" in verify_close  # 防残留 UI 卡死

    # 非宣战动作保持原单阶段流程
    deleg = diplomacy.build_send_diplo_action(7, "DIPLOMATIC_DELEGATION")
    # 共享模板:WAR_REQUESTED 只在 else(war)分支,delegation 运行时 isWar=false 走非 war 分支
    assert "if not isWar then" in deleg
    assert deleg.count("AddResponse") >= 2


def test_peace_deal_no_eager_session_close():
    """议和提案后不再同帧强关会话(会破坏异步提交),由 Python 端 verify 后清理。"""
    peace = diplomacy.build_propose_peace(5)
    assert "OK:PROPOSED" in peace
    # SendWorkingDeal 之后不应再有 _lua_close_diplo_session 的 AddResponse 循环
    after_send = peace.split("SendWorkingDeal")[1]
    assert "NEGATIVE" not in after_send


def test_cleanup_stale_sessions_sweeps_all():
    """端回合前的防御性全扫:关闭所有开放会话并恢复 UI。"""
    cleanup = diplomacy.build_cleanup_stale_sessions()
    assert "FindOpenSessionID" in cleanup
    assert "CloseSession" in cleanup
    assert "ShowIngameUI" in cleanup
    assert "OK:STALE_SESSIONS_CLEANED" in cleanup


def test_ai_hang_watchdog_pegged_cpu(monkeypatch):
    """AI 死循环看门狗:持续满载 CPU 两个采样窗口即判定挂起。"""
    import civ_mcp.end_turn as et

    cpu_abs: list[float] = []
    fake_wall: list[float] = []

    async def fake_cpu(pid: int) -> float:
        return cpu_abs.pop(0) if cpu_abs else 0.0

    monkeypatch.setattr(et, "_game_cpu_seconds", fake_cpu)

    async def scenario(cpu_values: list[float]) -> bool:
        wd = _AiHangWatchdog(now_fn=lambda: fake_wall.pop(0))
        wd._pid = 12345
        cpu_abs.clear()
        cpu_abs.extend(cpu_values)
        fake_wall.clear()
        fake_wall.extend([0.0, 25.0, 50.0, 75.0])
        wd._last_cpu = 0.0
        result = False
        for _ in cpu_values:
            if await wd.sample():
                result = True
        return result

    async def run() -> tuple[bool, bool]:
        # 绝对值序列:每 25s 窗口 +125s CPU = 5 核满载,持续 3 窗
        hang = await scenario([125.0, 255.0, 375.0])
        # 中间窗口只 +5s(0.2 核),回落重置计数
        normal = await scenario([125.0, 130.0, 255.0])
        return hang, normal

    hang, normal = asyncio.run(run())
    assert hang is True
    assert normal is False


# ---------------------------------------------------------------------------
# P0-1 攻击分派降级 bug
# ---------------------------------------------------------------------------


def test_attack_lua_ranged_class_never_degrades_to_melee():
    """远程类别由数据表判定(RangedCombat/Bombard),禁止降级近战。

    2026-08-04 P0: 此前 isRanged 用 CanStartOperation(RANGE_ATTACK, nil,
    true) 探测 —— 参数位错误且探测的是「有没有操作」,对海上目标返回
    false 导致降级 melee 打印假成功。现在一次定死:远程单位永远走
    RANGE_ATTACK 分支,失败即报 ERR:NO_LOS,绝无 isRanged = false。
    """
    lua = units_lua.build_attack_unit(1, 5, 5)
    assert "tonumber(unitInfo.RangedCombat)" in lua
    assert "tonumber(unitInfo.Bombard)" in lua
    assert (
        "UnitManager.CanStartOperation(unit, UnitOperationTypes.RANGE_ATTACK, nil, true)"
        not in lua
    )
    assert "isRanged = false" not in lua  # 任何情况下都不降级
    assert "ERR:NO_LOS|Ranged unit cannot melee-attack" in lua


def test_attack_lua_land_cannot_melee_water():
    """水面目标:远程攻击失败的原因要明说(悬崖 LOS 挡),提示换单位/换位置。"""
    lua = units_lua.build_attack_unit(1, 5, 5)
    assert "water target not reachable by land units" in lua
    assert "IsWater()" in lua


# ---------------------------------------------------------------------------
# P0-2 城市夷平警告
# ---------------------------------------------------------------------------


def test_attack_lua_city_will_raze_warning():
    """远程攻击 0 血城市前显式警告(执行前拦截)。"""
    lua = units_lua.build_attack_unit(1, 5, 5)
    assert "WARN:CITY_WILL_RAZE" in lua
    assert "夷平" in lua


class _WriteQueueConn:
    def __init__(self, responses):
        self._responses = list(responses)
        self.writes = []

    async def execute_write(self, lua):
        self.writes.append(lua)
        return self._responses.pop(0)

    async def execute_read(self, lua):
        return []


def _attack_gs(attack_response: list[str]) -> GameState:
    """force=True 跳过拦截:写队列 = dismiss_popup, combat estimate, attack。"""
    gs = GameState.__new__(GameState)
    gs.conn = _WriteQueueConn([[], attack_response])
    gs.combat_memory = discipline.CombatMemory()
    gs._high_water_turn = 1
    gs._local_player_id = 0

    async def _noop_dismiss():
        return "DISMISSED|ok"

    gs.dismiss_popup = _noop_dismiss
    return gs


def test_attack_result_warns_when_city_razed_by_ranged():
    """打穿场景:城市 HP -> 0 且无 CITY_CAPTURED = 夷平,必须警告。"""
    gs = _attack_gs(
        [
            "OK:MELEE_ATTACK|target:CITY Cairo at (5,5)"
            "|city HP:10 -> 0/10|your HP:100 -> 100 CS:25",
            "SENTINEL",
        ]
    )
    result = asyncio.run(gs.attack_unit(1, 5, 5, force=True))
    assert "city HP:10 -> 0/10" in result
    assert "夷平" in result


def test_attack_result_quiet_when_city_captured():
    """近战 CITY_CAPTURED 不触发夷平警告。"""
    gs = _attack_gs(
        [
            "OK:MELEE_ATTACK|target:CITY Cairo at (5,5)"
            "|city HP:10 -> 0/10|CITY_CAPTURED|your HP:80 -> 70 CS:25",
            "SENTINEL",
        ]
    )
    result = asyncio.run(gs.attack_unit(1, 5, 5, force=True))
    assert "CITY_CAPTURED" in result
    assert "夷平" not in result


# ---------------------------------------------------------------------------
# P0-3 金币审计
# ---------------------------------------------------------------------------


def _write_gold_log(
    tmp_path, rows: list[dict], extra_tool_calls: list[dict] | None = None
):
    """rows: [{turn, gold, gpt}];写入假日志文件。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "log_rome_1_test.jsonl"
    entries: list[dict] = []
    for r in rows:
        entries.append(
            {
                "game": "rome_1",
                "civ": "rome",
                "seed": 1,
                "turn": r["turn"],
                "type": "tool_call",
                "tool": "get_game_overview",
                "params": {},
                "result_summary": (
                    f"Gold: {r['gold']} (+{r['gpt']}/turn) | Income: 30 | Maintenance: -23"
                ),
                "ts": r["ts"],
            }
        )
    for e in extra_tool_calls or []:
        entries.append(
            {
                "game": "rome_1",
                "civ": "rome",
                "seed": 1,
                "turn": e["turn"],
                "type": "tool_call",
                "tool": e["tool"],
                "params": e.get("params", {}),
                "result_summary": e.get("result_summary", "ok"),
                "ts": e["ts"],
            }
        )
    entries.sort(key=lambda e: e["ts"])
    with open(log_file, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return log_dir


def test_gold_audit_detects_anomaly(tmp_path, monkeypatch):
    monkeypatch.setattr(gold_audit, "LOG_DIR", tmp_path / "logs")
    gold_audit._cache.clear()
    _write_gold_log(
        tmp_path,
        [
            {"turn": 1, "gold": 100, "gpt": 10, "ts": 1.0},
            {"turn": 2, "gold": 90, "gpt": 10, "ts": 2.0},  # 预期 110,差 -20
        ],
    )
    out = gold_audit.check_gold("rome", 1)
    assert "金币审计" in out
    assert "T1→T2" in out


def test_gold_audit_quiet_on_normal(tmp_path, monkeypatch):
    monkeypatch.setattr(gold_audit, "LOG_DIR", tmp_path / "logs")
    gold_audit._cache.clear()
    _write_gold_log(
        tmp_path,
        [
            {"turn": 1, "gold": 100, "gpt": 10, "ts": 1.0},
            {"turn": 2, "gold": 110, "gpt": 10, "ts": 2.0},  # 无异常
        ],
    )
    assert gold_audit.check_gold("rome", 1) == ""


def test_gold_audit_skips_sparse_gap(tmp_path, monkeypatch):
    """gap>10 = 中间记录缺失,预期不可靠,跳过(防 T150→T183 假异常)。"""
    monkeypatch.setattr(gold_audit, "LOG_DIR", tmp_path / "logs")
    gold_audit._cache.clear()
    _write_gold_log(
        tmp_path,
        [
            {"turn": 1, "gold": 100, "gpt": 10, "ts": 1.0},
            {"turn": 12, "gold": 50, "gpt": 10, "ts": 2.0},
        ],
    )
    assert gold_audit.check_gold("rome", 1) == ""


def test_gold_audit_attributes_to_purchases(tmp_path, monkeypatch):
    monkeypatch.setattr(gold_audit, "LOG_DIR", tmp_path / "logs")
    gold_audit._cache.clear()
    _write_gold_log(
        tmp_path,
        [
            {"turn": 1, "gold": 100, "gpt": 10, "ts": 1.0},
            {"turn": 2, "gold": 50, "gpt": 10, "ts": 2.0},
        ],
        extra_tool_calls=[
            {
                "turn": 2,
                "tool": "purchase_item",
                "params": {"item_name": "UNIT_ARCHER", "yield_type": "YIELD_GOLD"},
                "ts": 2.5,
            }
        ],
    )
    out = gold_audit.check_gold("rome", 1)
    assert "购买 UNIT_ARCHER" in out


# ---------------------------------------------------------------------------
# P1-4 交易 pending 清理
# ---------------------------------------------------------------------------


def test_respond_to_deal_clears_incoming():
    """接受/拒绝后必须清 INCOMING working deal,否则残留到下次查询。"""
    accept_lua = diplomacy.build_respond_to_deal(3, True)
    reject_lua = diplomacy.build_respond_to_deal(3, False)
    for lua in (accept_lua, reject_lua):
        assert "ClearWorkingDeal(DealDirection.INCOMING, me, target)" in lua


def test_peace_treaty_agreement_is_visible():
    """MAKE_PEACE 必须显形,不许设空被过滤(阿拉伯求和曾静默丢失)。"""
    lua = diplomacy.build_pending_deals_query()
    assert "MAKE_PEACE" in lua or "DIPLOACTION_MAKE_PEACE" in lua
    assert "Peace Treaty" in lua
    assert "Agreement#" in lua  # 未知子类型显形


def test_unknown_agreement_type_is_visible_not_dropped():
    """未知协议类型显示 Agreement#<原始值>,而不是被过滤。"""
    lua = diplomacy.build_pending_deals_query()
    assert 'itemName = "Agreement#" .. tostring(subType)' in lua


# ---------------------------------------------------------------------------
# P1-5 set_policies readback + clearList 语义
# ---------------------------------------------------------------------------


def test_set_policies_has_readback_verification():
    """设置后逐槽重读,未挂上的卡输出 NOTE 提示(同帧读取可能异步,不断言失败)。

    2026-08-09 缺陷61:NOTE 文案从 POLICIES_PENDING(断言引擎回滚,误导放弃)
    改为 POLICIES_SUBMITTED(异步请求已提交,下一帧生效,跨帧验证)。"""
    lua = governance.build_set_policies({0: "POLICY_ILKUM"})
    assert "local missing = {}" in lua
    assert "NOTE:POLICIES_SUBMITTED" in lua
    assert "POLICY_ILKUM@0" in lua


def test_set_policies_clear_list_uses_policy_hash():
    """回退后 clearList 传 slot 索引:实机验证旧语义换卡可用,
    policy-hash 版本导致所有换卡被引擎拒绝(2026-08-04 回归)。"""
    lua = governance.build_set_policies({0: "POLICY_ILKUM"})
    assert "table.insert(clearList, 0)" in lua
    assert "cpE_0.Hash" not in lua


# ---------------------------------------------------------------------------
# P1-6 settle 过滤已有城市/废墟
# ---------------------------------------------------------------------------


def test_settle_body_filters_city_here_and_ruins():
    """settle 扫描必须排除「该格已有城市」和「被摧毁城市的废墟」。"""
    body = map_lua._SETTLE_SCORE_BODY
    assert "Cities.GetCityInPlot(cx, cy)" in body
    assert "GetDestroyedCityPlot" in body
    assert "isRuins" in body


# ---------------------------------------------------------------------------
# P1-7 海军威胁建模
# ---------------------------------------------------------------------------


def test_threat_parse_naval_fields():
    lines = [
        "THREAT|63|Barbarian|UNIT_GALLEY|10,20|100/100|CS:25|RS:0|dist:2|cs:1|uid:7|mv:3|rg:0|dm:DOMAIN_SEA|cd:1|coast:1",
        "THREAT|63|Barbarian|UNIT_WARRIOR|11,20|100/100|CS:20|RS:0|dist:2|cs:1|uid:8|mv:2|rg:0",
    ]
    threats = units_lua.parse_threat_scan_response(lines)
    galley = next(t for t in threats if t.unit_type == "UNIT_GALLEY")
    warrior = next(t for t in threats if t.unit_type == "UNIT_WARRIOR")
    assert galley.is_naval
    assert galley.city_distance == 1
    assert galley.has_coastal_city
    assert not warrior.is_naval
    assert warrior.city_distance == 999  # 老格式兜底


def _wm():
    return wm_mod.WorldModel()


def _threat(**kw) -> ThreatInfo:
    base = dict(
        unit_type="UNIT_GALLEY",
        x=10,
        y=20,
        hp=100,
        max_hp=100,
        combat_strength=25,
        ranged_strength=0,
        distance=2,
    )
    base.update(kw)
    return ThreatInfo(**base)


def test_naval_close_threat_requires_coastal_city_and_reach():
    """GALLEY 贴脸(距城1格)且我方有沿海城市 → RED。"""
    t = _threat(domain="DOMAIN_SEA", city_distance=1, has_coastal_city=True)
    level, reason = wm_mod.compute_threat_level(_wm(), [t])
    assert level == "RED"


def test_naval_wandering_not_red():
    """GALLEY 距城 3 格游走(近战海军射程0) → 不是 RED。"""
    t = _threat(domain="DOMAIN_SEA", city_distance=3, has_coastal_city=True)
    level, _ = wm_mod.compute_threat_level(_wm(), [t])
    assert level != "RED"
    assert level == "YELLOW"


def test_naval_ignored_without_coastal_city():
    """我方无沿海城市 → 海军贴脸也不是 RED。"""
    t = _threat(domain="DOMAIN_SEA", city_distance=1, has_coastal_city=False)
    level, _ = wm_mod.compute_threat_level(_wm(), [t])
    assert level == "YELLOW"


def test_land_close_threat_still_red():
    """陆地单位照旧:距离<=3 → RED(回归保护)。"""
    t = _threat(
        unit_type="UNIT_WARRIOR",
        combat_strength=20,
        distance=2,
        domain="DOMAIN_LAND",
    )
    level, _ = wm_mod.compute_threat_level(_wm(), [t])
    assert level == "RED"


def test_naval_projection_uses_city_distance():
    """对手推演:海军用距城距离,无沿海城市时不出现在推演里。"""
    t = _threat(domain="DOMAIN_SEA", city_distance=1, has_coastal_city=True)
    out = wm_mod.render_enemy_projection([t])
    assert "海上" in out
    assert "1 格外" in out

    t2 = _threat(domain="DOMAIN_SEA", city_distance=1, has_coastal_city=False)
    assert wm_mod.render_enemy_projection([t2]) == ""


# ---------------------------------------------------------------------------
# P2-8 缺战略资源提示
# ---------------------------------------------------------------------------


def _prod_gs(
    write_responses: list[list[str]], read_responses: list[list[str]]
) -> GameState:
    class _Conn:
        def __init__(self):
            self.w = list(write_responses)
            self.r = list(read_responses)

        async def execute_write(self, lua):
            return self.w.pop(0)

        async def execute_read(self, lua):
            return self.r.pop(0)

    gs = GameState.__new__(GameState)
    gs.conn = _Conn()
    return gs


def test_production_hint_reports_missing_resource():
    """MAYBE 行带 NEED_RESOURCE → 明确提示缺什么(骑士缺铁)。"""
    gs = _prod_gs(
        [
            [
                "MAYBE:PRODUCING|UNIT_KNIGHT|canStart=false"
                "|NEED_RESOURCE:RESOURCE_IRON:0",
                "SENTINEL",
            ]
        ],
        [["NOT_SET|current=nil|expected=UNIT_KNIGHT"]],
    )
    result = asyncio.run(
        gs.set_city_production(65536, "UNIT", "UNIT_KNIGHT", force=True)
    )
    assert "缺战略资源 IRON" in result
    assert "库存 0" in result


def test_production_lua_checks_strategic_resource():
    """Lua 端 CanProduce 失败诊断包含 StrategicResource 检查。"""
    from civ_mcp.lua import cities as cities_lua

    # 直接生成一段 Lua 检查字符串是否含资源逻辑
    lua = cities_lua.build_produce_item(65536, "UNIT", "UNIT_KNIGHT", None, None)
    assert "item.StrategicResource" in lua
    assert "NEED_RESOURCE" in lua


# ---------------------------------------------------------------------------
# P2-9 交易通知探测(Lua 片段)
# ---------------------------------------------------------------------------


def test_end_turn_notification_probe_string_present():
    """end_turn.py 中应包含交易通知探测 Lua(TRADE_NOTIF)。"""
    import civ_mcp.end_turn as et

    src = open(et.__file__, encoding="utf-8").read()
    assert "TRADE_NOTIF" in src
    assert "NotificationManager.GetList" in src
