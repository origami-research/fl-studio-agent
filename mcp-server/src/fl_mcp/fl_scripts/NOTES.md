# Spike 3 findings - control-plane echo (`device_FLMCP.py`)

Everything below either upgrades or corrects an `[ASSUMED]`/`[REPORTED]` item
from docs/05-controller-script.md and docs/03-protocols.md, or records a new
one this spike surfaced. No FL Studio install was available in this
environment, so **nothing here was run against a live FL Studio process** -
see the "could not verify" section for exactly what that leaves open.

## Method

Every FL API name/signature claim below comes from reading the source of
`IL-Group/FL-Studio-API-Stubs` directly (`gh api repos/IL-Group/FL-Studio-API-Stubs/contents/...`
against the `main` branch), not the rendered docs site - the site's markdown
frequently drops function bodies and signatures that the source has. These
are community-maintained, Image-Line-endorsed *type stubs*: signatures are
trustworthy, but they describe the interpreter's interface, not its runtime
behavior. "Confirmed" below means "confirmed against the stub source"; it
does not mean "confirmed against a running FL Studio."

## Corrections to docs/05's draft capability table

| docs/05 said | Verified reality | Source |
|---|---|---|
| `general.getVersion` supplies `hello.fl_ver` (capability table lists `getVersion` under `general`) | `general.getVersion()` returns the **API** version (an `int`), not FL Studio's version. The FL version string is `ui.getVersion(mode=4)` (default arg, returns `str`). `device_FLMCP.py` calls `ui.getVersion()` for `fl_ver` - matching docs/05's own *code sample*, which already had this right; only the *capability table* was wrong. | `general/__fl_state.py`, `ui/__init__.py` |
| `device.midiOutSysex` - name "validated in Spike 3" | Confirmed: `midiOutSysex(message: bytes) -> None` | `device/__device.py:162` |
| `mixer.getCurrentTempo()` - name `[ASSUMED]` | Confirmed: `getCurrentTempo(asInt: bool = False) -> int \| float` | `mixer/__properties.py:81` |
| `processRECEvent(REC_Tempo, bpm*1000, REC_Control\|REC_UpdateControl)` - `[REPORTED - community scripts]` | The stubs repo's own module docstring gives this **exact** worked example (set tempo to 80.5 BPM via value `80500`). Upgraded from REPORTED to "verified against the stub source's own reference example," still short of a live-FL run. | `midi/__rec_events/global_properties.py` |
| - (not called out before) | `plugins.setParamValue`'s positional order is `(value, paramIndex, index, slotIndex=-1, pickupMode=0, useGlobalIndex=False)` - **value comes first**, the reverse of `getParamValue`'s `(paramIndex, index, slotIndex=-1, useGlobalIndex=False)`. Since the wire calls these positionally via `fn(*args)`, a server-side arg-order mixup here fails silently as a type error deep in FL, not a clean validation error. Worth a comment or a schema check server-side. | `plugins/__init__.py` |

## Roadmap Q17 - undo-stack introspection, what's possible

FL's full undo surface - confirmed from `general/__undo.py`:
`saveUndo`, `undo`, `undoUp`, `undoDown`, `undoUpDown`, `restoreUndo`,
`restoreUndoLevel`, `getUndoLevelHint`, `getUndoHistoryPos`,
`getUndoHistoryCount`, `getUndoHistoryLast`, `setUndoHistoryPos`,
`setUndoHistoryCount`, `setUndoHistoryLast`.

**Possible:**
- Numeric position in the undo stack (`getUndoHistoryLast()`, `0` = most
  recent) and total stack size (`getUndoHistoryCount()`), plus a formatted
  string (`getUndoLevelHint()`).
- Inferring that *something* changed the stack: call `saveUndo(name, flags)`
  after an MCP write, then check whether `getUndoHistoryLast()` moved to `0`
  - if so, the write is now the top step, *assuming nothing else touched the
  stack in between*.
- Programmatic navigation (`undoUp`/`undoDown`/`undoUpDown`/`setUndoHistoryLast`).

**Not possible:**
- **There is no getter that returns the name string passed to `saveUndo`.**
  `getUndoLevelHint()` sounds like it could be a description, but its actual
  body - per the stub's own reference implementation - is just
  `f"{getUndoHistoryLast() + 1}/{getUndoHistoryCount()}"`: a position
  fraction, not a label. FL's undo history has no readback of *what* a step
  was, only *where* you are in it.
