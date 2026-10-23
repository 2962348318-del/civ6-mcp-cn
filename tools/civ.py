"""civ6 MCP 的命令行入口 —— 给拿不到 MCP 工具、只有 shell 的 agent 用。

宿主(Codex 等)挂不上 MCP 工具时,用这个代替:一条 shell 命令 = 一次工具调用。

    python tools/civ.py get_game_overview
    python tools/civ.py get_units
    python tools/civ.py unit_action --unit_id 12345 --action move --target_x 10 --target_y 20
    python tools/civ.py set_city_production --city_id 65536 --item_type UNIT --item UNIT_SETTLER
    python tools/civ.py end_turn

自省(不连游戏,随时可用):
    python tools/civ.py --list              列出全部工具名
    python tools/civ.py --list settle       只列名字含 settle 的
    python tools/civ.py --schema unit_action  看某个工具的参数与说明

参数规则:
    --key value   → args["key"] = value,自动识别 int / float / true / false
    --flag        → args["flag"] = True
    --args-json '{"x":1}'  → 直接给整个 args(需要嵌套结构时用)

连接方式(全自动,调用方什么都不用管):
    如果本地共享 HTTP 服务(127.0.0.1:8123)正在运行,优先复用它。
    第一次调用时,如果后台没有 driver,会自己把它拉起来(脱离终端常驻,
    日志写 var/driver.log),之后所有调用都复用它已经建好的游戏连接。
    所以不需要任何人事先开服务 —— 直接调工具就行。
    `--no-driver` 可以强制走一次性进程(每次多花几秒重连游戏)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Windows 控制台默认 GBK:工具输出里的 emoji/生僻字会直接
# UnicodeEncodeError(实测 strategy_notes / opening_assessment /
# assess_attack 三个工具炸过)。__main__.py 已给 MCP server 进程做过
# 同款修复 —— 但 civ.py 是独立的前台进程,得自己再做一遍。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parents[1]
QUEUE_DIR = ROOT / "var" / "queue"
PYTHON = sys.executable
# end_turn can legitimately wait ~10 min for a late-game / popup-blocked AI
# turn before resolving (the driver's own Phase 2/3 polling covers ~11 min and
# then returns a clean result — advanced, TURN_NOT_SUBMITTED, or AI_TURN_HANG).
# A 300s client timeout mistook those slow turns for a hang and killed the
# driver mid-turn. Bump to 700s so slow-but-completing turns get their result
# instead of being aborted (the driver always returns within its internal cap).
DRIVER_TIMEOUT = 700


def _coerce(text: str):
    """命令行来的都是字符串,但工具的 schema 要真类型。"""
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null" or low == "none":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_args(argv: list[str]) -> tuple[str, dict]:
    """argv → (工具名, args)。--args-json 优先于逐个 --key。"""
    if not argv:
        raise SystemExit("usage: python tools/civ.py <tool> [--key value ...]")
    tool = argv[0]
    args: dict = {}
    i = 1
    while i < len(argv):
        token = argv[i]
        if not token.startswith("--"):
            raise SystemExit(f"参数要用 --key value 形式,收到:{token}")
        key = token[2:].replace("-", "_")
        if key == "args_json":
            args = json.loads(argv[i + 1])
            i += 2
            continue
        # 下一个也是 --xxx(或者没了)→ 当布尔开关
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            args[key] = True
            i += 1
        else:
            args[key] = _coerce(argv[i + 1])
            i += 2
    return tool, args


def _driver_running() -> bool:
    """driver 在跑吗?用它自己的进程命令行判断,比 ping 队列可靠。"""
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
                "Where-Object { $_.CommandLine -like '*driver.py*' } | "
                "Measure-Object | Select-Object -ExpandProperty Count",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return int((out.stdout or "0").strip() or 0) > 0
    except Exception:
        return False


def _shared_http_running() -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 8123), timeout=0.25):
            return True
    except OSError:
        return False


def call_shared_http(tool: str, args: dict) -> dict:
    import asyncio

    sys.path.insert(0, str(ROOT / "scripts"))
    from http_call import invoke

    return asyncio.run(invoke("http://127.0.0.1:8123/mcp", tool, args))


def ensure_driver(wait: float = 45.0) -> bool:
    """保证后台有一个 driver 在跑;没有就拉起来。返回是否可用。

    脱离父终端启动(DETACHED_PROCESS),这样调用方的 shell 命令返回后
    driver 仍然活着 —— agent 只管调工具,不用先手动开服务。
    """
    if _driver_running():
        return True
    log_path = ROOT / "var" / "driver.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 清掉上一次的内容,好用"出现 DRIVER_READY"判断本次就绪
    try:
        log_path.write_text("", encoding="utf-8")
    except OSError:
        pass
    creation = 0
    if sys.platform == "win32":
        creation = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
        subprocess.Popen(
            [PYTHON, str(ROOT / "tools" / "driver.py")],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(ROOT),
            creationflags=creation,
            close_fds=True,
        )
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            if "DRIVER_READY" in log_path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
        time.sleep(0.4)
    return _driver_running()


def call_via_driver(tool: str, args: dict) -> dict:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    (QUEUE_DIR / f"req_{rid}.json").write_text(
        json.dumps({"tool": tool, "args": args}, ensure_ascii=False), encoding="utf-8"
    )
    resp_path = QUEUE_DIR / f"resp_{rid}.json"
    deadline = time.time() + DRIVER_TIMEOUT
    while time.time() < deadline:
        if resp_path.exists():
            try:
                data = json.loads(resp_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.2)
                continue
            resp_path.unlink(missing_ok=True)
            return data
        time.sleep(0.25)
    return {"ok": False, "error": f"driver 超时({DRIVER_TIMEOUT}s)未响应"}


def call_direct(tool: str, args: dict) -> dict:
    """不经 driver,自己起一个一次性 MCP 进程。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import asyncio

    import mcp_call  # noqa: E402

    line = json.dumps({"tool": tool, "args": args}, ensure_ascii=False)
    buf: list[str] = []
    orig_print = print

    # mcp_call.run_once 把结果 print 到 stdout —— 截下来解析成对象
    import builtins

    def _capture(*a, **k):
        if a and isinstance(a[0], str):
            buf.append(a[0])
        else:
            orig_print(*a, **k)

    builtins.print = _capture
    try:
        asyncio.run(mcp_call.run_once([line]))
    finally:
        builtins.print = orig_print
    for text in buf:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "error": "no response", "raw": buf[-1] if buf else ""}


