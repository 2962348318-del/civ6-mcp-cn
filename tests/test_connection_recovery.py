"""Regression tests for tuner races, dead sockets, and uncertain commands."""

import asyncio
import struct
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from civ_mcp import tuner_client
from civ_mcp.connection import GameConnection
from civ_mcp.tuner_lock import ConnectionBusyError, TunerLease


def writer():
    value = MagicMock()
    value.is_closing.return_value = False
    value.wait_closed = AsyncMock()
    return value


def mock_tuner(monkeypatch, *, handshake=None):
    readers, writers = [], []

    async def connect(*args):
        reader = asyncio.StreamReader()
        stream = writer()
        readers.append(reader)
        writers.append(stream)
        return reader, stream

    connector = AsyncMock(side_effect=connect)
    monkeypatch.setattr(tuner_client, "connect", connector)
    monkeypatch.setattr(tuner_client, "handshake", handshake or AsyncMock(
        return_value=("Civ6", ["1", "GameCore_Tuner", "2", "InGame"])
    ))
    return connector, readers, writers


def test_concurrent_first_calls_share_one_handshake(monkeypatch):
    async def run():
        connector, _, _ = mock_tuner(monkeypatch)
        conn = GameConnection(port=54321)
        try:
            conn._locked_execute = AsyncMock(return_value=["ok"])
            results = await asyncio.gather(
                conn.execute_read("query"), conn.execute_write("poll"), conn.connect()
            )
            assert results[:2] == [["ok"], ["ok"]]
            assert connector.await_count == 1
            assert tuner_client.handshake.await_count == 1
        finally:
            await conn.disconnect()
    asyncio.run(run())


def test_handshake_failure_closes_socket_and_retries(monkeypatch):
    async def run():
        handshake = AsyncMock(side_effect=[
            OSError(64, "network name unavailable"),
            ("Civ6", ["7", "GameCore_Tuner", "9", "InGame"]),
        ])
        connector, _, streams = mock_tuner(monkeypatch, handshake=handshake)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        conn = GameConnection(port=54322)
        try:
            await conn.connect()
            assert connector.await_count == 2
            streams[0].close.assert_called_once()
            assert conn.is_connected and conn.gamecore_index == 7
        finally:
            await conn.disconnect()
    asyncio.run(run())


def test_disconnect_resets_state_even_when_windows_close_fails(monkeypatch):
    async def run():
        _, _, streams = mock_tuner(monkeypatch)
        conn = GameConnection(port=54323)
        await conn.connect()
        streams[0].wait_closed.side_effect = OSError(64, "dead socket")
        await conn.disconnect()
        assert not conn.is_connected
        assert conn.lua_states == {} and conn.gamecore_index is None
        other = TunerLease(conn.host, conn.port)
        other.acquire()
        other.release()
    asyncio.run(run())


def test_read_retry_uses_new_lua_index(monkeypatch):
    async def run():
        handshake = AsyncMock(side_effect=[
            ("Civ6", ["1", "GameCore_Tuner", "2", "InGame"]),
            ("Civ6", ["7", "GameCore_Tuner", "9", "InGame"]),
        ])
        mock_tuner(monkeypatch, handshake=handshake)
        conn = GameConnection(port=54324)
        conn._locked_execute = AsyncMock(side_effect=[ConnectionResetError(), ["T94"]])
        try:
            assert await conn.execute_read("turn query") == ["T94"]
            assert [call.args[0] for call in conn._locked_execute.await_args_list] == [1, 7]
        finally:
            await conn.disconnect()
    asyncio.run(run())


def test_write_is_not_replayed_after_lost_ack(monkeypatch):
    async def run():
        connector, _, _ = mock_tuner(monkeypatch)
        conn = GameConnection(port=54325)
        conn._locked_execute = AsyncMock(side_effect=ConnectionResetError())
        with pytest.raises(ConnectionError, match="not replayed automatically"):
            await conn.execute_write("purchase settler")
        assert conn._locked_execute.await_count == 1
        assert connector.await_count == 1
        assert not conn.is_connected
    asyncio.run(run())


