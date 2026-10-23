# Civ6 MCP CN

A Chinese-focused maintenance fork of [lmwilki/civ6-mcp](https://github.com/lmwilki/civ6-mcp), originally created by Liam Wilkinson and distributed under the MIT license.

[中文文档](README.md) · [Provenance](docs/provenance.md) · [Validation](docs/validation.md)

This fork retains the upstream MCP/FireTuner foundation and adds local work on Chinese narration, vanilla-ruleset handling, turn discipline, persistent strategy assistance, and shared HTTP connections. It is an independent community project, with no official affiliation to the game publisher, OpenAI, or the upstream author.

## Install

Use Python 3.12+. Download this repository and run from its root:

```bash
python -m venv .venv
# Windows: use .venv\Scripts\python.exe in place of .venv/bin/python
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/doctor.py
```

With uv: `uv sync --frozen`, then `uv run python scripts/doctor.py`.

The doctor checks dependencies, rule resources, and tool registration offline. It does not launch or connect to Civilization VI.

## Run

Enable Tuner in a legally installed copy of Civilization VI, restart, and load a single-player save. Close the SDK FireTuner GUI. Tuner disables achievements. Use one owner of the game's TCP 4318 connection.

```bash
.venv/bin/python tools/serve_http.py --port 8123
```

Connect an HTTP-capable MCP client to `http://127.0.0.1:8123/mcp`. See [HTTP configuration](examples/mcp-http.json) and [stdio configuration](examples/mcp-stdio.json). Do not run a second stdio server against the same game while HTTP owns the connection.

Tool introspection works without a game:

```bash
.venv/bin/python tools/civ.py --list
.venv/bin/python tools/civ.py --schema unit_action
```

Read `get_game_overview`, `get_units`, and `get_cities` before taking actions; on resuming a session, read `get_diary` first. Follow [AGENTS.md](AGENTS.md) for the turn loop.

## Scope and limitations

The bundled rule snapshot is **RULESET_STANDARD**. Expansion and modded games need matching exports; strategic recommendations are heuristics, not a verified optimal policy. Windows is the main environment for local changes. Other-platform support is inherited and has not been revalidated in a live game for this fork.

Current release preparation verifies offline behavior only. Passing tests do not demonstrate a full-game win, Deity performance, or complete compatibility with every ruleset. See [validation](docs/validation.md) for the measured results.

Runtime state uses `~/.civ6-mcp` or `CIV_MCP_WORKSPACE`. Repository-local queue files use `var/`. Do not publish local state or game logs without reviewing their contents.

## Develop

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

The CI workflow covers offline tests and packaging on Windows/Linux. Follow [CONTRIBUTING.md](CONTRIBUTING.md). The original copyright and MIT terms remain in [LICENSE](LICENSE); attribution and the snapshot scope are in [NOTICE](NOTICE).