def _load_tool_specs() -> list[dict]:
    """从服务器实例直接读工具定义 —— 不连游戏,纯自省。"""
    sys.path.insert(0, str(ROOT / "src"))
    from civ_mcp.server import mcp

    out = []
    for name, tool in mcp._tool_manager._tools.items():
        params = getattr(tool, "parameters", None) or {}
        out.append(
            {
                "name": name,
                "description": (getattr(tool, "description", "") or "").strip(),
                "params": params.get("properties", {}),
                "required": params.get("required", []),
            }
        )
    return sorted(out, key=lambda t: t["name"])


def cmd_list(filter_text: str = "") -> None:
    specs = _load_tool_specs()
    if filter_text:
        specs = [s for s in specs if filter_text.lower() in s["name"].lower()]
    print(f"{len(specs)} 个工具" + (f"(过滤:{filter_text})" if filter_text else ""))
    for s in specs:
        first_line = s["description"].split("\n")[0][:70]
        print(f"  {s['name']:<28} {first_line}")


def cmd_schema(tool_name: str) -> None:
    for s in _load_tool_specs():
        if s["name"] == tool_name:
            print(f"== {s['name']} ==")
            print(s["description"][:1200])
            print("\n参数:")
            if not s["params"]:
                print("  (无)")
            for k, v in s["params"].items():
                req = " [必填]" if k in s["required"] else ""
                typ = v.get("type", "?")
                desc = (v.get("description") or "")[:60]
                print(f"  --{k:<22} {typ}{req}  {desc}")
            return
    print(f"没有名为 {tool_name} 的工具。用 --list 看全部。")


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        cmd_list(argv[1] if len(argv) > 1 else "")
        return 0
    if argv[0] == "--schema":
        if len(argv) < 2:
            print("用法: python tools/civ.py --schema <tool>")
            return 1
        cmd_schema(argv[1])
        return 0

    no_driver = "--no-driver" in argv
    if no_driver:
        argv = [a for a in argv if a != "--no-driver"]

    tool, args = parse_args(argv)
    if no_driver:
        resp = call_direct(tool, args)
    elif _shared_http_running():
        resp = call_shared_http(tool, args)
    elif ensure_driver():
        resp = call_via_driver(tool, args)
        # A failed response can follow a successful action. Inspect state before
        # retrying; only GameConnection's read path may replay automatically.
    else:
        # driver 拉不起来也不能让 agent 卡住 —— 退回一次性进程
        print("(driver 启动失败,本次走一次性进程)", file=sys.stderr)
        resp = call_direct(tool, args)

    if resp.get("ok"):
        result = resp.get("result", "")
        print(
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False)
        )
        return 0
    print(f"ERROR: {resp.get('error', resp)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
