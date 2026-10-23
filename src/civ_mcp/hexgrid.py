"""Hex-grid distance for Civilization VI offset coordinates.

Civ6 plots use **odd-r** offset coordinates: odd rows (y % 2 == 1) are
shifted right by half a hex.  All reachability / threat / siege distance
checks must go through this module; Chebyshev (``max(abs, abs)``) is
forbidden because it misreports diagonal distances on a hex grid
(a 2-tile diagonal reads as 2 in Chebyshev but 3 in hex distance).

Reference: Red Blob Games offset-to-cube conversion for odd-r layout.
"""

from __future__ import annotations

from typing import Any


def hex_distance(x1: Any, y1: Any, x2: Any, y2: Any) -> int:
    """Hex distance between two plots in Civ6 offset coordinates.

    Accepts anything int()-able (Lua output often arrives as strings).
    Returns 999 on malformed input so callers degrade to "far away"
    instead of crashing.
    """
    try:
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    except (TypeError, ValueError):
        return 999

    # odd-r offset -> axial (q, r): q = col - (row - (row & 1)) // 2
    q1 = x1 - (y1 - (y1 & 1)) // 2
    r1 = y1
    q2 = x2 - (y2 - (y2 & 1)) // 2
    r2 = y2

    # axial -> cube: s = -q - r; distance = (|dq| + |dr| + |ds|) / 2
    s1 = -q1 - r1
    s2 = -q2 - r2
    return (abs(q1 - q2) + abs(r1 - r2) + abs(s1 - s2)) // 2


def hex_neighbors(x: Any, y: Any) -> list[tuple[int, int]]:
    """The six plots adjacent to (x, y) under odd-r offset layout."""
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return []
    if y % 2 == 0:
        deltas = ((1, 0), (0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1))
    else:
        deltas = ((1, 0), (1, -1), (0, -1), (-1, 0), (0, 1), (1, 1))
    return [(x + dx, y + dy) for dx, dy in deltas]
