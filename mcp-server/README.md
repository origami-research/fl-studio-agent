# FL Studio MCP server

This package is the MCP front end for deterministic FL Studio work. It routes
project data through `flpkit`, live host operations through the native Bridge,
and live controller operations through the MIDI wire/control broker.

## Setup

From the repository root, follow [../SETUP.md](../SETUP.md). For direct package
development:

```bash
cd mcp-server
uv sync --extra private
```

The `private` extra pins the unreleased `flpkit` source for internal work. A
public installation requires the released `flpkit` package described in the
OSS launch checklist.

Install the FL-side components with `uv run fl-mcp-install`. The native Bridge
is optional for file-lane work and required for live host operations.

Register the server with an MCP client using this environment's Python:

```bash
claude mcp add fl-studio -- /absolute/path/to/mcp-server/.venv/bin/python -m fl_mcp
```

## Tools and SDK

The server exposes `run`, `get_notes`, `get_scene`, `render`, `analyze`, and
`fl_doctor`. The `run` tool docstring is the SDK cheatsheet: it documents
`fl`, `theory`, `Note`, the small music-code standard-library allowlist, and each
supported operation.

Use `uv run fl-mcp-dev-session --no-repl` for a disposable live smoke session.
Run `../scripts/gate.sh` for the repository gate; [../docs/gate.md](../docs/gate.md)
defines its current checks and test count.
