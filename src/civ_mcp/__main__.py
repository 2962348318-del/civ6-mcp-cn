"""Entry point: python -m civ_mcp"""

import sys

# stdio 强制 UTF-8(2026-08-05)。
#
# Windows 上 Python 通过管道运行时,sys.stdout/stderr 用的是本地
# ANSI 代码页(中文系统 = GBK),而这个项目的日志、门禁消息、建议文本
# 大量是中文。后果:
#   · stderr —— 宿主按 UTF-8 读会解码失败。Codex 实测报了 36 次
#     "Failed to read MCP server stderr: stream did not contain valid
#     UTF-8",civ6 工具因此挂载不进会话。
#   · stdout —— 那是 MCP 协议流本身,编码不一致会直接损坏 JSON-RPC。
#
# driver.py / scripts/mcp_call.py 是靠自己传 PYTHONIOENCODING=utf-8
# 绕过的,但那要求每个宿主都记得配。在入口处自己定死,任何宿主
# (Codex / Claude Code / 裸 stdio)都不用操心。
# errors="replace" 只作用于 stderr:日志里一个字符编不出来也绝不能
# 让服务器崩掉;stdout 不设 errors,协议流宁可报错也不能静默出错。
for _stream, _errors in ((sys.stdout, None), (sys.stderr, "replace")):
    try:
        if hasattr(_stream, "reconfigure"):
            if _errors:
                _stream.reconfigure(encoding="utf-8", errors=_errors)
            else:
                _stream.reconfigure(encoding="utf-8")
    except Exception:  # 极老的 Python / 被替换过的流 —— 不阻断启动
        pass

from civ_mcp.server import main

main()
