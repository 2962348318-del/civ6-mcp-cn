"""Serialized, reconnectable FireTuner connection to Civilization VI."""

from __future__ import annotations

import asyncio
import logging

from civ_mcp import tuner_client
from civ_mcp.lua._helpers import SENTINEL
from civ_mcp.tuner_lock import TunerLease

log = logging.getLogger(__name__)


class LuaError(Exception):
    """Raised when Lua code execution returns an error."""


class GameConnection:
    """Own one tuner socket; serialize handshake, commands, and teardown."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4318):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._lease = TunerLease(host, port)
        self._ready = False
        self.lua_states: dict[int, str] = {}
        self.gamecore_index: int | None = None
        self.ingame_index: int | None = None
        self._connect_failed_before = False

    @property
    def is_connected(self) -> bool:
        return (
            self._ready
            and self._reader is not None
            and not self._reader.at_eof()
            and self._writer is not None
            and not self._writer.is_closing()
        )

    async def connect(self) -> None:
        async with self._lock:
            await self._ensure_connected_locked()

    async def ensure_connected(self) -> None:
        await self.connect()

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def reconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()
            await self._connect_locked()

    async def _disconnect_locked(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self._ready = False
        self.lua_states.clear()
        self.gamecore_index = self.ingame_index = None
        try:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except (OSError, TimeoutError):
                    # A dead Windows socket may raise WinError 64 on close.
                    pass
        finally:
            self._lease.release()

    async def _ensure_connected_locked(self) -> None:
        if not self.is_connected:
            await self._disconnect_locked()
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        quiet = self._connect_failed_before
        last_exc: Exception | None = None
        for attempt in range(1, 5):
            if attempt > 1:
                await asyncio.sleep(1.5)
            # Contention is not a broken game and must not cause a retry storm.
            self._lease.acquire()
            try:
                self._reader, self._writer = await tuner_client.connect(
                    self.host, self.port
                )
                identity, states = await tuner_client.handshake(
                    self._reader, self._writer
                )
                i = 0
                while i + 1 < len(states):
                    try:
                        idx = int(states[i])
                    except ValueError:
                        i += 1
                        continue
                    self.lua_states[idx] = states[i + 1]
                    i += 2
                if not self.lua_states:
                    raise ConnectionError("FireTuner handshake returned no Lua states")
                self.gamecore_index = self._state_index("GameCore_Tuner")
                self.ingame_index = self._state_index("InGame")
                self._ready = True
                self._connect_failed_before = False
                log.info("Connected: %s (%d Lua states)", identity, len(self.lua_states))
                return
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
                last_exc = exc
                await self._disconnect_locked()
                log.debug("Tuner handshake attempt %d/4 failed: %s", attempt, exc)
            except BaseException:
                # Cancellation during handshake must also release the lease.
                await self._disconnect_locked()
                raise
        self._connect_failed_before = True
        message = (
            f"Cannot connect to Civ 6 at {self.host}:{self.port} after 4 attempts. "
            f"Last error: {last_exc}. Check EnableTuner=1 and close other tuner clients."
        )
        (log.debug if quiet else log.info)(message)
        raise ConnectionError(message) from last_exc

    def _state_index(self, name: str) -> int | None:
        return next((idx for idx, state in self.lua_states.items() if state == name), None)

    async def _ensure_game_states_locked(self) -> None:
        await self._ensure_connected_locked()
        if self.gamecore_index is None or self.ingame_index is None:
            await self._disconnect_locked()
            await self._connect_locked()
        if self.gamecore_index is None or self.ingame_index is None:
            raise ConnectionError(
                "GameCore_Tuner/InGame states not found. "
                "Make sure a game is in progress (not at the main menu)."
            )

    async def execute_read(self, lua_code: str, timeout: float = 5.0) -> list[str]:
        return await self._execute_and_collect("GameCore_Tuner", lua_code, timeout, retry=True)

    async def execute_write(self, lua_code: str, timeout: float = 5.0) -> list[str]:
        # A lost acknowledgement does not prove the command was not executed.
        return await self._execute_and_collect("InGame", lua_code, timeout)

    async def execute_in_state(
        self, state_index: int, lua_code: str, timeout: float = 5.0
    ) -> list[str]:
        return await self._execute_and_collect(state_index, lua_code, timeout)

    async def _execute_and_collect(
        self, state: str | int, lua_code: str, timeout: float, *, retry: bool = False
    ) -> list[str]:
        async with self._lock:
            # Remember named arbitrary states before a reconnect changes indexes.
            if isinstance(state, int):
                state = self.lua_states.get(state, state)
            for attempt in range(2 if retry else 1):
                if state in ("GameCore_Tuner", "InGame"):
                    await self._ensure_game_states_locked()
                else:
                    await self._ensure_connected_locked()
                idx = self._state_index(state) if isinstance(state, str) else state
                if idx is None:
                    raise ConnectionError(f"Lua state {state!r} is unavailable after reconnect")
                try:
                    return await self._locked_execute(idx, lua_code, timeout)
                except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
                    await self._disconnect_locked()
                    if retry and attempt == 0:
                        log.info("Read connection lost, reconnecting once...")
                        continue
                    detail = (
                        "Read failed after one retry." if retry else
                        "Command outcome is unknown; inspect game state before retrying. "
                        "The command was not replayed automatically."
                    )
                    raise ConnectionError(f"FireTuner connection lost: {exc}. {detail}") from exc
                except asyncio.CancelledError:
                    # An interrupted receive can leave half a frame on the socket.
                    await self._disconnect_locked()
                    raise
        raise AssertionError("unreachable")

    async def _locked_execute(
        self, state_index: int, lua_code: str, timeout: float
    ) -> list[str]:
        assert self._reader is not None and self._writer is not None
        await tuner_client.drain_messages(self._reader, timeout=0.1)
        if SENTINEL not in lua_code:
            lua_code += f"\nprint('{SENTINEL}')"
        await tuner_client.send_message(
            self._writer, tuner_client.TAG_COMMAND, f"CMD:{state_index}:{lua_code}"
        )
        lines: list[str] = []
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Lua response did not reach the completion marker")
            msg = await tuner_client.recv_message_timeout(self._reader, timeout=remaining)
            if msg is None:
                raise TimeoutError("Lua response did not reach the completion marker")
            if msg.payload.startswith("ERR:"):
                raise LuaError(msg.payload)
            value = _parse_output(msg.payload)
            if value is not None:
                if value.strip() == SENTINEL:
                    return lines
                lines.append(value)


def _parse_output(payload: str) -> str | None:
    if not payload.startswith("O"):
        return None
    sep = payload.find(": ", 2)
    if sep >= 0:
        return payload[sep + 2 :]
    return payload.lstrip("O").lstrip("\x00").strip()
