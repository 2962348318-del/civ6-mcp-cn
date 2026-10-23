"""Resolve bundled read-only resources and user-writable runtime state."""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent
_REPOSITORY = _PACKAGE.parents[1]


def gamedata_path() -> Path:
    """Editable checkout or resources included in a built wheel."""
    bundled = _PACKAGE / "resources" / "gamedata"
    return bundled if bundled.is_dir() else _REPOSITORY / "docs" / "gamedata"


def strategy_path() -> Path:
    bundled = _PACKAGE / "resources" / "strategy"
    return bundled if bundled.is_dir() else _REPOSITORY / "data" / "strategy"


def runtime_data_path(filename: str) -> Path:
    """Do not write queues into site-packages or the public source tree."""
    workspace = os.environ.get("CIV_MCP_WORKSPACE")
    base = Path(workspace).expanduser() if workspace else Path.home() / ".civ6-mcp"
    return base / "data" / filename
