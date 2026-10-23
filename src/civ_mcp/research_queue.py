"""Research queue -- plan techs/civics once, auto-advance on completion.

Why this exists (2026-08-09, calibration game 3): every tech/civic completion
left "Research: None" idling until the agent noticed the Action Required item
and hand-picked the next entry -- at least 5 such stalls in one game, and the
T372 hang-recovery window sat on an empty civic slot for multiple turns. The
route was always planned turns earlier (e.g. 核裂变→激光→核聚变 for the Mars
reactor gate); the loss was purely the gap between "planned in a diary entry"
and "wired into the engine".

The queue is a fallback, not a lock: a manual set_research always wins, and
the queue is only consumed when the current research/civic slot is EMPTY at
end_turn time. Persisted to disk keyed by game identity so it survives server
restarts and never bleeds across games.
"""

from __future__ import annotations

from civ_mcp.resources import runtime_data_path

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_QUEUE_PATH = (
    runtime_data_path("research_queue.json")
)

_VALID_CATEGORIES = ("tech", "civic")


def _read() -> dict:
    try:
        data = json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _write(data: dict) -> None:
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_QUEUE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _QUEUE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_queue(game_key: str, category: str) -> list[str]:
    """Return the pending queue for this game+category (may be empty)."""
    data = _read()
    if data.get("game_key") != game_key:
        return []
    items = data.get(category, [])
    return list(items) if isinstance(items, list) else []


def set_queue(game_key: str, category: str, items: list[str]) -> list[str]:
    """Replace the queue for this game+category; returns the stored list.

    Switching game_key drops the other game's queues entirely -- queues never
    bleed across games.
    """
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"category must be one of {_VALID_CATEGORIES}")
    data = _read()
    if data.get("game_key") != game_key:
        data = {"game_key": game_key}
    data[category] = list(items)
    _write(data)
    return list(items)


def pop_next(game_key: str, category: str) -> str | None:
    """Remove and return the queue head, or None if empty/wrong game."""
    data = _read()
    if data.get("game_key") != game_key:
        return None
    items = data.get(category)
    if not isinstance(items, list) or not items:
        return None
    head = items.pop(0)
    _write(data)
    return str(head)
