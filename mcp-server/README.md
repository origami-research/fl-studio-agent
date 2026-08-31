# FL Studio MCP server

Lets a model write notes into FL Studio's piano roll.

See [../docs/10-v0-vertical-slice.md](../docs/10-v0-vertical-slice.md) for what this
version does and doesn't do.

## Setup

```bash
cd mcp-server
uv venv && uv pip install -e .

# Copy the piano roll script into FL Studio's settings folder
uv run python -m fl_mcp.fl.install
```

Restart FL Studio so it picks up the new script.

Then register the server with your MCP client. For Claude Code:

```bash
claude mcp add fl-studio -- /absolute/path/to/mcp-server/.venv/bin/python -m fl_mcp
```

Verify with the `fl_status` tool — it reports whether the script is installed and
where the spool lives.

## Using it

1. Open the piano roll on the channel you want (double-click the channel).
2. Ask Claude for notes — *"write a 4-bar bassline in A minor"*.
3. In the piano roll, run **Tools > Scripting > MCP Write Notes**.

Step 3 is manual in this version. The MCP server queues the edit; FL applies it when
you run the script. The native Bridge plugin
([../docs/04-native-plugin.md](../docs/04-native-plugin.md)) removes that step.

## Tools

| Tool | What it does |
|---|---|
| `write_notes(notes, mode)` | Queue notes. `mode="merge"` adds, `"replace"` clears first. |
| `clear_notes()` | Queue a clear of the open piano roll. |
| `fl_status()` | Setup check + whether the last command was applied. |

Note format:

```json
{"key": "C4", "start": 0, "length": 1, "velocity": 0.8, "pan": 0.5, "color": 0}
```

- `key` — note name or MIDI number. **C4 = MIDI 60 = middle C.** FL's piano roll
  labels this `C5` because it displays an octave higher; the pitch is correct. Pass a
  MIDI number if you want no ambiguity.
- `start`, `length` — **beats**. `length: 1.0` is a quarter note. Conversion to FL's
  ticks happens inside FL, using the score's real PPQ.
- `velocity`, `pan` — `0.0`–`1.0`. Defaults `0.8` and `0.5`.
- `color` — `0`–`15`, FL's note colors (also the MIDI channel).

## Development

```bash
uv run pytest
```

`tests/test_pyscript.py` executes the real `.pyscript` against a fake `flpianoroll`
built from the official API stubs, so the executor is covered without FL Studio. FL's
own behavior still needs the manual smoke test in
[../docs/10-v0-vertical-slice.md](../docs/10-v0-vertical-slice.md).

## Troubleshooting

**"nothing to apply"** — no command is queued. Ask Claude for notes first.

**Script missing from the Tools > Scripting menu** — re-run `python -m fl_mcp.fl.install`
and restart FL Studio. `fl_status` shows the resolved install path.

**Notes land an octave off** — see the `key` note above; pass MIDI numbers directly.

**Notes went to the wrong instrument** — they land in whichever piano roll was open.
This version cannot target a channel.
