"""MCP server for Civilization VI — lets LLM agents read game state and play.

Uses FastMCP with the lifespan pattern to maintain a persistent TCP connection
to the running game via FireTuner protocol.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# uvicorn removed – web dashboard disabled
from mcp.server.fastmcp import Context, FastMCP

# from civ_mcp.web_api import create_app  # disabled – Uvicorn web dashboard removed
from civ_mcp import game_launcher, heartbeat, strategic_memory
from civ_mcp import lua as lq
from civ_mcp import narrate as nr
from civ_mcp.connection import GameConnection, LuaError
from civ_mcp.tuner_lock import ConnectionBusyError
from civ_mcp.diary import (
    diary_path as _diary_path,
)
from civ_mcp.diary import (
    format_diary_entry as _format_diary_entry,
)
from civ_mcp.diary import (
    merge_agent_reflections as _merge_agent_reflections,
)
from civ_mcp.game_over_watchdog import GameOverWatchdog
from civ_mcp.game_state import GameState
from civ_mcp.hexgrid import hex_distance
from civ_mcp.logger import GameLogger
from civ_mcp.map_capture import MapCapture
from civ_mcp.spatial import SpatialTracker
from civ_mcp.spectator import CameraController, PopupWatcher
from civ_mcp.telemetry import (
    EVENT_CITY_ROW,
    EVENT_DIARY_ROW,
    AlertSink,
    CloudSink,
    LocalSink,
    TelemetryEmitter,
)

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    game: GameState
    logger: GameLogger
    camera: CameraController
    popup_watcher: PopupWatcher
    spatial: SpatialTracker
    map_capture: MapCapture
    watchdog: GameOverWatchdog


async def _auto_boot(conn: GameConnection, save_name: str) -> None:
    """Launch game and load a save before MCP tools become available.

    Called during lifespan when CIV_MCP_SAVE_FILE is set (eval mode).
    Blocks until the game is loaded and ready for play.
    """
    import glob

    from civ_mcp.game_lifecycle import load_game_save

    # 0. Clear stale MCP autosaves. These are the saves that the main
    # menu's "Continue Game" button would load. If a previous run
    # crashed at T197, "Continue Game" resumes T197 instead of loading
    # the scenario save. Clearing them makes "Continue Game" harmless
    # (it would load the scenario save or nothing).
    # This does NOT break --resume-save which loads by name via Lua.
    stale = glob.glob(os.path.join(game_launcher.SINGLE_SAVE_DIR, "0_MCP_*.Civ6Save"))
    if stale:
        for f in stale:
            try:
                os.remove(f)
            except OSError:
                pass
        log.info("Auto-boot: cleared %d stale MCP autosave(s)", len(stale))

    # 1. Launch game (or reuse if already running).
    # The eval runner's ensure_game_ready() typically launches the game
    # before the MCP server starts. _launch_game_sync() detects an
    # already-running game and returns immediately, avoiding a wasteful
    # kill + relaunch cycle through the Aspyr launcher.
    # The step-5 verification below catches wrong-save scenarios as a
    # safety net (the Lua load path fails when mid-session, not from
    # main menu).
    heartbeat.write("launching")
    log.info("Auto-boot: launching game...")
    result = await asyncio.to_thread(game_launcher._launch_game_sync)
    log.info("Auto-boot: launch result: %s", result)

    # 2. Connect to FireTuner (retry — game takes time to start)
    for attempt in range(90):
        try:
            await conn.connect()
            log.info("Auto-boot: connected to FireTuner")
            heartbeat.write("connecting")
            break
        except ConnectionError:
            if attempt % 10 == 0:
                log.info("Auto-boot: waiting for FireTuner... (%ds)", attempt)
            await asyncio.sleep(1)
    else:
        log.error("Auto-boot: could not connect to FireTuner after 90s")
        heartbeat.write("error")
        return

    # 2b. Verify Lua states exist (port can open before game initialises).
    # A hung splash screen ("Loading, Please Wait...") has port open but
    # GameCore never appears. Skip this check — the main menu legitimately
    # has no GameCore on any platform; it only appears after a save is
    # loaded (step 3). The splash hang detection was causing false kills
    # when autosaves were cleaned (game stays at main menu, no GameCore).
    if conn.gamecore_index is None and False:  # disabled — see comment above
        log.warning(
            "Auto-boot: FireTuner connected but GameCore not found "
            "— game may be hung at splash screen"
        )
        for retry in range(30):
            await asyncio.sleep(2)
            try:
                await conn.reconnect()
                if conn.gamecore_index is not None:
                    log.info("Auto-boot: GameCore found after %ds", (retry + 1) * 2)
                    break
            except ConnectionError:
                pass
        else:
            log.error("Auto-boot: GameCore never appeared — killing hung game")
            heartbeat.write("error")
            await asyncio.to_thread(game_launcher._kill_game_sync)
            await asyncio.sleep(5)
            result = await asyncio.to_thread(game_launcher._launch_game_sync)
            log.info("Auto-boot: relaunched after hung splash: %s", result)
            for attempt in range(90):
                try:
                    await conn.connect()
                    if conn.gamecore_index is not None:
                        log.info("Auto-boot: GameCore found on relaunch")
                        break
                except ConnectionError:
                    pass
                await asyncio.sleep(1)
            if conn.gamecore_index is None:
                log.error("Auto-boot: relaunch also failed — giving up")
                heartbeat.write("error")
                return

    # 3. Load save (Lua on Windows/macOS, OCR menu nav on Linux)
    log.info("Auto-boot: loading save '%s'...", save_name)
    result = await load_game_save(conn, save_name)
    log.info("Auto-boot: load result: %s", result)
    heartbeat.write("loading")

    # 4. Wait for save to load, click through leader intro, then reconnect.
    # The CONTINUE GAME button on the leader screen has low-contrast
    # teal-on-teal text that OCR often misses — fall back to positional
    # click grid if OCR fails. Verify the click actually worked by
    # checking for Lua states (only available once in-game, not on leader
    # screen).
    log.info("Auto-boot: waiting 15s for save to load...")
    await asyncio.sleep(15)
    clicked = await asyncio.to_thread(
        lambda: game_launcher._click_text("CONTINUE", timeout=105, post_delay=1),
    )
    if clicked:
        log.info("Auto-boot: clicked CONTINUE GAME via OCR")
    else:
        log.warning("Auto-boot: OCR missed CONTINUE — using positional click grid")
        await asyncio.to_thread(game_launcher._click_continue_positional)

    # Verify the click worked — Lua states only appear once past the
    # leader screen into gameplay. Retry positional click if needed.
    await asyncio.sleep(3)
    game_ready = False
    for attempt in range(45):
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None:
                log.info("Auto-boot: game ready (GameCore=%s)", conn.gamecore_index)
                heartbeat.write("playing")  # turn unknown until first end_turn
                game_ready = True
                break
        except ConnectionError:
            pass
        # Retry positional click every 10s in case the first click missed
        if attempt > 0 and attempt % 10 == 0:
            heartbeat.write("loading")  # keep heartbeat fresh during retry
            if not clicked:
                log.info("Auto-boot: retrying positional click (attempt %d)", attempt)
                await asyncio.to_thread(game_launcher._click_continue_positional)
        await asyncio.sleep(1)
    if not game_ready:
        log.warning("Auto-boot: save may not have loaded — GameCore not found")
        heartbeat.write("error")
        return

    # 5. Verify correct save loaded. If the wrong save loaded (e.g.
    # main-menu "Continue Game" loaded a stale autosave instead of the
    # scenario save), reload the correct one via Lua — no OCR needed.
    try:
        verify = await conn.execute_read(
            "local t = Game.GetCurrentGameTurn(); "
            'print("VERIFY|" .. t); '
            'print("---END---")'
        )
        for line in verify:
            if line.startswith("VERIFY|"):
                turn = int(line.split("|")[1])
                if turn > 5:
                    log.error(
                        "Auto-boot: loaded T%d but expected T1 — wrong save! "
                        "Reloading '%s' via Lua",
                        turn,
                        save_name,
                    )
                    # Retry via Lua (Network.LoadGame) — bypasses OCR entirely
                    result = await load_game_save(conn, save_name)
                    log.info("Auto-boot: Lua reload result: %s", result)
                    await asyncio.sleep(15)
                    # Click CONTINUE again for the leader screen
                    await asyncio.to_thread(game_launcher._click_continue_positional)
                    await asyncio.sleep(5)
                    for retry in range(30):
                        try:
                            await conn.reconnect()
                            if conn.gamecore_index is not None:
                                break
                        except ConnectionError:
                            pass
                        await asyncio.sleep(1)
                    # Verify again
                    try:
                        verify2 = await conn.execute_read(
                            "local t = Game.GetCurrentGameTurn(); "
                            'print("VERIFY|" .. t); '
                            'print("---END---")'
                        )
                        for line2 in verify2:
                            if line2.startswith("VERIFY|"):
                                t2 = int(line2.split("|")[1])
                                if t2 > 5:
                                    log.error(
                                        "Auto-boot: Lua reload also loaded T%d "
                                        "— falling back to kill + OCR",
                                        t2,
                                    )
                                    await game_launcher.kill_game()
                                    r = await asyncio.to_thread(
                                        game_launcher._launch_game_sync
                                    )
                                    log.info("Auto-boot: relaunch: %s", r)
                                    r = await asyncio.to_thread(
                                        game_launcher._navigate_to_save_sync,
                                        save_name,
                                        None,
                                    )
                                    log.info("Auto-boot: OCR nav: %s", r)
                                    for a in range(30):
                                        try:
                                            await conn.reconnect()
                                            if conn.gamecore_index is not None:
                                                return
                                        except ConnectionError:
                                            pass
                                        await asyncio.sleep(1)
                                    log.warning("Auto-boot: all fallbacks failed")
                                    return
                                log.info("Auto-boot: Lua reload verified at T%d", t2)
                                heartbeat.write("playing", turn=t2)
                    except Exception:
                        log.debug("Auto-boot: post-reload verify failed", exc_info=True)
                    return
                log.info("Auto-boot: verified save at T%d", turn)
                heartbeat.write("playing", turn=turn)
    except Exception:
        log.debug("Auto-boot: save verification failed", exc_info=True)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    conn = GameConnection()

    # Telemetry emitter — routes events to local JSONL + optional cloud sink
    emitter = TelemetryEmitter()
    emitter.add_sink(LocalSink())
    cloud_bucket = os.environ.get("CIV_MCP_TELEMETRY_BUCKET")
    if cloud_bucket:
        emitter.add_sink(CloudSink(cloud_bucket))
    alert_webhook = os.environ.get("CIV_MCP_ALERT_WEBHOOK")
    if alert_webhook:
        emitter.add_sink(AlertSink(alert_webhook))
    emitter.start()
    heartbeat.init(emitter.run_id)
    # Bind eval identity so the orchestrator can match running games to jobs
    eval_model = os.environ.get("CIV_MCP_AGENT_MODEL", "")
    eval_metadata = os.environ.get("CIV_MCP_METADATA", "")
    eval_scenario = ""
    if eval_metadata:
        try:
            eval_scenario = json.loads(eval_metadata).get("scenario_id", "")
        except Exception:
            pass
    heartbeat.bind_eval(eval_model, eval_scenario)
    heartbeat.write("starting")

    logger = GameLogger(emitter)
    spatial = SpatialTracker(emitter)
    map_capture = MapCapture(emitter)
    gs = GameState(conn)
    log.info("Game logger session: %s", logger.session_id)

    # Auto-boot: launch game + load save when running as eval
    save_file = os.environ.get("CIV_MCP_SAVE_FILE")
    if save_file:
        await _auto_boot(conn, save_file)

    # Spectator-mode background services (camera tracking + popup auto-dismiss)
    camera = CameraController(conn)
    popup_watcher = PopupWatcher(conn)
    watchdog = GameOverWatchdog(gs, logger)
    camera.start()
    popup_watcher.start()
    watchdog.start()

    # Web dashboard disabled to prevent Uvicorn worker spawning system Python
    # web_app = create_app(gs)
    # uvi_config = uvicorn.Config(web_app, host="0.0.0.0", port=8001, log_level="info")
    # uvi_server = uvicorn.Server(uvi_config)
    # api_task = asyncio.create_task(uvi_server.serve())
    log.info("Web API disabled (Uvicorn causes double-process on Windows)")

    try:
        yield AppContext(
            game=gs,
            logger=logger,
            camera=camera,
            popup_watcher=popup_watcher,
            spatial=spatial,
            map_capture=map_capture,
            watchdog=watchdog,
        )
    finally:
        # 2026-08-04 实机修复：web API 已禁用，但 finally 仍引用已删除的
        # uvi_server / api_task —— 每次 server 退出都抛 NameError，导致
        # conn.disconnect() 不执行、FireTuner 连接不释放、进程残留堆积
        # （Codex app 每次会话 spawn 的 civ_mcp 因此越积越多，互相抢
        # 4318 端口）。已删除这两个幽灵引用，确保连接正常释放。
        # Every cleanup runs even when an earlier service fails during shutdown.
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(conn.disconnect)
            cleanup.push_async_callback(popup_watcher.stop)
            cleanup.push_async_callback(camera.stop)
            cleanup.push_async_callback(watchdog.stop)
            cleanup.push_async_callback(emitter.close)


mcp = FastMCP(
    "Civilization VI",
    instructions="Read game state and issue commands to a running Civ 6 game. Call get_game_overview first to orient yourself.",
    lifespan=lifespan,
)


def _get_game(ctx: Context) -> GameState:
    return ctx.request_context.lifespan_context.game


def _get_logger(ctx: Context) -> GameLogger:
    return ctx.request_context.lifespan_context.logger


def _get_camera(ctx: Context) -> CameraController:
    return ctx.request_context.lifespan_context.camera


def _get_spatial(ctx: Context) -> SpatialTracker:
    return ctx.request_context.lifespan_context.spatial


def _get_map_capture(ctx: Context) -> MapCapture:
    return ctx.request_context.lifespan_context.map_capture


def _get_watchdog(ctx: Context) -> GameOverWatchdog:
    return ctx.request_context.lifespan_context.watchdog


def _param_summary(params: dict[str, Any]) -> str:
    """Compact one-line summary of tool params for console logging."""
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return " ".join(parts)


def _result_summary(result: str) -> str:
    """First meaningful line of a result, truncated."""
    line = result.split("\n", 1)[0].strip()
    return line[:120] + "..." if len(line) > 120 else line


async def _logged(
    ctx: Context,
    tool_name: str,
    params: dict[str, Any],
    fn: Callable[[], Awaitable[str]],
    *,
    tiles: set[tuple[int, int]] | None = None,
) -> str:
    """Run a tool function with timing, error handling, and logging."""
    logger = _get_logger(ctx)
    turn = logger._turn or "?"
    # F1:force 越权自动落盘。挂在这里而不是各工具里 —— 六个带 force 的
    # 工具共用这一个入口,将来新增带 force 的工具也自动被覆盖(这正是
    # "同一概念两套实现"那类结构病的解法:收敛到单一位置)。
    if params.get("force"):
        try:
            from civ_mcp import force_log

            force_log.record(
                tool_name,
                params,
                reason=str(params.get("force_reason", "") or ""),
                turn=logger._turn,
            )
        except Exception:
            log.debug("force 记录失败(不影响操作)", exc_info=True)
    start = time.monotonic()
    try:
        result = await fn()
    except (LuaError, ValueError) as e:
        result = f"Error: {e}"
        ms = int((time.monotonic() - start) * 1000)
        log.info(
            "[T%s] %s(%s) ERR %dms: %s",
            turn,
            tool_name,
            _param_summary(params),
            ms,
            _result_summary(result),
        )
        await logger.log_error(tool_name, result)
        return result
    except ConnectionBusyError as e:
        # Another client owns a healthy game. Never trigger crash recovery.
        result = str(e)
        await logger.log_error(tool_name, result)
        return result
    except ConnectionError as e:
        result = str(e)
        ms = int((time.monotonic() - start) * 1000)
        log.info(
            "[T%s] %s(%s) ERR %dms: %s",
            turn,
            tool_name,
            _param_summary(params),
            ms,
            _result_summary(result),
        )
        await logger.log_error(tool_name, result)

        # Connection-loss recovery: after consecutive failures,
        # the game has likely crashed. Auto-restart from autosave.
        _logged._conn_errors = getattr(_logged, "_conn_errors", 0) + 1
        if _logged._conn_errors >= 5 and not await asyncio.to_thread(
            game_launcher.is_game_running
        ):
            log.error(
                "CONNECTION RECOVERY: %d consecutive connection failures "
                "and game process is absent — triggering restart_and_load",
                _logged._conn_errors,
            )
            _logged._conn_errors = 0
            try:
                from civ_mcp.autosave import get_autosave_for_turn, get_latest_autosave

                turn_num = logger._turn
                save = (
                    get_autosave_for_turn(int(turn_num))
                    if turn_num
                    else get_latest_autosave()
                )
                restart_result = await game_launcher.restart_and_load(save)
                log.info("CONNECTION RECOVERY: %s", restart_result)
                gs = _get_game(ctx)
                for rc_attempt in range(30):
                    try:
                        await gs.conn.reconnect()
                        if gs.conn.gamecore_index is not None:
                            log.info("CONNECTION RECOVERY: reconnected")
                            break
                    except ConnectionError:
                        pass
                    await asyncio.sleep(1)
            except Exception:
                log.error("CONNECTION RECOVERY: restart failed", exc_info=True)

        return result
    # Success — reset connection error counter + refresh heartbeat
    _logged._conn_errors = 0
    heartbeat.write("playing", turn=turn or 0)
    ms = int((time.monotonic() - start) * 1000)
    log.info(
        "[T%s] %s(%s) OK %dms: %s",
        turn,
        tool_name,
        _param_summary(params),
        ms,
        _result_summary(result),
    )
    await logger.log_tool_call(tool_name, params, result, ms)
    try:
        await _get_spatial(ctx).record(tool_name, params, result, ms, tiles=tiles)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Query tools (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_game_overview(ctx: Context) -> str:
    """Get a high-level summary of the current game state.

    Returns turn number, civilization, yields (gold/science/culture/faith),
    current research and civic, and counts of cities and units.
    Call this first to orient yourself.
    """
    gs = _get_game(ctx)

    async def _run():
        ov = await gs.get_game_overview()
        logger = _get_logger(ctx)
        logger.set_turn(ov.turn)
        spatial = _get_spatial(ctx)
        spatial.set_turn(ov.turn)
        try:
            civ, seed = await gs.get_game_identity()
            logger.bind_game(civ, seed)
            spatial.bind_game(civ, seed)
            heartbeat.bind_game(civ, seed)
            gs.spatial = spatial
        except Exception:
            pass
        # Seed revealed tiles for visibility diff (once per session)
        if not spatial._revealed_seeded:
            try:
                seed_lines = await gs.conn.execute_read(
                    lq.build_revealed_tiles_seed_query()
                )
                seed_tiles = lq.parse_revealed_tiles_seed(seed_lines)
                spatial.seed_revealed(seed_tiles)
                log.info(
                    "Seeded spatial tracker with %d revealed tiles", len(seed_tiles)
                )
            except Exception:
                # 曾经是 debug:NameError 被这里吞了 40+ 局,可见度差分从来没被播种过。
                log.warning("Failed to seed revealed tiles", exc_info=True)
        text = nr.narrate_overview(ov)
        # 2026-08-04 P0-3:金币审计。T124→T131 缺 265g、T182→T183 缺 462g
        # 两次都查不到去向 —— overview 是每回合必读的锚点,在这里核对
        # 大额差值并归因(购买/交易/升级),让经济异常不再静默。
        try:
            from civ_mcp import gold_audit

            audit_text = gold_audit.check_gold(civ, seed)
            if audit_text:
                text += "\n" + audit_text
        except Exception:
            log.debug("Gold audit failed", exc_info=True)
        # Check for game-over state
        gameover = await gs.check_game_over()
        if gameover is not None:
            vtype = (
                gameover.victory_type.replace("VICTORY_", "").replace("_", " ").title()
            )
            if gameover.is_defeat:
                text += (
                    f"\n\n*** GAME OVER — DEFEAT ***\n"
                    f"{gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory.\n"
                    f"No further actions are possible."
                )
            else:
                text += f"\n\n*** GAME OVER — VICTORY ***\nYou won a {vtype} victory!"
            try:
                await logger.log_game_over(
                    is_defeat=gameover.is_defeat,
                    winner_civ=gameover.winner_name,
                    winner_leader=gameover.winner_leader,
                    victory_type=vtype,
                    player_alive=gameover.player_alive,
                )
            except Exception:
                log.warning("Failed to log game-over in overview", exc_info=True)
        return text

    return await _logged(ctx, "get_game_overview", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_units(ctx: Context) -> str:
    """List all your units with position, type, movement, and health.

    Each unit shows its id and idx (needed for action commands).
    Consumed units (e.g. settlers that founded cities) are excluded.
    """
    gs = _get_game(ctx)
    unit_tiles: set[tuple[int, int]] = set()

    async def _run():
        units = await gs.get_units()
        unit_tiles.update((u.x, u.y) for u in units if u.x >= 0)
        try:
            threats = await gs.get_threat_scan()
        except Exception:
            threats = None
        trade_status = None
        try:
            trade_status = await gs.get_trade_routes()
        except Exception:
            pass
        body = nr.narrate_units(units, threats, trade_status)
        # 挡路的东西顶格放。排在 16 个单位后面等于没报
        # (2026-08-07 用户实机:交易弹窗识别不到,后续动作全部静默失效)。
        blocker = getattr(gs, "_blocking_sessions", "") or ""
        return f"{blocker}\n\n{body}" if blocker else body

    return await _logged(ctx, "get_units", {}, _run, tiles=unit_tiles)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_spies(ctx: Context) -> str:
    """List all your spy units with position, rank, city, and available missions.

    Shows each spy's composite id (needed for spy_action), current location,
    rank (Recruit/Agent/Special Agent/Senior Agent), XP, and which operations
    are available at their current position.

    Note: offensive missions only become available once the spy has physically
    arrived in the target city. Use spy_action with action='travel' first.
    """
    gs = _get_game(ctx)

    async def _run():
        spies = await gs.get_spies()
        return nr.narrate_spies(spies)

    return await _logged(ctx, "get_spies", {}, _run)


@mcp.tool()
async def spy_action(
    ctx: Context,
    unit_id: int,
    action: str,
    target_x: int,
    target_y: int,
) -> str:
    """Send a spy to a city or launch a spy mission.

    Args:
        unit_id: The spy's composite ID (from get_spies output)
        action: 'travel' to move spy to a city, or a mission type to launch a mission.
            Mission types: COUNTERSPY, GAIN_SOURCES, SIPHON_FUNDS, STEAL_TECH_BOOST,
            SABOTAGE_PRODUCTION, GREAT_WORK_HEIST, RECRUIT_PARTISANS,
            NEUTRALIZE_GOVERNOR, FABRICATE_SCANDAL
        target_x: X coordinate of the target city tile
        target_y: Y coordinate of the target city tile

    Travel notes:
        - Valid targets: your own cities and city-states only.
        - Allied civ cities are NOT valid travel targets.
        - Travel is queued end-of-turn; spy position updates after turn ends.

    Mission notes:
        - Spy must be physically IN the target city to launch any offensive mission.
        - Use 'travel' first, then end the turn, then launch the mission.
        - COUNTERSPY defends your own city (spy must be in your city).
        - get_spies shows which ops are available at the spy's current location.
    """
    gs = _get_game(ctx)
    params = {
        "unit_id": unit_id,
        "action": action,
        "target_x": target_x,
        "target_y": target_y,
    }

    async def _run():
        if action.lower() == "travel":
            return await gs.spy_travel(unit_id, target_x, target_y)
        return await gs.spy_mission(unit_id, action.upper(), target_x, target_y)

    result = await _logged(ctx, "spy_action", params, _run)
    _get_camera(ctx).push(target_x, target_y, f"spy {action}")
    return result


@mcp.tool(annotations={"readOnlyHint": True})
async def get_cities(ctx: Context) -> str:
    """List all your cities with yields, population, production, growth, and loyalty.

    Each city shows its id (needed for production commands).
    Cities losing loyalty show warnings with flip timers.
    """
    gs = _get_game(ctx)

    async def _run():
        cities, distances = await gs.get_cities()
        return nr.narrate_cities(cities, distances)

    return await _logged(ctx, "get_cities", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_city_production(ctx: Context, city_id: int) -> str:
    """List what a city can produce right now.

    Args:
        city_id: City ID (from get_cities output)

    Returns available units, buildings, and districts with production costs.
    Call this when a city finishes building or to decide what to produce next.
    """
    gs = _get_game(ctx)

    async def _run():
        options = await gs.list_city_production(city_id)
        return nr.narrate_city_production(options)

    return await _logged(ctx, "get_city_production", {"city_id": city_id}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_map_area(
    ctx: Context, center_x: int, center_y: int, radius: int = 2
) -> str:
    """Get terrain info for tiles around a point.

    Args:
        center_x: X coordinate of center tile
        center_y: Y coordinate of center tile
        radius: How many tiles out from center (default 2, max 4)
    """
    radius = min(radius, 4)
    gs = _get_game(ctx)
    tile_coords: set[tuple[int, int]] = set()

    async def _run():
        tiles = await gs.get_map_area(center_x, center_y, radius)
        tile_coords.update((t.x, t.y) for t in tiles)
        return nr.narrate_map(tiles)

    result = await _logged(
        ctx,
        "get_map_area",
        {"center_x": center_x, "center_y": center_y, "radius": radius},
        _run,
        tiles=tile_coords,
    )
    _get_camera(ctx).push(center_x, center_y, f"map_area ({center_x},{center_y})")
    return result


@mcp.tool(annotations={"readOnlyHint": True})
async def get_settle_advisor(ctx: Context, unit_id: int) -> str:
    """List best settle locations near a settler unit.

    Args:
        unit_id: The settler's composite ID (from get_units output)

    Scores locations by yields, water, defense, and resource value.
    Returns top 5 candidates sorted by score.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_settle_advisor",
        {"unit_id": unit_id},
        lambda: gs.get_settle_advisor(unit_id),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pathing_estimate(
    ctx: Context, unit_id: int, target_x: int, target_y: int
) -> str:
    """Estimate how many turns a unit needs to reach a destination.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        target_x: Destination X coordinate
        target_y: Destination Y coordinate

    Returns estimated turns, path length, and reachable tiles this turn.
    """
    gs = _get_game(ctx)

    async def _run():
        est = await gs.get_pathing_estimate(unit_id, target_x, target_y)
        return nr.narrate_pathing_estimate(est)

    return await _logged(
        ctx,
        "get_pathing_estimate",
        {"unit_id": unit_id, "target_x": target_x, "target_y": target_y},
        _run,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_global_settle_advisor(ctx: Context) -> str:
    """Find the best settle locations across the entire revealed map.

    Unlike get_settle_advisor (which searches near a specific settler),
    this scans all revealed land for the top 10 settle candidates.
    Use this when deciding WHERE to send a settler, not just where to settle.
    """
    gs = _get_game(ctx)

    async def _run():
        candidates = await gs.get_global_settle_scan()
        if not candidates:
            return "No valid settle locations found on revealed map."
        return nr.narrate_settle_candidates(candidates)

    return await _logged(ctx, "get_global_settle_advisor", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_builder_tasks(ctx: Context) -> str:
    """Get a prioritized task board for all your builders.

    Scans your territory for tiles needing improvements and matches them
    with idle builders. Like the builder lens in the UI — shows what to
    build where and which builder is closest.

    Priority tiers:
    - URGENT: Pillaged improvements (yield loss), unimproved strategic resources
    - HIGH: Unimproved luxury/bonus resources
    - NORMAL: Empty tiles that could benefit from farms/mines/lumber mills

    Call this before issuing builder orders each turn.
    """
    gs = _get_game(ctx)

    async def _run():
        tasks, builders = await gs.get_builder_tasks()
        return nr.narrate_builder_tasks(tasks, builders)

    return await _logged(ctx, "get_builder_tasks", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_empire_resources(ctx: Context) -> str:
    """Get a summary of all resources in and near your empire.

    Shows owned resources (improved/unimproved) grouped by type,
    and unclaimed resources near your cities.
    """
    gs = _get_game(ctx)

    async def _run():
        stockpiles, owned, nearby, luxuries = await gs.get_empire_resources()
        return nr.narrate_empire_resources(stockpiles, owned, nearby, luxuries)

    return await _logged(ctx, "get_empire_resources", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_strategic_map(ctx: Context) -> str:
    """Get fog-of-war boundaries and unclaimed resources across the map.

    Shows how far explored territory extends from each city (in 6 directions),
    highlighting directions that need exploration. Also lists unclaimed luxury
    and strategic resources on revealed but unowned land.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_strategic_map",
        {},
        lambda: _narrate(gs.get_strategic_map, nr.narrate_strategic_map),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_diplomacy(ctx: Context) -> str:
    """Get diplomatic status with all known civilizations.

    Shows diplomatic state (Friendly/Neutral/Unfriendly), relationship modifiers
    with scores and reasons, grievances, delegations/embassies, and available
    diplomatic actions you can take. Also shows visible enemy city details
    (name, population, loyalty, walls).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy, nr.narrate_diplomacy),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_tech_civics(ctx: Context) -> str:
    """Get technology and civic research status.

    Shows current research, current civic, turns remaining,
    and lists of available technologies and civics to choose from.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_tech_civics",
        {},
        lambda: _narrate(gs.get_tech_civics, nr.narrate_tech_civics),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_trades(ctx: Context) -> str:
    """Check for pending trade deal offers from other civilizations.

    Shows what each civ is offering and what they want in return.
    Use respond_to_trade to accept or reject.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_pending_trades",
        {},
        lambda: _narrate(gs.get_pending_deals, nr.narrate_pending_deals),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_policies(ctx: Context) -> str:
    """Get current government, policy slots, and available policies.

    Shows current government type, each policy slot with its type and current
    policy (if any), and all unlocked policies grouped by compatible slot type.
    Wildcard slots accept any policy type.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "get_policies", {}, lambda: _narrate(gs.get_policies, nr.narrate_policies)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_notifications(ctx: Context) -> str:
    """Get all active game notifications.

    Shows action-required items (need your decision) and informational
    notifications. Action-required items include which MCP tool to use
    to resolve them. Call this to check what needs attention without
    ending the turn.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_notifications",
        {},
        lambda: _narrate(gs.get_notifications, nr.narrate_notifications),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_diplomacy(ctx: Context) -> str:
    """Check for pending diplomacy encounters (e.g. first meeting with a civ).

    Diplomacy encounters block turn progression. Call this if end_turn
    reports the turn didn't advance. Returns any open sessions with their
    dialogue text, visible buttons, and response guidance.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_pending_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy_sessions, nr.narrate_diplomacy_sessions),
    )


# ---------------------------------------------------------------------------
# Action tools (mutating)
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_governors(ctx: Context) -> str:
    """Get governor status, appointed governors, and available types.

    Shows governor points, currently appointed governors with assignments,
    and governors available to appoint. Use appoint_governor to appoint one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_governors",
        {},
        lambda: _narrate(gs.get_governors, nr.narrate_governors),
    )


@mcp.tool()
async def appoint_governor(ctx: Context, governor_type: str) -> str:
    """Appoint a new governor.

    Args:
        governor_type: e.g. GOVERNOR_THE_EDUCATOR (Pingala), GOVERNOR_THE_DEFENDER (Victor)

    Requires available governor points. Use get_governors to see options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "appoint_governor",
        {"governor_type": governor_type},
        lambda: gs.appoint_governor(governor_type),
    )


@mcp.tool()
async def assign_governor(ctx: Context, governor_type: str, city_id: int) -> str:
    """Assign an appointed governor to a city.

    Args:
        governor_type: The governor type (from get_governors output)
        city_id: The city ID (from get_cities output)

    Governor must already be appointed. Takes several turns to establish.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "assign_governor",
        {"governor_type": governor_type, "city_id": city_id},
        lambda: gs.assign_governor(governor_type, city_id),
    )


@mcp.tool()
async def promote_governor(
    ctx: Context, governor_type: str, promotion_type: str
) -> str:
    """Promote a governor with a new ability.

    Args:
        governor_type: The governor type (from get_governors output)
        promotion_type: The promotion type (from get_governors output, shown under each governor)

    Requires available governor points. Use get_governors to see available promotions.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "promote_governor",
        {"governor_type": governor_type, "promotion_type": promotion_type},
        lambda: gs.promote_governor(governor_type, promotion_type),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_unit_promotions(ctx: Context, unit_id: int) -> str:
    """List available promotions for a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)

    Shows promotions filtered by the unit's promotion class.
    Only units with enough XP will have promotions available.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_unit_promotions(unit_id)
        return nr.narrate_unit_promotions(status)

    return await _logged(ctx, "get_unit_promotions", {"unit_id": unit_id}, _run)


