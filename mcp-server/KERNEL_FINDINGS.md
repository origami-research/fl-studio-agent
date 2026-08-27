# Lane D findings — notebook kernel + bootstrap (M3)

Scope: `mcp-server/src/fl_mcp/kernel.py` (+ `theory.py` for the pure composition
helpers), built on Lane C's `session.py`/`records.py`/`actions.py`. Tests:
`tests/test_kernel.py`, `tests/test_theory.py`. 150/150 passing
(`uv run pytest`).

## Seam decisions (schemas.py untouched, two small additive extensions elsewhere)

**schemas.py: no changes.** Everything below was reachable without touching the
frozen seam.

### 1. Issue #11 (dirty_channel/dirty_mixer_track/refresh/project_load) — resolved as "loose dicts"

Issue #11 explicitly left this as an open call: add pydantic models for these
four events, or consume them as loose dicts server-side, "either is fine, but
should be a deliberate call." Stale-handle detection (this lane's explicit
scope: "if the project was reloaded/undone since the last cell, the session
must detect it and refuse to act on stale handles") needs *some* signal from
these events, so I made the call: **loose dicts**. The kernel only ever reads
`payload["op"]` — full validation of `index`/`flags`/`status` buys nothing it
uses.

To make that possible, `control.py` gained one additive, backward-compatible
hook (`ControlChannel.add_event_listener`); previously any event payload that
wasn't `hello` was silently dropped (the comment at that call site literally
said "handled by whoever owns the channel — extend here as those land"). No
existing test's behavior changed; `test_control.py`'s two tests that monkeypatch
`_dispatch` wholesale are unaffected since they replace the method entirely.

If a future lane wants typed events instead, `add_event_listener`'s callback
signature (`dict[str, Any] -> None`) doesn't need to change — it would just
start receiving `schemas.SomeEvent.model_validate(payload)` at the call site
inside kernel.py, still no schemas.py change required unless the *model*
itself needs to exist for another consumer.

### 2. errors.py: one new category, `stale_handle`

Not the frozen seam, but shared taxonomy, so flagging it here too. Follows the
exact existing pattern (category + factory function alongside
`unknown_target`, `control_plane_unavailable`, etc.) — `errors.stale_handle(kind, name)`.

## Deferred, not blocked: bootstrap's ground-truth note read

docs/11's bootstrap sequence has four steps; step 2 is "save a copy of the
project and read the notes out of it, so records start from ground truth."
That read is `sync_from_project` (save → PyFLP → shadow rebuild) — **explicitly
M4 scope** per docs/08's milestone table, and not implemented anywhere in this
codebase yet (confirmed: no PyFLP dependency in `pyproject.toml`, and the
`get_notes` tool in `server.py` already says as much: "fresh read - not
implemented yet, see sync_from_project in docs/11").

`kernel.bootstrap()` does the other three steps for real (asks FL for the
structural layout via `state.snapshot`, persists `manifest.json`, opens the
action log with a `bootstrap` entry) and takes an optional `project_reader`
callable as the seam for step 2. With no reader supplied — the only option
today — the action-log entry records `ground_truth_notes: "pending: ...
M4 scope, not implemented yet"` rather than silently pretending the read
happened. Not a design gap on this lane's part; wiring `sync_from_project` in
as the reader is a one-line change once M4 lands.

## Known limitation: undo detection is model-initiated only

`fl.undo_last()` (the kernel calling `general.undo` on the model's behalf,
confirmed dispatchable — `device_FLMCP.py`'s generic `mod.fn` reflection
already reaches `general.undo` with no special-casing needed on the
controller side) correctly bumps the generation and marks the undone target's
shadow entry stale. What it *cannot* do: detect a human pressing Ctrl+Z
outside the model's control. docs/11's own "Known problems" section already
flags this ("Undo introspection may be too limited to tell whose edit is on
top... falls back to convention") — `fl-scripts/controller/NOTES.md` (Lane B,
Spike 3) confirms FL exposes undo-stack *position* (`getUndoHistoryPos`,
`getUndoHistoryLast`) but never the name string passed to `saveUndo`, so there
is no reliable "was this an MCP action or a human one" signal today. Treating
every `dirty_channel`/`dirty_mixer_track`/`refresh`/`project_load` event as a
generation bump (this lane's choice, see above) is the closest available
mitigation — a human undo that touches channel/mixer structure trips one of
those and invalidates handles; a human undo that only reverts note content
inside a channel the model already believes it owns does not, and the shadow
score would need a fresh read to catch it (Layer 3, also M4-adjacent). Not
something this lane can close without the Layer-1 signals genuinely covering
note-level undo, which docs/11 already documents as out of reach for now.

## What's implemented

- `FLKernel` (the `fl` object): `track(name_or_number)` with per-name caching
  guarded by a generation counter; `play()`/`stop()`/`set_tempo()`/
  `undo_last()`; `flush()`; `cell()` context manager (flushes on clean exit,
  not on an exception — an errored cell shouldn't apply partial work).
- `Track` (`bass = fl.track("Bass")`): `.notes(filter?, pattern?)` → lazy
  `NoteSetHandle` (summary via the existing `notes.describe()`, keys resolved
  to MIDI ints regardless of how they were originally written);
  `.write(notes, mode, pattern?)` → queues, applied on `flush()`; `.clear()`.
- Stale-handle refusal: checked both when a `Track` method is called *and*
  again inside `flush()` (a write can be queued fresh and then go stale before
  the cell ends if an event lands mid-cell — covered by
  `test_queued_write_goes_stale_between_queueing_and_flush`).
- Bootstrap: `state.snapshot` → `Manifest` (channels/mixer/pattern_count/ppq)
  persisted to `<project>/manifest.json`, `bootstrap` action-log entry.
- Composition helpers (`theory.py`, pure math, no FL round trip):
  `chord()`, `scale()`, `drums()`, `transpose()`, `quantize()` — all
  non-mutating (return new `Note`s, per this codebase's immutability
  convention already established in `records.py`/`schemas.py`).

## Nothing PENDING on a live-FL-only unknown

Everything above was resolvable from static reading + the fake-FL harness.
No genuine FL-Studio-in-the-loop unknown was hit this lane — the two gaps
above (M4's PyFLP read, human-initiated undo detection) are scoping/design
notes, not unknowns that a live run would resolve differently.
