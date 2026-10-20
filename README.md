# fl-studio-agent

`fl-studio-agent` is an agent runtime for FL Studio: a Python SDK, an MCP
server that exposes it, and a native Bridge plugin. It uses three
complementary lanes: `flpkit` edits project files, the native FL MCP
Bridge performs live host operations, and the MIDI wire/control broker handles
live controller operations.

## Tools

The server exposes five tools, one per job:

| Tool | Purpose |
| --- | --- |
| `run` | Execute Python against the FL SDK: write and clear notes, tempo, levels, parameters. Its docstring is the SDK cheatsheet. |
| `get_scene` | Read what FL has now: a compact musical summary, or the notes (`detail="notes"`, by channel and pattern). |
| `check` | Play a few bars and read FL's mixer meters: which channels sounded, their peaks, and why a silent one is silent. |
| `render` | Play the live arrangement once and record the mix to WAV. |
| `analyze` | Analyze a WAV deterministically. |

The working rhythm is write, check at milestones, render at the end.
Every result is what FL now has, so nothing needs waiting for, saving, or re-reading.
FL's transient refusals ("operation unsafe at current time", a busy control wire) are retried inside the engine; a model sees only errors it can act on.
Setup diagnostics are for the operator, not the model: `fl-mcp-doctor` prints the same report as JSON.

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