- Consequently, **the server cannot ask FL "whose step is on top?" after the
  fact.** It can only infer authorship by convention - save an undo point
  right after every MCP write and trust nothing interleaves before the next
  position check. A human undo/redo/edit landing in that same window is
  indistinguishable from the server's own step by content; only timing tells
  them apart, and only while the server is the sole writer for that instant.
- This confirms docs/11's existing hedge ("undo introspection may be too
  limited... falls back to convention") is the *correct* fallback, not a
  pessimistic placeholder - the API genuinely has no name/description
  readback to build anything sturdier on.

Implemented: the `state.undo_probe` composite op returns
`{history_pos, history_count, history_last, history_hint}` in one SysEx round
trip, so the server can snapshot position before and after its own
`general.saveUndo` call without four separate requests.

## Roadmap Q20 - `score.selection` availability for note-level selection

Checked `IL-Group/FL-Studio-API-Stubs`, `src/piano_roll_scripting/flpianoroll/`
(the piano-roll `.pyscript` surface - `Score`, `Note`, `Marker` classes).

**There is no `score.selection` attribute.** The docs/08 open-question
phrasing was shorthand, not a real API name. What actually exists:

- **`Note.selected`** (`flpianoroll/__note.py`) - a per-note read/write
  `bool` property: "Whether the note is selected within the piano roll." To
  read the full selection you iterate: `[score.getNote(i) for i in
  range(score.noteCount()) if score.getNote(i).selected]`. There is no bulk
  "give me the selected indices" call - it's a per-note flag, checked one at
  a time.
- **`Score.getTimelineSelection() -> tuple[int, int]`** (`flpianoroll/__score.py`)
  - the selected *timeline range* (coarser: a tick span, not which notes),
  separate from per-note selection.
- Markers have no independent selected flag; a marker's selected state is
  defined as "all notes within them are selected" (docstring on
  `Score.clear`/`clearMarkers`), derived from note selection rather than
  stored.

**This is entirely a piano-roll-scripting fact, not a controller-script
one.** `flpianoroll`'s own module docstring is explicit: *"this module is
not accessible in MIDI Controller Scripts, it can only be used in scripts
that run in FL Studio's piano roll."* `device_FLMCP.py` (this script)
cannot import it, cannot call it, and has no path to note-level selection -
confirmed by the same absence that makes note read/write control-plane-only
impossible (docs/01). Reading note-level selection therefore stays exactly
where docs/11's readback ladder already puts it: step 2 (piano-roll script
dump), manually triggered from FL's Tools ▸ Scripting menu, same as note
reads - never live, never push-based, and out of this spike's surface
(control plane) entirely. Nothing to implement here; this closes Q20 as
"confirmed API shape, confirmed wrong surface" rather than leaving it open.

## Seam check - SysEx payloads vs. `mcp-server/src/fl_mcp/schemas.py`

Read-only on `schemas.py` per scope; verified every payload this script
sends/receives against `ControlRequest`, `ControlResponse`, and `HelloEvent`
by round-tripping real dict shapes through the actual pydantic models (not
just by inspection) - see the empirical check below.

