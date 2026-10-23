"""以 streamable-HTTP 模式常驻运行 civ6 MCP 服务器。

    python tools/serve_http.py            # 默认 127.0.0.1:8000/mcp
    python tools/serve_http.py --port 8123
    python tools/serve_http.py --core      # 只暴露 26 个核心工具

宿主(Codex 等)按"流式 HTTP"类型连 http://127.0.0.1:<port>/mcp 即可。

为什么用它:
  · STDIO 模式下每个宿主都会自己 spawn 一个 civ_mcp 进程,而 FireTuner
    端口 4318 只接受一个客户端 —— 多个实例互抢,谁都连不稳(实测同时
    出现过 10 个进程)。HTTP 模式下服务器只有这一个,所有宿主共用它。
  · 改代码后重启这一个进程即可,不用管宿主什么时候重新 spawn。

注意:退出时(Ctrl-C)会释放 FireTuner 连接;别和 tools/driver.py 同时跑,
它们都会占 4318。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--core",
        action="store_true",
        help="只暴露核心工具(26 个),给对工具数量敏感的宿主用",
    )
    args = ap.parse_args()

    os.environ["CIV_MCP_TRANSPORT"] = "streamable-http"
    if args.core:
        os.environ["CIV_MCP_CORE_TOOLS"] = "1"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 监听地址直接写进 settings —— FastMCP 实例在 server 模块导入时就
    # 创建好了,那之后再设 FASTMCP_* 环境变量已经来不及。
    from civ_mcp.http_lifespan import share_http_lifespan
    from civ_mcp.server import main as server_main
    from civ_mcp.server import mcp

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    factory = mcp.streamable_http_app
    mcp.streamable_http_app = lambda: share_http_lifespan(factory(), mcp._mcp_server)

    print(f"civ6 MCP (streamable-http) -> http://{args.host}:{args.port}/mcp")
    print("宿主里填这个 URL,类型选「流式 HTTP」。Ctrl-C 退出。")
    server_main()


if __name__ == "__main__":
    main()
