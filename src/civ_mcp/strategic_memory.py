"""Strategic Memory Layer for civ6-mcp.

Provides long-term strategic memory that persists across game sessions.
Unlike the per-turn diary, this compresses multi-turn patterns into a
single strategic context that the LLM can read before each turn cycle.

Data stored as JSON in ~/.civ6-mcp/strategic_summary.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path.home() / ".civ6-mcp"
MEMORY_FILE = MEMORY_DIR / "strategic_summary.json"


def _file_for(game_id: str) -> Path:
    """按局分键的落盘位置。game_id 为空时回退到旧的全局文件。

    2026-08-09 修复:此前全部对局共写一个文件 —— 新局开局
    read_strategic_summary 读到的是上一局 T300 的战略摘要,跨局串味。
    """
    if not game_id:
        return MEMORY_FILE
    safe = "".join(c for c in game_id if c.isalnum() or c in "_-")
    return MEMORY_DIR / f"strategic_summary_{safe}.json"


def _load(path: Path) -> dict:
    """Load current strategic summary, returns empty dict if none exists."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict, path: Path) -> None:
    """Persist strategic summary to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_summary(
    turn: int,
    summary: str,
    goals: str = "",
    threats: str = "",
    opportunities: str = "",
    key_decisions: str = "",
    game_id: str = "",
) -> str:
    """Write or update the strategic summary for the current game.

    Called every 5-10 turns to compress recent history into a compact
    strategic context. The LLM reads this before each turn cycle to
    maintain continuity across long game sessions.

    Args:
        turn: Current turn number.
        summary: 2-3 sentence summary of current empire state and situation.
        goals: Current long-term goals (victory target, expansion plans, etc).
        threats: Major threats (hostile neighbors, barbarian camps, resource gaps).
        opportunities: Current opportunities (weak neighbors, good settle spots).
        key_decisions: Important decisions made recently and their rationale.

    Returns:
        Confirmation string.
    """
    path = _file_for(game_id)
    data = _load(path)
    history = data.get("history", [])

    entry = {
        "turn": turn,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary.strip(),
        "goals": goals.strip(),
        "threats": threats.strip(),
        "opportunities": opportunities.strip(),
        "key_decisions": key_decisions.strip(),
    }

    # Keep only last 20 entries to avoid bloat
    history.append(entry)
    if len(history) > 20:
        history = history[-20:]

    data["history"] = history
    data["current"] = entry
    data["last_updated_turn"] = turn

    _save(data, path)
    return (
        f"Strategic summary written for turn {turn}. "
        f"Total history entries: {len(history)}."
    )


def read_summary(game_id: str = "") -> str:
    """Read the current strategic summary.

    Returns a formatted string with the full strategic context:
    - Current state and recent history
    - Long-term goals
    - Known threats and opportunities
    - Key decisions log

    The LLM should call this at the start of each decision cycle
    to re-establish strategic context that may have been lost
    from the conversation window.
    """
    path = _file_for(game_id)
    data = _load(path)
    if not data or not data.get("current"):
        # 分局文件不存在时不回退读全局旧文件 —— 那可能是别局的摘要,
        # 串味比空白更危险(会拿上一局的威胁/目标当本局事实)
        legacy_hint = (
            " (存在一份未分局的旧摘要,可能属于以前的对局,已不再读取)"
            if game_id and MEMORY_FILE.exists()
            else ""
        )
        return (
            "No strategic summary exists yet for this game. "
            "Start playing and build context." + legacy_hint
        )

    current = data["current"]
    history = data.get("history", [])

    lines = ["=== STRATEGIC SUMMARY ===", ""]

    # Current state
    lines.append(f"Last Updated: Turn {current['turn']}")
    lines.append("")
    lines.append(f"Current State: {current['summary']}")
    lines.append("")

    if current.get("goals"):
        lines.append(f"Long-Term Goals: {current['goals']}")
        lines.append("")

    if current.get("threats"):
        lines.append(f"Active Threats: {current['threats']}")
        lines.append("")

    if current.get("opportunities"):
        lines.append(f"Opportunities: {current['opportunities']}")
        lines.append("")

    if current.get("key_decisions"):
        lines.append(f"Key Decisions: {current['key_decisions']}")
        lines.append("")

    # Recent history (last 5 entries, excluding current)
    if len(history) > 1:
        recent = history[-6:-1]  # exclude current
        lines.append("--- Recent History ---")
        for h in recent:
            lines.append(f"T{h['turn']}: {h['summary'][:120]}")
        lines.append("")

    lines.append(f"(Strategy file: {path})")

    return "\n".join(lines)
