"""Spectator-mode background services for video recording.

Two asyncio tasks that run alongside the agent and share the GameConnection:

CameraController — hops the in-game camera to key locations as the agent acts.
  Tools push (x, y) events; the controller replays them at 1-second intervals.
  Pauses automatically when a diplomacy screen is active.

PopupWatcher — polls for non-critical popups and auto-dismisses them after 1 second,
  keeping the view uncluttered. Skips while diplomacy screens are showing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from civ_mcp.connection import GameConnection

log = logging.getLogger(__name__)

SENTINEL = "---END---"

# How long (seconds) the camera dwells at each location before the next hop.
CAMERA_DWELL = 1.0

# Maximum queued camera events — oldest dropped when full.
CAMERA_QUEUE_MAX = 6

# How often (seconds) the popup watcher polls for visible non-critical popups.
POPUP_POLL_INTERVAL = 0.5

# How long (seconds) a popup must be visible before it is auto-dismissed.
POPUP_DISMISS_DELAY = 1.0

# How long (seconds) a CRITICAL diplomacy screen must stay visible before the
# watcher auto-responds. Must exceed the ~8s war-declaration animation window
# so we never race the agent's own diplomacy actions (send_diplomatic_action,
# propose_trade) which open sessions intentionally and clean up after ~9s.
CRITICAL_AUTO_RESOLVE_DELAY = 12.0

# How long (seconds) an incoming trade deal popup may block before it is
# auto-REJECTED. Generous so the agent has a window to accept it via
# respond_to_trade first; encounters (non-deal) are answered POSITIVE at
# CRITICAL_AUTO_RESOLVE_DELAY already.
DEAL_AUTO_REJECT_DELAY = 45.0

# Minimum spacing (seconds) between auto-resolve attempts.
CRITICAL_RESOLVE_COOLDOWN = 5.0

# Non-critical popups that will be auto-dismissed.
_NONCRITICAL_POPUPS = [
    "InGamePopup",
    "GenericPopup",
    "PopupDialog",
    "TechCivicCompletedPopup",
    "BoostUnlockedPopup",
    "GreatWorkShowcase",
    "NaturalWonderPopup",
    "WonderBuiltPopup",
    "EraCompletePopup",
    "HistoricMoments",
    "MomentPopup",
    "ProjectBuiltPopup",
    "RockBandPopup",
    "RockBandMoviePopup",
    "NaturalDisasterPopup",
]

# Critical screens — pause both camera and popup watcher while visible.
_CRITICAL_SCREENS = [
    "DiplomacyActionView",
    "DiplomacyDealView",
]

# Lua snippet that returns CLEAR / POPUP / CRITICAL in one roundtrip.
_POPUP_POLL_LUA = (
    "local r='CLEAR' "
    + "".join(
        f"do local c=ContextPtr:LookUpControl('/InGame/{n}') "
        f"if c and not c:IsHidden() then r='CRITICAL' end end "
        for n in _CRITICAL_SCREENS
    )
    + "if r=='CLEAR' then "
    + "".join(
        f"do local c=ContextPtr:LookUpControl('/InGame/{n}') "
        f"if c and not c:IsHidden() then r='POPUP' end end "
        for n in _NONCRITICAL_POPUPS
    )
    + "end "
    + f"print(r) print('{SENTINEL}')"
)

# Lua snippet to check for active diplomacy screens (used by camera).
_DIPLOMACY_CHECK_LUA = (
    "local active=false "
    + "".join(
        f"do local c=ContextPtr:LookUpControl('/InGame/{n}') "
        f"if c and not c:IsHidden() then active=true end end "
        for n in _CRITICAL_SCREENS
    )
    + f"print(active and 'YES' or 'NO') print('{SENTINEL}')"
)


@dataclass
class CameraEvent:
    x: int
    y: int
    label: str = ""


class CameraController:
    """Hops the game camera to locations pushed by tool handlers.

    Call push(x, y) from any tool. The controller dequeues events in the
    background, fires UI.LookAtPlot, and waits CAMERA_DWELL seconds before
    the next hop. Pauses automatically during active diplomacy screens.
    """

    def __init__(self, conn: "GameConnection") -> None:
        self._conn = conn
        self._queue: asyncio.Queue[CameraEvent] = asyncio.Queue(
            maxsize=CAMERA_QUEUE_MAX
        )
        self._task: asyncio.Task | None = None

    def push(self, x: int, y: int, label: str = "") -> None:
        """Push a camera event. Drops the oldest event if the queue is full."""
        event = CameraEvent(x, y, label)
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def clear(self) -> None:
        """Drain all pending events (call when a turn advances)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="camera-controller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _is_diplomacy_active(self) -> bool:
        try:
            lines = await self._conn.execute_write(_DIPLOMACY_CHECK_LUA, timeout=2.0)
            return any(line.strip() == "YES" for line in lines)
        except Exception:
            return False

    async def _look_at(self, x: int, y: int) -> None:
        lua = (
            f"local p=Map.GetPlot({x},{y}) "
            f"if p then pcall(function() UI.LookAtPlot(p) end) end "
            f"print('{SENTINEL}')"
        )
        try:
            await self._conn.execute_write(lua, timeout=2.0)
        except Exception:
            pass

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            # Hold until diplomacy screen closes.
            while True:
                try:
                    if not await self._is_diplomacy_active():
                        break
                except Exception:
                    break
                await asyncio.sleep(0.5)
            await self._look_at(event.x, event.y)
            await asyncio.sleep(CAMERA_DWELL)


