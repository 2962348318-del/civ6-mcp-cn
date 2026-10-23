"""force 越权记录 —— 把"记录 force 理由"从叮嘱变成机制。

本项目的一条铁律:**执行前拦截优先于文档提醒**,理由是"写进文档没用
(下一个会话的我照样会忘)"。而"每次 force 都要记一行理由"这件事,
到 2026-08-06 为止一直是靠交接文档叮嘱、靠 agent 自觉抄 —— 恰好违反了
这条铁律本身。校准局 #3 的 force 记录表就是人肉维护的。

后果不是少一份表:**误伤一次就会养成无脑 force 的习惯**,而复盘时
分不清"拦截判错"还是"模型判错" —— 没有证据,两边都可以说是对方的错,
于是拦截层的每一次误报都无法被证伪,也就无法被修。

所以改成机制:每一次 force 都自动落盘,不依赖任何模型的自觉。
换任何 AI 来用,这份数据的质量都一样 —— 这正是"换一个 AI 也能达到
同样水平"要求的东西。

写入 data/force_log.jsonl,一行一次:
    {"turn":64,"tool":"unit_action","reason":"...","args":{...}}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

FORCE_LOG_PATH = Path("data") / "force_log.jsonl"
# 理由缺失时的占位。过渡期(一个校准局)允许,之后收紧为必填。
NO_REASON = "(未给理由)"
# args 里不落盘的键 —— force 自身与理由已经单独成列
_SKIP_KEYS = frozenset({"force", "force_reason"})


def record(
    tool: str,
    args: dict | None = None,
    reason: str = "",
    turn: int | None = None,
    path: Path | None = None,
) -> None:
    """记一次 force 越权。**任何失败都不许影响操作本身。**

    落盘失败只是少一行记录;把异常抛出去会让一次合法的 force 操作
    整个失败 —— 那是拿"观测"去破坏"被观测的事",本末倒置。
    """
    try:
        target = path or FORCE_LOG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "turn": turn,
            "tool": tool,
            "reason": (reason or "").strip() or NO_REASON,
            "args": {k: v for k, v in (args or {}).items() if k not in _SKIP_KEYS},
        }
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("force 记录写入失败(不影响操作)", exc_info=True)


def read_all(path: Path | None = None) -> list[dict]:
    """读回全部记录。文件不存在返回空列表。"""
    target = path or FORCE_LOG_PATH
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


def summary(path: Path | None = None) -> str:
    """复盘用的 force 记录表。无记录时明说是 0 次(那是好消息,要说出来)。"""
    rows = read_all(path)
    if not rows:
        return "force 越权记录:0 次 —— 所有拦截都被接受了"
    lines = [f"force 越权记录:{len(rows)} 次"]
    missing = sum(1 for r in rows if r.get("reason") == NO_REASON)
    for r in rows:
        t = r.get("turn")
        lines.append(
            f"  T{t if t is not None else '?'} | {r.get('tool', '?')} | "
            f"{r.get('reason', NO_REASON)}"
        )
    if missing:
        lines.append(
            f"  ⚠️ 其中 {missing} 次没给理由 —— 复盘时无法判断是拦截判错还是操作判错"
        )
    return "\n".join(lines)
