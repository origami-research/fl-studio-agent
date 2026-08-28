# v2 rewrite: lane spec

The owner's call (2026-08-27, sightmesh review): purge the accreted code and rewrite the server core as a minimal, readable system.
SDK-first (real Python, immediate execution, no queue/flush), firm types, no hidden state, raw byte surgery for file writes, and our own FLP library with pyflp demoted to a dev-only differential oracle (decision C).
This document is the delegation contract: the lanes, what each implements, what each may NOT touch, and the gates that decide done.

## Ground rules for every lane

- **The contracts are frozen.** `mcp-server/src/fl_mcp/v2/` holds the skeleton files - full signatures, docstrings, and types.
  A lane implements bodies; changing a signature requires the manager's sign-off because other lanes code against it.
- **AGENTS.md is law.** Trust types, inject dependencies, boundaries verify (read back, never echo the request), one canonical function per rule, ship the tested path, no module-level mutable state, no `sys.path` surgery, ruff clean (`ruff check src tests`).
- **Verified facts are load-bearing.** Everything in the "live-verified facts" section below was paid for with real FL boots; contradicting one requires re-verification, not argument.
- **Each lane lands as small green commits** on its own branch off `feat/v2-rewrite`; the fast suite (`pytest`, currently 257 passing / skips only for FL-dependent) must stay green at every commit.
- **v1 keeps working until the cutover.** v2 is built in the `fl_mcp/v2/` namespace; nothing in v1 is deleted until migration step 5 (the purge), which the manager runs after all gates pass.

## Live-verified facts (2026-08-27, real FL 2026 Producer Edition on this machine)

1. `pyflp.save()` corrupts some templates (wrong FLhd channel count byte; a mangled UTF-16 text event) in ways our parser reads back fine but **FL refuses to load**. pyflp is therefore parse-only, dev-only.
2. FLP tempo is event id **156**, u32, BPM x 1000. Mutating the payload in place works. When the event is absent (FL expresses default tempo by omission), **appending at the end of the event stream works** (reads back exactly); mid-stream splices load-but-ignore or reject depending on position.
3. FLP framing: `FLhd` + u32 len + 6 header bytes (u16 format, u16 nChannels, u16 PPQ), then `FLdt` + u32 len + event stream.
   Event encoding: id < 64 -> 1 data byte, 64-127 -> 2, 128-191 -> 4, 192+ -> varint length + payload.
4. Notes are 24-byte packed records (`<IHHIHHBBBBBBBB`: position, flags, rack_channel, length, key, group, fine_pitch=120, u1, release=64, midi_channel, pan 0-128, velocity 0-127, mod, mod) inside a `PatternID.Notes` (224) event, preceded by `PatternID.New` (65, u16).
5. `mixer.getCurrentTempo` returns BPM x 1000; `setCurrentTempo(bpm, 1)` takes BPM. Live reads must be used for confirmation - a rejected project file leaves FL on a default project whose read-backs look plausible, so **window-title checks accompany tempo assertions in e2e**.
6. FL parks engine calls arriving "unsafe at current time" for OnIdle retry, and **OnIdle freezes while a modal dialog is up** - `ping` still answers (it never touches the FL API). Licensed FL shows no startup modal; the e2e keeps the Escape-dismissal loop anyway.
7. FL's embedded Python: `open()` only inside FL's data tree, writes only under `Piano roll scripts`, `os.remove` raises `SystemError`. Consumption is tombstones + a fail-closed ledger.
8. Virtual MIDI ports are per-process and must exist **before** FL launches; two `MidiControl`s in one process would show FL duplicate devices.

## The lanes

Dependency shape: `schemas` (frozen day 0) <- F, W in parallel <- S <- K <- V. E runs continuously.

### Lane F - `flp.py`: our own FLP library (the critical path)

Implements `v2/flp.py`: the reader subset (ppq, tempo, channels with names+levels, notes per pattern/channel) and the raw writers (`write_notes`, `set_tempo`, `set_channel_levels`).

