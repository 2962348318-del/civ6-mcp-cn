# Changelog

## 0.1.0 — 2026-10-23

First public-source preparation of this independent maintenance fork. It packages existing local development rather than claiming a new implementation of the upstream MCP server.

- Retains local Chinese narration, vanilla compatibility work, turn/civilian/expansion gates, strategy modules, persistent state, and connection recovery tests.
- Includes the shared streamable-HTTP service and CLI tools; subprocess helpers use the active Python interpreter.
- Adds wheel resource packaging and separates writable queue state from installed code.
- Keeps the MCP dependency on its compatible v1 API and the existing Anthropic integration on v0.x; fresh installs must not silently select breaking major versions.
- Adds bilingual project documentation, upstream attribution, example configuration, contribution guidance and an offline installation doctor.
- Isolates civilian-tracking fixtures from the later expansion gate without removing the expansion check; adds explicit expansion regression coverage.

Exact validation results are recorded separately in [docs/validation.md](docs/validation.md). Historical upstream changes are preserved in [docs/upstream/CHANGELOG.md](docs/upstream/CHANGELOG.md).