**Bug found and fixed:** `ControlResponse.id` is a required `int` (not
`int | None`). `OnSysEx`'s error path for a frame corrupted badly enough
that the sender's id can't be recovered (bad version byte, unparseable
base64/JSON) was sending `{"id": None, ...}`, which `ControlResponse`
rejects - a response the server couldn't have parsed. Fixed by introducing
`UNKNOWN_ID = -1` (outside the server's "monotonically increasing per
session" id range per docs/03) and using it everywhere a response's id can't
be trusted: the `OnSysEx`/`FrameError` path, and `_dispatch` itself now
coerces any missing/`None`/non-`int` `id` in an inbound request to
`UNKNOWN_ID` before echoing it back, instead of trusting the sender.
`tests/test_framing.py::test_dispatch_never_echoes_a_non_int_id` and
`::test_onsysex_reports_unknown_id_for_a_frame_error` cover this.

**Confirmed correct (validated against the live pydantic models, not just
read):**
- `ControlRequest` round-trips through `encode_frame`/`decode_frame`
  byte-for-byte unchanged (`id`, `op`, `args`, `kwargs`).
- Every `_dispatch` response shape (`ping`, unknown op, exception-with-trace,
  the corrected id-recovery cases) validates as `ControlResponse`.
- The `hello` event payload (`{"op": "hello", "script_ver", "fl_ver", "ppq",
  "tempo"}`) validates as `HelloEvent` two ways: with `"op"` stripped, and
  *without* stripping it - `HelloEvent` (and every model in schemas.py) has
  no `model_config`, so pydantic v2's default `extra="ignore"` silently
  drops the wire-level `"op"` key. Confirmed empirically, not assumed: no
  server-side stripping step is required before validating.

**Gap, not a bug - flagging for Lane C:** `schemas.py` has no models at all
for the freshness-layer-1 events this script emits -
`dirty_channel`, `dirty_mixer_track`, `refresh`, `project_load`
(`OnDirtyChannel`/`OnDirtyMixerTrack`/`OnRefresh`/`OnProjectLoad` in
docs/11). Confirmed by listing `schemas.py`'s actual exports - only
`HelloEvent` exists on the event side. The wire shape for each (documented
inline in `device_FLMCP.py`) is:
- `{"op": "dirty_channel", "index": int}`
- `{"op": "dirty_mixer_track", "index": int}`
- `{"op": "refresh", "flags": int}`
- `{"op": "project_load", "status": int}`

These are simple enough that the server may choose to consume them as loose
dicts rather than pydantic models - noting the gap rather than filling it
myself, since it's a schemas.py change and out of this lane's write scope.

## [ASSUMED] - could not verify (no FL Studio available here)

1. **`json`/`base64` stdlib availability inside FL's embedded Python 3.9**
   (open question 10). Everything here is written and tested against CPython
   3.13; the embedded interpreter's actual stdlib subset is unverified. If
   either is missing, `device_FLMCP.py` fails at import time inside FL and
   needs the 7-in-8 packer fallback docs/03 mentions - not implemented here,
   since building an untestable fallback for an unconfirmed problem is
   speculative work.
2. **`OnSysEx` firing for our frames at all**, and `device.midiOutSysex`
   actually reaching a virtual output port. The frame shape
   (`F0 7D 4D 43 <ver> <type> <payload> F7`) matches docs/03 exactly and
   `midiOutSysex`'s signature is confirmed, but nothing here proves FL's
   SysEx routing behaves as documented end to end.
3. **SysEx frame-size cap across drivers** (open question 14) -
   `MAX_FRAME_BYTES = 512` is taken directly from docs/03, not re-verified
   against CoreMIDI/loopMIDI behavior with large frames.
4. **Throughput/latency** ("ops per second sustained without FL UI jank") -
   unmeasurable without FL running.
5. **Ctrl+S emulation for save** (open question 13). The `ui` module's
   keyboard submodule - confirmed from `ui/__keyboard.py` - exposes exactly
   `cut, copy, paste, insert, delete, enter, escape, yes, no, up, down, left,
   right`. **There is no generic "send key combination" function and no
   save-specific one.** docs/05's assumption ("keyboard emulation of Ctrl+S")
   does not correspond to anything in the stubs - the mechanism it describes
   may not exist via this API surface at all. Left unimplemented here
   (`sync_from_project` is M4 scope, not Spike 3); flagging now so M4 doesn't
   inherit a false assumption.
6. **Autosave file locations/format stability** (open question 19),
   **`OnDirtyChannel`/`OnRefresh` coverage of pattern/note edits** (open
   question 18), and **`score.selection` availability** (open question 20) -
   none are testable from the controller script in isolation; out of scope
   for this spike's deliverable (transport/mixer/plugin round trips + event
   forwarding).
7. Whether `OnSysEx` needs to mark the event handled to stop it propagating
   to other MIDI callbacks. `FlMidiMsg` exposes a `.handled`-style API
   surface per the stub, but its necessity for SysEx specifically is
   unconfirmed; not set here.

## Design choices worth flagging in review