class PopupWatcher:
    """Auto-dismisses non-critical popups after POPUP_DISMISS_DELAY seconds.

    Polls the InGame UI every POPUP_POLL_INTERVAL seconds. Pauses completely
    while diplomacy screens are active (CRITICAL status).
    """

    def __init__(self, conn: "GameConnection") -> None:
        self._conn = conn
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="popup-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @staticmethod
    async def _dismiss_crash_dialogs() -> None:
        """Check for and dismiss Win32 crash reporter dialogs."""
        from civ_mcp.game_launcher import dismiss_crash_dialogs

        dismissed = await dismiss_crash_dialogs()
        if dismissed:
            log.warning("PopupWatcher: dismissed crash dialog: %s", dismissed)

    async def _poll(self) -> str:
        """Returns 'POPUP', 'CRITICAL', or 'CLEAR'."""
        # Idle MCP hosts must not claim the only tuner socket in the background.
        # A foreground tool connects first; spectators then reuse that connection.
        if not self._conn.is_connected or self._conn.ingame_index is None:
            return "CLEAR"
        try:
            lines = await self._conn.execute_write(_POPUP_POLL_LUA, timeout=2.0)
            for line in lines:
                s = line.strip()
                if s in ("POPUP", "CRITICAL", "CLEAR"):
                    return s
        except Exception:
            pass
        return "CLEAR"

    async def _run(self) -> None:
        from civ_mcp.game_lifecycle import (
            auto_resolve_diplomacy_sessions,
            dismiss_popup,
        )

        first_seen: float | None = None
        critical_since: float | None = None
        last_resolve: float = 0.0
        # Check for Win32 crash dialogs every N iterations (not every 0.5s)
        _crash_check_interval = 6  # every ~3 seconds
        _iteration = 0

        while True:
            await asyncio.sleep(POPUP_POLL_INTERVAL)
            _iteration += 1
            try:
                status = await self._poll()
                now = asyncio.get_running_loop().time()

                if status == "POPUP":
                    critical_since = None
                    if first_seen is None:
                        first_seen = now
                    elif now - first_seen >= POPUP_DISMISS_DELAY:
                        log.debug(
                            "PopupWatcher: dismissing popup after %.1fs",
                            now - first_seen,
                        )
                        await dismiss_popup(self._conn)
                        first_seen = None
                elif status == "CRITICAL":
                    # Diplomacy screen is blocking. Give the agent's own
                    # diplomacy flows time to finish, then auto-respond so
                    # the game never deadlocks on a leader screen.
                    first_seen = None
                    if critical_since is None:
                        critical_since = now
                    elapsed = now - critical_since
                    if (
                        elapsed >= CRITICAL_AUTO_RESOLVE_DELAY
                        and now - last_resolve >= CRITICAL_RESOLVE_COOLDOWN
                    ):
                        last_resolve = now
                        reject_deals = elapsed >= DEAL_AUTO_REJECT_DELAY
                        actions = await auto_resolve_diplomacy_sessions(
                            self._conn, reject_deals=reject_deals
                        )
                        if actions:
                            log.info(
                                "PopupWatcher: auto-resolved diplomacy after "
                                "%.0fs (%s)",
                                elapsed,
                                "; ".join(actions),
                            )
                        else:
                            # CRITICAL screen with no open session — orphaned
                            # view (e.g. after a crash-recovered session).
                            # dismiss_popup restores the in-game UI.
                            log.info(
                                "PopupWatcher: CRITICAL view with no open "
                                "session after %.0fs — dismissing UI",
                                elapsed,
                            )
                            await dismiss_popup(self._conn)
                else:
                    # CLEAR — reset all timers
                    first_seen = None
                    critical_since = None

            except Exception:
                first_seen = None

            # Periodically check for Win32 crash reporter dialogs.
            # These are OS-level dialogs outside the game's Lua layer.
            if _iteration % _crash_check_interval == 0:
                await self._dismiss_crash_dialogs()