def test_command_timeout_never_returns_partial_success(monkeypatch):
    async def run():
        mock_tuner(monkeypatch)
        monkeypatch.setattr(tuner_client, "drain_messages", AsyncMock(return_value=[]))
        monkeypatch.setattr(tuner_client, "send_message", AsyncMock())
        monkeypatch.setattr(tuner_client, "recv_message_timeout", AsyncMock(side_effect=[
            tuner_client.Message(3, "O\0InGame: partial"), None,
        ]))
        conn = GameConnection(port=54326)
        with pytest.raises(ConnectionError, match="outcome is unknown"):
            await conn.execute_write("print('partial')")
        assert not conn.is_connected
    asyncio.run(run())


def test_cancelled_handshake_releases_socket_and_lease(monkeypatch):
    async def run():
        entered = asyncio.Event()

        async def handshake(*args):
            entered.set()
            await asyncio.Event().wait()

        _, _, streams = mock_tuner(monkeypatch, handshake=handshake)
        conn = GameConnection(port=54327)
        task = asyncio.create_task(conn.connect())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        streams[0].close.assert_called_once()
        assert not conn.is_connected
        lease = TunerLease(conn.host, conn.port)
        lease.acquire()
        lease.release()
    asyncio.run(run())


def test_competing_process_cannot_claim_tuner(tmp_path):
    lease = TunerLease("127.0.0.1", 54328)
    lease.acquire()
    code = (
        "from civ_mcp.tuner_lock import TunerLease, ConnectionBusyError\n"
        "import sys\n"
        "try:\n TunerLease('127.0.0.1', 54328).acquire()\n"
        "except ConnectionBusyError:\n sys.exit(17)\n"
        "sys.exit(0)\n"
    )
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10, check=False)
        assert result.returncode == 17, result.stderr.decode(errors="replace")
    finally:
        lease.release()
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0


def test_partial_frame_timeout_invalidates_stream():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(tuner_client.HEADER_FMT, 20, 3) + b"part")
        with pytest.raises(ConnectionError, match="frame body timed out"):
            await tuner_client.recv_message_timeout(reader, timeout=0.01)
    asyncio.run(run())


def test_oversized_frame_is_rejected_before_allocation():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(tuner_client.HEADER_FMT, 2**32 - 1, 3))
        with pytest.raises(ConnectionError, match="Invalid FireTuner frame length"):
            await tuner_client.recv_message(reader)
    asyncio.run(run())


def test_idle_popup_watcher_does_not_open_connection():
    from civ_mcp.spectator import PopupWatcher

    async def run():
        conn = MagicMock(is_connected=False)
        conn.execute_write = AsyncMock()
        assert await PopupWatcher(conn)._poll() == "CLEAR"
        conn.execute_write.assert_not_awaited()
    asyncio.run(run())


def test_tuner_contention_never_restarts_game(monkeypatch):
    from civ_mcp import server

    async def run():
        logger = MagicMock(_turn=94)
        logger.log_error = AsyncMock()
        monkeypatch.setattr(server, "_get_logger", lambda ctx: logger)
        monkeypatch.setattr(server._logged, "_conn_errors", 4, raising=False)
        restart = AsyncMock()
        monkeypatch.setattr(server.game_launcher, "restart_and_load", restart)
        fn = AsyncMock(side_effect=ConnectionBusyError("TUNER_BUSY: occupied"))
        assert await server._logged(MagicMock(), "get_game_overview", {}, fn) == "TUNER_BUSY: occupied"
        assert server._logged._conn_errors == 4
        restart.assert_not_awaited()
    asyncio.run(run())


def test_connection_errors_do_not_restart_a_running_game(monkeypatch):
    from civ_mcp import server

    async def run():
        logger = MagicMock(_turn=94)
        logger.log_error = AsyncMock()
        monkeypatch.setattr(server, "_get_logger", lambda ctx: logger)
        monkeypatch.setattr(server._logged, "_conn_errors", 4, raising=False)
        monkeypatch.setattr(server.game_launcher, "is_game_running", lambda: True)
        restart = AsyncMock()
        monkeypatch.setattr(server.game_launcher, "restart_and_load", restart)
        fn = AsyncMock(side_effect=ConnectionError("handshake failed"))
        await server._logged(MagicMock(), "get_game_overview", {}, fn)
        restart.assert_not_awaited()
    asyncio.run(run())


def test_cli_does_not_replay_failed_batch(monkeypatch):
    import io
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import mcp_call

    request = '{"tool":"unit_action","args":{}}\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(request))
    execute = AsyncMock(return_value=1)
    monkeypatch.setattr(mcp_call, "run_once", execute)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(mcp_call.run())
    assert exc.value.code == 1
    execute.assert_awaited_once_with([request])
