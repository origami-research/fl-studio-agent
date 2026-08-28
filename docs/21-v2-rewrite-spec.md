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

## Manager addendum 2 (2026-08-27, live FL 2026 Producer Edition, manager worktree)

Gate-rig validation turned up two live facts and one real v1 bug. All three were
measured against real FL, not reasoned about.

### A4. The live rigs work; here is the honest baseline

- `FL_E2E=1 pytest tests/e2e/test_flp_writer_live.py -m e2e` -> **1 passed in 12.18s**,
  clean boot and teardown, no modal intervention needed (licensed FL, as fact 6 predicted).
  G1's live half is proven working.
- `FL_E2E=1 pytest tests/e2e/test_control_plane_e2e.py -m e2e` -> **4 passed, 2 failed**.
  The two failures are A5 and A6 below. Neither is caused by v2 work; both are
  pre-existing v1 defects that the rewrite fixes.

### A5. v1 silently discards every CHUNKED control-plane response (real bug)

`state.snapshot` and `ctl.selftest` never answer - not at 8s, not at 45s - while
small ops (`channelCount`, `getChannelName`, `undo`, `state.undo_probe`,
`getCurrentTempo`, `setCurrentTempo`) answer in under 3s.

Instrumenting the raw rtmidi callback during one `state.snapshot`:

```
12 MIDI messages, 4796 bytes
frame types: TYPE_CHUNK (0x4) x10, TYPE_EVENT (0x3) x3, TYPE_RESPONSE (0x2) x1
result: caller timed out at 40s
```

The complete response **arrives** and reassembles. `control_midi._on_message` then
routes on `frame_type`, which is still `TYPE_CHUNK` for a reassembled message, so it
matches neither the `TYPE_RESPONSE` nor the `TYPE_EVENT` branch and the finished
message is dropped. Every composite read - the ones docs/11 calls the ops "the server
calls most" - has therefore never worked over MIDI.

The codec is not at fault: chunking round-trips correctly in
`fl-scripts/controller/tests/test_framing.py`. Only the *routing* after reassembly is
wrong, and that is server-side, which is why in-process tests never caught it.

Fixed in v2 by Lane W (found independently by reading, confirmed by this measurement):
a reassembled chunk is classified by shape - the controller's responses carry `ok`,
its events do not. This must stay covered by a non-live test for both directions
(chunked response correlating to its request; chunked event dispatching to listeners).
`dropped_frames` must NOT count this path - those frames are valid - so that
"malformed input" and "large response" stay distinguishable in `fl_doctor`.

### A6. The first engine call after a project load can time out, and that is not a failure

Measured: an identical `mixer.getCurrentTempo` FAILED at a 5s timeout as the first
call after hello, then succeeded seconds later. FL parks the call as "unsafe at
current time" (fact 6) and the controller gives up first - `DEFER_MAX_TICKS = 200`
is only ~4s at FL's ~20ms OnIdle cadence. This is why
`test_control_plane_e2e.py::test_tempo_round_trips` fails with its 6s budget: it is
the first engine call after boot.

Consequences, both load-bearing:

- **Live-plane writes DO land.** `mixer.setCurrentTempo(141, 1)` -> ok, and
  `getCurrentTempo` read back `141000`; `transport.stop` -> ok. `session.set_tempo`'s
  live-first path is verified, not aspirational.
- **A single timeout is not evidence of a dead control plane.** After `_reload_fl`
  the live plane is briefly present-but-unresponsive. Treating that warm-up timeout as
  `NotePlaneUnavailable` and falling through to another backend would reintroduce
  exactly the v1 cascade bug Lane S exists to kill. It means "busy", not "absent".

Do not hide either fact inside a retry loop in `send_request`; a swallowed retry makes
a genuinely dead control plane indistinguishable from a slow one. `timeout` is already
a caller-visible parameter on the frozen signature - that is the seam.

### A7. CI is red for a non-code reason (human item, not a lane blocker)

