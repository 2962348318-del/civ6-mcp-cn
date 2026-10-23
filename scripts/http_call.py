"""Call the shared local MCP server without spawning another tuner client.

Usage: python scripts/http_call.py get_game_overview
       python scripts/http_call.py get_map_area --args-json '{"center_x":10,"center_y":20}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def invoke(url: str, tool: str, args: dict) -> dict:
    async with (
        streamable_http_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool, args)
        text = "\n".join(block.text for block in result.content if block.type == "text")
        failed = result.isError or text.startswith((
            "Error:", "Cannot connect to Civ 6", "TUNER_BUSY:",
            "FireTuner connection lost:", "GameCore_Tuner/InGame states not found",
        ))
        return {"ok": not failed, "tool": tool, "result": text}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool")
    parser.add_argument("--args-json", default="{}")
    parser.add_argument("--url", default="http://127.0.0.1:8123/mcp")
    args = parser.parse_args()
    result = asyncio.run(invoke(args.url, args.tool, json.loads(args.args_json)))
    print(json.dumps(result, ensure_ascii=False))
    return int(not result["ok"])


if __name__ == "__main__":
    sys.exit(main())
