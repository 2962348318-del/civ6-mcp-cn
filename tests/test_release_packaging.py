"""Regressions for distributing this fork, independent of a running Civ VI game."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

from civ_mcp import end_turn, resources


def test_checkout_includes_real_rule_and_strategy_resources():
    units = json.loads((resources.gamedata_path() / "Units.json").read_text(encoding="utf-8"))
    assert units
    assert list(resources.strategy_path().glob("*.json"))


def test_installed_resources_take_precedence(tmp_path, monkeypatch):
    package = tmp_path / "site-packages" / "civ_mcp"
    (package / "resources" / "gamedata").mkdir(parents=True)
    (package / "resources" / "strategy").mkdir()
    monkeypatch.setattr(resources, "_PACKAGE", package)
    assert resources.gamedata_path() == package / "resources" / "gamedata"
    assert resources.strategy_path() == package / "resources" / "strategy"


def test_runtime_queues_respect_configured_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CIV_MCP_WORKSPACE", str(tmp_path))
    assert resources.runtime_data_path("research_queue.json") == tmp_path / "data" / "research_queue.json"
    assert not resources.runtime_data_path("research_queue.json").exists()


def test_runtime_queues_default_to_user_data_not_package(monkeypatch):
    monkeypatch.delenv("CIV_MCP_WORKSPACE", raising=False)
    assert resources.runtime_data_path("turn_reads.json") == Path.home() / ".civ6-mcp" / "data" / "turn_reads.json"


def test_expansion_gate_remains_active_after_fixture_isolation():
    async def units():
        return []

    async def cities():
        return [NS(currently_building="BUILDING_MONUMENT")], ""

    state = NS(get_units=units, get_cities=cities, has_read=lambda kind: True)
    result = asyncio.run(end_turn._turn_completion_gate(state, 100))
    assert "城数 1 < 目标 6" in result


def test_expansion_gate_allows_an_existing_settler():
    async def units():
        return [NS(unit_type="UNIT_SETTLER", unit_id=1, combat_strength=0, moves_remaining=0,
                   max_moves=2, build_charges=0, needs_orders=False, x=1, y=1)]

    async def cities():
        return [NS(currently_building="BUILDING_MONUMENT")], ""

    state = NS(get_units=units, get_cities=cities, has_read=lambda kind: True)
    assert asyncio.run(end_turn._turn_completion_gate(state, 100)) == ""