- Port the live-verified raw `set_tempo` from `flp_writer.py` as-is.
- Convert `write_notes` to raw surgery: locate/replace the `PatternID.Notes` blob (the v1 pyflp version's event-juggling documents the semantics; the bytes-level splice replaces it). The readback is `notes_at()` of the SAVED file compared field-by-field against what was sent (exact-match, owner's call) - tick-quantized positions, velocity within 1/127.
- `set_channel_levels` raw: **discovery task** - find the per-channel volume/pan/pitch event ids by diffing bytes of a template before/after pyflp sets them (pyflp's writer mapping is the map even though its serializer is untrustworthy), then live-verify.
- **Differential oracle:** a dev-only test compares our reader against pyflp across the corpus (`tests/corpus`, 164 projects; pyflp itself fails 3). Parity target: tempo, channel count/names, note tuples on every project both parsers read.
- May NOT touch: anything outside `v2/flp.py` + its tests.

### Lane W - `wire.py` + `server.py`: transport and thin MCP layer

- `v2/wire.py`: port `control_midi.py` (MidiControl, ControlUnavailable, codec import from the installed `flmcp_codec` package). No singleton. The C-callback swallow stays but **counts** drops; the counter is exposed for `fl_doctor`.
- `v2/server.py`: lifespan builds the session via `create_session()` and injects through the framework `Context` (verified supported). Three tools: `run` (execute Python against `fl`), `get_notes`, `fl_doctor`. `main()` configures logging to **stderr** (stdout is the JSON-RPC wire).
- May NOT touch: flp, spool internals (uses `session` only).

### Lane S - `session.py` + `spool.py`: the one owner of FL state

- `v2/spool.py`: ONE protocol (the sequenced commands/responses dirs). The v0 single-pending-file generation dies at cutover, which requires updating the piano-roll pyscript in lockstep - that update is this lane's deliverable too, with its fail-closed ledger preserved.
- `v2/session.py`: `create_session()` factory; explicit backend choice (live plane when connected, else project file + `_reload_fl`, else raise `NotePlaneUnavailable` naming the fix); write-through to `store`; typed `DoctorReport`.
- May NOT touch: flp internals (calls `flp.*`), wire internals (calls `MidiControl`).

### Lane K - `sdk.py` + `store.py`: the product surface

- `v2/store.py`: merge records/actions/hashing/diff essentials per the skeleton. Pure data, no FL IO.
- `v2/sdk.py`: kernel minus queue/flush/cell. `Track.write()` applies immediately and raises on rejection; every read returns confirmed state. Port `theory` integration.
- May NOT touch: transport or file formats.

### Lane E - e2e + gates (continuous)

- Extends `tests/e2e/` for v2: notes exact-match readback live test, channel-levels live test, tempo tests (exist), one SDK smoke (`run`-tool driving a write end to end).
- Owns the gate checklist below; a lane is done when its gate row passes.

### Lane V - cutover + purge (manager-run, last)

- Flip imports v2 -> `fl_mcp` proper, delete the 13 dead v1 modules + pyflp runtime dependency, update install/docs, final live e2e + full suite, single PR.

## Gates

| Gate | Proves | Command |
|---|---|---|
| G1 (F) | reader parity on corpus + writers live-verified | corpus differential test green; `FL_E2E=1 pytest tests/e2e/test_flp_writer_live.py` |
| G2 (W) | transport + 3 tools work with injected session | tool tests + live hello/tempo round trip |
| G3 (S) | one spool protocol end to end incl. updated pyscript | pyscript suite + live manual-lane check |
| G4 (K) | immediate-mode SDK against fakes + live smoke | sdk tests + `run`-tool live note write |
| G5 (V) | purge complete, nothing references v1 | full suite + live e2e + `grep` sweeps (no pyflp import in src, no v1 module names) |

## What stays untouched by everyone

`pitch.py`, `clock.py`, `theory.py`, `flpaths.py`, `errors.py` (trim only), the controller script's frozen codec (`flmcp_codec.py`), `harness/` (except the pyscript update in Lane S), and PR #35's branch.

## Manager addendum 1 (2026-08-27, verified in the manager worktree)

Three facts in this document were imprecise enough to send Lane F down a wrong
path. They are corrected here; the corrected form is the contract.

### A1. The differential corpus is FL's own bundled projects, not `tests/corpus/data`

`mcp-server/tests/corpus/` is a *fetch* manifest of **4** third-party files
(PyFLP's fixtures), downloaded on demand and gitignored. It is not the 164.

The 164 projects are the ones FL Studio ships, on this machine at:

```
$(fl_mcp.flpaths.fl_app())/Contents/Resources/FL/Data/**/*.flp
```

164 files exactly (Demo projects, Templates, System/Render tests). They are read
in place, never copied into the repo and never redistributed - so the corpus
test resolves them through `flpaths.fl_app()` and **skips when FL is absent**,
exactly like the e2e suite. The 4 fetched files stay in the differential set too;
they are simply not where the number came from.

### A2. pyflp is unusable without the compat shim, and it fails 1 of 164, not 3

Bare `import pyflp; pyflp.parse(path)` fails **164 of 164** on this venv's
Python 3.12 - every file, with the misleading
`<enum 'EventEnum'> has no members` `TypeError`. That is not a corpus property;
it is PyFLP 2.2.1 being broken on 3.12 (see `harness/flp_compat.py`).

The oracle must be reached as:

```python
from harness.flp_compat import import_pyflp, parse
import_pyflp()          # idempotent; raises clearly if PyFLP changes shape
project = parse(str(path))
```

Measured with the shim applied: **163 parse, 1 fails** -
`Demo projects/Demo songs/Astes - Bien Duro.flp`, `StringError: cannot use
encoding 'utf-16-le' to decode ...`. So the parity target is the 163 pyflp reads;
the 164th is a *reader-robustness* case - our reader must fail cleanly with
`FlpError` or parse it, never crash or hang, and the test asserts that
explicitly rather than skipping it.

### A3. Lane worktree setup (each worker's first command)

A fresh worktree has no venv. From the repo root:

```sh
uv venv --python 3.12 mcp-server/.venv
uv pip install --python mcp-server/.venv/bin/python -e . -e ./mcp-server pytest pyflp python-rtmidi
```

Both editable installs are required: the root package supplies `harness` and
`flmcp_codec`, the `mcp-server` one supplies `fl_mcp`. Baseline on
`feat/v2-rewrite` before any lane work: **257 passed, 7 skipped** in ~16s
(`cd mcp-server && .venv/bin/python -m pytest -q`).
