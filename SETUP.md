# Setup

From a fresh `git clone` to "ask an agent for notes, they appear in FL Studio".

**Requirements:** macOS, FL Studio (tested on 2025), Python 3.11+, and
[uv](https://docs.astral.sh/uv/). Windows is not supported yet.

---

## 1. Install

```bash
cd mcp-server
uv venv                       # required first — uv will not install without it
uv pip install -e .
uv pip install pyflp          # reads notes back out of a project, for verification
```

## 2. Create the agent's project

```bash
uv run python -m fl_mcp.make_agent_project
```

Creates `~/Documents/Image-Line/FL Studio/Projects/FLMCP Agent.flp` from a stock
FL template. It is the **only** project the agent modifies — your own work is
never touched. A `.flp.bak` rollback is written before every change.

## 3. Register the server with your agent

```bash
claude mcp add fl-studio -- "$(pwd)/.venv/bin/python" -m fl_mcp
```

Must be an absolute path. Restart the agent session afterwards so it picks the
server up.

## 4. Ask for something

> "make hot cross buns"

FL Studio opens on the agent project with the notes in it. Open the piano roll
on the channel named in the reply.

**That is the whole setup** — no macOS permissions, no hotkeys, no plugins.

---

## What you can ask for

| Tool | What it does |
|---|---|
| `write_notes` | Notes by name or MIDI number, positions in beats, merge or replace |
| `list_channels` | What instruments this project has |
| `set_tempo` | Project tempo in BPM |
| `set_channel_mix` | Channel volume, pan and pitch |
| `clear_notes` | Empty a channel's notes |
| `fl_status` | Which backend is active, and read the notes back |

You do not call these directly — just say what you want:

> "make hot cross buns"
> "write a 4-bar bassline in A minor on Bass, at 128 BPM"
> "pan the lead slightly left and turn it down a bit"
> "add a countermelody over what's there"

The agent project ships with **Chords**, **Bass**, **Lead** and **Drums** on
real instruments. Notes go to the Lead unless you name another channel; a
melody is never placed on a drum.

### What it cannot do

`set_channel_mix` controls the channel strip, not the instrument. Preset,
filter cutoff, detune and envelopes live inside the plugin's own saved state,
which is an opaque blob in the project file — so "make it sound like a supersaw"
is out of reach. Loading a different instrument is too; the palette is whatever
the project was created with.

---

## How it works, and the trade-off

```
write_notes()  →  edit FLMCP Agent.flp  →  open -a "FL Studio" <project>
```

Both halves are unprivileged: editing a file is just writing a file, and
`open -a` is the documented way to open a document. Neither needs the automation
consent that every other route into FL's piano roll requires.

**The trade-off: FL reloads rather than being edited live.** Anything unsaved in
the running session is lost, which is why this is scoped to one dedicated
project. Point it elsewhere with `FLMCP_AGENT_PROJECT=/path/to/your.flp`.

---

## Optional: live in-place editing

Notes appear in the piano roll you are already looking at. Nothing reloads.

```bash
python -m harness.enable_autonomy
```

It walks two grants, then **proves** the result rather than trusting you:

1. **macOS automation consent** — provokes the prompt (an app does not appear in
   the Automation list until it tries), opens the right pane, waits for it.
2. **An FL shortcut bound to the script** — then queues a harmless empty
   command, presses the shortcut, and only records success if FL actually ran
   the script.

Neither step can be automated: macOS keeps its permission database
SIP-protected, and FL stores shortcut bindings internally. A security boundary,
not a missing feature.

Once both pass, `write_notes` prefers the live backend automatically.

## Which backend am I on?

```bash
python -m harness.doctor
```

| Backend | Behaviour | Setup |
|---|---|---|
| **live** | Edits the open piano roll in place | the two grants above |
| **project-write** | Rewrites the project, reopens FL | none — the default |
| **manual** | Queues; you run the script in FL | fallback |

`write_notes` tries them in that order and reports which it used.

---

## Which channel notes land on

Name one — `"put the bassline on Bass"` — or leave it and the most
melody-appropriate channel is chosen: a lead before a bass, and never a drum.
Rack 0 is a drum in most FL templates, so defaulting to it put melodies where
they could be neither heard properly nor found.

The response names the channel it used. `list_channels` shows the options.

## Why not edit the live session by default

Every in-place route is blocked or gated on macOS, each measured rather than
assumed:

| Route | Status |
|---|---|
| Native plugin polling FL's Idle callback | **Blocked.** FL never loads third-party binaries — a `dlopen` constructor trace never fired. [docs/11](docs/11-native-plugin-blocked.md) |
| Piano roll `.pyscript` | Works, but only runs from FL's Scripting menu — one click per request |
| Keystroke to trigger that menu | Needs macOS consent (SIP-protected) **and** an FL hotkey — the optional path above |
| MIDI controller script | Cannot write notes; the API has no note-writing function |

## Troubleshooting

**"No virtual environment found"** — run `uv venv` first (step 1).

**Agent replies `queued` instead of `applied`** — no backend was available. The
reply lists what is missing; `harness.doctor` gives the fix.

**Notes look an octave off** — they are not. FL labels middle C as `C5`; this
project uses scientific pitch, where it is `C4`. Same note. Pass a MIDI number
to remove the ambiguity.

**Can't find the notes** — check the channel named in the reply, and Pattern 1.

**Different FL version** — `export FL_APP="/Applications/FL Studio 21.app"`.

## Development

```bash
cd mcp-server && uv run pytest                     # 110 tests
uv run python tests/mutation_check.py              # verifies the FL sandbox emulator
python -m harness.doctor                           # live status of every link
python -m harness.verify <project.flp>             # dump notes from any project
```

Note read-back parses the saved `.flp` — ground truth, not a claim about what
was sent.
