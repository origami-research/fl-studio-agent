# Setup: ask for notes, they appear in FL Studio

```bash
cd mcp-server && uv pip install -e . && uv pip install pyflp
.venv/bin/python -m fl_mcp.make_agent_project
```

That is the whole setup. Ask an agent for notes and they appear in FL Studio,
with no clicking, no macOS permission, and no hotkey.

## How it works

Notes are written **directly into the project file**, then FL is opened on it:

```
write_notes()  ->  edit FLMCP Agent.flp  ->  open -a "FL Studio" <project>
```

Both halves are unprivileged — editing a file is just writing a file, and
`open -a` is the documented way to open a document. Neither needs automation
consent, which is what every other route to FL's piano roll requires.

**The trade-off:** FL *reloads* the project rather than being edited live, so
anything unsaved in the running session is lost. That is why this backend is
scoped to one dedicated project (`~/Documents/Image-Line/FL Studio/Projects/FLMCP
Agent.flp`) and never touches a project you opened yourself. Point it elsewhere
with `FLMCP_AGENT_PROJECT=/path/to/your.flp`.

A `.flp.bak` rollback copy is written before every change.

## Which channel notes land on

Notes go to the first **melodic** channel — anything named kick, snare, clap,
hat, 808 and so on is skipped. Rack 0 is a drum in most FL templates, so
defaulting to it put melodies where they could be neither heard properly nor
found.

The response says which channel it used. Open that channel's piano roll in the
Channel Rack to see the notes.

## Verifying

`fl_status` reads the notes back out of the project file — ground truth, not a
claim about what was sent.

## Why not edit the live session instead

Every in-place route is blocked on macOS, each measured rather than assumed:

| Route | Status |
|---|---|
| Native plugin polling FL's Idle callback | **Blocked.** FL never loads third-party binaries — a `dlopen` constructor trace never fired. [docs/11](docs/11-native-plugin-blocked.md) |
| Piano roll `.pyscript` | Works, but only runs from FL's Scripting menu — one click per request |
| Keystroke to trigger that menu | Needs macOS automation consent (SIP-protected, cannot be scripted) **and** a hotkey bound inside FL |
| MIDI controller script | Cannot write notes; the API has no note-writing function |

The project-write backend sidesteps all of it.

## The live-session fallback

Without an agent project configured, `write_notes` falls back to the spool +
piano roll script, which edits the session you already have open without
reloading it. That path needs one click per request:

**piano roll ▸ menu ▸ Tools ▸ Scripting ▸ MCP Write Notes**

To reduce that to zero clicks, see `python -m harness.enable_autonomy`.

## Development

```bash
cd mcp-server && .venv/bin/python -m pytest -q     # 55 tests
.venv/bin/python tests/mutation_check.py           # verifies the FL sandbox emulator
python -m harness.doctor                           # live status of every link
python -m harness.verify <project.flp>             # dump notes from any project
```
