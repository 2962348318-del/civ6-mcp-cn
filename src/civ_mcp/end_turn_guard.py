"""Cross-process end_turn guard -- prevents duplicate ACTION_ENDTURN sends.

Why this exists: civ6-mcp servers are short-lived (each mcp_call.py batch
spawns a fresh process, and the FireTuner port only accepts one client, so
processes race and get killed by self-healing retries). The in-process
``_pending_end_turn`` flag dies with its process. If ACTION_ENDTURN executed
and the turn advanced but the response was lost, the client retries the same
request in a NEW process which has no memory of the previous send -- it sends
ACTION_ENDTURN again and burns a fresh turn.

This module persists an "ACTION_ENDTURN in flight / completed" marker to disk
so any process can detect that the turn already advanced and report instead
of re-sending.

Marker lifecycle:
  send ACTION_ENDTURN        -> {"from_turn": N, "written_at": t0, "advanced": false}
  turn confirmed advanced    -> {"from_turn": N, "written_at": t1, "advanced": true}
  next end_turn call entry   -> consumed (report flow or cleared) by the
                                caller; see end_turn.py execute_end_turn
"""

from __future__ import annotations

from civ_mcp.resources import runtime_data_path

import json
import os
import tempfile
import time
from pathlib import Path

_GUARD_PATH = (
    runtime_data_path("end_turn_guard.json")
)

# If a pending (not advanced) marker is older than this and the turn has NOT
# advanced, the previous ACTION_ENDTURN is presumed dead -- safe to re-send.
STALE_AFTER_SECONDS = 45.0

# After a turn has been confirmed advanced, this is how long the marker
# remains "fresh" for the retry-detection window. A new end_turn call within
# this window on the same turn is treated as a lost-response retry (report
# only, no re-send). After the window, the marker is treated as completed
# history and the call proceeds normally. This is the only safe default:
# wrongly reporting costs one call; wrongly re-sending burns a turn.
# mcp_call self-heal retry takes ~10-15s; a normal new-turn call with
# queries+moves+reflections takes well over 30s.
ADVANCED_GRACE_SECONDS = 30.0

# A *pending* (advanced=false) marker whose turn HAS advanced normally means
# the send worked and the response was lost -- report-only is correct. But if
# that marker is this old, the real story is an AI-turn hang: game killed,
# relaunched, save reloaded (the reload lands on turn+1, mimicking a lost
# response). Treating the fresh call as a retry then swallows a genuine
# end-turn request (T373 field defect, calibration game 3). Lost-response
# retries arrive within minutes (self-heal ~15s, agent retry minutes); hang
# recovery takes 30+ minutes (6-10min hang detection + kill + relaunch +
# manual reload). Past this age, discard the marker and send normally.
PENDING_ADVANCE_STALE_SECONDS = 1800.0


def _read() -> dict | None:
    try:
        raw = _GUARD_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("from_turn"), int):
            return data
    except (OSError, ValueError):
        pass
    return None


def read_marker() -> dict:
    """Return the current marker (or an empty dict if absent/invalid)."""
    data = _read()
    if not data:
        return {}
    return {
        "from_turn": data["from_turn"],
        "written_at": float(data.get("written_at", 0.0)),
        "advanced": bool(data.get("advanced", False)),
    }


def _write(from_turn: int, advanced: bool) -> None:
    _GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"from_turn": from_turn, "written_at": time.time(), "advanced": advanced}
    fd, tmp = tempfile.mkstemp(dir=str(_GUARD_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _GUARD_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_pending(from_turn: int) -> None:
    """Persist the marker right before ACTION_ENDTURN is sent."""
    _write(from_turn, advanced=False)


def write_advanced(from_turn: int) -> None:
    """Refresh the marker after the turn has been confirmed advanced."""
    _write(from_turn, advanced=True)


def clear() -> None:
    try:
        _GUARD_PATH.unlink(missing_ok=True)
    except OSError:
        pass