@mcp.tool()
async def promote_unit(ctx: Context, unit_id: int, promotion_type: str) -> str:
    """Apply a promotion to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        promotion_type: e.g. PROMOTION_BATTLECRY, PROMOTION_TORTOISE

    Use get_unit_promotions first to see available options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "promote_unit",
        {"unit_id": unit_id, "promotion_type": promotion_type},
        lambda: gs.promote_unit(unit_id, promotion_type),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_city_states(ctx: Context) -> str:
    """List known city-states with envoy counts and types.

    Shows envoy tokens available, each city-state's type (Scientific,
    Industrial, etc.), how many envoys you've sent, and who is suzerain.
    Use send_envoy to send an envoy.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_city_states",
        {},
        lambda: _narrate(gs.get_city_states, nr.narrate_city_states),
    )


@mcp.tool()
async def send_envoy(ctx: Context, player_id: int) -> str:
    """Send an envoy to a city-state.

    Args:
        player_id: The city-state's player ID (from get_city_states)

    Requires available envoy tokens. Use get_city_states to see options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "send_envoy", {"player_id": player_id}, lambda: gs.send_envoy(player_id)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pantheon_beliefs(ctx: Context) -> str:
    """Get pantheon status and available beliefs for selection.

    Shows current pantheon (if any), faith balance, and all available
    pantheon beliefs with their bonuses. Use choose_pantheon to found one.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_pantheon_status()
        return nr.narrate_pantheon_status(status)

    return await _logged(ctx, "get_pantheon_beliefs", {}, _run)


@mcp.tool()
async def choose_pantheon(ctx: Context, belief_type: str) -> str:
    """Found a pantheon with the specified belief.

    Args:
        belief_type: e.g. BELIEF_GOD_OF_THE_FORGE, BELIEF_DIVINE_SPARK

    Use get_pantheon_beliefs first to see options. Requires enough faith
    and no existing pantheon.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "choose_pantheon",
        {"belief_type": belief_type},
        lambda: gs.choose_pantheon(belief_type),
    )


@mcp.tool()
async def get_religion_beliefs(ctx: Context) -> str:
    """Get religion founding status, available religions, and available beliefs.

    Shows whether you've founded a religion, available religion types to choose,
    and beliefs grouped by class (Follower, Founder, Enhancer, Worship).
    Use found_religion to found a religion after your Great Prophet activates.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_religion_founding_status()
        return nr.narrate_religion_founding_status(status)

    return await _logged(ctx, "get_religion_beliefs", {}, _run)


