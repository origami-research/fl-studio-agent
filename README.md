# fl-studio-mcp

`fl-studio-mcp` is an MCP server for deterministic work in FL Studio. It uses
three complementary lanes: `flpkit` edits project files, the native FL MCP
Bridge performs live host operations, and the MIDI wire/control broker handles
live controller operations.

## Tools

The server exposes six tools:

| Tool | Purpose |
| --- | --- |
| `run` | Execute Python against the FL SDK. Its docstring is the SDK cheatsheet. |
| `get_notes` | Read notes from a project. |
| `get_scene` | Read a compact musical scene summary. |
| `render` | Save and render a project to WAV. |
| `analyze` | Analyze a WAV deterministically. |
| `fl_doctor` | Report live-lane readiness and actionable fixes. |

Use `run` for composition and SDK operations. Every write returns confirmed
readback where the underlying lane can provide it.

## Install and verify

Follow [SETUP.md](SETUP.md) from a fresh clone. It installs the server with
`fl-mcp-install`, including the controller and MIDI assignment, and explains
the optional native Bridge for live operations.

For a disposable live smoke session, run `fl-mcp-dev-session`. For the local
merge gate, run:

```bash
scripts/gate.sh
```

The gate is the authoritative test count and verification contract; see
[docs/gate.md](docs/gate.md) for its current stages.

## Documentation

- [SETUP.md](SETUP.md): installation and first project.
- [docs/README.md](docs/README.md): current documentation map.
- [docs/24-coverage-map.md](docs/24-coverage-map.md): capability status.
- [RUNBOOK_E2E.md](RUNBOOK_E2E.md): end-to-end verification.