Every GitHub Actions run on this repo fails in ~4s having executed **zero steps**
(10/10 runs, including on `main` and on PR #35). That is a repo/account-level Actions
problem - billing or org policy - not a workflow or code defect, and it is not
fixable from here. The gates in this document are all local commands and are
unaffected. Flagged for the owner.

## Manager addendum 3 (2026-08-27): the channel-levels map, SOLVED

Lane F's first worker was killed with an uncommitted context and no commits. Its
transcript survived, so the discovery was recovered from it and then re-verified
directly against FL's own files. **The Lane F "discovery task" is therefore closed** -
what remains is a live write-verification, which the manager runs.

### A8. Channel levels are ONE event, not three

They live in `ChannelID.Levels` = **event id 219**, a fixed 24-byte payload:

| bytes | field | type | range | default |
|---|---|---|---|---|
| `[0:4]`   | pan          | int32 (signed) | 0..12800 | 6400 (centre) |
| `[4:8]`   | volume       | uint32         | 0..12800 | 10000 |
| `[8:12]`  | pitch_shift  | int32 (signed) | semitones | 0 |
| `[12:24]` | filter mod_x / mod_y / type | 3x uint32 | - | - |

So `set_channel_levels` patches three int32s inside one existing event - it does not
splice new events, which makes it markedly simpler than the skeleton's docstring
assumed.

**Evidence (this is the verification, not a citation).** Decoding event 219 straight
out of FL 2026's own stock templates with our own event walker:

```
Basic with limiter.flp         219 size=24 pan=6400 volume=10000 pitch=0 rest=12B  (x4 channels)
Create a chord progression.flp 219 size=24 pan=6372 volume=10000 pitch=0 rest=12B
                               219 size=24 pan=6400 volume=10000 pitch=0 rest=12B  (x3)
Empty.flp                      219 size=24 pan=6400 volume=10000 pitch=0 rest=12B
```

The non-default `pan=6372` in "Create a chord progression" is what proves the field is
live and positional rather than coincidentally-zero padding.

This independently confirms the three constants already in the `v2/flp.py` skeleton:
`LEVEL_MAX = 12800`, `PAN_CENTRE = 6400`, `VOLUME_DEFAULT = 10000`.

**Legacy fallback (older projects).** When a channel has no 219 event, pyflp falls
back to single-value events, and our reader should do the same rather than reporting a
default it did not read: `_VolWord` = 72, `_VolByte` = 2, `_PanWord` = 73,
`_PanByte` = 3. Also resolved while here: `ChannelID.New` = 64, `PatternID.New` = 65,
`PatternID.Notes` = 224, `PatternID.Name` = 193.

**Still open, and it is the manager's to run:** a live write-verification - set a
known volume/pan/pitch, load the file in real FL, read the values back through the
control plane. Byte-diff agreement is not sufficient on its own (AGENTS.md: the fake
always says yes).

### A9. Two reader edge cases the first worker surfaced

1. **`Remix a song.flp` has a 224 (`PatternID.Notes`) event whose size is 34** - not a
   multiple of the 24-byte note record, and it appears *before* any pattern event. A
   reader that assumes `size % 24 == 0` will raise on a stock FL project. Decide
   deliberately: skip a non-conforming blob with a logged warning, or raise `FlpError`.
   Do not discover this by crashing on the corpus.
2. **Text-event encoding is version-dependent**: the `FLVersion` event (id 199, ascii)
   at >= 11.5 means text events are UTF-16-LE. This is the same rot that makes pyflp
   fail `Astes - Bien Duro.flp` (addendum A2).

### A10. Process rule, learned the hard way

An uncommitted investigation is not work; it is a liability. Roughly an hour of Lane F
discovery evaporated because nothing was committed. Every lane commits evidence
*before* it is complete and *before* it is pretty. A worker that has been running for a
while with a clean worktree is a red flag the manager should act on early.

## Manager addendum 4 (2026-08-27): SIGNATURE CHANGE, signed off

### A11. `FLSession` gains `play()` / `stop()` / `undo()`

Lane K flagged this rather than hiding it, which is what the process is for.

**The problem.** `sdk.FL` exposes `play`/`stop`/`undo`, but `FLSession` had no
counterpart, so `sdk.py` had to do this three times:

```python
if self._session.control is None:
    raise errors.control_plane_unavailable("transport.start", 0.0)
resp = self._session.control.send_request("transport.start")
...  # + the operation-unsafe conversion, inlined again
```

That breaks three rules at once: docs/21 says Lane K "may NOT touch transport";
`sdk.py`'s own docstring says it has no helpers because anything reusable belongs in
session/flp/store; and AGENTS.md requires one canonical function per rule - this was
the same conversion written three times, reaching through `session` into `wire`.

**The change.** Three methods added to the `FLSession` frozen surface. Each sends its
control op and RAISES on failure - `ToolError(control_plane_unavailable)` with no
control plane, `ToolError(operation_unsafe)` when FL refuses the call as unsafe at
current time (retryable - addendum A6). They return `None`; handing back a
`ControlResponse` would just relocate the same conversion into every caller.

**Why session and not wire.** `wire.send_request` returning a typed `ControlResponse`
is correct - the transport reports what happened and does not decide policy. Deciding
that "no control plane" and "FL refused" are *errors* is FL-state policy, and
`FLSession` is the one owner of FL state. `sdk.FL.play()` becomes a one-line delegation
and never touches the transport again.

**Owners.** Lane S implements the three bodies (they are thin). The `sdk.py` side -
replacing the three inlined blocks with delegation - is the manager's at integration,
since Lane K has already completed and retired; it is a ~15-line deletion.

This is the only signature change to the frozen surface so far. Everything else in
`v2/` is unchanged from the day-0 skeleton.

## GATE RESULTS (manager-run, re-verified in the manager's own checkout)

### G2 (Lane W) - PASSED 2026-08-27

**Fast half** - `tests/v2/test_wire.py` + `test_server.py`: **30 passed**. Full merged
suite (all lanes) **362 passed, 7 skipped**, `ruff check src tests` clean.

**Live half** - v2 `wire.py` driven against real FL 2026 Producer Edition v26.1.5:

```
[PASS] hello received            fl_ver=Producer Edition v26.1.5 [build 5384] ppq=96
[PASS] window title is FL        title='FL Studio 2026'
[PASS] tempo round trip          getCurrentTempo=141000 (expected 141000)
[PASS] chunked state.snapshot    elapsed=0.0s ok=True
[PASS] snapshot shape            channels=5 mixer=18, keys incl. tempo/ppq/playing
[PASS] snapshot tempo agrees     snapshot.tempo=141000 == readback
[PASS] dropped_frames == 0       for valid traffic
```

The third line is the one that matters: `state.snapshot` is the exact op v1 never
answered - not at 8s, not at 45s (addendum A5) - because it discarded the reassembled
chunked response. Under v2's wire it returns in **0.0s**. The bug measured in A5 is
fixed and the fix is verified against real FL, not against the fake.

`dropped_frames == 0` also confirms the counter does not fire on valid chunked traffic,
so "malformed input" and "large response" stay distinguishable in `fl_doctor` as
required.

Window-title assertion included per fact 5 - a rejected project leaves FL on a default
project whose read-backs look plausible, so tempo assertions never stand alone.

### A8 CLOSED - channel levels live-verified end to end (2026-08-27)

The open item in A8 is now discharged. Method: patch event 219 in a stock template,
load it in real FL, read the values back through the control plane.

**Round 1 failed instructively** and is worth recording, because it is fact 5's trap
firing exactly as documented. FL reported `volume=0.78125` and `pan=0.0` - which are
`10000/12800` and dead centre, i.e. the DEFAULTS - and the window-title read came back
empty, so "did FL even load my file?" was unanswerable. Plausible-looking readbacks
from a default project are indistinguishable from a successful write unless something
independent proves which project is loaded.

**Round 2 added a positive control**: patch the TEMPO to a unique value in the same
file with the live-verified v1 `set_tempo`, and only trust the levels readback if the
tempo readback proves FL loaded our file.

```
set_tempo -> readback 137.0
  219 before: (6400, 10000, 0)      # pan, volume, pitch_shift
  219 after:  (3200,  6400, 5)
[PASS] POSITIVE CONTROL: FL loaded OUR file (tempo==137000)   got 137000
       channel 0: volume=0.5 pan=-0.5
[PASS] volume 6400/12800 -> 0.5    got 0.5
[PASS] pan    3200 -> -0.5         got -0.5
```

So the byte layout in A8 is confirmed by FL itself, **and** the unit conversions are
pinned:

- `volume_0_1  = raw / LEVEL_MAX`            (raw 6400 -> 0.5; default 10000 -> 0.78125)
- `pan_-1_1    = (raw - PAN_CENTRE) / PAN_CENTRE`  (raw 3200 -> -0.5; 6400 -> 0.0)

`set_channel_levels` can be implemented against this with no further discovery.

**Method note for every future live gate in this repo:** never assert a readback from a
file-write without an independent marker proving FL loaded that file. A tempo patch is
a cheap, proven positive control. Round 1 would have been read as "the map is wrong"
when the truth was "FL was never looking at our file".

### G3 (Lane S) - fast half PASSED; live half BLOCKED on a one-time human step

**Fast half - PASSED.** `tests/v2/test_spool.py` + `test_session.py` +
`tests/test_pyscript.py`: **56 passed**. The pyscript suite (19 of those) drives the
updated script against a fake `flpianoroll` and covers exactly the properties that
matter, including the fail-closed ledger: applies-exactly-once, cancelled-never-
applies, corrupt-ledger-refuses-instead-of-replaying, blocked `os.remove` tolerated,
read-only and fully-unwritable spool folders diagnosed rather than silently
duplicating, `encoding=` never used, protocol-version mismatch reported.

Server-side protocol verified live as far as it can be without FL executing the script:
`Spool.send()` wrote `commands/002003.json` and `commands/queue.json`
(`{"proto": 2, "ids": [2003]}`) into the measured writable root
(`Settings/Piano roll scripts/FLMCP`), and the command **stayed queued and unconsumed**
when the script never ran - which is the fail-closed behavior we want.

**Live half - BLOCKED, and it is a human step, not a defect.** The manual lane fires
the pyscript by sending FL the keyboard shortcut bound to it (`harness/trigger.py`,
default Cmd+Opt+Y). On this machine:

- macOS Automation/Accessibility consent **is** granted (`trigger.check_permission()`
  -> granted). That is not the blocker.
- FL launched on our project (window title `g3_probe.flp - FL Studio 2026`), the piano
  roll was opened, and the shortcut was sent 5 times. **No response file ever
  appeared** and the command stayed queued.
- No shortcut binding for the script exists anywhere in FL's settings tree - grepping
  `Settings/` for the script name matches only the controller script.

Conclusion: **the piano-roll script has never been bound to a shortcut inside FL.**
That binding is done in FL's own UI and cannot be scripted - the same class of
one-time human step as the Accessibility grant (`harness/enable_autonomy.py` exists
precisely because TCC consent is un-scriptable; this is its in-FL sibling).

The MCPTools escape hatch is closed and already documented: FL 2026 ships
`run_piano_roll_script` in `System/Tools/MCP/MCPTools.pyc`, but that file is CPython
**3.12** bytecode while the controller script runs in FL's embedded Python **3.9**, so
`device_FLMCP.py` cannot import it (`fl-scripts/controller/NOTES.md`).

**What remains genuinely unproven** (worth being precise, rather than calling this
"basically passed"): the fast suite executes the pyscript under CPython 3.12 against a
fake `flpianoroll`. It therefore cannot prove that FL's *embedded* interpreter accepts
the v2 script - a syntax or sandbox regression introduced by the rewrite would still be
invisible. Only a real run closes that, and that needs the binding.

**To unblock (one-time, ~1 minute in FL):** open the piano roll -> Tools > Scripting,
find "MCP Write Notes", and bind it to a keyboard shortcut (Cmd+Opt+Y is what
`harness/trigger.py` sends by default; any other binding works if passed to
`run_piano_roll_script`). After that, `/tmp/g3_live.py` runs end to end unattended.

### G1 (Lane F) - partial: reader + set_tempo landed and live-verified

`v2/flp.set_tempo` verified against real FL 2026 on **both** code paths, each with the
window-title assertion fact 5 requires:

```
Basic with limiter.flp          file tempo None -> set 137.0    (APPEND path)
  [PASS] set_tempo returns the readback           returned 137.0
  [PASS] re-read confirms                         re-read 137.0
  [PASS] FL LOADED OUR FILE (title)               'g1_Basic_with_limiter.flp - FL Studio 2026'
  [PASS] REAL FL reports 137000                   got 137000

Create a chord progression.flp  file tempo 128.0 -> set 143.0   (MUTATE path)
  [PASS] set_tempo returns the readback           returned 143.0
  [PASS] re-read confirms                         re-read 143.0
  [PASS] FL LOADED OUR FILE (title)               'g1_Create_a_chord_progression.flp - FL Studio 2026'
  [PASS] REAL FL reports 143000                   got 143000
```

So the port of the live-verified v1 path is faithful: in-place u32 patch when event 156
exists, append at end-of-stream when FL omitted it, readback-or-`FlpError`.

Reader also landed. Notable: our reader **fully parses `Astes - Bien Duro.flp`**
(308 channels, 1937 notes) - the one corpus file pyflp fails on (addendum A2). On that
file our library is strictly better than the oracle it is measured against.

Merged tree with the reader + set_tempo: **427 passed, 7 skipped**, ruff clean.

Outstanding for G1: `write_notes` (in progress), `set_channel_levels` (map already
solved and live-verified - A8), and the corpus differential test.
