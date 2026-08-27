# fl-studio-mcp

Ask an AI agent for music; it appears in FL Studio's piano roll.

```
you: "make hot cross buns"
 →  notes written into the project, FL opens on it, no clicking
```

**Status: working on macOS.** See **[SETUP.md](SETUP.md)** to go from clone to
notes in about two minutes.

## What it does

| Tool | |
|---|---|
| `write_notes` | Write notes — note names or MIDI, positions in beats, merge or replace |
| `list_channels` | What instruments the project has |
| `set_tempo` | Project tempo in BPM |
| `set_channel_mix` | Channel volume, pan and pitch |
| `clear_notes` | Empty the piano roll |
| `fl_status` | Which backend is active, and read the notes back from the project |
| `get_notes` | Read a pattern/channel's notes from the shadow score, with provenance |
| `get_session_state` | Live tempo, channels and mixer over the MIDI control plane |
| `fl_doctor` | What's connected, what's broken, and the exact fix |
| `undo_last` | The model's last recorded action (from the delta history) |

Notes land via a **backend cascade** (docs/17): live in-place edit if set up,
else the permission-free project-write (rewrite the `.flp` and reopen FL), else
queued. Non-note control (`get_session_state`) rides a live MIDI control plane.
Whichever backend runs, every write is recorded as a foldable `NoteDelta` in the
action log. See **[docs/17-unified-architecture.md](docs/17-unified-architecture.md)**.

Notes are specified musically (`{"key": "C4", "start": 0, "length": 1}`), never
in FL's internal units.

## How it works

Three backends, tried in order; `write_notes` reports which one ran.

| Backend | Behaviour | Setup |
|---|---|---|
| **live** | Edits the piano roll you have open, in place | two one-time grants |
| **project-write** | Rewrites the `.flp` and reopens FL on it | none — the default |
| **manual** | Queues; you run the script inside FL | fallback |

The default needs no macOS permission at all, because editing a file and
`open -a` are both unprivileged. The cost is that FL reloads rather than being
edited live.

## What we learned the hard way

The original design — a native FL plugin polling the host's Idle callback — is
**impossible on macOS**. FL never loads third-party binaries from its plugin
folder. That was established by instrumenting the dylib with a constructor that
fires at `dlopen` and observing it never run, not by inference.
[docs/11](docs/11-native-plugin-blocked.md) records the full attempt and what it
does and does not rule out.

FL's embedded Python is also sandboxed in undocumented ways — `open()` works
only inside FL's own tree and rejects `encoding=`, `os.remove` is blocked
outright, and refusals raise `SystemError` rather than `OSError`. Each of those
broke a working-looking implementation.
[docs/10](docs/10-v0-vertical-slice.md) lists them; `mcp-server/tests/fl_env.py`
emulates them so they cannot break anything again.

## Testing

62 tests, plus two things worth calling out:

- **`tests/fl_env.py`** emulates FL's Python sandbox. An earlier suite emulated
  the *API* but not the *sandbox*, and passed while the real script failed three
  times running.
- **`tests/mutation_check.py`** verifies that emulator by reverting the script to
  each historical bug and asserting the suite catches it. An emulator that does
  not reproduce known failures is worthless.

```bash
cd mcp-server && uv run pytest
python -m harness.doctor
```

## Layout

```
mcp-server/        the MCP server and its backends
fl-scripts/        the piano roll .pyscript that runs inside FL
harness/           autonomous test harness: FL lifecycle, verification, setup
native/            the native plugin attempt (blocked — kept for the record)
docs/              design docs and findings
```

## Documentation

| | |
|---|---|
| [SETUP.md](SETUP.md) | **Start here** — clone to working |
| [docs/10](docs/10-v0-vertical-slice.md) | What shipped, and FL's undocumented sandbox |
| [docs/11](docs/11-native-plugin-blocked.md) | Why the native plugin cannot work |
| [docs/01](docs/01-fl-studio-surfaces.md) | FL's four programmable surfaces, and their limits |
| [docs/02](docs/02-architecture.md) | Intended architecture — parts are superseded by 11 |
| [harness/README.md](harness/README.md) | The autonomous test harness |

Docs 00–09 were written before implementation. They are kept as the design
record, but where they disagree with 10 and 11, the later ones are what is
true.
