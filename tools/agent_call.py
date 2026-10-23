"""Submit tool requests to the running driver and wait for responses.

Usage: python tools/agent_call.py var/reqs.jsonl

reqs.jsonl: one JSON request per line:
    {"tool": "get_units", "args": {}}

Prints one JSON line per request.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # tools/ 的上一级 = 项目根
QUEUE_DIR = ROOT / "var" / "queue"


def main() -> int:
    if len(sys.argv) < 2:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "usage: python tools/agent_call.py var/reqs.jsonl",
                }
            )
        )
        return 1
    req_file = Path(sys.argv[1])
    if not req_file.is_file():
        print(json.dumps({"ok": False, "error": f"no such file: {req_file}"}))
        return 1
    QUEUE_DIR.mkdir(exist_ok=True)
    lines = [
        ln.strip()
        for ln in req_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    ids = []
    for line in lines:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"bad json line: {exc}"}))
            return 1
        if "tool" not in req:
            print(json.dumps({"ok": False, "error": "missing 'tool'"}))
            return 1
        rid = uuid.uuid4().hex[:12]
        (QUEUE_DIR / f"req_{rid}.json").write_text(
            json.dumps(req, ensure_ascii=False), encoding="utf-8"
        )
        ids.append(rid)

    deadline = time.time() + 660
    pending = set(ids)
    results: dict[str, dict] = {}
    while pending and time.time() < deadline:
        for rid in list(pending):
            rp = QUEUE_DIR / f"resp_{rid}.json"
            if rp.exists():
                try:
                    results[rid] = json.loads(rp.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                pending.discard(rid)
                # 读完即删(2026-08-05):driver 只清 req_*.json,响应文件
                # 此前从不回收 —— 实测堆积到 361 个。响应已读进内存,
                # 留着只是垃圾。删除失败不影响本次结果,忽略即可。
                try:
                    rp.unlink()
                except OSError:
                    pass
        if pending:
            time.sleep(0.3)

    code = 0
    for rid in ids:
        resp = results.get(rid, {"ok": False, "error": "TIMEOUT"})
        print(json.dumps(resp, ensure_ascii=False))
        if not resp.get("ok"):
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