@mcp.tool()
async def found_religion(
    ctx: Context, religion_type: str, follower_belief: str, founder_belief: str
) -> str:
    """Found a religion with a chosen name, follower belief, and founder belief.

    Args:
        religion_type: e.g. RELIGION_HINDUISM, RELIGION_BUDDHISM, RELIGION_ISLAM
        follower_belief: e.g. BELIEF_WORK_ETHIC, BELIEF_CHORAL_MUSIC
        founder_belief: e.g. BELIEF_STEWARDSHIP, BELIEF_CHURCH_PROPERTY

    Requires your Great Prophet to have already activated on a Holy Site
    (via UNITOPERATION_FOUND_RELIGION). Use get_religion_beliefs
    first to see available options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "found_religion",
        {
            "religion_type": religion_type,
            "follower_belief": follower_belief,
            "founder_belief": founder_belief,
        },
        lambda: gs.found_religion(religion_type, follower_belief, founder_belief),
    )


@mcp.tool()
async def upgrade_unit(ctx: Context, unit_id: int) -> str:
    """Upgrade a unit to its next type (e.g. Slinger -> Archer).

    Args:
        unit_id: The unit's composite ID (from get_units output)

    Requires the right technology, enough gold, and the unit must have
    moves remaining. The unit's movement is consumed by upgrading.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "upgrade_unit", {"unit_id": unit_id}, lambda: gs.upgrade_unit(unit_id)
    )


@mcp.tool()
async def get_dedications(ctx: Context) -> str:
    """Get current era age, available dedications, and active ones.

    Shows era score thresholds, whether you're in a Golden/Dark/Normal age,
    and lists available dedication choices with their bonuses.
    Use choose_dedication to select one when required.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_dedications()
        return nr.narrate_dedications(status)

    return await _logged(ctx, "get_dedications", {}, _run)


@mcp.tool()
async def choose_dedication(ctx: Context, dedication_index: int) -> str:
    """Choose a dedication/commemoration for the current era.

    Args:
        dedication_index: The index of the dedication (from get_dedications output)

    Use get_dedications first to see available options and their bonuses.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "choose_dedication",
        {"dedication_index": dedication_index},
        lambda: gs.choose_dedication(dedication_index),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_options(ctx: Context, other_player_id: int) -> str:
    """See what both sides can trade — like opening the trade screen.

    Args:
        other_player_id: The player ID (from get_diplomacy output)

    Shows gold, resources, favor, open borders status, and alliance eligibility
    for both you and the other civilization. Use before propose_trade to see
    what's available.
    """
    gs = _get_game(ctx)

    async def _run():
        opts = await gs.get_deal_options(other_player_id)
        text = nr.narrate_deal_options(opts)
        # 资源交易分析(审查 F2-4 接线):RivalHoldings 的设计数据源就是
        # 本工具的 DealOptions。
        #
        # 2026-08-11 T286 订正:原注释说"我方奢侈份数在这里未知,全记 1 份,
        # 盈余段自然静默" —— 份数其实【就印在标签里】("柑橘 x3"),只是没人
        # 去读。结果是本模块收益最高的那一段(卖掉重复奢侈,第 2 份起对宜居度
        # 零贡献)从来没有过数据、一次也没出过声。现在按标签解析真实份数。
        try:
            from civ_mcp import resource_trade as rt

            rt_ctx = rt.ResourceTradeContext(
                my_luxuries={
                    (rt.normalize(x) or x): rt.parse_count(x)
                    for x in opts.our_luxuries or []
                },
                rivals=[
                    rt.RivalHoldings(
                        player_id=opts.other_player_id,
                        civ_name=opts.other_civ_name,
                        luxuries=tuple(opts.their_luxuries or ()),
                    )
                ],
            )
            advice_lines = rt.get_resource_trade_advice(rt_ctx)
            if advice_lines:
                text += "\n" + "\n".join(advice_lines)
        except Exception:
            log.debug("资源交易分析失败", exc_info=True)
        return text

    return await _logged(
        ctx, "get_trade_options", {"other_player_id": other_player_id}, _run
    )


@mcp.tool()
async def respond_to_trade(
    ctx: Context,
    other_player_id: int,
    accept: bool,
    force: bool = False,
    force_reason: str = "",
) -> str:
    """Accept or reject a pending trade deal.

    Args:
        other_player_id: The player ID of the civilization (from get_pending_trades)
        accept: True to accept the deal, False to reject it
        force: Override the pre-war gate when accepting a joint-war invitation
        force_reason: One sentence on WHY you are overriding. Recorded to
            data/force_log.jsonl. Without it a post-game review cannot tell
            a wrong gate from a wrong call — write it even when in a hurry.
               (NO_SIEGE_VS_WALLS — accepting joint war = declaring war on the
               target immediately)

    Use get_pending_trades first to see what's being offered.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "respond_to_trade",
        {
            "other_player_id": other_player_id,
            "accept": accept,
            "force": force,
            "force_reason": force_reason,
        },
        lambda: gs.respond_to_deal(other_player_id, accept, force=force),
    )


@mcp.tool()
async def propose_trade(
    ctx: Context,
    other_player_id: int,
    offer_gold: int = 0,
    offer_gold_per_turn: int = 0,
    offer_resources: str = "",
    offer_favor: int = 0,
    offer_open_borders: bool = False,
    request_gold: int = 0,
    request_gold_per_turn: int = 0,
    request_resources: str = "",
    request_favor: int = 0,
    request_open_borders: bool = False,
    request_cities: str = "",
    offer_cities: str = "",
    make_peace: bool = False,
    joint_war_target: int = 0,
    mode: str = "send",
    force: bool = False,
    force_reason: str = "",
) -> str:
    """Propose a trade deal to another civilization.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        offer_gold: Lump sum gold to give them
        offer_gold_per_turn: Gold per turn to give them (30-turn duration)
        offer_resources: Comma-separated resource types to offer, e.g. "RESOURCE_SILK,RESOURCE_TEA"
        offer_favor: Diplomatic favor to offer
        offer_open_borders: True to offer our open borders
        request_gold: Lump sum gold to request from them
        request_gold_per_turn: Gold per turn to request (30-turn duration)
        request_resources: Comma-separated resource types to request
        request_favor: Diplomatic favor to request from them
        request_open_borders: True to request their open borders
        request_cities: Comma-separated city IDs to demand from them (get them from
            get_diplomacy / run_lua; a losing AI will cede cities to end a war)
        offer_cities: Comma-separated city IDs of ours to hand over
        make_peace: True to include a peace treaty. Required when at war — the
            ceasefire is what you are paying with, and demanding cities for it is
            the whole point of a war-ending deal.
        joint_war_target: Player ID of a third civ to declare joint war against
        mode: "send" to commit the deal, "test" to preview AI's counter-offer without committing
        force: Override the pre-war gate for joint wars (NO_SIEGE_VS_WALLS —
        force_reason: One sentence on WHY you are overriding. Recorded to
            data/force_log.jsonl. Without it a post-game review cannot tell
            a wrong gate from a wrong call — write it even when in a hurry.
               same rule as send_diplomatic_action war declarations)

    Examples: Gift 100 gold: offer_gold=100. Trade silk for 3 gpt: offer_resources="RESOURCE_SILK", request_gold_per_turn=3.
    Mutual open borders: offer_open_borders=True, request_open_borders=True.
    Test a deal first: mode="test" to see what the AI thinks is fair, then mode="send" to commit.

    Ending a war on your terms: make_peace=True plus request_cities/request_resources.
    Always run mode="test" first — the AI's willingness to cede a city swings hard with
    war score, and the test call shows what it considers fair without committing.
    """
    gs = _get_game(ctx)

    offer_items: list[dict] = []
    request_items: list[dict] = []
    if offer_gold > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})
    if offer_gold_per_turn > 0:
        offer_items.append(
            {"type": "GOLD", "amount": offer_gold_per_turn, "duration": 30}
        )
    for res in (r.strip() for r in offer_resources.split(",") if r.strip()):
        offer_items.append(
            {"type": "RESOURCE", "name": res, "amount": 1, "duration": 30}
        )
    if offer_favor > 0:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})
    if offer_open_borders:
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if request_gold > 0:
        request_items.append({"type": "GOLD", "amount": request_gold, "duration": 0})
    if request_gold_per_turn > 0:
        request_items.append(
            {"type": "GOLD", "amount": request_gold_per_turn, "duration": 30}
        )
    for res in (r.strip() for r in request_resources.split(",") if r.strip()):
        request_items.append(
            {"type": "RESOURCE", "name": res, "amount": 1, "duration": 30}
        )
    if request_favor > 0:
        request_items.append({"type": "FAVOR", "amount": request_favor})
    if request_open_borders:
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    # 2026-08-12 缺陷114:城市条目在 Lua 侧(_lua_deal_item 的 CITY 分支)早就
    # 实现了,只是工具签名从没暴露它 —— 于是「用和解换城市」这条路整局不存在。
    # 罗马局 T456 实测:刚果/英国/巴西三家【主动】提的和约里就带 City 条目,
    # 我方收得下却提不出。劣势方割地求和是标准玩法,这条不该缺。
    for cid in (c.strip() for c in request_cities.split(",") if c.strip()):
        try:
            request_items.append({"type": "CITY", "city_id": int(cid)})
        except ValueError:
            return f"Error: request_cities 里的 '{cid}' 不是城市 ID(整数)"
    for cid in (c.strip() for c in offer_cities.split(",") if c.strip()):
        try:
            offer_items.append({"type": "CITY", "city_id": int(cid)})
        except ValueError:
            return f"Error: offer_cities 里的 '{cid}' 不是城市 ID(整数)"
    if make_peace:
        # 停战是双方条款,两侧都要挂 —— 只挂一侧引擎不认。
        offer_items.append({"type": "AGREEMENT", "subtype": "MAKE_PEACE"})
        request_items.append({"type": "AGREEMENT", "subtype": "MAKE_PEACE"})
    if joint_war_target > 0:
        # Joint war is mutual — both sides commit. target_player 必须带上:
        # 引擎靠 SetValueType(目标ID) 才知道对谁宣战(2026-08-05 修复,
        # 此前目标 ID 被静默丢弃,deal 是残缺的)。
        offer_items.append(
            {
                "type": "AGREEMENT",
                "subtype": "JOINT_WAR",
                "target_player": joint_war_target,
            }
        )
        request_items.append(
            {
                "type": "AGREEMENT",
                "subtype": "JOINT_WAR",
                "target_player": joint_war_target,
            }
        )

    if not offer_items and not request_items:
        return "Error: must specify at least one offer or request item"

    if mode == "test":
        return await _logged(
            ctx,
            "test_trade",
            {
                "other_player_id": other_player_id,
                "offer_items": offer_items,
                "request_items": request_items,
            },
            lambda: gs.test_trade(other_player_id, offer_items, request_items),
        )

    return await _logged(
        ctx,
        "propose_trade",
        {
            "other_player_id": other_player_id,
            "offer_items": offer_items,
            "request_items": request_items,
            "force": force,
            "force_reason": force_reason,
        },
        lambda: gs.propose_trade(
            other_player_id, offer_items, request_items, force=force
        ),
    )


@mcp.tool()
async def propose_peace(ctx: Context, other_player_id: int) -> str:
    """Propose white peace to a civilization you're at war with.

    Args:
        other_player_id: The player ID (from get_diplomacy output)

    Requires being at war and past the 10-turn war cooldown.
    The AI may accept or reject based on war score and relationship.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "propose_peace",
        {"other_player_id": other_player_id},
        lambda: gs.propose_peace(other_player_id),
    )


@mcp.tool()
async def set_policies(ctx: Context, assignments: str) -> str:
    """Set policy cards in government slots.

    Args:
        assignments: Comma-separated slot assignments, e.g.
            "0=POLICY_AGOGE,1=POLICY_URBAN_PLANNING"
            Slots not listed keep their current policy. Use NONE to
            explicitly clear a slot (e.g. "2=NONE"). Use get_policies to
            see available policies and slot indices.

    Wildcard slots can accept any policy type. Military slots accept
    military policies, economic slots accept economic policies, etc.
    """
    gs = _get_game(ctx)

    async def _run():
        parsed: dict[int, str] = {}
        for pair in assignments.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            idx_str, policy = pair.split("=", 1)
            parsed[int(idx_str.strip())] = policy.strip()
        if not parsed:
            return "Error: no valid assignments. Format: '0=POLICY_AGOGE,1=POLICY_URBAN_PLANNING'"
        return await gs.set_policies(parsed)

    return await _logged(ctx, "set_policies", {"assignments": assignments}, _run)


@mcp.tool()
async def respond_to_diplomacy(
    ctx: Context, other_player_id: int, response: str
) -> str:
    """Respond to a pending diplomacy encounter.

    Args:
        other_player_id: The player ID of the other civilization (from get_pending_diplomacy)
        response: "POSITIVE" (friendly) or "NEGATIVE" (dismissive)

    First meetings typically have 2-3 rounds. The tool automatically detects
    and closes goodbye-phase sessions (where dialogue text stops changing).
    If SESSION_CONTINUES is returned, send another response for the next round.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "respond_to_diplomacy",
        {"other_player_id": other_player_id, "response": response},
        lambda: gs.diplomacy_respond(other_player_id, response),
    )


@mcp.tool()
async def send_diplomatic_action(
    ctx: Context,
    other_player_id: int,
    action: str,
    force: bool = False,
    force_reason: str = "",
) -> str:
    """Send a proactive diplomatic action to another civilization.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        action: One of: DIPLOMATIC_DELEGATION, DECLARE_FRIENDSHIP, DENOUNCE,
                RESIDENT_EMBASSY, OPEN_BORDERS,
                DECLARE_SURPRISE_WAR, DECLARE_FORMAL_WAR, DECLARE_HOLY_WAR,
                DECLARE_LIBERATION_WAR, DECLARE_RECONQUEST_WAR,
                DECLARE_PROTECTORATE_WAR, DECLARE_COLONIAL_WAR,
                DECLARE_TERRITORIAL_WAR
        force: Override the pre-war gate (NO_SIEGE_VS_WALLS). War declarations
        force_reason: One sentence on WHY you are overriding. Recorded to
            data/force_log.jsonl. Without it a post-game review cannot tell
            a wrong gate from a wrong call — write it even when in a hurry.
               against a civ with visible walled cities are blocked while you
               have no wall-breaker units (siege class, battering ram/siege
               tower, or ranged strength >= 60).

    Delegations cost 25 gold and can be rejected if the civ dislikes you.
    Embassies require Writing tech. Use get_diplomacy to see available actions.
    Surprise war is always available if not allied/friends. Other war types
    (casus belli) require specific civics and conditions.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "send_diplomatic_action",
        {
            "other_player_id": other_player_id,
            "action": action,
            "force": force,
            "force_reason": force_reason,
        },
        lambda: gs.send_diplomatic_action(other_player_id, action, force=force),
    )


