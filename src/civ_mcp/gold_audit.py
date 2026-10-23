"""Gold ledger audit for the current game.

2026-08-04 P0-3:金币异常在实机两次查不到去向(T124→T131 缺 265g、
T182→T183 缺 462g)。get_game_overview 每次调用时跑 check_gold():
1. 从会话日志解析各回合 gold/GPT(与 scripts/econ_track.py 同源正则)
2. 计算 delta 与预期(gpt * 回合间隔)的差值
3. 差值超容差时,扫描该回合涉及金币的工具调用做归因
4. 返回中文审计文本;无异常返回空串

解析结果按日志文件 (name, size, mtime) 缓存,避免每次 overview 全量重读。
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

LOG_DIR = Path.home() / ".civ6-mcp"
# 容差:delta 与 gpt*gap 差不超过 3 金不算异常(取整误差范围)
TOLLERANCE = 3

# 涉及金币变动的工具(用于归因;propose_trade 的 mode=test 不实际成交,
# 归因文本里会带上 mode 供判断)
_GOLD_TOOLS = (
    "purchase_item",
    "purchase_tile",
    "patronize_great_person",
    "upgrade_unit",
    "propose_trade",
    "respond_to_trade",
)

# GPT 带符号(审查 2026-08-05 F2-3):narrate 用 {:+.0f} 渲染,负值输出
# "(-20/turn)" —— 旧正则写死 \+,GPT 一转负整行不入账,审计在经济最崩、
# 最需要它的时刻(T335 就是 -20/回合)恰好瞎掉。
_OVERVIEW_RE = re.compile(
    r"Gold: (\d+) \(([+-]?\d+)/turn\) \| Income: (\d+) \| Maintenance: (-?\d+)"
)

_cache: dict[tuple, tuple] = {}


def _log_files(civ: str, seed: int) -> list[Path]:
    return sorted(
        Path(p) for p in glob.glob(str(LOG_DIR / f"log_{civ}_{seed}_*.jsonl"))
    )


def _files_signature(civ: str, seed: int) -> tuple:
    sig = []
    for p in _log_files(civ, seed):
        try:
            st = p.stat()
            sig.append((p.name, st.st_size, st.st_mtime))
        except OSError:
            continue
    return tuple(sig)


def _iter_log_lines(civ: str, seed: int):
    for p in _log_files(civ, seed):
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    yield line
        except OSError:
            continue


def _parse_overview_rows(civ: str, seed: int) -> list[dict]:
    rows: list[dict] = []
    for line in _iter_log_lines(civ, seed):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("tool") != "get_game_overview":
            continue
        res = r.get("result_summary") or ""
        m = _OVERVIEW_RE.search(res)
        if not m:
            continue
        turn = r.get("turn")
        if not isinstance(turn, int):
            continue
        rows.append(
            {
                "turn": turn,
                "gold": int(m.group(1)),
                "gpt": int(m.group(2)),
                "ts": r.get("ts", 0),
            }
        )
    by_turn: dict[int, dict] = {}
    for row in sorted(rows, key=lambda r: r["ts"]):
        by_turn[row["turn"]] = row
    return [by_turn[t] for t in sorted(by_turn)]


def _attribution(civ: str, seed: int, turn: int) -> list[str]:
    """扫描某回合内涉及金币的工具调用,返回中文描述(去重保序)。"""
    out: list[str] = []
    seen: set[str] = set()
    for line in _iter_log_lines(civ, seed):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") != "tool_call" or r.get("turn") != turn:
            continue
        tool = r.get("tool")
        if tool not in _GOLD_TOOLS:
            continue
        params = r.get("params") or {}
        if tool == "purchase_item":
            text = (
                f"购买 {params.get('item_name')}"
                f"({params.get('yield_type', 'YIELD_GOLD')})"
            )
        elif tool == "purchase_tile":
            text = f"购地 ({params.get('x')},{params.get('y')})"
        elif tool == "upgrade_unit":
            text = "单位升级"
        elif tool == "patronize_great_person":
            text = "赞助伟人"
        elif tool == "propose_trade":
            text = f"提议交易(mode={params.get('mode')})"
        elif tool == "respond_to_trade":
            text = f"回应交易 accept={params.get('accept')}"
        else:
            continue
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def check_gold(civ: str, seed: int) -> str:
    """返回审计文本;数据不足或无异常时返回空字符串。永不抛异常。"""
    key = (civ, seed)
    sig = _files_signature(civ, seed)
    if key in _cache and _cache[key][0] == sig:
        return _cache[key][1]
    try:
        rows = _parse_overview_rows(civ, seed)
        if len(rows) < 2:
            result = ""
        else:
            latest, prev = rows[-1], rows[-2]
            gap = latest["turn"] - prev["turn"]
            # gap 超过 10 回合 = 中间 overview 记录缺失(日志轮转/会话断开),
            # 预期值(gpt*gap)不可靠 —— T150→T183 曾报出 -750 的假异常。
            # 只审计相邻(或接近相邻)回合,数据稀疏时静默跳过。
            if gap <= 0 or gap > 10:
                result = ""
            elif abs(latest["gpt"] - prev["gpt"]) > TOLLERANCE and gap > 1:
                # 费率跳变 + 区间多回合(审查 F2-10):区间内实际生效的是
                # prev 的费率,用 latest 的反推必然报假账 —— T335 那种
                # "解散弩手+签 10GPT,-20 转 +28"的回合能凭空造出几百金
                # 的"异常"。费率变了无法可靠归因,与数据稀疏同样静默跳过。
                result = ""
            else:
                # 区间内生效的是 prev 的费率(F2-10):gap==1 时两者等价,
                # gap>1 时上面已排除费率跳变,用 prev 更接近真实。
                expected = prev["gold"] + prev["gpt"] * gap
                diff = (latest["gold"] - prev["gold"]) - prev["gpt"] * gap
                if abs(diff) <= TOLLERANCE:
                    result = ""
                else:
                    attrib = _attribution(civ, seed, latest["turn"])
                    lines = [
                        f"⚠️ 金币审计 T{prev['turn']}→T{latest['turn']}:"
                        f"{prev['gold']}→{latest['gold']}(预期 {expected},差值 {diff:+d})"
                    ]
                    if attrib:
                        lines.append("  本回合金币操作: " + "; ".join(attrib))
                    else:
                        lines.append(
                            "  未找到本回合金币工具调用 —— 差值来源不明,建议人工核对。"
                        )
                    result = "\n".join(lines)
    except Exception:
        result = ""
    _cache[key] = (sig, result)
    return result
