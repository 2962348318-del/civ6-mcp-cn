"""Long-running MCP driver for civ6-mcp.

Owns the FireTuner connection via a persistent civ_mcp server subprocess and
serves tool requests from a file queue, so tool calls never race with other
Codex-hosted civ_mcp processes (which caused lost turns when end_turn was
re-executed after a reconnect).

Queue protocol (QUEUE_DIR = var/queue):
  request:  QUEUE_DIR/req_<id>.json   {"tool": ..., "args": {...}}
  response: QUEUE_DIR/resp_<id>.json  {"ok": true, "result": ...}
                                     | {"ok": false, "error": ...}

Usage: python tools/driver.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]  # tools/ 的上一级 = 项目根
QUEUE_DIR = ROOT / "var" / "queue"
PYTHON = sys.executable


def _text(result) -> str:
    parts = []
    for content in result.content:
        if getattr(content, "type", None) == "text":
            parts.append(content.text)
        else:
            parts.append(str(content))
    return "\n".join(parts)


async def _handle(session: ClientSession, req: dict) -> dict:
    tool = req.get("tool")
    args = req.get("args") or {}
    try:
        result = await session.call_tool(tool, args)
        text = _text(result)
        if result.isError or text.startswith((
            "Cannot connect to Civ 6", "TUNER_BUSY:", "FireTuner connection lost:",
            "GameCore_Tuner/InGame states not found", "[WinError",
        )):
            return {"ok": False, "tool": tool, "error": text}
        return {"ok": True, "tool": tool, "result": text}
    except Exception as exc:  # noqa: BLE001 - client boundary
        return {"ok": False, "tool": tool, "error": f"{type(exc).__name__}: {exc}"}


async def _serve(session: ClientSession) -> None:
    QUEUE_DIR.mkdir(exist_ok=True)
    # Clear leftovers from a previous driver run.
    for f in QUEUE_DIR.glob("req_*.json"):
        try:
            f.unlink()
        except OSError:
            pass
    while True:
        try:
            reqs = sorted(QUEUE_DIR.glob("req_*.json"))
            for rp in reqs:
                rid = rp.stem[len("req_") :]
                try:
                    req = json.loads(rp.read_text(encoding="utf-8"))
                    resp = await _handle(session, req)
                except Exception as exc:  # noqa: BLE001
                    resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                (QUEUE_DIR / f"resp_{rid}.json").write_text(
                    json.dumps(resp, ensure_ascii=False), encoding="utf-8"
                )
                rp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - keep serving
            pass
        await asyncio.sleep(0.25)


async def main() -> None:
    # Never evict another live client. Connection ownership is checked by the server.
    params = StdioServerParameters(
        command=PYTHON,
        args=["-m", "civ_mcp"],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("DRIVER_READY", flush=True)
            await _serve(session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"DRIVER_DIED: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
