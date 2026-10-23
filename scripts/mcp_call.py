"""Stdio MCP client for civ6-mcp.

Reads JSON lines from stdin: {"tool": "get_units", "args": {}}
Writes JSON lines to stdout: {"ok": true, "result": ...} or {"ok": false, "error": ...}

Usage:  python scripts/mcp_call.py  < input.jsonl

Connection failures are reported without killing other clients or replaying requests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _connection_failed(text: str) -> bool:
    return text.startswith((
        "Cannot connect to Civ 6", "TUNER_BUSY:", "FireTuner connection lost:",
        "GameCore_Tuner/InGame states not found", "[WinError",
    ))


async def run_once(lines: list[str]) -> int:
    params = StdioServerParameters(
        command=PYTHON,
        args=["-m", "civ_mcp"],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
        },
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(json.dumps({"ok": False, "error": f"bad json: {exc}"}))
                        continue
                    tool = req.get("tool")
                    args = req.get("args") or {}
                    try:
                        result = await session.call_tool(tool, args)
                        out = {"ok": True, "tool": tool}
                        text = _text(result)
                        if result.isError or _connection_failed(text):
                            out["ok"] = False
                            out["error"] = text
                        else:
                            out["result"] = text
                        if _connection_failed(text):
                            # Stop the batch; preceding actions may have succeeded.
                            print(json.dumps(out, ensure_ascii=False))
                            return 1
                        print(json.dumps(out, ensure_ascii=False))
                    except Exception as exc:  # noqa: BLE001 - client boundary
                        msg = f"{type(exc).__name__}: {exc}"
                        if "Cannot connect to Civ 6" in msg or "competing civ_mcp" in msg:
                            # FireTuner connection lost mid-batch — abort so the
                            # caller can self-heal and retry the whole batch.
                            print(
                                json.dumps(
                                    {
                                        "ok": False,
                                        "tool": tool,
                                        "error": f"CONNECT_FAILED: {msg}",
                                    },
                                    ensure_ascii=False,
                                )
                            )
                            return 1
                        print(
                            json.dumps(
                                {
                                    "ok": False,
                                    "tool": tool,
                                    "error": msg,
                                },
                                ensure_ascii=False,
                            )
                        )
                    sys.stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001 - startup/connection failure
        print(
            json.dumps(
                {"ok": False, "error": f"CONNECT_FAILED: {type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1


def _text(result) -> str:
    parts = []
    for content in result.content:
        if getattr(content, "type", None) == "text":
            parts.append(content.text)
        else:
            parts.append(str(content))
    return "\n".join(parts)


async def run() -> None:
    lines = sys.stdin.readlines()
    status = await run_once(lines)
    if status:
        raise SystemExit(status)


if __name__ == "__main__":
    asyncio.run(run())