@mcp.tool()
async def form_alliance(
    ctx: Context, other_player_id: int, alliance_type: str = "MILITARY"
) -> str:
    """Form an alliance with another civilization.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        alliance_type: One of: MILITARY, RESEARCH, CULTURAL, ECONOMIC, RELIGIOUS

    Requires declared friendship and Diplomatic Service civic.
    Use get_trade_options to check alliance eligibility first.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "form_alliance",
        {"other_player_id": other_player_id, "alliance_type": alliance_type},
        lambda: gs.form_alliance(other_player_id, alliance_type.upper()),
    )


@mcp.tool()
async def city_action(
    ctx: Context,
    city_id: int,
    action: str,
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
) -> str:
    """Issue a command to a city.

    Args:
        city_id: City ID (from get_cities output)
        action: Currently supported: 'attack' (city ranged attack)
        target_x: Target X coordinate (required for attack)
        target_y: Target Y coordinate (required for attack)

    For attack: city must have walls and not have fired this turn.
    Range is 2 tiles from city center.

    For captured/disloyal city decisions (city_id is ignored, uses pending city):
    - 'keep': Keep the city (works for both captured and loyalty-flipped cities)
    - 'reject': Reject/free a disloyal city (loyalty flip only)
    - 'raze': Raze a captured city (military conquest only)
    - 'liberate_founder': Liberate to original founder
    - 'liberate_previous': Liberate to previous owner
    """
    gs = _get_game(ctx)
    match action:
        case "attack":
            if target_x is None or target_y is None:
                return "Error: attack requires target_x and target_y"
            result = await _logged(
                ctx,
                "city_attack",
                {"city_id": city_id, "x": target_x, "y": target_y},
                lambda: gs.city_attack(city_id, target_x, target_y),
            )
            _get_camera(ctx).push(target_x, target_y, "city attack")
            return result
        case "keep" | "reject" | "raze" | "liberate_founder" | "liberate_previous":
            return await _logged(
                ctx,
                "resolve_city_capture",
                {"action": action},
                lambda: gs.resolve_city_capture(action),
            )
        case _:
            return f"Error: Unknown city action '{action}'. Available: attack, keep, reject, raze, liberate_founder, liberate_previous"


@mcp.tool()
async def unit_action(
    ctx: Context,
    unit_id: int,
    action: str,
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
    improvement: Optional[str] = None,
    force: bool = False,
    force_reason: str = "",
) -> str:
    """Issue a command to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        force: Override a safety block. Only set this after reading the
            block's explanation and deciding the risk is worth it — the
            safety checks exist because these mistakes cost whole games
            (civilians walking into barbarians, ranged units moving before
            they shoot). Leave it False by default.
        force_reason: One sentence on WHY you are overriding. Recorded to
            data/force_log.jsonl. Without it a post-game review cannot tell
            a wrong gate from a wrong call — write it even when in a hurry.
        action: One of: move, attack, fortify, skip, found_city, improve, repair, remove_improvement, remove_feature, build_route, automate, heal, alert, sleep, delete, trade_route, activate, sacrifice_charges, hurry_production, hurry_wonder, hurry_district, teleport, spread_religion, excavate, form_corps, form_army, rebase
        target_x: Target X coordinate (required for move/attack/trade_route/teleport/rebase)
        target_y: Target Y coordinate (required for move/attack/trade_route/teleport/rebase)
        improvement: Improvement type for builders (required for improve), e.g.
            IMPROVEMENT_FARM, IMPROVEMENT_MINE, IMPROVEMENT_QUARRY,
            IMPROVEMENT_PLANTATION, IMPROVEMENT_CAMP, IMPROVEMENT_PASTURE,
            IMPROVEMENT_FISHING_BOATS, IMPROVEMENT_LUMBER_MILL

    For move/attack: provide target_x and target_y.
    For trade_route: provide target_x and target_y of destination city.
    For teleport: provide target_x and target_y of destination city. Traders only, must be idle (not on active route).
    For rebase: provide target_x/target_y of a friendly aerodrome or carrier. Air units only —
        they never use move (they don't walk the map), and anything past their 15-tile strike
        range can only be reached by rebasing. Long hops may be refused; go via an intermediate
        aerodrome (measured: 44 tiles rejected, 20 accepted).
    For improve: provide improvement name. Builder must be on the tile.
    For repair: repairs a pillaged improvement on the builder's current tile. No improvement name needed.
    For remove_improvement: demolishes an intact improvement on the builder's current tile (e.g. to replace a farm with a mine). Costs one charge.
    For activate: activates a Great Person on their matching district.
    For sacrifice_charges: Royal Society builder sacrifice — spends ALL builder charges to boost a district project (2% of cost per charge). Builder must be on the district tile. Expansion-only; absent in RULESET_STANDARD.
    For hurry_production: spends builder charges to hurry construction on the builder's current tile, auto-detecting which variant this civ has. hurry_wonder forces the wonder variant (Qin Shi Huang / FIRST_EMPEROR_TRAIT, early eras only), hurry_district forces the district variant (Aztec). Move the builder onto the in-progress wonder/district tile first. If your leader lacks the ability the error says so explicitly — it is a ruleset limit, not a tool failure.
    For form_corps / form_army: merges the SAME-TYPE unit at target_x/target_y into this
        one. Corps (+10 combat strength) needs Nationalism; Army (+17) needs Mobilization —
        these are the two free power spikes on the civics tree, and the same standing army
        gains roughly an era's worth of strength by merging. Both units must be the same
        unit type; move them together first.
    For spread_religion: spreads religion at current tile. Missionaries/Apostles only.
    For build_route: builds road/railroad on current tile. Military Engineers only. No charges used; costs 1 Iron + 1 Coal per railroad tile.
    For fortify/skip/found_city/automate/heal/alert/sleep/delete: no target needed.
    heal = fortify until healed (auto-wake at full HP).
    alert = sleep but auto-wake when enemy enters sight range.
    delete = permanently disband the unit.
    """
    gs = _get_game(ctx)
    params: dict[str, Any] = {"unit_id": unit_id, "action": action}
    if target_x is not None:
        params["target_x"] = target_x
    if target_y is not None:
        params["target_y"] = target_y
    if improvement:
        params["improvement"] = improvement
    if force:
        # 记录进遥测:force 使用率是判断拦截是否过严的直接指标(方案 §13.2)
        params["force"] = True
        params["force_reason"] = force_reason

    async def _run():
        match action.lower():
            case "move":
                if target_x is None or target_y is None:
                    return "Error: move requires target_x and target_y"
                return await gs.move_unit(unit_id, target_x, target_y, force=force)
            case "attack":
                if target_x is None or target_y is None:
                    return "Error: attack requires target_x and target_y"
                return await gs.attack_unit(unit_id, target_x, target_y, force=force)
            case "fortify":
                return await gs.fortify_unit(unit_id)
            case "skip":
                return await gs.skip_unit(unit_id)
            case "found_city":
                return await gs.found_city(unit_id)
            case "improve":
                if not improvement:
                    return "Error: improve requires improvement name (e.g. IMPROVEMENT_FARM). To repair a pillaged improvement, use action='repair' instead."
                return await gs.improve_tile(unit_id, improvement)
            case "repair":
                return await gs.repair_improvement(unit_id)
            case "remove_improvement":
                return await gs.remove_improvement(unit_id)
            case "remove_feature":
                return await gs.remove_feature(unit_id)
            case "build_route":
                return await gs.build_route(unit_id)
            case "automate":
                return await gs.automate_explore(unit_id)
            case "heal":
                return await gs.heal_unit(unit_id)
            case "alert":
                return await gs.alert_unit(unit_id)
            case "sleep":
                return await gs.sleep_unit(unit_id)
            case "delete":
                return await gs.delete_unit(unit_id)
            case "trade_route":
                if target_x is None or target_y is None:
                    return "Error: trade_route requires target_x and target_y of destination city"
                return await gs.make_trade_route(unit_id, target_x, target_y)
            case "activate":
                return await gs.activate_great_person(unit_id)
            case "sacrifice_charges":
                return await gs.sacrifice_builder_charges(unit_id)
            case "hurry_production":
                return await gs.hurry_production(unit_id)
            case "hurry_wonder":
                return await gs.hurry_production(unit_id, "wonder")
            case "hurry_district":
                return await gs.hurry_production(unit_id, "district")
            case "spread_religion":
                return await gs.spread_religion(unit_id)
            case "teleport":
                if target_x is None or target_y is None:
                    return "Error: teleport requires target_x and target_y of the destination city"
                return await gs.teleport_to_city(unit_id, target_x, target_y)
            case "excavate":
                # 2026-08-09 T304 实机:考古学家的核心操作没有入口,只能
                # run_lua 手发 UNITOPERATION_EXCAVATE。工具化补齐。
                return await gs.excavate(unit_id)
            case "rebase":
                # 2026-08-12 缺陷113 的另一半。空军射程 15 格,超出【只能转场】,
                # 而这个入口此前不存在 —— 罗马局 T455-458 四次转场全靠 run_lua
                # 手发 UNITOPERATION_REBASE。没有它,空军打不到半张地图外的城,
                # 而空军的全部价值恰恰是机动。
                if target_x is None or target_y is None:
                    return (
                        "Error: rebase requires target_x/target_y — 目标【航空港或"
                        "航母】所在格。空军不能用 move(它不在地图上逐格走),"
                        "超出 15 格射程的目标只能先转场过去。"
                    )
                return await gs.rebase_unit(unit_id, target_x, target_y)
            case "form_corps" | "form_army":
                # 2026-08-12 罗马 T384:23 个军事单位全程未编队,因为整个
                # 工具层没有入口。军团 +10/大军 +17 战力,是市政树白送的
                # 力量尖峰。
                if target_x is None or target_y is None:
                    return (
                        f"Error: {action} requires target_x/target_y — 要合编进来的"
                        f"【同类型】单位所在的格子。合编是两个相同兵种合体,"
                        f"先把另一个同型单位移到相邻或同格位置。"
                    )
                return await gs.form_unit(
                    unit_id, target_x, target_y, army=(action.lower() == "form_army")
                )
            case _:
                return f"Error: Unknown action '{action}'. Valid: move, attack, fortify, skip, found_city, improve, repair, remove_improvement, remove_feature, build_route, automate, heal, alert, sleep, delete, trade_route, activate, sacrifice_charges, hurry_production, hurry_wonder, hurry_district, teleport, spread_religion, excavate, form_corps, form_army, rebase"

    result = await _logged(ctx, "unit_action", params, _run)
    if (
        action.lower() in ("move", "attack", "trade_route", "teleport")
        and target_x is not None
        and target_y is not None
    ):
        _get_camera(ctx).push(target_x, target_y, f"{action}→({target_x},{target_y})")
    return result


@mcp.tool()
async def fill_idle_production(ctx: Context) -> str:
    """Give every idle city something to build, in one call.

    Walks all your cities, finds the ones producing nothing, and starts the first
    basic building each one can actually make (monument → granary → water mill →
    sewer → builder as a fallback).

    Why this exists: idle production is not banked, it evaporates that turn. At 20
    cities that is an annoyance; at 90+ the end_turn gate names a dozen idle cities
    per turn and the per-city set_city_production tool would need a dozen round
    trips to clear it. The choice itself carries no strategic content — cities that
    need a real decision (spaceport city, front-line production) are never idle in
    the first place, so this never touches them.

    Returns a per-city list of what was started, plus any city with nothing
    buildable at all (those need manual handling).
    """
    gs = _get_game(ctx)

    async def _run():
        return await gs.fill_idle_production()

    return await _logged(ctx, "fill_idle_production", {}, _run)


@mcp.tool()
async def skip_remaining_units(ctx: Context) -> str:
    """Skip all units that still have moves remaining.

    Useful after diplomacy encounters invalidate all standing orders.
    Uses GameCore FinishMoves on each unit — fast, reliable, no async issues.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "skip_remaining_units", {}, lambda: gs.skip_remaining_units()
    )


@mcp.tool()
async def set_city_production(
    ctx: Context,
    city_id: int,
    item_type: str,
    item_name: str,
    target_x: int | None = None,
    target_y: int | None = None,
    force: bool = False,
    force_reason: str = "",
) -> str:
    """Set what a city should produce.

    Args:
        city_id: City ID (from get_cities output)
        item_type: UNIT, BUILDING, DISTRICT, or PROJECT
        item_name: e.g. UNIT_WARRIOR, BUILDING_MONUMENT, DISTRICT_CAMPUS, PROJECT_LAUNCH_EARTH_SATELLITE
        target_x: X coordinate for district/wonder placement (required for districts — use get_district_advisor to find best tile)
        target_y: Y coordinate for district/wonder placement
        force: Override a RED-alert block on civilian production. Only use
            after reading the block's explanation — it fires when producing
            a settler/builder while hostiles are at the gates.
        force_reason: One sentence on WHY you are overriding. Recorded to
            data/force_log.jsonl. Without it a post-game review cannot tell
            a wrong gate from a wrong call — write it even when in a hurry.

    Tip: call get_cities first to see your cities and their IDs.
    """
    gs = _get_game(ctx)
    params: dict = {"city_id": city_id, "item_type": item_type, "item_name": item_name}
    if target_x is not None:
        params["target_x"] = target_x
        params["target_y"] = target_y
    if force:
        params["force"] = True
        params["force_reason"] = force_reason
    return await _logged(
        ctx,
        "set_city_production",
        params,
        lambda: gs.set_city_production(
            city_id, item_type, item_name, target_x, target_y, force=force
        ),
    )


