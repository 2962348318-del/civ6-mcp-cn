"""CORE+ 建议留痕 —— 校准指标 #3 的自动化底稿。

四个过程指标里的第三条(「决策树推荐与事后最优的偏离次数」)一直靠
agent 人肉抄:每次采纳一条 CORE 及以上的建议记一行,5-10 回合后回看。
校准局 #3 的报告就是这么写出来的。

问题和 force 记录同源:**靠自觉的记录,换个模型就换个质量**。而
「换一个 AI 来用也能达到同样水平」是这个项目的目标之一 —— 靠叮嘱
达不到,只能靠机制。

所以把"系统给过什么建议"这一半自动落盘;agent 只需要补
"为什么没照做",不用再抄建议原文。对照后续实际(城市数/科技数变化
已在 PowerHistory 里)可以半自动判定采纳与效果。

写入 data/advice_log.jsonl,一回合一行:
    {"turn":64,"items":[{"key":"playbook:expand_plan:schedule",
                         "priority":"CORE","action":"..."}]}

**只记 CORE 及以上**:BONUS/OPTIMIZE 档每回合几十条,记下来会淹掉
真正需要复盘的决策,而复盘看的从来是那几条硬建议。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

ADVICE_LOG_PATH = Path("data") / "advice_log.jsonl"
# action 摘要长度上限 —— 复盘认得出是哪条即可,全文在简报里
_ACTION_MAX = 120


def record(turn: int, items, path: Path | None = None) -> None:
    """记录本回合的 CORE+ 建议。**任何失败都不影响简报生成。**"""
    try:
        from civ_mcp.advice import Priority

        rows = []
        for it in items or []:
            pri = getattr(it, "priority", None)
            if pri is None or int(pri) > int(Priority.CORE):
                continue  # 数值越小越紧急,> CORE 的是 OPTIMIZE/BONUS
            rows.append(
                {
                    "key": str(getattr(it, "dedup_key", "") or ""),
                    "priority": getattr(pri, "name", str(pri)),
                    "module": str(getattr(it, "module", "") or ""),
                    "action": str(getattr(it, "action", "") or "")[:_ACTION_MAX],
                }
            )
        if not rows:
            return
        target = path or ADVICE_LOG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"turn": int(turn), "items": rows}, ensure_ascii=False)
                + "\n"
            )
    except Exception:
        log.debug("建议留痕写入失败(不影响简报)", exc_info=True)


def read_all(path: Path | None = None) -> list[dict]:
    target = path or ADVICE_LOG_PATH
    if not target.exists():
        return []
    out: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def standing_advice(min_turns: int = 3, path: Path | None = None) -> list[dict]:
    """连续出现 >= min_turns 回合仍在给的建议 —— 大概率是没被执行的。

    这是"偏离"的自动检测:一条 CORE 建议如果被采纳了,下回合它的触发
    条件就该消失。连着给了五回合还在给,说明要么没做,要么做了没用 ——
    两种都值得复盘时看一眼。
    """
    runs: dict[str, dict] = {}
    for rec in read_all(path):
        turn = int(rec.get("turn", 0) or 0)
        for it in rec.get("items") or []:
            key = it.get("key") or it.get("action", "")
            r = runs.setdefault(
                key, {"key": key, "action": it.get("action", ""), "turns": []}
            )
            r["turns"].append(turn)
    out = [r for r in runs.values() if len(set(r["turns"])) >= min_turns]
    out.sort(key=lambda r: len(set(r["turns"])), reverse=True)
    return out
