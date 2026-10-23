"""2026-08-06 提分方案批 3:经济修复计划 + 闲置资源 + 建议留痕。

三条都在治同一类病 —— **有信息但没有动作**:

  A9 维护费:T64 局 T61 起持续正确报警,但只说病不说药,agent 自己
     想解法还想错了(以为造商人要先有市场)。
  A10 闲置资源:罗马局信仰囤到 3338 才在 T358 想起来花。存量不报警,
     因为它看起来"很安全"。
  F3 建议留痕:校准指标 #3 一直靠 agent 人肉抄建议原文 —— 与 force
     记录同源的病,靠自觉的记录换个模型就换个质量。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from civ_mcp import advice_log
from civ_mcp import end_turn as et
from civ_mcp.advice import Advice, Priority

# ---------------------------------------------------------------------------
# A9. 维护费:诊断 → 修复清单
# ---------------------------------------------------------------------------


def _ov(income=6.0, maint=5.0, unit_maint=5, gold=176.0, gpt=1.0, faith=0.0):
    return NS(
        gold_income=income,
        total_maintenance=maint,
        unit_maintenance=unit_maint,
        gold=gold,
        gold_per_turn=gpt,
        faith=faith,
    )


def test_burden_below_threshold_is_silent():
    assert et.maintenance_burden_event(_ov(income=100.0, maint=10.0)) is None


def test_burden_fires_with_repair_list():
    """T64 实景:维护 5/t 吃掉毛收入 6 的 83%。"""
    ev = et.maintenance_burden_event(_ov())
    assert ev is not None and "83%" in ev.message
    assert "修复顺序" in ev.message


def test_idle_trade_route_is_first_fix():
    """收入端优先于支出端:捡钱不损失战力,解散单位是永久损失。"""
    ev = et.maintenance_burden_event(_ov(), trade_cap=(2, 0))
    assert ev is not None
    body = ev.message
    assert body.index("商路") < body.index("解散"), "闲置商路要排在解散单位前面"


def test_trader_does_not_need_market():
    """T64 局 agent 自己想的解法是错的 —— 必须当场纠正。"""
    ev = et.maintenance_burden_event(_ov(), trade_cap=(2, 0))
    assert "不需要先有市场" in ev.message


def test_no_idle_capacity_skips_that_fix():
    ev = et.maintenance_burden_event(_ov(), trade_cap=(2, 2))
    assert "商路容量闲置" not in ev.message
    assert "解散" in ev.message, "支出端那条始终要给"


def test_burden_survives_missing_trade_data():
    """贸易数据缺失时仍要报警 —— 少一条修法,不是整条静默。"""
    ev = et.maintenance_burden_event(_ov(), trade_cap=None)
    assert ev is not None and "修复顺序" in ev.message


# ---------------------------------------------------------------------------
# A10. 闲置资源
# ---------------------------------------------------------------------------


def _cities(faith_each=5.0, n=2):
    return [NS(name=f"C{i}", faith=faith_each) for i in range(n)]


def test_faith_hoard_is_reported():
    """罗马局 3338 信仰的形态:产出 10/回合,存了 300+ 回合的量。"""
    ev = et.idle_resource_event(_ov(faith=3338.0), _cities())
    assert ev is not None and "信仰 3338" in ev.message


def test_faith_below_threshold_silent():
    """10/回合 × 20 回合 = 200,刚好不到线。"""
    assert et.idle_resource_event(_ov(faith=150.0), _cities()) is None


def test_threshold_is_relative_not_absolute():
    """同样 500 信仰:早期(产出 2/t)是囤积,后期(产出 50/t)不是。"""
    early = et.idle_resource_event(_ov(faith=500.0), _cities(faith_each=1.0))
    late = et.idle_resource_event(_ov(faith=500.0), _cities(faith_each=25.0))
    assert early is not None and "信仰" in early.message
    assert late is None or "信仰" not in late.message


def test_gold_hoard_is_reported():
    """2026-08-07(T110 实测)后改为报【可自由支配余额】而非总额:
    只说"该花"不说"留多少",会被照着花到 0 → 国库破产 → 单位强制解散。"""
    ev = et.idle_resource_event(_ov(gold=900.0, gpt=20.0), _cities(faith_each=0.0))
    assert ev is not None
    assert "可自由支配" in ev.message and "总额 900" in ev.message


def test_saving_for_big_ben_is_a_legitimate_reason():
    """大本钟建成瞬间国库翻倍 —— 建成前囤金是正确操作,不该反过来催。"""
    wm = NS(saving_for_big_ben=True)
    ev = et.idle_resource_event(_ov(gold=900.0, gpt=20.0), _cities(faith_each=0.0), wm)
    assert ev is None


def test_event_says_what_can_be_bought_not_what_to_buy():
    """边界:忘了花是缺陷,花什么是决策。"""
    ev = et.idle_resource_event(_ov(faith=3338.0), _cities())
    assert "花什么由你定" in ev.message


def test_no_overview_is_safe():
    assert et.idle_resource_event(None, _cities()) is None


def test_zero_yield_avoids_division():
    """产出为 0 时不能除零,也不该报"无穷回合的产量"。"""
    assert et.idle_resource_event(_ov(faith=3338.0), _cities(faith_each=0.0)) is None


# ---------------------------------------------------------------------------
# A4 接线回归:住房字段必须真的读得到
# ---------------------------------------------------------------------------


def test_growth_stall_needs_housing_bearing_cities():
    """CitySnapshot 没有 housing 字段,CityInfo 才有 ——
    接错集合会让住房那一支永远静默,而单元测试照样全绿。"""
    from civ_mcp.lua import models

    snap_fields = {f.name for f in models.CitySnapshot.__dataclass_fields__.values()}
    info_fields = {f.name for f in models.CityInfo.__dataclass_fields__.values()}
    assert "housing" not in snap_fields, "若快照加了 housing,可改回挂快照"
    assert "housing" in info_fields


# ---------------------------------------------------------------------------
# F3. 建议留痕
# ---------------------------------------------------------------------------


def _advice(pri, key, action="做某事"):
    return Advice(
        module="playbook.test",
        priority=pri,
        action=action,
        reason="r",
        consequence="c",
        dedup_key=key,
    )


def test_only_core_and_above_recorded(tmp_path):
    """BONUS/OPTIMIZE 每回合几十条,记下来会淹掉真正要复盘的决策。"""
    p = tmp_path / "a.jsonl"
    advice_log.record(
        64,
        [
            _advice(Priority.SURVIVAL, "s"),
            _advice(Priority.CORE, "c"),
            _advice(Priority.OPTIMIZE, "o"),
            _advice(Priority.BONUS, "b"),
        ],
        p,
    )
    keys = {i["key"] for r in advice_log.read_all(p) for i in r["items"]}
    assert keys == {"s", "c"}


def test_nothing_recorded_when_no_core(tmp_path):
    p = tmp_path / "a.jsonl"
    advice_log.record(64, [_advice(Priority.BONUS, "b")], p)
    assert advice_log.read_all(p) == []


def test_standing_advice_detects_unexecuted(tmp_path):
    """采纳了的建议,下回合触发条件就该消失 —— 连着给说明没做。"""
    p = tmp_path / "a.jsonl"
    for t in (60, 61, 62, 63):
        advice_log.record(t, [_advice(Priority.CORE, "expand", "补 2 座城")], p)
    advice_log.record(60, [_advice(Priority.CORE, "once", "一次性")], p)
    standing = advice_log.standing_advice(min_turns=3, path=p)
    assert [s["key"] for s in standing] == ["expand"]


def test_record_never_raises(tmp_path):
    """留痕失败不许影响简报生成。"""
    bad = tmp_path / "a.jsonl"
    bad.mkdir()
    advice_log.record(64, [_advice(Priority.CORE, "c")], bad)  # 不抛


def test_read_all_tolerates_corrupt_lines(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text('{"turn":1,"items":[]}\n<截断的坏行\n', encoding="utf-8")
    assert len(advice_log.read_all(p)) == 1
