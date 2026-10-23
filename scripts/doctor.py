"""Offline installation check. Does not connect to a game or execute game commands."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def diagnose() -> dict:
    checks = [{"name": "python", "ok": sys.version_info >= (3, 12), "detail": sys.version.split()[0]}]
    for package in ("mcp", "fastapi", "anthropic"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            checks.append({"name": package, "ok": False, "detail": "not installed; run python -m pip install -e ."})
        else:
            checks.append({"name": package, "ok": True, "detail": version})
    from civ_mcp.resources import gamedata_path, strategy_path

    for filename in ("Units.json", "Technologies.json", "Buildings.json", "Policies.json"):
        path = gamedata_path() / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("rows", []) if isinstance(data, dict) else data
            ok = isinstance(rows, list) and bool(rows)
            detail = f"loaded ({len(rows)} rows)" if ok else "missing or empty rows"
        except (OSError, ValueError) as error:
            ok, detail = False, str(error)
        checks.append({"name": filename, "ok": ok, "detail": detail})
    checks.append({"name": "strategy resources", "ok": strategy_path().is_dir(), "detail": str(strategy_path())})
    if all(check["ok"] for check in checks):
        try:
            from civ_mcp.server import mcp

            count = len(mcp._tool_manager._tools)
            checks.append({"name": "tool registration", "ok": count > 0, "detail": f"{count} tools; game connection not attempted"})
        except Exception as error:
            checks.append({"name": "tool registration", "ok": False, "detail": f"{type(error).__name__}: {error}"})
    return {"ok": all(check["ok"] for check in checks), "game_connection_tested": False, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print structured results.")
    args = parser.parse_args()
    report = diagnose()
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        for check in report["checks"]:
            print(f"{'PASS' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}")
        print("Game connection: NOT TESTED (offline checks only)")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