- **`device_FLMCP.py` is import-safe outside FL.** All FL-only modules
  (`transport`, `mixer`, ..., `device`, `midi`) are imported through one
  `try/except ImportError` block and set to `None` on failure. This isn't
  for graceful degradation inside FL (they're always present there) - it's
  what lets pytest and the host probe script import the framing/dispatch
  logic directly, so the wire format used by tests, the probe, and the real
  script can never drift apart. `fl-scripts/controller/probe/host_probe.py`
  imports `encode_message`/`decode_frame`/`Reassembler` straight from this
  file rather than reimplementing them.
- **Chunk envelopes carry base64 text, not raw JSON**, so reassembly is a
  single decode step (`base64.b64decode` -> `json.loads`) whether or not the
  message was chunked. Splitting mid-base64-group is safe (base64 is plain
  ASCII), so this costs nothing but is simpler than tracking two reassembly
  paths.
- **`fn_name.startswith("_")` is blocked** in the dispatcher on top of the
  module whitelist docs/05 specifies - defense-in-depth, not a documented
  requirement; calling it out in case it's considered unnecessary scope
  creep.
- **`state.snapshot`/`state.undo_probe` were the only composite ops as of
  Spike 3.** Every other op was a 1:1 passthrough to the whitelisted module,
  per docs/05 - new server-side capability should stay a server-side change,
  not grow this file's dispatch table. This spike adds two more,
  `mcp.import_tools`/`mcp.call` (see "MCPTools bridge" below) - justified as
  an exception since they bridge to a whole separate FL-bundled module, not
  server-side logic that could live anywhere else.

## MCPTools bridge - can `device_FLMCP.py` import FL Studio 2026's own MCPTools.pyc?

FL Studio 2026 ships `System/Tools/MCP/MCPTools.pyc`, a module used by FL's
built-in Gopher assistant.
It is `mcp_tool`-decorated with ~50 functions covering everything our own
`ALLOWED` table reaches plus real gaps: `add_channel`/`add_effect`/`remove_channels`
(create/destroy, which docs/01's Surface 1 table already says the legacy
Controller Scripting API cannot do), plugin-parameter get/set by name, and
`run_piano_roll_script`/`save_piano_roll_script`.
The hypothesis behind this spike was that `device_FLMCP.py` could `sys.path.insert`
the `Tools/MCP` directory and `import MCPTools` directly, since it appears to
import the same FL API modules (`transport`, `channels`, `mixer`, ...) our own
script already imports.

**Answer: no, not directly, and this is provable without running FL.**

`MCPTools.pyc`'s header magic number is `3531` (`cb 0d 0d 0a`), which is
CPython 3.12's bytecode magic.
docs/01 documents the MIDI Controller Scripting interpreter (where
`device_FLMCP.py` runs) as **embedded Python 3.9** - and that's independently
confirmable on this machine: the system's real Python 3.9.6 binary
(`/usr/bin/python3`) reports magic `3425` via
`importlib.util.MAGIC_NUMBER`, and attempting `import MCPTools` under that
exact binary reproduces the failure directly:

```
$ /usr/bin/python3 -c "import MCPTools"
ImportError: bad magic number in 'MCPTools': b'\xcb\r\r\n'
```

This is a hard bytecode-version wall, not a missing-module problem: CPython's
import machinery checks the magic number before unmarshalling and refuses a
mismatch outright, regardless of `sys.path` or whether the FL API modules it
needs are importable.
No amount of path manipulation from inside `device_FLMCP.py` fixes this - the
`.pyc` would need to be recompiled for 3.9, and we don't have the `.py`
source to recompile from (see method below).

**Where MCPTools actually runs, as far as we can tell:** `FLEngine_x64.dylib`
contains the strings `TMCPServer`, `MCPServer`, `TMCPPythonSession`,
`PyModule_MCP`, `"jsonrpc": "2.0"`, and a
`window.flHelper.onMCPTools` JS-bridge call - consistent with a separate,
purpose-built Python session (backing a webview-based Gopher assistant UI)
driving MCPTools, not the legacy per-device controller-script interpreter.
The FL 2026 bundle separately ships a full CPython 3.12 framework at
`Contents/Resources/FL/Shared/Python/Python.framework/Versions/3.12` (with
`libpython3.12.dylib` and a real `lib/python3.12` stdlib tree) - a plausible
home for that session, though `otool -L` on `FLEngine_x64.dylib` shows no
link-time dependency on it, so it is presumably `dlopen`'d at runtime rather
than statically linked; we could not confirm the two are the same process
without a live FL run.

### Method (fully reproducible without FL Studio)

1. Read the `.pyc` header's first 4 bytes as the magic number; compared
   against `importlib.util.MAGIC_NUMBER` under a real Python 3.9.6 install
   (`/usr/bin/python3` on this machine) and under FL's own bundled 3.12
   framework binary, both present on disk already.
2. Reproduced the `ImportError` directly by copying `MCPTools.pyc` next to a
   throwaway script and running `python3 -c "import MCPTools"` under the 3.9
   binary.
3. To read `MCPTools`'s actual contents (since direct import fails and we
   don't have its `.py` source), imported it under a matching CPython 3.12
   instead, with every FL module it imports (`transport`, `channels`,
   `mixer`, `plugins`, `ui`, `general`, `patterns`, `playlist`, `arrangement`,
   `device`, `midi`) stubbed out as inert placeholder modules, plus one
   additional dependency the file needs that isn't in the `Tools/MCP`
   directory at all: `ilmcp` (presumably compiled into FL itself), which
   supplies the `mcp_tool` decorator. Stubbing `ilmcp.mcp_tool` as an
   identity-preserving decorator let the whole module import cleanly, after
   which `inspect.signature` on every top-level function gave the real
   argument names, types, and `Annotated` docstrings-as-hints FL's assistant
   uses. This is why `MCP_TOOL_WHITELIST` in `device_FLMCP.py` is a hardcoded
   set rather than read live via `MCPTools.get_mcp_tools_json()`: that
   function reads a registry populated by the *real* `ilmcp.mcp_tool`
   decorator at call time; under any stand-in decorator (identity or
   otherwise) nothing registers into it, so it always returns `"[]"`
   regardless of what's actually defined in the module.

### What was implemented anyway

`device_FLMCP.py` gained two new composite ops, `mcp.import_tools` and
`mcp.call`, following the existing `state.*` composite-op pattern:

- `mcp.import_tools` (optional `kwargs.tools_dir` override, else tries FL's
  default per-OS install path) attempts the live import at runtime and
  reports `{"imported": bool, ...}` either way - it does not assume the
  finding above holds on every FL install (a different FL version, a custom
  install path, or a controller-scripting interpreter that turns out not to
  enforce magic numbers the same way are all things we didn't verify against
  a live FL Studio process).
- `mcp.call` only dispatches to functions in `MCP_TOOL_WHITELIST` (defense in
  depth beyond "the module happens to have this attribute"), and requires
  `mcp.import_tools` to have succeeded first.

Both are still worth shipping and probing for real, precisely because this
finding is inferred from static analysis and a same-magic-number reproduction
outside FL, not a live FL Studio run - see `fl-scripts/controller/probe/host_probe.py`,
which now chains `hello -> mcp.import_tools -> mcp.call get_session_context ->
mcp.call add_effect` automatically and prints a pass/fail report, so a human
running it once either confirms the import failure predicted here or
discovers we were wrong about something (untested FL version, a controller
script interpreter that isn't real CPython 3.9, etc).

### Fallback path if the import keeps failing on real FL

We don't need the import to work to get value out of this research: the
function list, signatures, and argument semantics extracted above (step 3)
describe exactly what each MCPTools function does against the same
`channels`/`mixer`/`plugins`/`general` modules `device_FLMCP.py` already
imports and already whitelists.
The genuinely new capabilities - `add_effect`/`add_channel`/`remove_channels`
and plugin-parameter access by name instead of index - are each a handful of
FL API calls we can read directly from the decompiled bytecode's semantics
and reimplement as new composite ops in this file, same-version, no
cross-interpreter import required.
This is strictly more implementation work than a free import would have
been, but it is buildable with what we already have visibility into, and
does not depend on FL Studio's Gopher assistant internals staying stable.

## Architecture implication: does this change whether a Bridge (native) plugin is needed?

Short answer: **not by itself, and the interesting question is still open.**

MCPTools' function surface reaches everything our `ALLOWED` table reaches
plus create/destroy operations and, notably, `run_piano_roll_script(source: str)`
- per its `Annotated` argument hint ("The full Python code block for the
script") and per docs/01 Surface 2, this runs arbitrary source as a
`flpianoroll` piano-roll script, which is the *only* surface with persistent
note read/write (`Score`/`Note`, docs/01's "Cannot: ... read piano-roll note
data" is specifically about Surface 1, not Surface 2).
If MCPTools were reachable from outside FL by any channel, `run_piano_roll_script`
alone could plausibly cover note writes end to end - obsoleting both the
note-writing half of the Bridge plugin (docs/04) and docs/11's manual,
human-triggered piano-roll-script step in the read-back ladder.

But that "if" is exactly what this spike closes for one specific channel and
leaves open for another:

- **Closed:** importing `MCPTools` from inside `device_FLMCP.py` (the legacy
  per-device controller-script interpreter) is blocked by the CPython
  bytecode-version wall documented above. This channel does not give us a
  path to MCPTools, piano-roll scripting, or note writes.
- **Open:** whether FL's own MCP server (`TMCPServer`/`TMCPPythonSession`,
  the JSON-RPC 2.0 strings in `FLEngine_x64.dylib`) is reachable by some
  *other* external channel - a local socket or pipe, independent of our own
  SysEx control plane and independent of `device_FLMCP.py` entirely. We did
  not probe for this; it's a distinct spike (does `TMCPServer` listen
  anywhere, is it authenticated, is it loopback-only to FL's own Gopher UI).

If that second channel turns out to be open, it is the more direct route to
note writes than anything in this repo today, native plugin included -
worth a dedicated spike before investing further in docs/04's Bridge plugin
for the note-write case specifically.
If it turns out to be closed (FL-UI-only, entitlement-gated, or otherwise
unreachable from an external process), nothing here changes docs/04's
premise: `FHD_AddNotesToPR`-grade persistent note writes still require an
in-process native plugin (docs/01 Surface 3), since that dispatcher call has
no other path in, and MCPTools brings no new leverage on it from outside FL.

## Test coverage

`mcp-server/tests/test_controller_framing.py` - 30 tests, all green under
`uv run pytest tests/test_controller_framing.py` from `mcp-server/`. Covers
encode/decode round trips, 7-bit-safety of the frame body, rejecting foreign
SysEx traffic (wrong magic, silently ignored) vs. raising on traffic that IS
ours but corrupted (wrong version, bad base64/JSON), chunking + reassembly
(including out-of-order and interleaved concurrent messages), the dispatch
whitelist (unknown ops, unwhitelisted modules, blocked private names,
exception capture with traceback), the `state.*` and `mcp.*` composite ops
(including `mcp.import_tools` against a real throwaway stub module on disk,
and `mcp.call`'s whitelist/import-order/argument-forwarding/exception
behavior against a monkeypatched fake `MCPTools`), and the unknown-composite-op
path for both namespaces.

Not covered by pytest, because it needs a real FL Studio process: every item
in the `[ASSUMED]` section above, plus whether `mcp.import_tools` actually
succeeds or fails as predicted on a live FL Studio 2026 install.
`fl-scripts/controller/probe/host_probe.py` is the tool for a human to close
that gap - see its module docstring for setup (MIDI settings: input
"FLMCP Req" -> FLMCP Bridge Script, output "FLMCP Res"); it now drives the
full MCPTools bridge chain automatically once `hello` arrives and prints a
pass/fail report.

## Lane C: OnIdle deferral + shared codec (docs/15 fix items 1, 6)

**OnIdle deferral for "Operation unsafe at current time" (docs/15 item 1,
docs/14's discovered limitation).** `_dispatch`'s direct ALLOWED-module
passthrough now catches `RuntimeError` whose message contains "unsafe at
current time", parks `{message, req_id, ticks: 0}` on `_deferred_ops`, and
returns `None` instead of an error response - `OnSysEx` skips sending when
`_dispatch` returns `None`. `OnIdle` calls `_retry_deferred_ops()` first
(ahead of the existing ctl spool poll, unchanged): each tick re-calls the
same FL function for every parked op; a success sends the real response, a
different exception sends that error, and a repeated "unsafe" error is
re-parked up to `DEFER_MAX_TICKS` (50, ~1s at the ~20ms OnIdle cadence
docs/14 observed) before giving up and sending the original error. Composite
ops (`state.*`, `mcp.*`, `ctl.*`) are unaffected - deliberately scoped to the
passthrough path, which is where every mutating op (the ones docs/14 saw hit
this guard) lives. Tests: `test_framing.py`'s new "OnIdle deferral" section
fakes an ALLOWED module whose function raises the guard error N times then
succeeds (and a variant that never succeeds, to prove the bounded give-up),
driving `_dispatch` + `OnIdle` directly, no FL required.

**Shared wire codec (docs/15 item 6).** `flmcp_codec.py` (new file, this
directory) is now the single source of truth for `TYPE_*`, `MAGIC`/framing
constants, `encode_frame`, `encode_message`, `decode_frame`, `FrameError`,
`Reassembler`. `device_FLMCP.py` embeds it verbatim between
`# === BEGIN flmcp_codec.py (embedded verbatim) ===` /
`# === END flmcp_codec.py ===` markers so it stays the one standalone,
stdlib-only file FL loads - nothing about how FL loads this script changes.
`mcp-server/tests/test_codec_sync.py` asserts the embedded block is byte-identical to
`flmcp_codec.py` and that `flmcp_codec.py` is independently importable, so
the two can't silently drift.

**Packaging (2026-09-24).** This directory ships inside the `fl-studio-agent` wheel as
`fl_mcp.fl_scripts`. The server imports `fl_mcp.fl_scripts.flmcp_codec` directly, and
`fl-mcp-install` copies `device_FLMCP.py` and the piano-roll script out of the installed
package, so no repository checkout is needed at runtime.
