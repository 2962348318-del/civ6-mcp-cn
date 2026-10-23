"""2026-08-06 提分方案批 2:路线 ETA(方向盘)+ 军力窗口 + force 留痕(护栏)。

批 1 是仪表盘(分数健康度),批 2 是方向盘和护栏:

  E1 路线 ETA —— 罗马局活到 T362,每一步都有里程碑、每一步都在推进,
     但整条路早就输了。系统从没问过一句「来得及吗」。里程碑回答
     "下一步做什么",ETA 回答"这条路还赢不赢得了"。
  E3 军力窗口 —— 两局赔的同一笔学费:罗马 T335 的 1.57× 拖 15 回合、
     法国局 T55-62 的 1.63× 拖到掉回 1.26×。窗口会自己关,而系统
     只报过当前比值,从没算过还剩几回合。
  F1 force 留痕 —— "每次 force 记一行理由"此前靠交接文档叮嘱,
     恰好违反本项目"拦截优先于文档"的铁律本身。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import force_log, lategame, playbook
from civ_mcp import power_balance as pb
from civ_mcp import victory_eta as ve
from civ_mcp.doctrine import DoctrineInput

# ---------------------------------------------------------------------------
# E1. 胜利路线 ETA
# ---------------------------------------------------------------------------


def _hist(samples):
    """samples: [(turn, [(who, victory, progress), ...]), ...]"""
    h = ve.ETAHistory()
    for turn, rows in samples:
        h.record(turn, [{"who": w, "victory": v, "progress": p} for w, v, p in rows])
    return h


def test_eta_extrapolates_from_two_samples():
    h = _hist([(100, [("刚果", "SCIENCE", 0.20)]), (120, [("刚果", "SCIENCE", 0.40)])])
    e = h.eta("刚果", "SCIENCE")
    # 20 回合涨 0.20 → 每回合 0.01 → 还剩 0.60 → 60 回合
    assert e is not None and e.eta_turns == 60


def test_single_sample_gives_no_eta():
    """一个采样点不编趋势 —— 与 score_gap/expansion_rates 同一条纪律。"""
    h = _hist([(100, [("刚果", "SCIENCE", 0.20)])])
    assert h.eta("刚果", "SCIENCE") is None


def test_stalled_progress_reports_no_eta():
    """进度不涨时外推会得出负数或无穷 —— 报进度不报 ETA。"""
    h = _hist([(100, [("印度", "SCIENCE", 0.30)]), (120, [("印度", "SCIENCE", 0.30)])])
    e = h.eta("印度", "SCIENCE")
    assert e is not None and e.eta_turns is None and e.progress == 0.30


def test_completed_progress_is_zero_turns():
    h = _hist([(100, [("英国", "SCIENCE", 0.9)]), (110, [("英国", "SCIENCE", 1.0)])])
    assert h.eta("英国", "SCIENCE").eta_turns == 0


def test_rows_from_progress_uses_real_denominators():
    """只取有明确分母的两条;文化的分母非线性,不做 ETA。"""
    vp = NS(
        players=[
            NS(name="刚果", science_vp=10, science_vp_needed=50, diplomatic_vp=4),
            NS(name="Unmet", science_vp=5, science_vp_needed=50, diplomatic_vp=0),
        ]
    )
    rows = ve.rows_from_progress(vp)
    kinds = {(r["who"], r["victory"]) for r in rows}
    assert ("刚果", "SCIENCE") in kinds
    assert ("刚果", "DIPLOMATIC") in kinds
    assert not any(r["who"] == "Unmet" for r in rows), "未接触的文明不该进 ETA"
    sci = next(r for r in rows if r["victory"] == "SCIENCE")
    assert abs(sci["progress"] - 0.2) < 1e-9


def test_material_lag_triggers_three_options():
    """落后到实质程度时,给的是三个选项而不是一句焦虑。"""
    rivals = [ve.VictoryETA("刚果", "SCIENCE", 0.5, 60)]
    lines = ve.compare(150, "SCIENCE", rivals)
    assert any("⚠️" in ln for ln in lines)
    body = "\n".join(lines)
    assert "提速" in body and "换道" in body and "压制" in body


def test_lead_is_reported_so_agent_does_not_thrash():
    rivals = [ve.VictoryETA("刚果", "SCIENCE", 0.2, 200)]
    lines = ve.compare(100, "SCIENCE", rivals)
    assert any("领先" in ln for ln in lines)


def test_small_gap_called_noise():
    rivals = [ve.VictoryETA("刚果", "SCIENCE", 0.5, 105)]
    lines = ve.compare(100, "SCIENCE", rivals)
    assert any("噪音范围" in ln for ln in lines)


def test_extrapolation_is_labeled():
    """诚实边界:任何外推数字都要带标注。"""
    rivals = [ve.VictoryETA("刚果", "SCIENCE", 0.5, 60)]
    body = "\n".join(ve.compare(150, "SCIENCE", rivals))
    assert "外推,非精确" in body


def test_no_rivals_no_noise():
    assert ve.compare(None, "SCIENCE", []) == []


def test_my_eta_only_for_science():
    """文化/外交的剩余量不是确定的产能数 —— 给不出诚实 ETA 就不给。"""
    sci = lategame.LateGameContext(
        victory_type="SCIENCE", total_production=100, completed_projects=set()
    )
    assert isinstance(lategame.my_eta_turns(sci), int)
    cul = lategame.LateGameContext(victory_type="CULTURE", total_production=100)
    assert lategame.my_eta_turns(cul) is None


def test_my_eta_needs_production():
    ctx = lategame.LateGameContext(victory_type="SCIENCE", total_production=0)
    assert lategame.my_eta_turns(ctx) is None


# ---------------------------------------------------------------------------
# E3. 军力窗口倒计时与兑现计划
# ---------------------------------------------------------------------------


def _mil_hist(t0, mine0, best0):
    h = pb.PowerHistory()
    h.records = [
        {"turn": t0, "military": mine0, "rival_military_best": best0, "cities": 2}
    ]
    return h


def _psnap(turn=64, mine=173, best=137, cities=2):
    me = pb.Side(name="我方", cities=cities, military=mine)
    rivals = [pb.Side(name="希腊", player_id=1, cities=4, military=best)]
    snap = pb.PowerSnapshot(turn=turn, me=me, rivals=rivals)
    snap.ratios["military"] = pb.Ratio("military", float(mine), float(best))
    return snap


def test_window_countdown_from_rival_growth():
    """对手军力涨得比我快 → 窗口在关,要算出还剩几回合。

    我方 180→200(+2/回合)、对手 100→125(+2.5/回合),当前 1.60×。
    按这个增速,约 30 回合后跌破 1.3×。
    """
    h = _mil_hist(54, 180, 100)
    turns = h.military_window_turns(_psnap(64, 200, 125), floor=1.3)
    assert turns is not None and 0 < turns < 60


def test_window_already_closed_reports_zero():
    h = _mil_hist(54, 150, 140)
    assert h.military_window_turns(_psnap(64, 150, 145), floor=1.3) == 0


def test_no_history_no_countdown():
    assert pb.PowerHistory().military_window_turns(_psnap()) is None


def test_snapshot_persists_rival_military_best():
    d = _psnap().to_dict()
    assert d["rival_military_best"] == 137


def _civ(name="希腊", cities=(("雅典", False, 8),), at_war=False):
    return NS(
        civ_name=name,
        is_at_war=at_war,
        visible_cities=[
            NS(name=n, x=18, y=36, population=p, has_walls=w) for n, w, p in cities
        ],
    )


def _unit(t="UNIT_SWORDSMAN"):
    return NS(unit_type=t, combat_strength=35, ranged_strength=0)


def _inp(ratio=1.63, units=None, civs=None, hist=None, prod=10):
    snap = _psnap(mine=int(137 * ratio), best=137)
    return DoctrineInput(
        turn=64,
        power=snap,
        my_units=units if units is not None else [_unit()],
        civs=civs if civs is not None else [_civ()],
        my_cities=[NS(name="伦敦", production=prod)],
        production=prod,
        power_history=hist,
    )


def test_window_names_a_specific_target():
    """ "选一个接壤的弱邻居"不是计划 —— 要点名到城。"""
    out = playbook.war_campaign(_inp())
    assert out and "雅典" in out[0].action


def test_walled_target_without_breaker_is_flagged():
    inp = _inp(civs=[_civ(cities=(("雅典", True, 8),))])
    out = playbook.war_campaign(inp)
    assert out and "破墙" in out[0].action


def test_no_city_taker_is_flagged():
    """远程打空血也占不下城 —— 编成核对必须抓这条。"""
    inp = _inp(
        units=[NS(unit_type="UNIT_ARCHER", combat_strength=15, ranged_strength=25)]
    )
    out = playbook.war_campaign(inp)
    assert out and "占城" in out[0].action


def test_countdown_shorter_than_eta_says_do_not_start():
    """核心判断:补齐要 43 回合、窗口只剩 ~10 回合 → 明说别打。

    对手军力 100→137 涨得比我快,窗口约 10 回合后关闭;而产能只有 3,
    补齐"能占城 + 能破墙"两项要约 43 回合。这正是罗马局 T335 的形态。
    """
    hist = _mil_hist(54, 220, 100)
    inp = _inp(
        units=[NS(unit_type="UNIT_ARCHER", combat_strength=15, ranged_strength=25)],
        civs=[_civ(cities=(("雅典", True, 8),))],
        hist=hist,
        prod=3,
    )
    out = playbook.war_campaign(inp)
    assert out and "来不及" in out[0].action


def test_stale_intel_warning_on_walled_target():
    """法国局 T64 败在 9 回合前的旧情报 —— 有墙目标必须提醒核实。"""
    inp = _inp(civs=[_civ(cities=(("雅典", True, 8),))])
    out = playbook.war_campaign(inp)
    assert any("情报" in d or "侦察" in d for d in out[0].detail)


def test_no_window_below_threshold():
    assert playbook.war_campaign(_inp(ratio=1.1)) == []


def test_silent_when_already_at_war():
    """交战中归 war_timing 的攻城四档,本树不插嘴。"""
    inp = _inp(civs=[_civ(at_war=True)])
    assert playbook.war_campaign(inp) == []


def test_unusable_ratio_is_silent():
    """军力比不可信时不谈窗口(法国局 T4 的 2.00× 假窗口)。"""
    snap = _psnap()
    snap.ratios["military"] = pb.Ratio("military", 2.0, 1.0, usable=False)
    inp = DoctrineInput(turn=10, power=snap, my_units=[_unit()], civs=[_civ()])
    assert playbook.war_campaign(inp) == []


def test_no_visible_target_suggests_diplomacy():
    inp = _inp(civs=[NS(civ_name="希腊", is_at_war=False, visible_cities=[])])
    out = playbook.war_campaign(inp)
    assert out and ("外交" in out[0].action or "探图" in out[0].action)


def test_war_campaign_registered():
    assert playbook.war_campaign in playbook.TREES


# ---------------------------------------------------------------------------
# F1. force 留痕
# ---------------------------------------------------------------------------


def test_force_record_roundtrip(tmp_path):
    p = tmp_path / "force_log.jsonl"
    force_log.record(
        "unit_action", {"unit_id": 1, "force": True}, "营地必须现在拆", 64, p
    )
    rows = force_log.read_all(p)
    assert len(rows) == 1
    assert rows[0]["tool"] == "unit_action" and rows[0]["turn"] == 64
    assert rows[0]["reason"] == "营地必须现在拆"


def test_force_flag_itself_not_stored_in_args(tmp_path):
    p = tmp_path / "f.jsonl"
    force_log.record("unit_action", {"unit_id": 1, "force": True}, "x", 1, p)
    assert "force" not in force_log.read_all(p)[0]["args"]
    assert force_log.read_all(p)[0]["args"]["unit_id"] == 1


def test_missing_reason_is_marked_not_dropped(tmp_path):
    """没给理由也要留痕 —— 只是标记出来,复盘时知道这条无法判断。"""
    p = tmp_path / "f.jsonl"
    force_log.record("purchase_item", {"force": True}, "", 70, p)
    assert force_log.read_all(p)[0]["reason"] == force_log.NO_REASON


def test_record_never_raises_on_bad_path(tmp_path):
    """观测不许破坏被观测的事:落盘失败不能让 force 操作整个失败。"""
    bad = tmp_path / "f.jsonl"
    bad.mkdir()  # 目录占用文件名 → 写入必失败
    force_log.record("unit_action", {"force": True}, "reason", 1, bad)  # 不抛


def test_summary_says_zero_is_good_news(tmp_path):
    assert "0 次" in force_log.summary(tmp_path / "none.jsonl")


def test_summary_flags_missing_reasons(tmp_path):
    p = tmp_path / "f.jsonl"
    force_log.record("a", {"force": True}, "有理由", 1, p)
    force_log.record("b", {"force": True}, "", 2, p)
    s = force_log.summary(p)
    assert "2 次" in s and "1 次没给理由" in s


def test_every_force_tool_accepts_a_reason():
    """六个带 force 的工具都要能收理由 —— 漏一个就有一条无痕通道。"""
    import inspect

    from civ_mcp import server

    checked = 0
    for name in (
        "respond_to_trade",
        "propose_trade",
        "send_diplomatic_action",
        "unit_action",
        "set_city_production",
        "purchase_item",
    ):
        fn = getattr(server, name)
        fn = getattr(fn, "fn", fn)  # FastMCP 包装
        params = inspect.signature(fn).parameters
        assert "force" in params, name
        assert "force_reason" in params, f"{name} 缺 force_reason,是一条无痕通道"
        checked += 1
    assert checked == 6


def test_self_is_excluded_by_player_id_not_name():
    """回归:VictoryProgress.players 包含我方。

    2026-08-06 自查抓到 —— 原本按名字过滤,而 `wm.my_civ_name` 根本
    不存在,my_name 恒为 "你",于是我方进度会被当成"最快的对手"报出来。
    在场上这会读成"英格兰快赢了,你得压制他" —— 而英格兰就是你自己。
    """
    me = ve.VictoryETA("英格兰", "SCIENCE", 0.9, 10, pid=0)
    rival = ve.VictoryETA("刚果", "SCIENCE", 0.3, 200, pid=3)
    lines = ve.compare(150, "SCIENCE", [me, rival], my_pid=0)
    body = "\n".join(lines)
    assert "英格兰" not in body, "我方不能出现在对手列表里"
    assert "刚果" in body


def test_pid_flows_from_progress_through_history():
    vp = NS(
        players=[
            NS(
                name="英格兰",
                player_id=0,
                science_vp=45,
                science_vp_needed=50,
                diplomatic_vp=0,
            ),
            NS(
                name="刚果",
                player_id=3,
                science_vp=10,
                science_vp_needed=50,
                diplomatic_vp=0,
            ),
        ]
    )
    h = ve.ETAHistory()
    h.record(100, ve.rows_from_progress(vp))
    vp.players[0].science_vp = 48
    vp.players[1].science_vp = 15
    h.record(110, ve.rows_from_progress(vp))
    assert h.eta("英格兰", "SCIENCE").pid == 0
    assert h.eta("刚果", "SCIENCE").pid == 3


def test_unknown_pid_keeps_everyone():
    """拿不到 pid 时宁可多报 —— 漏掉真正逼近胜利的对手更危险。"""
    r = ve.VictoryETA("刚果", "SCIENCE", 0.5, 60)
    assert ve.compare(150, "SCIENCE", [r], my_pid=-1)
