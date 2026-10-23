# Civ6 MCP CN — repository and gameplay guide

This is an independent maintenance fork. Preserve the upstream MIT copyright and the attribution in NOTICE. Do not describe inherited code or upstream usage as this fork's original work or adoption.

## Working on code

- Run offline tests with `python -m pytest -q`. Manual scripts named `scripts/test_*.py` require a live game and are not the default suite.
- Read-only game rules must work both from an editable checkout and from a wheel: use `civ_mcp.resources.gamedata_path()` and `strategy_path()`.
- Runtime files belong in user state or `var/`, never in the packaged resource directories. Do not commit logs, saves, credentials or personal client configuration.
- Changes under `src/` require restarting the one MCP server that owns the game connection. Do not start competing servers or kill other processes as part of offline checks.
- Report offline tests and live-game validation separately. Rule tables here are a RULESET_STANDARD snapshot; mark estimates and unverified rules explicitly.

## Resuming a game

Before taking any action, read `get_diary`, `get_game_overview`, `get_power_balance`, then `get_units` and `get_cities`. Confirm the loaded ruleset and save; a historical strategy or another ruleset is not the current game state.

## Each turn

1. Read the previous briefing, resolve urgent threats, and query current units/cities.
2. Inspect destinations before moving exposed civilians; use matching game rules and the actual engine response.
3. Execute unit orders, set production and research, and check diplomatic blockers.
4. Review victory progress, finances, expansion and trade routes periodically; use strategy tools as assistance.
5. Call `end_turn` with concrete reflections. If a gate blocks it, read and address the reason before deliberately overriding it.

Persistent diaries/world models help with context loss but do not replace fresh observations. Fog-of-war disappearance does not establish that an enemy died. Do not claim an action succeeded until a fresh query or engine result confirms it.

## Recovery

One process owns FireTuner TCP 4318. The shared HTTP service is suitable when several clients need access. After a connection failure, recover the connection and re-read state before reissuing writes; never blindly replay a whole action batch.

The original upstream agent guide is retained for provenance at [docs/upstream/AGENTS.md](docs/upstream/AGENTS.md). Some of its tactics target expansions or reflect historical implementation behavior; current engine feedback and the loaded ruleset take precedence.