@mcp.tool()
async def purchase_item(
    ctx: Context,
    city_id: int,
    item_type: str,
    item_name: str,
    yield_type: str = "YIELD_GOLD",
    force: bool = False,
    force_reason: str = "",
) -> str:
    """Purchase a unit or building instantly with gold or faith.

    Args:
        city_id: City ID (from get_cities output)
        item_type: UNIT or BUILDING
        item_name: e.g. UNIT_WARRIOR, BUILDING_MONUMENT
        yield_type: YIELD_GOLD (default) or YIELD_FAITH
        force: Override the upkeep gate (UPKEEP_CEILING). Buying a unit is
        force_reason: One sentence on WHY you are overriding. Recorded to
            data/force_log.jsonl. Without it a post-game review cannot tell
            a wrong gate from a wrong call — write it even when in a hurry.
               blocked when it would push military upkeep past 60% of gross
               income — the purchase feels free, the bill arrives next turn.

    Costs gold/faith immediately. Use get_city_production to see what's available.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "purchase_item",
        {
            "city_id": city_id,
            "item_type": item_type,
            "item_name": item_name,
            "yield_type": yield_type,
            "force": force,
            "force_reason": force_reason,
        },
        lambda: gs.purchase_item(
            city_id, item_type, item_name, yield_type, force=force
        ),
    )


@mcp.tool()
async def set_research(ctx: Context, tech_or_civic: str, category: str = "tech") -> str:
    """Choose a technology or civic to research.

    Args:
        tech_or_civic: The type name, e.g. TECH_POTTERY or CIVIC_CRAFTSMANSHIP
        category: "tech" or "civic" (default: tech)

    Tip: call get_tech_civics first to see available options.
    """
    gs = _get_game(ctx)

    async def _run():
        if category.lower() == "civic":
            return await gs.set_civic(tech_or_civic)
        return await gs.set_research(tech_or_civic)

    return await _logged(
        ctx,
        "set_research",
        {"tech_or_civic": tech_or_civic, "category": category},
        _run,
    )


@mcp.tool()
async def set_research_queue(
    ctx: Context, items: str = "", category: str = "tech"
) -> str:
    """Plan a research route once — end_turn auto-advances it on completion.

    Args:
        items: Comma-separated type names in order, e.g.
               "TECH_NUCLEAR_FISSION,TECH_LASERS,TECH_NUCLEAR_FUSION".
               Empty string shows the current queues without changing them.
               "clear" empties the queue for the given category.
        category: "tech" or "civic" (default: tech)

    The queue is a fallback, not a lock: it is only consumed when the
    current research/civic slot is EMPTY at end_turn time, so a manual
    set_research always wins. A queue item the engine rejects pauses the
    queue with a visible event. Persisted per game — survives restarts.
    """
    gs = _get_game(ctx)

    async def _run():
        from . import research_queue as rqm

        civ_id, seed_id = await gs.get_game_identity()
        game_key = f"{civ_id}_{seed_id}"
        cat = category.lower()
        if cat not in ("tech", "civic"):
            return f"ERR:BAD_CATEGORY|{category} — use 'tech' or 'civic'"
        if items.strip().lower() == "clear":
            rqm.set_queue(game_key, cat, [])
            return f"QUEUE_CLEARED|{cat}"
        if items.strip():
            parsed = [s.strip() for s in items.split(",") if s.strip()]
            prefix = "TECH_" if cat == "tech" else "CIVIC_"
            bad = [s for s in parsed if not s.startswith(prefix)]
            if bad:
                return (
                    f"ERR:BAD_NAMES|{','.join(bad)} — {cat} queue items must "
                    f"start with {prefix} (use get_tech_civics for exact names)"
                )
            rqm.set_queue(game_key, cat, parsed)
        tq = rqm.get_queue(game_key, "tech")
        cq = rqm.get_queue(game_key, "civic")
        return (
            f"QUEUE|tech({len(tq)}): {' -> '.join(tq) if tq else '(empty)'}\n"
            f"QUEUE|civic({len(cq)}): {' -> '.join(cq) if cq else '(empty)'}\n"
            "Auto-advances when the slot is empty at end_turn; manual set_research overrides."
        )

    return await _logged(
        ctx,
        "set_research_queue",
        {"items": items, "category": category},
        _run,
    )


@mcp.tool()
async def set_production_queue(ctx: Context, city_id: int, items: str = "") -> str:
    """Plan a city's build order once — end_turn auto-advances it when the
    city's production slot is empty (defect 65: cities idling on
    "Building: nothing" for turns after every completion).

    Args:
        city_id: City ID (from get_cities output)
        items: Comma-separated item names in order. item_type is inferred
               from the prefix (BUILDING_/UNIT_/DISTRICT_/PROJECT_).
               Districts MUST carry placement coords inline:
               "DISTRICT_CAMPUS@69,47" (use get_district_advisor first).
               **Wonders need coords too** — they are BUILDING_ items but
               are placed on a tile like districts:
               "BUILDING_FORBIDDEN_CITY@67,49" (use get_wonder_advisor).
               A wonder queued without coords cannot be rejected here (the
               prefix doesn't say which buildings are wonders) and fails at
               consumption time instead — 2026-08-11 T269 cost Ostia a whole
               idle turn that way.
               Example: "BUILDING_MARKET,UNIT_TRADER,DISTRICT_CAMPUS@69,47"
               Empty string shows this city's current queue.
               "clear" empties this city's queue.

    The queue is a fallback, not a lock: a manual set_city_production always
    wins; consumption happens only when the slot is empty at end_turn time.
    A rejected item pauses the queue with a visible event — this includes
    the RED-alert civilian-production gate, which still applies at
    consumption time. Persisted per game — survives restarts.
    """
    gs = _get_game(ctx)

    async def _run():
        from . import production_queue as pqm

        civ_id, seed_id = await gs.get_game_identity()
        game_key = f"{civ_id}_{seed_id}"
        if items.strip().lower() == "clear":
            pqm.set_queue(game_key, city_id, [])
            return f"QUEUE_CLEARED|city {city_id}"
        if items.strip():
            # 2026-08-11 罗马局 T261(缺陷 90):逗号既是项目分隔符,又出现在
            # 区域坐标里。"DISTRICT_CAMPUS@71,57" 被拆成 "DISTRICT_CAMPUS@71"
            # 和 "57",两个都解析失败;更糟的是报错时用 ','.join(bad) 把拆开的
            # 又拼回去,于是错误信息回显的是【完整且合法】的那一项,读起来像
            # 工具在说"这个正确的格式不对"。文档里的示例
            # "BUILDING_MARKET,UNIT_TRADER,DISTRICT_CAMPUS@69,47" 因此从来
            # 没有能用过 —— 带坐标的区域根本排不进队列。
            # 修法:分割后把落单的纯数字并回上一项(它只可能是坐标的后半)。
            tokens = [s.strip() for s in items.split(",") if s.strip()]
            raw_items: list[str] = []
            for tok in tokens:
                if (
                    raw_items
                    and re.fullmatch(r"-?\d+", tok)
                    and "@" in raw_items[-1]
                    and "," not in raw_items[-1]
                ):
                    raw_items[-1] = f"{raw_items[-1]},{tok}"
                else:
                    raw_items.append(tok)
            parsed: list[dict] = []
            bad: list[str] = []
            for raw in raw_items:
                entry = pqm.parse_item(raw)
                if entry is None:
                    bad.append(raw)
                else:
                    parsed.append(entry)
            if bad:
                return (
                    f"ERR:BAD_ITEMS|{','.join(bad)} — each item must start with "
                    "BUILDING_/UNIT_/DISTRICT_/PROJECT_, and districts need "
                    "inline coords like DISTRICT_CAMPUS@69,47"
                )
            pqm.set_queue(game_key, city_id, parsed)
        q = pqm.get_queue(game_key, city_id)

        def _fmt(e: dict) -> str:
            n = e.get("item_name", "?")
            if "target_x" in e:
                return f"{n}@{e['target_x']},{e['target_y']}"
            return n

        return (
            f"QUEUE|city {city_id}({len(q)}): "
            f"{' -> '.join(_fmt(e) for e in q) if q else '(empty)'}\n"
            "Auto-advances when the city's production slot is empty at end_turn; "
            "manual set_city_production overrides."
        )

    return await _logged(
        ctx,
        "set_production_queue",
        {"city_id": city_id, "items": items},
        _run,
    )


# ---------------------------------------------------------------------------
# L2 计划链 / L3 胜利路线 —— 把"打算"外化成状态,不再依赖模型的记性
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_victory_goal(ctx: Context, victory_type: str, reason: str) -> str:
    """Lock in the victory path for this game. Do this once, early.

    After locking, every turn's briefing shows the goal, and deterministic
    deviation checks will call out actions that drift from it (e.g. building
    armies while pursuing a science victory with no threats around).

    Args:
        victory_type: SCIENCE, CULTURE, RELIGION, or DOMINATION
        reason: Why this path fits this civ/map (shown in briefings)
    """
    gs = _get_game(ctx)
    vt = victory_type.upper()
    if vt not in ("SCIENCE", "CULTURE", "RELIGION", "DOMINATION"):
        return "Error: INVALID_VICTORY|用 SCIENCE / CULTURE / RELIGION / DOMINATION"

    async def _run():
        wm = await gs.ensure_world()
        turn = wm.current_turn
        wm.victory_type = vt
        wm.victory_reason = reason
        wm.victory_locked_turn = turn
        gs.save_world()
        from civ_mcp import lategame

        playbook = lategame.victory_playbook(vt)
        return (
            f"胜利路线已锁定:{vt}(T{turn})。理由:{reason}\n\n"
            f"{playbook}\n\n"
            f"之后每回合简报都会显示这条路线,并按当前时代提示该做什么;"
            f"跑偏时会被点名。改路线请再次调用本工具。"
        )

    return await _logged(
        ctx, "set_victory_goal", {"victory_type": vt, "reason": reason}, _run
    )


@mcp.tool()
async def plan_add(
    ctx: Context,
    goal: str,
    cond: str = "manual",
    args: str = "",
    after: int = -1,
) -> str:
    """Add a step to the plan chain. Steps auto-activate when their
    prerequisite completes — you don't need to remember what comes next;
    the briefing shows it every turn.

    Completion is judged by SERVER-SIDE facts, not by your say-so:
        cond="cities_at_least"       args="2"        (city count reaches 2)
        cond="military_at_least"     args="3"        (military unit count)
        cond="camp_destroyed"        args="9,33"     (that camp confirmed gone)
        cond="exploration_at_least"  args="0.15"     (map revealed fraction)
        cond="tech_completed"        args="TECH_ARCHERY"
        cond="manual"                                (you mark it via plan_done)

    Args:
        goal: Short Chinese description, e.g. "拆除营地(9,33)"
        cond: Completion condition type (see above)
        args: Condition argument
        after: Plan id that must complete first (-1 = starts active now)
    """
    gs = _get_game(ctx)

    async def _run():
        wm = await gs.ensure_world()
        p = wm.add_plan(goal, cond, args, after, wm.current_turn)
        gs.save_world()
        state = "等待前置" if p.status == "pending" else "已激活"
        return f"计划 #{p.id} 已加入({state}):{goal}"

    return await _logged(
        ctx,
        "plan_add",
        {"goal": goal, "cond": cond, "args": args, "after": after},
        _run,
    )


@mcp.tool()
async def plan_list(ctx: Context) -> str:
    """List the full plan chain with status (active / pending / done)."""
    gs = _get_game(ctx)

    async def _run():
        wm = await gs.ensure_world()
        if not wm.plans:
            return "计划链为空。用 plan_add 挂目标 —— 写下来的计划才不会被忘掉。"
        icon = {"active": "▶", "pending": "⏸", "done": "✔"}
        lines = []
        for p in wm.plans:
            extra = ""
            if p.status == "pending":
                extra = f"(等待 #{p.after})"
            elif p.status == "done":
                extra = f"(T{p.done_turn} 完成)"
            elif p.cond != "manual":
                extra = f"(条件:{p.cond} {p.args})"
            lines.append(f"{icon.get(p.status, '?')} #{p.id} {p.goal} {extra}")
        return "\n".join(lines)

    return await _logged(ctx, "plan_list", {}, _run)


@mcp.tool()
async def plan_done(ctx: Context, plan_id: int) -> str:
    """Mark a manual-condition plan as completed. Plans with server-checked
    conditions complete automatically — this is only for cond="manual"."""
    gs = _get_game(ctx)

    async def _run():
        wm = await gs.ensure_world()
        for p in wm.plans:
            if p.id == plan_id:
                if p.status == "done":
                    return f"计划 #{plan_id} 本来就是完成状态。"
                if p.cond != "manual":
                    return (
                        f"Error: NOT_MANUAL|计划 #{plan_id} 的完成条件是 "
                        f"{p.cond} {p.args},由服务端数据自动判定 —— "
                        f"说了不算,数字说了算。"
                    )
                p.status = "done"
                p.done_turn = wm.current_turn
                wm.evaluate_plans({}, wm.current_turn)  # 激活后继
                gs.save_world()
                return f"计划 #{plan_id} 已标记完成。后继计划(若有)已激活。"
        return f"Error: PLAN_NOT_FOUND|没有 #{plan_id}"

    return await _logged(ctx, "plan_done", {"plan_id": plan_id}, _run)


@mcp.tool()
async def plan_drop(ctx: Context, plan_id: int) -> str:
    """Remove a plan that no longer makes sense (situation changed)."""
    gs = _get_game(ctx)

    async def _run():
        wm = await gs.ensure_world()
        before = len(wm.plans)
        wm.plans = [p for p in wm.plans if p.id != plan_id]
        if len(wm.plans) == before:
            return f"Error: PLAN_NOT_FOUND|没有 #{plan_id}"
        gs.save_world()
        return f"计划 #{plan_id} 已移除。"

    return await _logged(ctx, "plan_drop", {"plan_id": plan_id}, _run)


@mcp.tool(annotations={"destructiveHint": True})
async def end_turn(
    ctx: Context,
    tactical: str = "",
    strategic: str = "",
    tooling: str = "",
    planning: str = "",
    hypothesis: str = "",
) -> str:
    """End the current turn.

    Make sure you've moved all units, set production, and chosen research
    before ending the turn.

    All 5 reflection parameters are required and must be non-empty.
    These form the per-turn diary — your persistent memory across sessions:
        tactical: What happened this turn — combat, movements, improvements.
        strategic: Current standing vs rivals — yields, city count, victory path.
        tooling: Tool issues or observations. Write "No issues" if none.
        planning: Concrete actions for the next 5-10 turns.
        hypothesis: Predictions — enemy behavior, resource needs, timelines.

    IMPORTANT: Reflections are recorded BEFORE the AI processes its turn.
    Anything that surfaces after end_turn (diplomacy proposals, AI movements,
    events reported in the turn result) belongs in the NEXT turn's diary.
    If end_turn is blocked and you call it again after resolving the blocker,
    the diary entry from the first call is kept — do not repeat reflections.
    """
    gs = _get_game(ctx)

    reflections = {
        "tactical": tactical,
        "strategic": strategic,
        "tooling": tooling,
        "planning": planning,
        "hypothesis": hypothesis,
    }
    missing = [k for k, v in reflections.items() if not v.strip()]
    if missing:
        # 2026-08-10 罗马局 T170/T171:被"回合完成度门禁"拦下后重调,
        # 文档说"第一次的反思会保留、不用重写",实际却又要求五项全填 ——
        # 于是每次拦截都要把五段反思原样再打一遍(两回合各浪费一次)。
        # 上一次已经记下的反思在这里复用,retry 只补门禁要求的动作即可。
        prev = getattr(gs, "_last_reflections", None)
        if prev and getattr(gs, "_end_turn_blocked", False):
            reflections = dict(prev)
            log.info("end_turn retry after blocker — reusing recorded reflections")
        else:
            return (
                f"Empty reflections: {', '.join(missing)}. "
                "Provide non-empty entries for all 5 fields: "
                "tactical, strategic, tooling, planning, hypothesis."
            )
    else:
        gs._last_reflections = dict(reflections)

    # Model ID comes from CIV_MCP_AGENT_MODEL env var (set by eval runner)
    env_model = os.environ.get("CIV_MCP_AGENT_MODEL", "")
    if env_model:
        _get_logger(ctx).set_agent_model(env_model)

    # Capture diary state and write BEFORE advancing the turn.
    # This ensures the entry is saved even if the session is interrupted
    # during AI turn processing.
    #
    # If the last end_turn hit a blocker (diplomacy, WC), the turn may have
    # advanced during processing. On retry, we merge reflections into the
    # previous entry rather than writing a duplicate with terse reflections.
    _diary_turn = 0
    _diary_player_id = -1
    _diary_civ_type = None
    _diary_seed = None
    _diary_run_id = _get_logger(ctx).session_id
    _diary_snapshot = None
    _is_retry = getattr(gs, "_end_turn_blocked", False)
    try:
        ov = await gs.get_game_overview()
        _diary_player_id = ov.player_id
        _diary_turn = ov.turn
        # Keep logger/spatial turn in sync (agent may not call get_game_overview every turn)
        _get_logger(ctx).set_turn(ov.turn)
        _get_spatial(ctx).set_turn(ov.turn)
    except Exception:
        log.warning("Diary: failed to capture overview", exc_info=True)

    # —— 回合漂移检测(2026-08-11 罗马局 T228)——
    # 上一次 end_turn 写完 N 之后,下一次理应在 N+1 被调用。真出现跳号,
    # 说明回合在 agent 不知情时被推进过(AutoEndTurn 是已知原因,见
    # ensure_manual_turn_control)—— 中间那些回合的日记永远不会存在,而且
    # 这一刻写下的反思描述的其实是**上一个**回合。不出声就永远发现不了。
    _turn_drift = 0
    _prev_written = int(getattr(gs, "_diary_written_turn", 0) or 0)
    if _diary_turn > 0 and _prev_written > 0 and not _is_retry:
        _turn_drift = _diary_turn - _prev_written - 1
        if _turn_drift > 0:
            log.warning(
                "回合漂移:上次记到 T%s,现在已是 T%s —— 中间 %s 个回合没有日记",
                _prev_written,
                _diary_turn,
                _turn_drift,
            )

    try:
        _diary_civ_type, _diary_seed = await gs.get_game_identity()
        # 2026-08-08 日记丢失根因落码:LocalSink 在 bind_game() 前无限缓冲,
        # 而 bind 此前只挂在 get_game_overview 工具上 —— 压缩续会话直接进
        # 回合循环不调 overview,缓冲区随进程被杀蒸发(T69-104 等四段共 30+
        # 回合)。end_turn 本来就查了 identity,就地绑定(bind_game 幂等)。
        _logger_obj = _get_logger(ctx)
        if not _logger_obj.bound:
            _logger_obj.bind_game(_diary_civ_type, _diary_seed)
            _get_spatial(ctx).bind_game(_diary_civ_type, _diary_seed)
            heartbeat.bind_game(_diary_civ_type, _diary_seed)
    except Exception:
        log.warning("Diary: failed to get game identity", exc_info=True)

    if _is_retry and _diary_civ_type is not None:
        # Merge reflections into the most recent agent row (from the
        # previous end_turn call that wrote before hitting a blocker).
        # Merges into whichever turn that row belongs to — handles both
        # same-turn retries and turn-advanced-during-blocker cases.
        try:
            path = _diary_path(_diary_civ_type, _diary_seed, _diary_run_id)
            merged_row = _merge_agent_reflections(
                path, gs._diary_written_turn, reflections
            )
            if merged_row:
                log.info(
                    "Diary: merged retry reflections into turn %s",
                    gs._diary_written_turn,
                )
                # Re-emit merged row so CloudSink gets the updated reflections
                await _get_logger(ctx)._emitter.emit(EVENT_DIARY_ROW, merged_row)
        except Exception:
            log.warning("Diary: failed to merge reflections", exc_info=True)
    elif (
        _diary_civ_type is not None
        and _diary_turn > 0
        and gs._diary_written_turn != _diary_turn
    ):
        try:
            _diary_snapshot = await gs.get_diary_snapshot()
        except Exception:
            log.warning("Diary: failed to capture snapshot", exc_info=True)
        if _diary_snapshot:
            game_id = f"{_diary_civ_type}_{_diary_seed}"
            ts = datetime.now(timezone.utc).isoformat()
            # MCP client metadata (from handshake)
            agent_client = ""
            agent_client_ver = ""
            try:
                ci = ctx.session.client_params.clientInfo
                agent_client = ci.name or ""
                agent_client_ver = ci.version or ""
            except Exception:
                pass
            try:
                _emitter = _get_logger(ctx)._emitter
                # Write one row per player (emitter routes to sinks)
                for pr in _diary_snapshot.players:
                    row = asdict(pr)
                    row["v"] = 1
                    row["turn"] = _diary_turn
                    row["game"] = game_id
                    row["timestamp"] = ts
                    if pr.pid == _diary_player_id:
                        row["is_agent"] = True
                        # Merge agent extras
                        ag = _diary_snapshot.agent
                        row["diplo_states"] = ag.diplo_states
                        row["suzerainties"] = ag.suzerainties
                        row["envoys_available"] = ag.envoys_available
                        row["envoys_sent"] = ag.envoys_sent
                        row["gp_points"] = ag.gp_points
                        row["governors"] = ag.governors
                        row["trade_routes"] = {
                            "capacity": ag.trade_capacity,
                            "active": ag.trade_active,
                            "domestic": ag.trade_domestic,
                            "international": ag.trade_international,
                        }
                        row["reflections"] = reflections
                        row["agent_client"] = agent_client
                        row["agent_client_ver"] = agent_client_ver
                        row["agent_model"] = env_model
                        # Eval metadata from emitter (only non-empty)
                        for _mk, _mv in _emitter.metadata.items():
                            if _mv:
                                row[_mk] = _mv
                    await _emitter.emit(EVENT_DIARY_ROW, row)
                # Write one row per city
                for cr in _diary_snapshot.cities:
                    row = asdict(cr)
                    row["v"] = 1
                    row["turn"] = _diary_turn
                    row["game"] = game_id
                    await _emitter.emit(EVENT_CITY_ROW, row)
                gs._diary_written_turn = _diary_turn
            except Exception:
                log.warning("Diary: failed to write entry", exc_info=True)

    # Advance the turn
    result = await _logged(ctx, "end_turn", {}, gs.end_turn)

    # ---------------------------------------------------------------
    # Auto-recover from AI turn hangs (transparent to agent).
    # end_turn returns "HANG:{turn}:{save}|..." when AI processing is
    # stuck after ~39s of polling with no blockers found.
    # Recovery: restart_and_load the MCP autosave, reconnect, retry
    # up to _MAX_HANG_RETRIES times with escalating waits.
    # ---------------------------------------------------------------
    _MAX_HANG_RETRIES = 3
    _HANG_EXTRA_WAIT = [0, 15, 30]  # extra seconds before retry per attempt

    if result.startswith("HANG:") and not gs._hang_retry_active:
        parts = result.split("|", 1)
        hang_info = parts[
            0
        ]  # "HANG:57:AutoSave_0057" (Linux) or "HANG:57:0_MCP_0057" (Windows)
        _, hang_turn, hang_save = hang_info.split(":")
        hang_turn_int = int(hang_turn)

        # Check save file exists before attempting recovery.
        # MCP saves (0_MCP_*) are in SINGLE_SAVE_DIR; game autosaves
        # (AutoSave_*) are in SAVE_DIR (auto/ subdir).
        save_path = os.path.join(game_launcher.SINGLE_SAVE_DIR, f"{hang_save}.Civ6Save")
        if not os.path.exists(save_path):
            save_path = os.path.join(game_launcher.SAVE_DIR, f"{hang_save}.Civ6Save")
        if not os.path.exists(save_path):
            log.error(
                "HANG RECOVERY: Save file %s not found, cannot auto-recover",
                save_path,
            )
            # Fall through — return the hang message to agent
        else:
            identity_before = gs._game_identity
            gs._hang_retry_active = True
            try:
                for attempt in range(1, _MAX_HANG_RETRIES + 1):
                    extra_wait = _HANG_EXTRA_WAIT[
                        min(attempt - 1, len(_HANG_EXTRA_WAIT) - 1)
                    ]
                    log.warning(
                        "HANG RECOVERY: attempt %d/%d for T%s "
                        "(extra wait: %ds, save: %s)",
                        attempt,
                        _MAX_HANG_RETRIES,
                        hang_turn,
                        extra_wait,
                        hang_save,
                    )

                    # Step 1: Kill + relaunch + OCR load
                    restart_result = await game_launcher.restart_and_load(hang_save)
                    log.info("HANG RECOVERY: restart_and_load: %s", restart_result)

                    # Step 2: Reconnect
                    conn = gs.conn
                    reconnected = False
                    for rc_attempt in range(30):
                        try:
                            await conn.reconnect()
                            if conn.gamecore_index is not None:
                                reconnected = True
                                break
                        except ConnectionError:
                            pass
                        await asyncio.sleep(1)

                    if not reconnected:
                        log.error(
                            "HANG RECOVERY: could not reconnect (attempt %d)",
                            attempt,
                        )
                        continue  # try the whole cycle again

                    # Step 2b: Verify correct game loaded (with retries).
                    # The game may still be on the leader screen after
                    # restart — Lua states exist but game APIs aren't
                    # fully initialized. Retry the check rather than
                    # restarting the entire recovery cycle.
                    if identity_before is not None:
                        identity_ok = False
                        for id_check in range(3):
                            try:
                                actual = await gs.get_game_identity()
                                if actual == identity_before:
                                    identity_ok = True
                                    break
                                log.warning(
                                    "HANG RECOVERY: wrong identity %s vs %s "
                                    "(check %d/3)",
                                    actual,
                                    identity_before,
                                    id_check + 1,
                                )
                            except Exception:
                                log.debug(
                                    "HANG RECOVERY: identity check failed "
                                    "(check %d/3), waiting...",
                                    id_check + 1,
                                )
                            await asyncio.sleep(5)
                        if not identity_ok:
                            log.warning(
                                "HANG RECOVERY: identity check inconclusive "
                                "— proceeding anyway (attempt %d)",
                                attempt,
                            )

                    # Step 3: Reset state flags
                    gs._pending_end_turn = False
                    gs._pending_end_turn_from = None
                    gs._end_turn_blocked = False

                    # Step 4: Extra wait to give AI more processing time
                    if extra_wait > 0:
                        log.info(
                            "HANG RECOVERY: waiting %ds before retry...",
                            extra_wait,
                        )
                        await asyncio.sleep(extra_wait)

                    # Step 5: Retry end_turn
                    log.info(
                        "HANG RECOVERY: retrying end_turn for T%s...",
                        hang_turn,
                    )
                    result = await gs.end_turn()
                    log.info("HANG RECOVERY: retry result: %s", result[:200])

                    if not result.startswith("HANG:"):
                        log.info(
                            "HANG RECOVERY: T%s resolved on attempt %d",
                            hang_turn,
                            attempt,
                        )
                        break  # success — fall through to normal processing
                else:
                    # All retries exhausted
                    earlier = max(1, hang_turn_int - 3)
                    log.error(
                        "HANG RECOVERY: all %d attempts failed for T%s",
                        _MAX_HANG_RETRIES,
                        hang_turn,
                    )
                    return (
                        f"AI turn hung at T{hang_turn} after "
                        f"{_MAX_HANG_RETRIES} automatic restart attempts "
                        f"with escalating waits. The hang may be "
                        f"probabilistic — another attempt could work. "
                        f"Try restart_and_load('{hang_save.replace(hang_turn, str(earlier))}') "
                        f"to skip back a few turns."
                    )
            except Exception:
                log.error("HANG RECOVERY: failed", exc_info=True)
                return (
                    f"HANG RECOVERY FAILED at T{hang_turn}: "
                    f"restart_and_load threw an exception. "
                    f"Try restart_and_load('{hang_save}') manually."
                )
            finally:
                gs._hang_retry_active = False

    # Clear stale camera events on successful turn advance
    turn_advanced = (
        "->" in result and "Cannot end turn" not in result and "Error" not in result
    )
    if turn_advanced:
        _get_camera(ctx).clear()
        gs._end_turn_blocked = False
        # Update logger/spatial turn from result ("Turn X -> Y")
        m = re.search(r"Turn \d+ -> (\d+)", result)
        if m:
            new_turn = int(m.group(1))
            _get_logger(ctx).set_turn(new_turn)
            _get_spatial(ctx).set_turn(new_turn)
            heartbeat.write("playing", turn=new_turn)
        # Map capture — record terrain (first turn) + ownership delta
        if _diary_civ_type and _diary_seed:
            try:
                mc = _get_map_capture(ctx)
                mc.bind_game(_diary_civ_type, _diary_seed)
                capture_turn = new_turn if m else _diary_turn
                await mc.capture(gs.conn, capture_turn)
            except Exception:
                log.debug("Map capture failed", exc_info=True)
    elif "Cannot end turn" in result:
        # 完成度门禁也是一次"被拦下的 end_turn":反思已经写进日记了,
        # 重调时必须走 retry 合并路径,否则同一回合会写出两行日记,
        # 而且调用方被迫把五段反思重打一遍(2026-08-10 罗马局实测)。
        gs._end_turn_blocked = True
    elif "Turn paused" in result or "World Congress fires" in result:
        gs._end_turn_blocked = True
        # Safety net: if WC blocker fires repeatedly on the same turn,
        # auto-submit to break infinite loops (agent used wrong voting tool)
        if "World Congress fires" in result:
            wc_turn = getattr(gs, "_wc_blocker_turn", -1)
            wc_count = getattr(gs, "_wc_blocker_count", 0)
            current = _diary_turn or 0
            if wc_turn == current:
                gs._wc_blocker_count = wc_count + 1
                if gs._wc_blocker_count >= 3:
                    log.warning(
                        "WC blocker repeated %d times on T%d — auto-submitting",
                        gs._wc_blocker_count,
                        current,
                    )
                    try:
                        await gs.submit_congress()
                    except Exception:
                        log.debug("WC auto-submit failed", exc_info=True)
            else:
                gs._wc_blocker_turn = current
                gs._wc_blocker_count = 1

    # Log structured game-over entry.
    # Also check on HANG — the game may have ended during AI processing but
    # InGame Lua froze, so end_turn returned HANG instead of GAME OVER.
    # The GameCore fallback in check_game_over can detect this.
    if "HANG:" in result and "GAME OVER" not in result:
        try:
            hang_check = await gs.check_game_over()
            if hang_check is not None:
                gs._last_game_over = hang_check
                vtype = (
                    hang_check.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                if hang_check.is_defeat:
                    result = (
                        f"GAME OVER — DEFEAT. {hang_check.winner_leader} "
                        f"of {hang_check.winner_name} won a {vtype} victory. "
                        f"The game has ended. No further actions are possible."
                    )
                else:
                    result = (
                        f"GAME OVER — VICTORY! You won a {vtype} victory! "
                        f"The game has ended."
                    )
        except Exception:
            log.debug("HANG game-over recheck failed", exc_info=True)

    if "GAME OVER" in result:
        heartbeat.write("finished", turn=_diary_turn or 0)
        try:
            gameover = gs._last_game_over
            if gameover is None:
                gameover = await gs.check_game_over()
            if gameover is not None:
                gs._last_game_over = None
                vtype = (
                    gameover.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                await _get_logger(ctx).log_game_over(
                    is_defeat=gameover.is_defeat,
                    winner_civ=gameover.winner_name,
                    winner_leader=gameover.winner_leader,
                    victory_type=vtype,
                    player_alive=gameover.player_alive,
                )
            else:
                log.error(
                    "GAME OVER detected but no GameOverStatus available "
                    "— outcome will be missing from log"
                )
        except Exception:
            log.warning("Failed to log game-over entry", exc_info=True)

    # Arm the watchdog after first successful end_turn so it starts
    # polling for game-over independently of future tool calls.
    if "GAME OVER" not in result:
        _get_watchdog(ctx).arm()

    if _turn_drift > 0:
        result = (
            f"⚠️ 回合漂移:上一条日记记在 T{_prev_written},本次 end_turn 时已经是 "
            f"T{_diary_turn} —— 中间 {_turn_drift} 个回合没有日记,而且刚写下的这段"
            f"反思描述的其实是上一个回合。原因通常是游戏的「自动结束回合」把回合"
            f"推走了(所有单位都没有待办时触发,skip_remaining_units 最容易命中);"
            f"工具现在会在 skip 前关掉它,若仍复现说明还有别的推进路径。\n\n"
        ) + result

    return result


# ---------------------------------------------------------------------------
# Diary
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_diary(
    ctx: Context,
    last_n: int = 5,
    turn: Optional[int] = None,
    from_turn: Optional[int] = None,
    to_turn: Optional[int] = None,
) -> str:
    """Read diary entries for game memory.

    Args:
        last_n: Number of most recent entries to return (default 5, max 50).
                Used when turn/from_turn/to_turn are not specified.
        turn: Return the single entry for this turn number.
        from_turn: Return entries from this turn onward (inclusive).
        to_turn: Return entries up to this turn (inclusive).

    Auto-detects the current game from the live connection. Each game has
    its own diary file (keyed by civ + random seed).

    Call this at the start of a session or after context compaction to
    restore strategic memory from previous turns.
    """
    gs = _get_game(ctx)
    try:
        civ_type, seed = await gs.get_game_identity()
    except Exception:
        return "Could not detect current game. Is the game running?"

    # 2026-08-02 校准局 P1 1b:此前只读当前 run_id 的文件,重启换 run_id
    # 即"失忆"。改为 glob 该局全部 run_id,按回合归并(agent 行过滤与
    # 同回合去重在 read_all_game_entries 内完成)。
    from civ_mcp.diary import read_all_game_entries

    entries = read_all_game_entries(civ_type, seed)
    if not entries:
        return f"No diary entries yet for this game ({civ_type}, seed {seed})."

    # Filter by query mode
    if turn is not None:
        entries = [e for e in entries if e.get("turn") == turn]
    elif from_turn is not None or to_turn is not None:
        lo = from_turn if from_turn is not None else 0
        hi = to_turn if to_turn is not None else 999999
        entries = [e for e in entries if lo <= e.get("turn", 0) <= hi]
    else:
        last_n = min(max(last_n, 1), 50)
        entries = entries[-last_n:]

    if not entries:
        return "No diary entries match the query."

    return "\n\n".join(_format_diary_entry(e) for e in entries)


# ---------------------------------------------------------------------------
# Trade routes
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_routes(ctx: Context) -> str:
    """Get trade route capacity, active routes, and trader status.

    Shows how many routes are active vs capacity, and lists all trader
    units with their positions and whether they're idle or on a route.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_trade_routes",
        {},
        lambda: _narrate(gs.get_trade_routes, nr.narrate_trade_routes),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_destinations(ctx: Context, unit_id: int) -> str:
    """List valid trade route destinations for a trader unit.

    Args:
        unit_id: The trader's composite ID (from get_units output)

    Shows domestic and international destinations. Use unit_action
    with action='trade_route' and target_x/target_y to start a route.
    """
    gs = _get_game(ctx)

    async def _run():
        dests = await gs.get_trade_destinations(unit_id)
        return nr.narrate_trade_destinations(dests)

    return await _logged(ctx, "get_trade_destinations", {"unit_id": unit_id}, _run)


# ---------------------------------------------------------------------------
# District advisor
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_district_advisor(ctx: Context, city_id: int, district_type: str) -> str:
    """Show best tiles to place a district with adjacency bonuses.

    Args:
        city_id: City ID (from get_cities)
        district_type: e.g. DISTRICT_CAMPUS, DISTRICT_HOLY_SITE, DISTRICT_INDUSTRIAL_ZONE

    Returns valid placement tiles ranked by adjacency bonus.
    Use set_city_production with target_x/target_y to build the district.
    """
    gs = _get_game(ctx)

    async def _run():
        result = await gs.get_district_advisor(city_id, district_type)
        if isinstance(result, str):
            return f"Error: {result}"  # propagate specific error reason
        narrated = nr.narrate_district_advisor(result, district_type)
        if gs._advisor_budget_warning:
            warn = gs._advisor_budget_warning
            gs._advisor_budget_warning = None
            return f"!! {warn}\n\n{narrated}"
        return narrated

    return await _logged(
        ctx,
        "get_district_advisor",
        {"city_id": city_id, "district_type": district_type},
        _run,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_wonder_advisor(ctx: Context, city_id: int, wonder_name: str) -> str:
    """Show best tiles to place a wonder with displacement cost analysis.

    Args:
        city_id: City ID (from get_cities output)
        wonder_name: Wonder building type, e.g. BUILDING_CHICHEN_ITZA, BUILDING_ORSZAGHAZ

    Returns valid placement tiles ranked by displacement cost (lowest = best):
    tiles with no improvements or resources are preferred over productive tiles.
    Also shows terrain, feature, river/coastal status, and any resources/improvements
    that would be removed by placing the wonder there.
    Use set_city_production with target_x/target_y to build the wonder.
    """
    gs = _get_game(ctx)

    async def _run():
        placements = await gs.get_wonder_advisor(city_id, wonder_name)
        if isinstance(placements, str):
            return f"Error: {placements}"  # propagate budget/error string
        narrated = nr.narrate_wonder_advisor(placements, wonder_name)
        if gs._advisor_budget_warning:
            warn = gs._advisor_budget_warning
            gs._advisor_budget_warning = None
            return f"!! {warn}\n\n{narrated}"
        return narrated

    return await _logged(
        ctx,
        "get_wonder_advisor",
        {"city_id": city_id, "wonder_name": wonder_name},
        _run,
    )


# ---------------------------------------------------------------------------
# Tile purchase tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_purchasable_tiles(ctx: Context, city_id: int) -> str:
    """List tiles a city can purchase with gold.

    Args:
        city_id: City ID (from get_cities)

    Shows cost, terrain, and resources for each purchasable tile.
    Tiles with luxury/strategic resources are listed first.
    """
    gs = _get_game(ctx)

    async def _run():
        tiles = await gs.get_purchasable_tiles(city_id)
        return nr.narrate_purchasable_tiles(tiles)

    return await _logged(ctx, "get_purchasable_tiles", {"city_id": city_id}, _run)


@mcp.tool()
async def purchase_tile(
    ctx: Context,
    city_id: int,
    x: int = -9999,
    y: int = -9999,
    target_x: int = -9999,
    target_y: int = -9999,
) -> str:
    """Buy a tile for a city with gold.

    Args:
        city_id: City ID
        x: Tile X coordinate (target_x also accepted — unit_action 系工具用
            target_x/target_y,这里收编别名免得同族参数名不一致再咬人)
        y: Tile Y coordinate (target_y also accepted)

    Use get_purchasable_tiles first to see costs and options.
    """
    # 2026-08-09 T284 实机:传 target_x/target_y 被参数校验拒收 —— 同族工具
    # (unit_action)用的正是这对名字。两对都收,缺参才报错。
    if x == -9999:
        x = target_x
    if y == -9999:
        y = target_y
    if x == -9999 or y == -9999:
        return "Error: MISSING_COORDS|Pass x/y (or target_x/target_y)."
    gs = _get_game(ctx)
    result = await _logged(
        ctx,
        "purchase_tile",
        {"city_id": city_id, "x": x, "y": y},
        lambda: gs.purchase_tile(city_id, x, y),
    )
    _get_camera(ctx).push(x, y, f"purchase tile ({x},{y})")
    return result


# ---------------------------------------------------------------------------
# Government change
# ---------------------------------------------------------------------------


@mcp.tool()
async def change_government(ctx: Context, government_type: str) -> str:
    """Switch to a different government type.

    Args:
        government_type: e.g. GOVERNMENT_CLASSICAL_REPUBLIC, GOVERNMENT_OLIGARCHY

    Use get_policies to see current government. First switch after
    unlocking a new tier is free (no anarchy).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "change_government",
        {"government_type": government_type},
        lambda: gs.change_government(government_type),
    )


# ---------------------------------------------------------------------------
# Great People
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_great_people(ctx: Context) -> str:
    """See available Great People and recruitment progress.

    Shows which Great People are available, their recruitment cost,
    and which civilization (if any) is recruiting them.
    """
    gs = _get_game(ctx)

    async def _run():
        gp = await gs.get_great_people()
        return nr.narrate_great_people(gp)

    return await _logged(ctx, "get_great_people", {}, _run)


@mcp.tool()
async def get_gp_advisor(ctx: Context, unit_index: int) -> str:
    """Show best cities to activate a Great Person, ranked by suitability.

    Args:
        unit_index: The Great Person unit's index (from get_units output).

    Lists all cities with the matching district (e.g., campuses for Great Scientists),
    showing which ones the GP can activate on, distance, city yield, and great work
    slot availability for cultural GPs.
    """
    gs = _get_game(ctx)

    async def _run():
        result = await gs.get_gp_advisor(unit_index)
        if result is None:
            return "Could not get GP advisor info. Is this a Great Person unit?"
        return nr.narrate_gp_advisor(result)

    return await _logged(ctx, "get_gp_advisor", {"unit": unit_index}, _run)


@mcp.tool()
async def recruit_great_person(ctx: Context, individual_id: int) -> str:
    """Recruit a Great Person using accumulated GP points.

    Args:
        individual_id: The individual's ID (from get_great_people output, shown after ability)

    Requires enough Great Person points for that class.
    The GP spawns in your capital. Use get_great_people to check [CAN RECRUIT] status.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "recruit_great_person",
        {"id": individual_id},
        lambda: gs.recruit_great_person(individual_id),
    )


@mcp.tool()
async def patronize_great_person(
    ctx: Context,
    individual_id: int,
    yield_type: str = "YIELD_GOLD",
    currency: str = "",
) -> str:
    """Buy a Great Person instantly with gold or faith.

    Args:
        individual_id: The individual's ID (from get_great_people output)
        yield_type: YIELD_GOLD (default) or YIELD_FAITH
        currency: alias for yield_type — accepts "gold"/"faith" (overrides
            yield_type when set)

    Costs shown in get_great_people output under "Patronize:".
    Requires enough gold/faith to cover the cost.
    """
    # 2026-08-09 T287/T301 实机:调用方传了 currency="faith"(直觉参数名),
    # 未知参数被 MCP 层静默吞掉 → 三次"失败"全在测金买断,萨根多花 620 金
    # (信仰 430 本可搞定)。别名收编 + 非法值显式报错,不再静默。
    chosen = (currency or yield_type or "").strip().upper()
    if chosen in ("FAITH", "YIELD_FAITH", "F"):
        norm = "YIELD_FAITH"
    elif chosen in ("GOLD", "YIELD_GOLD", "G", ""):
        norm = "YIELD_GOLD"
    else:
        return (
            f"Error: BAD_YIELD|Unknown currency '{chosen}'. "
            f"Use yield_type='YIELD_GOLD' or 'YIELD_FAITH' "
            f"(or currency='gold'/'faith')."
        )
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "patronize_great_person",
        {"id": individual_id, "yield": norm},
        lambda: gs.patronize_great_person(individual_id, norm),
    )


