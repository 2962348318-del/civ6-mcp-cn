"""Per-city production queue -- plan builds once, auto-advance on completion.

Why this exists (2026-08-10, Rome calibration game, defect 65 -- user-named
priority): every completed build left the city on "Building: nothing" until the
agent noticed the Action Required nag and hand-picked the next item.  Aachen,
Ostia, Ravenna, Frankfurt, Paris and Aquileia each idled 1-2 turns this way,
repeatedly, in a single game -- pure production thrown away.  Same failure
shape as the research idle the research queue fixed: the plan existed in the
diary, it just was not wired into the engine.

Same contract as research_queue: the queue is a fallback, not a lock.  A
manual set_city_production always wins; the queue is only consumed when the
city's production slot is EMPTY at end_turn time.  Persisted keyed by game
identity so it survives restarts and never bleeds across games.

Item format (one string per queue entry):
    "BUILDING_MARKET"            -- item_type inferred from prefix
    "UNIT_TRADER"
    "PROJECT_ENHANCE_DISTRICT_CAMPUS"
    "DISTRICT_CAMPUS@69,47"      -- districts carry placement coords inline
"""

from __future__ import annotations

from civ_mcp.resources import runtime_data_path

import json
import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_QUEUE_PATH = (
    runtime_data_path("production_queue.json")
)

_PREFIX_TO_TYPE = {
    "BUILDING_": "BUILDING",
    "UNIT_": "UNIT",
    "DISTRICT_": "DISTRICT",
    "PROJECT_": "PROJECT",
}

_ITEM_RE = re.compile(r"^([A-Z0-9_]+?)(?:@(-?\d+),(-?\d+))?$")


def parse_item(raw: str) -> dict | None:
    """Parse one queue entry into {item_type, item_name, target_x, target_y}.

    Returns None if the string has no recognized prefix or bad syntax --
    callers should reject the whole set_queue call loudly rather than store
    an entry that will silently fail at consumption time.
    """
    m = _ITEM_RE.match(raw.strip())
    if not m:
        return None
    name, tx, ty = m.group(1), m.group(2), m.group(3)
    item_type = None
    for prefix, t in _PREFIX_TO_TYPE.items():
        if name.startswith(prefix):
            item_type = t
            break
    if item_type is None:
        return None
    if item_type == "DISTRICT" and (tx is None or ty is None):
        # Districts without coords fail placement at consumption time --
        # reject at queue time where the agent can still fix it.
        return None
    entry: dict = {"item_type": item_type, "item_name": name}
    if tx is not None:
        entry["target_x"] = int(tx)
        entry["target_y"] = int(ty)
    return entry


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


def get_queue(game_key: str, city_id: int) -> list[dict]:
    """Return the pending queue for this game+city (may be empty)."""
    data = _read()
    if data.get("game_key") != game_key:
        return []
    cities = data.get("cities", {})
    items = cities.get(str(city_id), []) if isinstance(cities, dict) else []
    return list(items) if isinstance(items, list) else []


def get_all(game_key: str) -> dict[int, list[dict]]:
    """Return {city_id: queue} for every city with a non-empty queue."""
    data = _read()
    if data.get("game_key") != game_key:
        return {}
    cities = data.get("cities", {})
    out: dict[int, list[dict]] = {}
    if isinstance(cities, dict):
        for cid, items in cities.items():
            if isinstance(items, list) and items:
                try:
                    out[int(cid)] = list(items)
                except ValueError:
                    continue
    return out


def set_queue(game_key: str, city_id: int, items: list[dict]) -> list[dict]:
    """Replace the queue for this game+city; returns the stored list.

    Switching game_key drops the other game's queues entirely.
    """
    data = _read()
    if data.get("game_key") != game_key:
        data = {"game_key": game_key}
    cities = data.get("cities")
    if not isinstance(cities, dict):
        cities = {}
        data["cities"] = cities
    if items:
        cities[str(city_id)] = list(items)
    else:
        cities.pop(str(city_id), None)
    _write(data)
    return list(items)


def pop_next(game_key: str, city_id: int) -> dict | None:
    """Remove and return the queue head for this city, or None."""
    data = _read()
    if data.get("game_key") != game_key:
        return None
    cities = data.get("cities")
    if not isinstance(cities, dict):
        return None
    items = cities.get(str(city_id))
    if not isinstance(items, list) or not items:
        return None
    head = items.pop(0)
    if not items:
        cities.pop(str(city_id), None)
    _write(data)
    return head if isinstance(head, dict) else None