@mcp.tool()
async def reject_great_person(ctx: Context, individual_id: int) -> str:
    """Pass on a Great Person (skip to the next one in that class).

    Args:
        individual_id: The individual's ID (from get_great_people output)

    Costs faith. The next Great Person in that class becomes available.
    Use when you don't want the current GP and want to save points for a better one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "reject_great_person",
        {"id": individual_id},
        lambda: gs.reject_great_person(individual_id),
    )


# ---------------------------------------------------------------------------
# World Congress
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_world_congress(ctx: Context) -> str:
    """Get World Congress status, active resolutions, and voting options.

    Shows whether congress is in session, resolutions to vote on (with options A/B
    and possible targets), turns until next session, and your diplomatic favor.
    When in session, use queue_wc_votes to register votes before end_turn.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_world_congress()
        return nr.narrate_world_congress(status)

    return await _logged(ctx, "get_world_congress", {}, _run)


@mcp.tool()
async def queue_wc_votes(ctx: Context, votes: str) -> str:
    """Pre-configure World Congress votes for the upcoming session.

    Args:
        votes: JSON array of vote objects, e.g.
            '[{"hash": -513644209, "option": 1, "target": 2, "votes": 5}]'
            hash = resolution type hash (from get_world_congress)
            option = 1 for A, 2 for B
            target = player ID for PlayerType resolutions (from get_world_congress
                     target list, e.g. [target=2] Portugal), or target value for
                     non-player resolutions. The handler resolves to the correct
                     0-based index at runtime.
            votes = max votes to allocate (will use as many as favor allows)

    Call this BEFORE end_turn when get_world_congress shows 0 turns until next
    session. Registers an event handler that fires during WC processing and
    casts your votes with the specified preferences.

    If you don't call this, end_turn will pause at the World Congress session
    and return control to you for interactive voting.
    """
    gs = _get_game(ctx)
    vote_list = json.loads(votes)

    async def _run():
        return await gs.queue_wc_votes(vote_list)

    return await _logged(ctx, "queue_wc_votes", {"votes": vote_list}, _run)


# ---------------------------------------------------------------------------
# Victory progress
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_victory_progress(ctx: Context) -> str:
    """Get victory condition progress for all civilizations.

    Shows progress toward Science, Domination, Culture, Religious,
    Diplomatic, and Score victories. Includes space race VP, diplomatic VP,
    tourism vs domestic tourists, religion spread, capital ownership,
    and military strength. Call every 20-30 turns to track the race.
    """
    gs = _get_game(ctx)

    async def _run():
        vp = await gs.get_victory_progress()
        return nr.narrate_victory_progress(vp)

    return await _logged(ctx, "get_victory_progress", {}, _run)


# ---------------------------------------------------------------------------
# Religion status
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_religion_spread(ctx: Context) -> str:
    """Get per-city religion breakdown across all visible cities.

    Shows which religion is majority in each city, follower counts,
    and which religions are closest to religious victory.
    """
    gs = _get_game(ctx)

    async def _run():
        rs = await gs.get_religion_status()
        return nr.narrate_religion_status(rs)

    return await _logged(ctx, "get_religion_spread", {}, _run)


# ---------------------------------------------------------------------------
# City yield focus
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_city_focus(ctx: Context, city_id: int, focus: str) -> str:
    """Set a city's citizen yield priority.

    Args:
        city_id: City ID
        focus: One of: food, production, gold, science, culture, faith, default
               'default' clears all focus settings.

    Cities automatically assign citizens to tiles. This biases the AI
    toward the chosen yield type when assigning new citizens.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_city_focus",
        {"city_id": city_id, "focus": focus},
        lambda: gs.set_city_focus(city_id, focus),
    )


# ---------------------------------------------------------------------------
# Utility tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def dismiss_popup(ctx: Context) -> str:
    """Dismiss any blocking popup in the game UI.

    Call this if you suspect a popup (e.g. historic moment, boost notification)
    is blocking interaction.
    """
    gs = _get_game(ctx)
    return await _logged(ctx, "dismiss_popup", {}, gs.dismiss_popup)


@mcp.tool(annotations={"destructiveHint": True})
async def run_lua(ctx: Context, code: str, context: str = "gamecore") -> str:
    """Run arbitrary Lua code in the game. Advanced escape hatch — prefer built-in tools.

    Args:
        code: Lua code to execute. Use print() for output, end with print("---END---").
        context: "gamecore" (default) for read-only state queries.
                 "ingame" for commands and UI-dependent queries.

    Context differences:
      gamecore: Players[], GameInfo.*, Map.*, Game.* — safe read-only access.
                CANNOT use: UI.*, UnitManager.*, CityManager.*, notifications.
      ingame:   All APIs including UI.*, UnitManager.*, CityManager.*.
                Use for: moving units, setting research, diplomacy actions.

    Always use print() for output (not return).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "run_lua", {"context": context}, lambda: gs.execute_lua(code, context)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def list_lua_states(ctx: Context) -> str:
    """List ALL available Lua state indices and their names."""
    gs = _get_game(ctx)
    await gs.conn.ensure_connected()
    states = gs.conn.lua_states
    out = [f"{idx}: {name}" for idx, name in sorted(states.items())]
    return "\n".join(out) if out else "No states"


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def list_saves(ctx: Context) -> str:
    """List available save files (normal, autosave).

    Returns indexed list of saves. Use load_save(save_index=N) to load one.
    Call this before load_save to see what's available.
    """
    gs = _get_game(ctx)
    return await _logged(ctx, "list_saves", {}, gs.list_saves)


@mcp.tool(annotations={"destructiveHint": True})
async def load_save(ctx: Context, save_index: int) -> str:
    """Load a save file by index from the most recent list_saves() result.

    Args:
        save_index: Index number from list_saves output (1-based)

    The game will reload entirely. Wait ~10 seconds after calling this,
    then use get_game_overview to verify the loaded state.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "load_save", {"save_index": save_index}, lambda: gs.load_save(save_index)
    )


# 恢复类工具统一贴的提醒(P2.3, 2026-08-05):T335 因为没有这一步,
# 同一回合的操作重做了 3 遍 —— 预存档已含操作完成态,盲目重做反而超做。
_POST_LOAD_DIARY_HINT = (
    "\nAfter the load settles: run get_diary + get_game_overview FIRST to "
    "check which operations are already in this save — only redo what is "
    "actually missing. (0_MCP_ saves are written right before the AI turn "
    "starts, so they usually already contain the turn's completed orders.)"
)


@mcp.tool(annotations={"destructiveHint": True})
async def load_game_save(ctx: Context, save_name: str) -> str:
    """Load a save file by name. No need to call list_saves first.

    Args:
        save_name: Save name without extension (e.g. "0_MCP_0079",
                   "0A_GROUND_CONTROL", "AutoSave_0221", "quicksave").

    Tries Lua-based loading first (fast, ~5s). If the save isn't found
    via Lua (common for autosaves/quicksaves), falls back to OCR menu
    navigation (~90s) after verifying the file exists on disk.
    """
    gs = _get_game(ctx)

    async def _load_with_hint() -> str:
        r = await gs.load_game_save(save_name)
        if r.startswith(("Error", "ERR", "FAILED")):
            return r
        return r + _POST_LOAD_DIARY_HINT

    return await _logged(
        ctx,
        "load_game_save",
        {"save_name": save_name},
        _load_with_hint,
    )


# ---------------------------------------------------------------------------
# Game Lifecycle (kill / launch / load from menu)
# ---------------------------------------------------------------------------
# These tools do NOT require a FireTuner connection — they manage the game
# process itself. Hardcoded to Civ 6 only (no arbitrary system commands).


@mcp.tool(annotations={"destructiveHint": True})
async def kill_game(ctx: Context) -> str:
    """Kill the Civ 6 game process and wait for Steam to deregister.

    Only kills Civ 6 processes. Waits ~10 seconds for Steam to deregister
    so the game can be relaunched cleanly.
    """
    return await game_launcher.kill_game()


@mcp.tool(annotations={"destructiveHint": True})
async def launch_game(ctx: Context) -> str:
    """Launch Civ 6 via Steam.

    Starts the game and waits for the process to appear (~15-30 seconds).
    The game will be at the main menu after launch — use load_save or
    restart_and_load to load a specific save.

    NOTE: FireTuner connection is NOT available at the main menu.
    Only in-game MCP tools work after a save is loaded.
    """
    return await game_launcher.launch_game()


@mcp.tool(annotations={"destructiveHint": True})
async def load_save_from_menu(ctx: Context, save_name: str | None = None) -> str:
    """Navigate the main menu to load a save via OCR-guided clicking.

    Args:
        save_name: Autosave name (e.g. "AutoSave_0221"). If not provided,
                   loads the most recent autosave.

    Requires the game to be running and at the main menu. Uses macOS Vision
    OCR to find and click menu elements. Takes 30-90 seconds.

    After loading, wait ~10 seconds then call get_game_overview to verify.

    Requires pyobjc: uv pip install 'civ6-mcp[launcher]'
    """
    result = await game_launcher.load_save_from_menu(save_name)
    if result.startswith(("FAILED", "Error", "No autosaves")):
        return result
    # 菜单读档不走 gs.load_game_save,in-flight 旗标要在这里清(审查 F1)
    try:
        _get_game(ctx).clear_pending_end_turn()
    except Exception:
        pass
    return result + _POST_LOAD_DIARY_HINT


@mcp.tool(annotations={"destructiveHint": True})
async def restart_and_load(ctx: Context, save_name: str | None = None) -> str:
    """Full game recovery: kill, relaunch, and load a save.

    Args:
        save_name: Autosave name (e.g. "AutoSave_0221"). If not provided,
                   loads the most recent autosave.

    This is the recommended tool for recovering from game hangs (e.g. AI turn
    processing stuck in infinite loop). Takes 60-120 seconds total:
    1. Kills the game process
    2. Waits for Steam to deregister (~10s)
    3. Relaunches via Steam (~15-30s for process start + main menu)
    4. Navigates menus via OCR to load the save (~30-60s)

    After completion, wait ~10 seconds then call get_game_overview to verify.
    """
    gs = _get_game(ctx)
    identity_before = gs._game_identity

    result = await game_launcher.restart_and_load(save_name)

    # Reconnect and verify correct game loaded
    conn = gs.conn
    for attempt in range(30):
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None:
                break
        except ConnectionError:
            pass
        await asyncio.sleep(1)

    if conn.gamecore_index is not None and identity_before is not None:
        try:
            actual = await gs.get_game_identity()
            if actual != identity_before:
                log.warning(
                    "restart_and_load: wrong game loaded "
                    "(expected %s, got %s) — retrying",
                    identity_before,
                    actual,
                )
                result2 = await game_launcher.restart_and_load(save_name)
                for attempt in range(30):
                    try:
                        await conn.reconnect()
                        if conn.gamecore_index is not None:
                            break
                    except ConnectionError:
                        pass
                    await asyncio.sleep(1)
                try:
                    actual2 = await gs.get_game_identity()
                    if actual2 != identity_before:
                        return (
                            f"{result2} | WARNING: Wrong game loaded "
                            f"(expected {identity_before[0]}, "
                            f"got {actual2[0]}). Manual recovery needed."
                        )
                except Exception:
                    pass
                return (
                    f"{result2} | Reloaded after wrong-game detection."
                    f"{_POST_LOAD_DIARY_HINT}"
                )
        except Exception:
            log.debug("Post-load identity check failed", exc_info=True)

    if "ABORTED" in result or "FAILED" in result:
        return result
    gs.clear_pending_end_turn()  # 重启+读档后 in-flight 旗标必然失效(审查 F1)
    return result + _POST_LOAD_DIARY_HINT


async def _narrate(
    query_fn: Callable[[], Awaitable[Any]], narrate_fn: Callable[..., str]
) -> str:
    """Helper: call a query function then narrate the result."""
    data = await query_fn()
    return narrate_fn(data)


#
# --- Strategic Memory Tools ---
# Called by the LLM to maintain long-term strategic context
# across turns, compensating for context window limitations.
#


@mcp.tool(annotations={"readOnlyHint": False})
async def write_strategic_summary(
    ctx: Context,
    turn: int,
    summary: str,
    goals: str = "",
    threats: str = "",
    opportunities: str = "",
    key_decisions: str = "",
) -> str:
    """Write a strategic summary for long-term memory.

    Call this every 5-10 turns to compress recent history into a compact
    strategic context. This persists to disk and survives session restarts.

    Args:
        turn: Current turn number.
        summary: 2-3 sentences summarizing your empire state and situation.
        goals: Current long-term goals (victory target, expansion plans).
        threats: Major threats (hostile neighbors, barbarians, resource gaps).
        opportunities: Current opportunities (weak neighbors, settle spots).
        key_decisions: Important recent decisions and their rationale.

    Returns:
        Confirmation string.
    """
    gs = _get_game(ctx)
    game_id = ""
    try:
        civ, seed = await gs.get_game_identity()
        game_id = f"{civ}_{seed}"
    except Exception:
        pass  # 拿不到身份就落全局文件 —— 比丢内容强
    return strategic_memory.write_summary(
        turn=turn,
        summary=summary,
        goals=goals,
        threats=threats,
        opportunities=opportunities,
        key_decisions=key_decisions,
        game_id=game_id,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def read_strategic_summary(ctx: Context) -> str:
    """Read the current strategic summary from long-term memory.

    Call this at the start of each turn cycle to re-establish strategic
    context that may have been lost from the conversation window.
    Returns goals, threats, opportunities, recent decisions, and
    a compressed history of key moments. Summaries are stored per game —
    a new game starts with a clean slate (no cross-game contamination).
    """
    gs = _get_game(ctx)
    game_id = ""
    try:
        civ, seed = await gs.get_game_identity()
        game_id = f"{civ}_{seed}"
    except Exception:
        pass
    return strategic_memory.read_summary(game_id=game_id)


@mcp.tool(annotations={"readOnlyHint": False})
async def record_learning(
    ctx: Context,
    lesson: str = "",
    result: str = "",
    standing_insight: str = "",
) -> str:
    """把本局教训写进跨局学习库(下局 T1-T3 简报自动注入)。

    局末必调:lesson 写「这局最该被记住的一件事」(一句话,可操作);
    result 写结果,如「失败(T384 美国文化胜利)」。事实字段(损失/探索率/
    红警回合)由服务端从复盘日志自动提取,不用你报。
    standing_insight 单独一层:跨局恒真的规则性教训(如「原版规则集下
    法典完成前不能排政策卡」),不随局淘汰 —— 只在确认是规则而非局势时用。
    局中也可以调(比如刚被教训过),重调同一局会覆盖而非重复。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import learning_store

        try:
            civ, seed = await gs.get_game_identity()
        except Exception:
            # 终局后游戏可能已关 —— 恒真规则不该因此写不进去
            if standing_insight.strip():
                store = learning_store.default_store()
                if store.add_insight(standing_insight.strip()):
                    store.save()
                    return "游戏已断开,本局教训无法定位;跨局规则已入库。"
                return "游戏已断开,且该跨局规则已存在 —— 未写入任何内容。"
            return "游戏已断开,拿不到本局标识 —— lesson 未入库。"
        return learning_store.record_manual_lesson(
            civ,
            seed,
            lesson=lesson,
            result=result,
            standing_insight=standing_insight,
        )

    return await _logged(ctx, "record_learning", {"lesson": lesson[:60]}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_learnings(ctx: Context) -> str:
    """查看跨局学习库:往局结果/教训/致命问题 + 跨局恒真规则。

    开局定策略、或怀疑自己在重蹈往局覆辙时调。简报只注入最近 3 局的
    一句话版本,这里是完整视图。"""

    async def _run():
        from civ_mcp import learning_store

        return learning_store.render_full_history()

    return await _logged(ctx, "get_learnings", {}, _run)


# ---------------------------------------------------------------------------
# 专家决策工具 —— 2026-08-01 wiring pass
#
# 简报注入覆盖"每回合该知道的";这些工具覆盖"做重大决策时该问的"。
# 换任何 AI 接上来,docstring 就是它的说明书:参数从哪来、缺了怎么降级。
# ---------------------------------------------------------------------------


def _tiles_as_dicts(tiles) -> list[dict]:
    return [
        {
            "x": t.x,
            "y": t.y,
            "terrain": t.terrain,
            "feature": t.feature,
            "resource": t.resource,
            "resource_class": getattr(t, "resource_class", ""),
            "is_hills": t.is_hills,
            "is_river": t.is_river,
            "is_coastal": t.is_coastal,
            "improvement": t.improvement,
            "district": getattr(t, "district", ""),
            "is_pillaged": getattr(t, "is_pillaged", False),
        }
        for t in tiles or []
    ]


async def _merge_live_city_defense(gs, city_x: int, city_y: int, target: dict) -> dict:
    """把引擎实读的城防覆盖进 target。

    2026-08-10 伦敦围城:城防原本靠调用方手填 —— 手填等于永远不填,于是
    评估永远在"城防未知"下照常发绿灯。现在直接问引擎;读不到才降级,
    调用方显式传的值仍然优先(侦察情报可能比当前视野新)。
    """
    reader = getattr(gs, "get_enemy_city_defense", None)
    if reader is None:
        return target
    try:
        live = await reader(city_x, city_y)
    except Exception:
        log.debug("城防实读失败,评估降级为城防未知", exc_info=True)
        live = None
    if not live:
        return target
    merged = dict(target)
    for key in (
        "name",
        "population",
        "defense_strength",
        "garrison_hp",
        "garrison_max_hp",
        "wall_hp",
        "wall_max_hp",
    ):
        if live.get(key) and not merged.get(key):
            merged[key] = live[key]
    return merged


_LAND_COMBAT_HINTS = (
    "SETTLER",
    "BUILDER",
    "TRADER",
    "MISSIONARY",
    "APOSTLE",
    "INQUISITOR",
    "GREAT_",
    "SPY",
    "NAVAL",
    "GALLEY",
    "QUADRIREME",
    "FRIGATE",
    "IRONCLAD",
    "DESTROYER",
    "SUBMARINE",
    "CARRIER",
    "CARAVEL",
    "BATTLESHIP",
    "CRUISER",
)


async def _land_reach_from_army(gs, units, city_x: int, city_y: int) -> bool | None:
    """陆军走不走得到这座城的相邻格?(缺陷105)

    从【离目标最近的己方陆战单位】起 BFS。用陆战单位而不是城市当起点,是因为
    问的正是"我的兵能不能开过去";城市可能在另一块地上。测不出来就返回 None,
    宁可不判也不误判 —— 误判成不可达会挡掉一场本来能打的仗。
    """
    from civ_mcp import lua as lq

    best = None
    for u in units:
        ut = str(getattr(u, "unit_type", "") or "").upper()
        if any(h in ut for h in _LAND_COMBAT_HINTS):
            continue
        try:
            d = hex_distance(u.x, u.y, city_x, city_y)
        except (AttributeError, TypeError, ValueError):
            continue
        if best is None or d < best[0]:
            best = (d, u.x, u.y)
    if best is None:
        return None
    try:
        lines = await gs.conn.execute_read(
            lq.build_land_reach_query(best[1], best[2], city_x, city_y)
        )
        return lq.parse_land_reach(lines)
    except Exception:
        return None


async def _war_game_state(gs, city_x: int, city_y: int, target: dict) -> dict:
    """assess_attack / suggest_siege_plan 共用的数据组装。"""
    target = await _merge_live_city_defense(gs, city_x, city_y, target)
    units = await gs.get_units()
    threats = await gs.get_threat_scan()
    cities, _ = await gs.get_cities()
    cache = getattr(gs, "_expert_cache", {}) or {}
    return {
        "target": target,
        "land_reachable": await _land_reach_from_army(gs, units, city_x, city_y),
        "my_units": [{"type": u.unit_type, "x": u.x, "y": u.y} for u in units],
        "enemy_units": [
            {
                "unit_type": t.unit_type,
                "x": t.x,
                "y": t.y,
                "cs": max(t.combat_strength, t.ranged_strength),
            }
            for t in threats
            if hex_distance(t.x, t.y, city_x, city_y) <= 5
        ],
        "my_cities": [{"x": c.x, "y": c.y} for c in cities],
        "my_techs": set(cache.get("techs") or ()),
    }


@mcp.tool(annotations={"readOnlyHint": True})
async def assess_attack(
    ctx: Context,
    city_x: int,
    city_y: int,
    city_name: str = "",
    population: int = 0,
    defense_strength: int = 0,
    wall_hp: int = -1,
    wall_max_hp: int = -1,
) -> str:
    """战前评估:这座敌城能不能打。四档判定(可打/需准备/不推荐/会全灭)+理由+替代方案。

    城防值/城血/城墙**自动向引擎实读**(城在视野内即可),连同我方兵力、
    守军、补给线一起算出"每回合净伤"和破城回合数:净伤 ≤ 城市回血(20/回合)
    直接判 🔴——这正是罗马局 T177-191 伦敦的情形(CS55 火枪手打城防 80,
    每击 10-15 伤,城市回满)。城不在视野时城防读不到,此时【不会给绿灯】。
    参数只在你有更新的侦察情报时才需要显式传(显式值优先)。宣战前必调。
    """
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import war_planner

        target = {
            "name": city_name,
            "population": population or None,
            "defense_strength": defense_strength or None,
        }
        if wall_max_hp >= 0:
            target["wall_hp"], target["wall_max_hp"] = max(0, wall_hp), wall_max_hp
        state = await _war_game_state(gs, city_x, city_y, target)
        out = war_planner.assess_attack(city_x, city_y, state)
        return "\n".join(out) if out else "评估无输出——检查坐标与我方单位数据。"

    return await _logged(
        ctx, "assess_attack", {"city_x": city_x, "city_y": city_y}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def suggest_siege_plan(
    ctx: Context,
    city_x: int,
    city_y: int,
    wall_hp: int = -1,
    wall_max_hp: int = -1,
) -> str:
    """攻城计划:3-5 步战术模板(站位→削墙→收尾→占领)。仅当战前评估为
    可打/需准备时给计划;不推荐/会全灭时会拒绝并说明先补什么条件。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import war_planner

        target: dict = {}
        if wall_max_hp >= 0:
            target = {"wall_hp": max(0, wall_hp), "wall_max_hp": wall_max_hp}
        state = await _war_game_state(gs, city_x, city_y, target)
        out = war_planner.suggest_siege_plan(city_x, city_y, state)
        return "\n".join(out) if out else "无计划输出。"

    return await _logged(
        ctx, "suggest_siege_plan", {"city_x": city_x, "city_y": city_y}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_post_war_advice(ctx: Context, city_x: int, city_y: int) -> str:
    """占领城市后的处理建议:保留/挽救/夷平,基于忠诚度计算。占城当回合调用。

    ⚠️ 敌方城市的人口数据无法自动获取 —— 忠诚估算只计入我方压力,结果
    偏乐观;若周边敌城密集,把建议降一档理解(保留→挽救,挽救→夷平)。
    """
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import war_planner

        cities, _ = await gs.get_cities()
        own = [
            {"x": c.x, "y": c.y, "population": c.population}
            for c in cities
            if not (c.x == city_x and c.y == city_y)
        ]
        out = war_planner.post_war_advice(
            city_x, city_y, {"own_cities": own, "enemy_cities": []}
        )
        return "\n".join(out) if out else "无输出。"

    return await _logged(
        ctx, "get_post_war_advice", {"city_x": city_x, "city_y": city_y}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pillage_advice(
    ctx: Context, center_x: int, center_y: int, radius: int = 3
) -> str:
    """掠夺经济:对指定区域内的敌方改良/区域估价排序,给掠夺路线。
    战争期间对敌方腹地调用;矿山/区域优先,已掠夺过的格子自动排除。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import war_planner

        tiles = await gs.get_map_area(center_x, center_y, radius)
        units = await gs.get_units()
        enemy_tiles = [
            t
            for t in _tiles_as_dicts(tiles)
            if t.get("improvement") or t.get("district")
        ]
        raiders = [{"type": u.unit_type, "x": u.x, "y": u.y} for u in units]
        out = war_planner.get_pillage_advice(enemy_tiles, raiders)
        return (
            "\n".join(out) if out else "区域内没有可掠夺目标(或没有可执行掠夺的单位)。"
        )

    return await _logged(
        ctx, "get_pillage_advice", {"center_x": center_x, "center_y": center_y}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def assess_loyalty_flip(
    ctx: Context,
    city_x: int,
    city_y: int,
    city_population: int,
    city_name: str = "",
) -> str:
    """忠诚翻转评估:敌方城市能不能不费一兵一卒翻过来。
    city_population 从你的侦察读(城市横幅显示人口)。
    ⚠️ 仅在本规则集有忠诚度机制时可用 —— 忠诚度是 Rise & Fall 的机制,
    原版(RULESET_STANDARD)上不存在,此时本工具会明确拒答而不是给你
    一个凭空算出来的数。
    ⚠️ 敌方其他城市的支撑压力无法自动获取,估算偏乐观 —— 目标城离敌方
    腹地越近,实际越难翻。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import world_model as wmod

        # 规则集探测优先:此前无条件用自算公式输出忠诚度建议,而原版
        # 根本没有这个机制(2026-08-12 罗马 T384:模型据此写下「忠诚净
        # +29.2 判定保留」并做了决策)。建议一个不存在的机制比沉默更糟。
        if await gs.has_loyalty_mechanic() is False:
            return (
                "本规则集(原版 RULESET_STANDARD)没有忠诚度机制 —— "
                "城市不会因忠诚度叛变或翻转,这条路子不存在。\n"
                "占领城市后真正的约束是【宜居度】与【厌战值】:占领城会拉低"
                "全局宜居,厌战值每 400 点扣 1 点宜居。对策是娱乐区/奢侈品/"
                "宣传·戒严政策卡,以及尽早谈和(和平一次清 2000 厌战值)。"
            )
        cities, _ = await gs.get_cities()
        mine = [{"x": c.x, "y": c.y, "population": c.population} for c in cities]
        state = {}
        try:
            st = await gs.get_governors()
            state["has_amani"] = any(
                "AMANI" in (getattr(g, "governor_type", "") or "")
                for g in getattr(st, "appointed", []) or []
            )
        except Exception:
            pass
        try:
            spies = await gs.get_spies()
            state["spy_available"] = bool(spies)
        except Exception:
            pass
        out = wmod.assess_loyalty_flip(
            {
                "name": city_name or f"({city_x},{city_y})",
                "x": city_x,
                "y": city_y,
                "population": city_population,
            },
            my_cities=mine,
            enemy_cities=[],
            game_state=state,
        )
        return "\n".join(out) if out else "无输出。"

    return await _logged(
        ctx, "assess_loyalty_flip", {"city_x": city_x, "city_y": city_y}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def assess_settle_location(ctx: Context, x: int, y: int) -> str:
    """铺城位置六维深评(产出之外):战略资源/奢侈覆盖/防御地形/扼守/
    忠诚度/卡位。对 get_settle_advisor 给出的候选点逐个调用,再定最终落点。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import world_model as wmod

        tiles = await gs.get_map_area(x, y, 3)
        cities, _ = await gs.get_cities()
        mine = [{"x": c.x, "y": c.y, "population": c.population} for c in cities]
        out = wmod.evaluate_settle_location(
            x, y, _tiles_as_dicts(tiles), own_cities=mine, enemy_cities=[]
        )
        return (
            "\n".join(out)
            if out
            else "该位置没有超出产出评分之外的战略维度(或数据不足)。"
        )

    return await _logged(ctx, "assess_settle_location", {"x": x, "y": y}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_opening_assessment(ctx: Context) -> str:
    """开局四维评估:文明能力利用率 × 地图地理 × 邻居威胁 → 推荐路线
    (含备选与不推荐,带置信度)。T1-T5 必调一次;之后想复审路线也可再调。
    未收录进策略库的文明会得到诚实的降级评估,不会编造能力。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import opening_assessor

        ov = await gs.get_game_overview()
        cities, _ = await gs.get_cities()
        cap = cities[0] if cities else None
        tiles: list = []
        if cap is not None:
            try:
                tiles = await gs.get_map_area(cap.x, cap.y, 5)
            except Exception:
                tiles = []
        neighbors = []
        try:
            civs = await gs.get_diplomacy()
            for c in civs:
                if not getattr(c, "has_met", False):
                    continue
                neighbors.append(
                    {
                        "civ_name": getattr(c, "civ_name", "?"),
                        "leader_name": getattr(c, "leader_name", "?"),
                        "military_strength": getattr(c, "military_strength", 0),
                        "agendas": [
                            getattr(a, "name", "")
                            for a in getattr(c, "agendas", []) or []
                        ],
                    }
                )
        except Exception:
            pass
        wm = await gs.ensure_world()
        state = {
            "civ": getattr(ov, "civ_name", ""),
            "leader": getattr(ov, "leader_name", ""),
            "difficulty": getattr(ov, "difficulty", ""),
            "map_type": getattr(wm, "map_type", ""),
            "turn": getattr(ov, "turn", 0),
            "capital": {"x": cap.x, "y": cap.y} if cap else None,
            "explored_tiles": _tiles_as_dicts(tiles),
            "neighbors": neighbors,
            "my_military": getattr(wm, "military_strength", 0)
            or getattr(wm, "military_count", 0),
        }
        out = opening_assessor.assess_opening(state)
        return out or "评估器无输出——用 get_strategy_notes 直接查文明策略库。"

    return await _logged(ctx, "get_opening_assessment", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_strategy_notes(
    ctx: Context, civ_or_leader: str = "", map_type: str = ""
) -> str:
    """查本文明套件:特色单位/战力代差/窗口开关时代 + 策略库原理(若收录)。
    civ_or_leader 留空 = 查自己的文明。也可查对手文明来理解他会怎么打。

    主路径是【运行时从 gamedata 推导】,所以任意随机文明都有输出;
    手写策略库(3 个文明)只作为覆盖层追加。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import civ_kit, strategy_space

        who = civ_or_leader
        mt = map_type
        civ_type = ""
        pairs: dict[str, str] = {}
        researched: set[str] = set()
        if not who:
            try:
                ov = await gs.get_game_overview()
                who = getattr(ov, "leader_name", "") or getattr(ov, "civ_name", "")
            except Exception:
                pass
            try:
                civ_type, _seed = await gs.get_game_identity()
            except Exception:
                pass
            try:
                wm = await gs.ensure_world()
                pairs = dict(getattr(wm, "my_unique_units", None) or {})
                # 已研科技决定 UU 显示「✅ 已解锁」还是「⏳ 需 X」。
                # 2026-08-03 实机:T110 场上已有罗马军团,却显示
                # 「⏳ 需 IRON_WORKING」—— 就是这里没传。
                researched = set(getattr(wm, "researched_techs", None) or ())
            except Exception:
                pass
        else:
            civ_type = who
        if not mt:
            try:
                wm = await gs.ensure_world()
                mt = getattr(wm, "map_type", "")
            except Exception:
                pass

        parts: list[str] = []
        # L2:运行时推导 —— 随机文明的主路径,永远有输出
        try:
            kit_text = civ_kit.render(
                civ_kit.derive(civ_type=civ_type, unique_units=pairs or None),
                researched=researched or None,
            )
            if kit_text:
                parts.append(kit_text)
        except Exception:
            pass
        # 手写策略库:只收录 3 个文明,作为覆盖层追加(判断性内容,推不出来的)
        overlay = strategy_space.get_strategy(who, mt)
        if overlay:
            parts.append(overlay)
        elif parts:
            parts.append(
                f"(手写策略库未收录「{who}」—— 上面的套件是运行时从 gamedata "
                f"推导的硬事实,判断性内容请结合 get_opening_assessment 与实测局势)"
            )
        return "\n\n".join(parts) or (
            f"查不到「{who}」的套件与策略。按通用原则打:先探图、按地形选专精、"
            f"用 get_opening_assessment 做本局评估。"
        )

    return await _logged(
        ctx, "get_strategy_notes", {"civ_or_leader": civ_or_leader}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_expert_playbook(ctx: Context, topic: str = "", turn: int = 0) -> str:
    """查高玩知识库:人类社区共识的通用打法原理(文明无关)。

    与 get_strategy_notes 互补:那边是本文明的套件与原理,这边是换任何
    文明都成立的判断力 —— 扩张节奏基准/蛮族机制/宣战窗口/难度加成表/
    胜利路线检查表。每条带置信度与来源(规则机制/社区共识/本机实证)。
    无参调用返回目录;topic 按主题查;turn 传当前回合可过滤掉不在
    适用窗口的条目。定线、开战、扩张这类重大决策前值得对一遍。"""

    async def _run():
        from civ_mcp import expert_playbook

        return expert_playbook.get_playbook(topic=topic, turn=turn or None)

    return await _logged(
        ctx, "get_expert_playbook", {"topic": topic, "turn": turn}, _run
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_power_balance(ctx: Context) -> str:
    """实测我方与对手的差距,给出五档局势判定(被碾压/落后/势均力敌/领先/统治级)。

    不看难度名 —— 难度加成表在导出数据里是空的,而且难度可能随机。
    判据是各维度「我 / 该维最强对手」的实测比值。这是决定整套打法的分档:
    劣势档要停扩张求生,领先档要主动兑现优势。每 10 回合简报里会自动带一行,
    本工具用于随时重测。"""
    gs = _get_game(ctx)

    async def _run():
        from civ_mcp import end_turn as et
        from civ_mcp import power_balance as pb
        from civ_mcp import world_model as wm_mod

        wm = await gs.ensure_world()
        lines = await gs.conn.execute_write(wm_mod.build_world_scan_query())
        scan = wm_mod.parse_world_scan(lines)
        turn = gs._high_water_turn
        try:
            ov = await gs.get_game_overview()
            turn = ov.turn
        except Exception:
            pass
        # 强制重测(绕过 10 回合缓存):调这个工具就是想要现在的数字
        et._expert_cache(gs).pop("power_snapshot", None)
        snap = await et.measure_power(gs, wm, scan, turn)
        if snap is None:
            return "局势测量失败:对手快照查询没有返回数据(可能还没遇到任何文明)。"
        hist = et._expert_cache(gs).get("power_history")
        return pb.render(snap, hist)

    return await _logged(ctx, "get_power_balance", {}, _run)


def main():
    """Entry point for the MCP server."""
    import signal

    logging.basicConfig(level=logging.INFO)

    # Remap SIGTERM → SIGINT so asyncio's existing SIGINT handler triggers a
    # graceful shutdown (cancels all tasks → lifespan finally block runs →
    # conn.disconnect() closes the FireTuner TCP connection cleanly).
    # Without this, SIGTERM kills the process immediately, leaving the game
    # with an abrupt TCP RST which can cause it to crash.
    # SIGTERM is not available on Windows, so skip the remap there.
    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM, lambda sig, frame: os.kill(os.getpid(), signal.SIGINT)
        )

    if os.environ.get("CIV_MCP_DISABLE_LUA"):
        mcp._tool_manager.remove_tool("run_lua")

    # 精简工具集(2026-08-05)。本服务器注册 93 个工具,加上宿主自带的
    # 内置工具,一个会话要向模型 API 声明 110+ 个工具定义(schema 约 73KB)。
    # 部分宿主/模型 provider 对工具数量或 schema 体积有上限,超了会**整批
    # 丢弃 MCP 工具**且不报错 —— 表现就是"服务器启动正常、ListTools 也
    # 应答了,但会话里一个工具都看不到"(Codex 桌面端实测到这个形态)。
    # 设 CIV_MCP_CORE_TOOLS=1 只保留下面这组打完一局必需的工具,
    # 用来区分"工具太多被丢弃"和"宿主根本没接上"。
    if os.environ.get("CIV_MCP_CORE_TOOLS"):
        keep = {
            # 局面读取
            "get_game_overview",
            "get_units",
            "get_cities",
            "get_map_area",
            "get_city_production",
            "get_tech_civics",
            "get_policies",
            "get_diplomacy",
            "get_notifications",
            "get_builder_tasks",
            "get_great_people",
            "get_governors",
            "get_victory_progress",
            "get_power_balance",
            "get_diary",
            # 行动
            "unit_action",
            "set_city_production",
            "set_research",
            "set_policies",
            "purchase_item",
            "recruit_great_person",
            "appoint_governor",
            "assign_governor",
            "end_turn",
            "dismiss_popup",
            "skip_remaining_units",
        }
        all_tools = list(mcp._tool_manager._tools.keys())
        for name in all_tools:
            if name not in keep:
                mcp._tool_manager.remove_tool(name)
        log.info(
            "CIV_MCP_CORE_TOOLS: %d -> %d tools",
            len(all_tools),
            len(mcp._tool_manager._tools),
        )

    mcp.run(transport=os.environ.get("CIV_MCP_TRANSPORT", "stdio"))
