# R2: ship the live capabilities we already proved

Research deliverable, 2026-08-30.
This is the implementation plan that turns the probe verdicts (`native/probe/PROBE_VERDICT_2026-08-30.md`, hereafter VERDICT) and the bridge live write verdict (`native/bridge/BRIDGE_FINDINGS.md`, hereafter FINDINGS; `RUNBOOK_BRIDGE_V2.md`) into shipped product: the bridge as the default note-write lane, a user-action event stream, live state reads, and the retirement of everything those capabilities supersede.
It is a plan, not code.
Doctrine (#54) binds every choice here: one canonical path per job, capability detection instead of flags, boundaries verify by readback, and every phase names what it deletes.

## 0. Evidence ledger

Every capability this plan builds on, with its verdict citation.
Nothing below rests on a guess.

| Capability | Verdict | Evidence |
|---|---|---|
| Bridge writes notes live, no reload | YES, FL-save-confirmed | FINDINGS "Spool-consume flow"; docs/24 row "Notes - write, SELECTED target" (done 2026-08-29, #65) |
| PatNum honored: write to ANY pattern live | YES, confirmed live | VERDICT Q2 (Pass 4: key 85 to pattern 2 landed in pattern 2) |
| PatNum write switches FL's current pattern | YES, by construction | FINDINGS "How FL 2026 resolves the target": `if PatNum > 0 then SetCurrentPattern(PatNum)` decoded from FL's own id-17 handler |
| ChanNum targeting | NO, refuted statically AND live | VERDICT Q2; FINDINGS: `ChanNum` is never loaded; notes land in the first selected channel |
| Channel object reachable via dispatcher | NO | VERDICT Q2 (Pass 5 sweep: no `TPublicChannel` from any id) |
| Live note readback | NO getter exists | VERDICT Q1 (curated getters + bounded sweep 88-105) |
| FL pushes transport/selection/focus/context/routing | YES, confirmed live | VERDICT Q3: `FPD_SetPlaying`, `FPD_SongPosChanged`, `FPD_SetFocus` (x6), `FPD_ContextInfoChanged` (x29), `FPD_RoutingChanged`, plus `FPD_ProjLoaded`, `FPD_SetTimeSig`, `FPD_SetSampleRate/BlockSize` |
| Score-edit push | NOT observed (inferred absence, minor open item) | VERDICT Q3 caveat |
| Getter cost | ~0 ms per 11-getter poll vs 8.7 ms mean tick | VERDICT Q4 (Pass 2) |
| Confirmed getters | position, tempo, PPQ, selection, playing state, track focus | VERDICT Q1/Q4; `evidence-2026-08-30/probe_getters_results.jsonl` (`FHD_GetMixingTime`, `FHD_GetPlaybackTime`, `FHD_GetSelTime`, `FHD_GetContextInfo[CI_TrackIndex/Selected/Focused]`, `FHD_GetProjectInfo`) |
| Dispatcher ids 94 (crash) / 99 (hang) | NEVER call | VERDICT "Unsafe dispatcher ids"; static ledger `probe_safety.h`, covered by `TestSweepSkipsLiveConfirmedFatalsOnAFreshSpool` |
| UDP transport, one executor, one ledger, exactly-once across carriers | shipped (#58) | docs/22; `bridge_udp.{h,cpp}`, `bridge_wire.py` |
| Bridge crash takes all of FL down | in-process by necessity | docs/25 limitation table; FINDINGS crash saga |

## 1. The shipped end state

```
                       agent (MCP tools / sdk.py)
                                 |
                      FLSession (thin orchestrator)
                       /         |          \
              NoteRouter      Observer      wire (MidiControl)
               /      \           |          params + tempo/levels/transport
        BridgeLane  FileLane   events in     WRITES + name enumeration
        (UDP+spool) (flpkit     (UDP push    (read plane retired to what
         live, any   +reload,    + checkpoint  only it can answer)
         PATTERN)    any target, poll-and-diff)
                     offline)
```

One write router chooses the lane per command by proven capability, never by flag.
One observer turns FL's push callbacks plus checkpoint diffs into a single user-action stream.
Live state reads come from the bridge's confirmed getters; note-content reads stay file-based (VERDICT Q1).
The file lane survives as a first-class lane, not a hedge: it is the only lane that honors a channel target (VERDICT Q2) and the only lane that works with FL closed.

Phases, in dependency order:

| Phase | Ships | Deletes |
|---|---|---|
| 0 | Bulkhead + circuit breaker + poison-command quarantine | nothing (prerequisite hardening) |
| 1 | Bridge as default note-write lane, side-effect surfacing | `FL_MCP_BRIDGE` flag, `_bridge_ready` double opt-in, the pyscript note lane (396 lines) |
| 2 | Observer: event stream over UDP + checkpoint diffs + concurrency rules | `wire.py` event machinery; `native/probe/` source (~2600 lines, per #80 item 2) |
| 3 | Live reads via bridge getters; wire read-plane retirement | `state.snapshot` composite (controller + server halves), wire-read settle gate |

## 2. Phase 0 - the bulkhead and the circuit breaker (prerequisite, per #54 and docs/25)

### 2.1 Why there is no literal out-of-process helper

`FHD_AddNotesToPR` and every other host call can only be issued from the plugin, in FL's process, on the GUI thread (docs/22 §1; FINDINGS call contract).
An out-of-process helper cannot make host calls, so the write executor cannot leave FL's process.
The honest bulkhead is therefore: **minimize what runs inside FL, prove that remainder cannot take FL down by any input we control, and contain every failure it can still have on the server side.**
The system already has the right macro shape - the MCP server is a separate process and the plugin is a thin executor - so the bulkhead work is finishing that shape, not inventing a new one.

### 2.2 In-process invariants (the crash-isolation strategy)

Each is an invariant, not an edge-case branch (#54 §3).
Most already exist; Phase 0 makes the set complete and states it as the promotion gate.

1. **Static fatal-id ledger.** Ids 94/99 are quarantined by construction in `probe_safety.h` (VERDICT); the bridge issues only `FHD_AddNotesToPR`, `FHD_SetDirty`, and (Phase 3) the confirmed getters - all live-proven ids. The ledger moves from `native/probe/` into `native/abi/` before the probe source is deleted, so the quarantine survives the deletion (#80).
2. **Claim-before-execute.** A command is recorded in `consumed.json` and tombstoned before any host call (FINDINGS "Spool-consume flow" step 3), so a command that crashes FL is never replayed on restart. This is live-proven behavior: the probe's id-94 crash did not replay after relaunch (VERDICT).
3. **Concrete targets only.** A non-concrete target is refused with an error, never resolved by FL (FINDINGS "The target is never a sentinel"); `schemas.Target` guarantees it server-side, and the bridge re-checks.
4. **Bounded work per tick, no blocking syscalls on host threads, socket thread never calls the host** (docs/22 §1-2, shipped with #58).
5. **Exception containment at the tick boundary.** The existing tick-level `try/catch` plus the ASan/UBSan-tested `bridge_core` stand; the one failure mode C++ cannot catch is FL's own setjmp/longjmp unwind (FINDINGS "One cause, two symptoms"), which is exactly what invariants 1-3 exist to never trigger.
6. **Struct layout enforced at build time.** `static_assert` against FL's own RTTI-decoded record layouts (FINDINGS "The FL 2026 note-params layout") and the `check_vtable.sh` ctest gate; an FL ABI drift fails the build, not FL.

### 2.3 Server-side circuit breaker

New module `mcp-server/src/fl_mcp/lanes.py` (extracted from `session.py`, which is over the 800-line doctrine limit - coordinate with R1 (@flmcp-r1-arch), who owns the #80 session split; R2 defines the semantics, R1 owns the file layout).

```python
class BridgeBreaker:
    """Failure containment for the bridge lane. States: closed -> open -> half_open -> closed.

    Soft failure (refusal, timeout, stale heartbeat mid-command): opens after
    FAILURES_TO_OPEN consecutive failures.
    Hard failure (FL process death within POISON_WINDOW_SECONDS of an
    unacknowledged bridge command): opens IMMEDIATELY and quarantines the
    command fingerprint.
    """

    FAILURES_TO_OPEN = 3
    COOLDOWN_SECONDS = 60.0
    POISON_WINDOW_SECONDS = 10.0

    def allows(self) -> bool: ...          # closed, or one half-open trial token
    def record_success(self) -> None: ...  # closes; resets the failure count
    def record_failure(self, reason: str) -> None: ...
    def record_crash(self, command: SpoolCommand) -> None: ...  # instant open + quarantine
    state: Literal["closed", "open", "half_open"]
```

Rules:

- While open, the router never offers the bridge lane; writes route to the file lane automatically and the receipt's `backend` field says so (the fallback is visible, never silent).
- Half-open entry requires cooldown elapsed AND a fresh heartbeat AND an id-less UDP `ping` pong (`bridge_wire.ping`, docs/22 - proves socket thread -> queue -> tick -> ack, the whole pipeline).
- The half-open trial is the next eligible real write; success closes the breaker, failure re-opens it.
- **Crash attribution.** After every bridge dispatch, the session snapshots `fl_process_running()` and the heartbeat age. If FL dies or the heartbeat goes permanently stale within `POISON_WINDOW_SECONDS` of a command that never acked, `record_crash` fires: the breaker opens, the command's fingerprint (`op`, target, note count) is written to a durable `store` quarantine list, and the tool error names the command as the suspect with a fix pointing at `bridge_status.txt` / `bridge_dispatch.txt` (the `:enter` with no `:return` is the only evidence a hang leaves - FINDINGS).
- A quarantined fingerprint routes to the file lane permanently until a human (or a future live rung) clears it; this is the server-side mirror of the plugin's claim-before-execute, covering the case where the crash happened before the claim.

### 2.4 The double-apply invariant on fallback

The UDP carrier cannot cancel a datagram the bridge already queued (docs/22 "Timeout semantics").
Falling back after a timeout must not let the timed-out command apply twice.
Two fallback levels, each safe by construction:

- **Carrier fallback (bridge alive, UDP flaky):** re-send the SAME command id via the file spool. The shared `consumed.json` ledger makes the race exactly-once across both carriers (docs/22 §4 "Idempotency", already tested).
- **Lane fallback (bridge dead or breaker open):** before applying via the file lane, tombstone the timed-out id in every spool root (`Spool._forget(tombstone=True)` semantics, which the file-carrier path already runs on timeout) so the bridge can never claim it later; the in-memory UDP queue dies with FL's process, and `_reload_fl` kills that process, so a late in-memory apply after the reload is impossible by construction.

### 2.5 Phase 0 tests

- `tests/test_lanes.py`: breaker state machine (N soft failures open; crash opens instantly; half-open needs pong; trial success closes), quarantine persistence across sessions via the store, fallback tombstoning order.
- Existing C++ suite unchanged; the promotion gate is the checklist in 2.2 being green (`ctest` + `check_vtable.sh` + the safety-ledger test).

## 3. Phase 1 - promote the bridge to the default note-write lane

### 3.1 Routing: capability detection, no flag

Delete `ENV_BRIDGE`, `_bridge_ready`, and the double opt-in (`session.py:104-112`, `395-403`).
Doctrine #54 forbids an off-by-default gate a human must flip; the replacement is a per-command routing decision proven at dispatch time:

```python
class NoteRouter:
    """THE lane decision for note writes. Chooses the best lane that can
    HONOR the full target, proven by reads, never hoped."""

    def route(self, target: Target, mode: NoteWriteMode) -> Literal["bridge", "project-write"]:
        # bridge iff ALL of:
        #  1. breaker.allows()                          (Phase 0)
        #  2. heartbeat fresh (<= 15 s) in a spool root  (existing _heartbeat_age)
        #  3. carrier reachable: UDP pong, else file-spool available
        #  4. the write's CHANNEL is proven current:
        #     selected-channel read == target.channel   (see 3.2)
        # else project-write.
```

Clause 4 is the load-bearing one and follows directly from the verdict: `PatNum` is honored but the channel FL writes to is the first selected channel, always (VERDICT Q2; FINDINGS id-17 decode).
A bridge write is therefore only correct when the live selected channel IS the target channel.
That is checkable, so we check it instead of hoping.

### 3.2 The selected-channel guard

The bridge has no selected-channel getter (`FHD_GetContextInfo` accepts only mixer-track `CI_` ids - FINDINGS, PROBE_FINDINGS).
The controller wire does: `channels.selectedChannel()` is a documented controller-API read, dispatched through the existing generic op dispatcher in `device_FLMCP.py` (no new controller code).
So:

- Wire connected: one `channels.selectedChannel()` round trip (milliseconds) at dispatch time; match -> bridge, mismatch -> file lane.
- Wire down: the channel cannot be proven, so the write routes to the file lane. Correctness beats liveness; the receipt says which lane ran and why.

This is one of exactly two wire reads that survive Phase 3 (see §5.3), because the bridge cannot make it.
Flag for R3 (@flmcp-r3-tier2): a write-side probe that finds a select-channel host call, or a channel-selection getter, deletes this guard and makes every pattern write bridge-eligible; the router absorbs that as a strictly weaker clause 4, no restructuring.

### 3.3 Surfacing the SetCurrentPattern side effect

Writing to a non-current pattern switches FL's current pattern; FL implements PatNum as select-then-write (FINDINGS: `SetCurrentPattern` writes FL's current-pattern global).
This is an intended, visible effect, and we do not undo it (the only lever that could switch back is another note write, which would be a second side effect pretending to be a cleanup).
It is surfaced on three surfaces so the model can never be surprised:

1. **Typed receipt field.** `WriteReceipt` gains `current_pattern_after: int | None` - set to `target.pattern` on every bridge write, `None` on file-lane writes (the reload re-opens the project; no live pattern switch happened).
2. **Tool contract.** The `write_notes` tool description states: "a live write to pattern N also makes N FL's current pattern - this is FL's own select-then-write behavior and is intentional."
3. **The event stream.** The `FPD_ContextInfoChanged` burst the write causes arrives through the Phase 2 observer tagged `origin="agent"` (§4.5), so the stream itself shows the switch as agent-caused, not as mystery user activity.

### 3.4 Session changes

`FLSession.write_notes` / `clear_notes` collapse to: resolve lane via `NoteRouter`, dispatch, verify, record.
The bridge path keeps its existing shape (`_bridge_send` -> `_bridge_confirmed`): readback stays the authority, never the bridge's echo (`notes_written` is what it SENT - FINDINGS step 6; `session.py:429-444`).
Known, deliberate gap, unchanged by this plan: `_bridge_confirmed` reads the project file, which lags the live session until FL saves, because no live note readback exists (VERDICT Q1).
The Phase 2 checkpoint machinery (§4.4) narrows the gap by diffing at save events; it cannot close it, and the docstring keeps saying so.

### 3.5 Deletions

- `ENV_BRIDGE`, `_bridge_ready`, the flag prose in `session.py` and `doctor()`.
- **The pyscript note lane**: `fl-scripts/piano-roll/MCP Write Notes.pyscript` (396 lines), its `install.py` entry, and the doctor's note-plane messaging about it. Doctrine #54 §1 named this deletion's trigger ("delete it when the bridge's live verdict lands") and the verdict landed 2026-08-29/30. The file spool itself STAYS - it is the bridge's fallback carrier and the bulk-payload path (docs/22), no longer a pyscript mailbox. The spool docstring sheds its sandbox-consumer prose (`spool.py:1-28` shrinks to the two-carrier story).
- `doctor()` note-plane branch re-keyed to bridge heartbeat + breaker state instead of pyscript installation.

### 3.6 Tests and live rung

- Router matrix test: (breaker state x heartbeat freshness x pong x selected-channel match) -> lane, all 16 cells.
- Differential test extension (`test_bridge_differential.py`): routed write through the real `Spool` + `bridge_spool_cli` confirms receipt `backend="bridge"` and `current_pattern_after`.
- Live rung (orchestrator-run, RUNBOOK_BRIDGE_V2 style): with channel 0 selected, a pattern-2/channel-0 write goes bridge and FL's current pattern becomes 2; a pattern-2/channel-1 write routes file-lane with the mismatch named in the receipt.

## 4. Phase 2 - the observer / user-action event stream

### 4.1 Shape: push first, lazy poll at checkpoints

FL pushes the plugin, on the GUI thread, for exactly the events a co-editing agent needs (VERDICT Q3): transport (`FPD_SetPlaying`, `FPD_SongPosChanged`), attention (`FPD_SetFocus`, `FPD_ContextInfoChanged`), structure (`FPD_RoutingChanged`, `FPD_ProjLoaded`, `FPD_SetTimeSig`).
Score edits have no push (VERDICT Q3, inferred absence) and no live readback (Q1), so they are detected by poll-and-diff at CHECKPOINTS, not by a realtime poller: before a destructive agent write, after a tool call, and when the project file's mtime advances (a user Cmd+S).
Reads are lazy on-demand per owner - the plan deliberately does NOT run a realtime observer loop even though Q4 proves a 25 ms poll affordable, because nothing consumes realtime state; if a consumer appears, the affordability verdict makes the upgrade a constant change, not a redesign.

### 4.2 Plugin side: `native/bridge/src/bridge_events.{h,cpp}`

- `struct BridgeEvent { uint32_t seq; uint64_t tick; int32_t id; int32_t index; int64_t value; };`
- `EventRing`: capacity 256, drop-oldest with a drop counter - the same bounded discipline as the UDP command queue (docs/22 §2).
- The `Dispatcher` handler (a `(GM)` callback) filters the subscribed id set {`FPD_SetPlaying`, `FPD_SongPosChanged`, `FPD_SetFocus`, `FPD_ContextInfoChanged`, `FPD_RoutingChanged`, `FPD_ProjLoaded`, `FPD_SetTimeSig`} and ONLY enqueues into the ring (mutex-guarded, value-only) - it never touches the socket, never calls the host. This is the same thread discipline the transport already enforces (docs/22 §1).
- Coalescing: `FPD_ContextInfoChanged` fired x29 in one short live window (VERDICT Q3); within one tick's drain, events with the same (`id`, `index`) coalesce to the latest. `seq` still advances per raw event so the server can see coalescing happened.
- The tick drains the ring after the command drain: events are batched into datagrams (`{"events":[...], "dropped": n}`, up to the 60 KB frame ceiling), at most 4 event datagrams per tick, sent with non-blocking `sendto` to the subscriber. Full socket buffer drops the batch; `seq` gaps tell the server to resync via `get_state` (§5.1).
- **Subscriber registry:** a `subscribe` datagram (id-less, like `ping`) registers the sender's address with a 30 s TTL; the server renews it every 10 s from its receive thread. Last subscriber wins (matching the spool's single-server assumption, docs/22 §3); TTL expiry stops all event traffic, so a dead server never receives datagrams forever.
- Backgrounded FL: the tick only runs steadily while FL is frontmost (VERDICT "How this was run"); events queue in the ring and flush on the next tick, so delivery latency degrades gracefully and nothing is lost until the ring wraps.

### 4.3 Event schema (`schemas.py`)

```python
EventKind = Literal[
    "playing",          # FPD_SetPlaying         value: 0|1
    "song_position",    # FPD_SongPosChanged
    "focus",            # FPD_SetFocus           index: what gained/lost focus
    "context",          # FPD_ContextInfoChanged index: CI_ id, value: new value
    "routing",          # FPD_RoutingChanged
    "project_loaded",   # FPD_ProjLoaded
    "time_sig",         # FPD_SetTimeSig
    "score_delta",      # synthesized server-side at checkpoints (never on the UDP wire;
                        # the bridge cannot read notes - VERDICT Q1)
]

class FLEvent(BaseModel):
    seq: int                                   # bridge-monotonic; a gap means drops, resync via get_state
    ts: datetime                               # server arrival stamp
    kind: EventKind
    origin: Literal["user", "agent", "unknown"] = "unknown"
    index: int = 0
    value: int = 0
    target: Target | None = None               # score_delta only
    delta: NoteDelta | None = None             # score_delta only (reuses the existing diff type)
```

One event type for both wire-pushed and checkpoint-synthesized events; `score_delta` carries the existing `NoteDelta` so history, receipts, and the stream speak one diff language.

### 4.4 Server side: `mcp-server/src/fl_mcp/observer.py`

```python
class Observer:
    """Owns the event stream: receives push events, synthesizes score_delta
    events at checkpoints, tags origin, and answers cursors."""

    def __init__(self, wire: BridgeWire, store: Store, project: Path | None) -> None: ...
    def start(self) -> None: ...               # receive thread + subscribe renewal
    def stop(self) -> None: ...
    def events_since(self, cursor: int) -> tuple[list[FLEvent], int]: ...
    def user_active_on(self, target: Target, within: float = QUIESCENCE_SECONDS) -> bool: ...
    def checkpoint(self, reason: Literal["pre_write", "post_tool", "save_seen"]) -> list[FLEvent]: ...
    def mark_agent_window(self, command_id: int, until: float) -> None: ...  # origin tagging
```

- `BridgeWire._parse_ack` learns to route payloads with an `"events"` key to the observer's queue before `SpoolResponse` validation; the socket is already `connect()`-pinned to the bridge's port, so event datagrams are peer-filtered by the kernel.
- `checkpoint()` runs the poll-and-diff: stat the project file; if mtime advanced past the store's ground-truth stamp, parse (flpkit), diff each known target against the store (`store.notes_at` vs file), emit `score_delta` events with `origin="user"`, and update ground truth. Cost is one file parse, only when the file actually changed - this is the "poll cost ~0 ms" lane made lazy (VERDICT Q4).
- Checkpoint triggers: `FLSession` calls `checkpoint("pre_write")` before any replace/clear, the server layer calls `checkpoint("post_tool")` after each tool dispatch, and a `save_seen` fires when any event arrives while the project mtime is newer than ground truth.

### 4.5 Origin tagging (agent-echo suppression)

A bridge PatNum write CAUSES `FPD_ContextInfoChanged` / focus traffic (the SetCurrentPattern side effect, §3.3).
The stream must not present the agent's own effects as user actions.
Rule: when the session dispatches a bridge command it calls `mark_agent_window(command_id, until=ack_time + 0.25 s)`; push events arriving inside an open agent window are tagged `origin="agent"`, everything else `origin="user"`.
The window is per-command and bounded, so a user action during an agent write is at worst tagged `unknown` (both were active), never dropped - and `unknown` is treated as user for concurrency purposes (fail toward caution).

### 4.6 Concurrency rules: agent vs human

The invariant set, smallest that satisfies "never clobber a note the user is mid-edit on" given that mid-edit unsaved notes are INVISIBLE (no readback, no edit push - VERDICT Q1/Q3):

1. **Quiescence gate for destructive ops.** `replace`-mode writes and `clear_notes` require `not observer.user_active_on(target)` where activity = any `origin="user"` or `unknown` event except pure transport (`playing`, `song_position`) within `QUIESCENCE_SECONDS = 5.0`. Violation raises a retryable `ToolError("user_editing", ...)` whose fix says what the user was just doing and to retry or use merge. Merge-mode writes pass the gate (additive; FL merges into whatever is there).
2. **Stale-base guard.** Before a destructive write, `checkpoint("pre_write")` runs; if it emits a `score_delta` for the write's own target (the user changed and saved that very score since our ground truth), the write is refused once with the delta in the error so the model can re-read and re-decide. This is the write-write conflict rule; the store's `before_hash` already gives it a stable basis.
3. **Reads are per-owner and lazy.** The agent reads through `read_notes`/`live_state` when IT needs state; the user reads FL's screen. Nothing forces a realtime mirror, so there is no realtime mirror to fight over.
4. **The agent never holds FL state.** Every write re-proves its preconditions (lane, selection, quiescence) at dispatch time; nothing is cached across tool calls except the store's ground truth, which checkpoints keep honest.

### 4.7 Surfacing the stream to the model

Two surfaces, one source:

- **Ambient:** every MCP tool result envelope gains `user_activity: list[str]` - compact renderings of `events_since(last_cursor)` (e.g. "user pressed play at bar 17", "user switched focus to the mixer", "user edited pattern 3 / channel 1: +4 notes (saved)"), capped at 10 with an "and N more" tail. The model sees what the human did between its actions without asking.
- **On demand:** a `fl_events(cursor: int = 0)` tool returning `(events, next_cursor)` with full typed `FLEvent`s for when the model wants detail or history.

### 4.8 Deletions

- `wire.py` event machinery: `add_event_listener`, `drain_events`, `_events`, `_event_listeners` (`wire.py:158-163`, and the listener dispatch in `_on_message`) - the observer supersedes the wire as THE event surface, and nothing consumes the wire's event list today.
- `native/probe/` source, tests, and scripts (~2600 lines, #80 item 2): this phase is the implementation that absorbs the verdicts, which was the named deletion trigger. `PROBE_VERDICT_2026-08-30.md` + `evidence-2026-08-30/` stay as the durable record; `probe_safety.h`'s ledger moves to `native/abi/` first (§2.2).

### 4.9 Tests and live rung

- C++ (`bridge_events_test.cpp`, ASan/UBSan): ring capacity/drop-oldest/drop counting, coalescing per (`id`,`index`) within a drain, seq monotonicity across coalescing, subscriber TTL expiry, batch framing under the datagram ceiling, Dispatcher-thread enqueue vs tick-thread drain under a race harness.
- Python: fake bridge responder (extend the existing `bridge_wire` fake) emits event batches; observer ordering, gap detection, origin tagging inside/outside agent windows, quiescence gate, checkpoint diff synthesis from a mutated temp project.
- Live rung: with the agent idle, play/stop/select in FL -> `fl_events` shows them as `origin="user"` with sane timestamps; an agent pattern-write -> the context burst shows as `origin="agent"`; a user note edit + Cmd+S -> a `score_delta` appears at the next checkpoint; a replace during active user editing -> refused with `user_editing`.

## 5. Phase 3 - live reads via the confirmed getters, and the wire read-plane retirement

### 5.1 The `get_state` read op

The confirmed getter set (VERDICT Q1/Q4: position, tempo, PPQ, selection, playing state, track focus; `probe_getters_results.jsonl`) becomes one stateless bridge op:

- Wire form: an id-less datagram like `ping` (`{"proto":2,"op":"get_state"}`), answered from the tick with the state in the ack. Reads never claim ledger slots - same rule that keeps `ping` out of the ledger ring (docs/22 §4).
- The bridge answers from its cached push state (playing from `FPD_SetPlaying`, time-sig/tempo inputs from `FPD_SetTimeSig` / `FPD_SetSamplesPerTick` + sample rate) plus the live getters (`FHD_GetMixingTime`, `FHD_GetPlaybackTime`, `FHD_GetSelTime`, `FHD_GetContextInfo[CI_TrackIndex/Selected/Focused]`); one full poll costs ~0 ms against an 8.7 ms tick (VERDICT Q4).
- The ack carries the current event `seq`, making `get_state` the resync point after an event gap (§4.2).

```python
class LiveState(BaseModel):
    playing: bool
    song_pos_beats: float
    song_pos_ticks: float
    tempo_bpm: float | None
    ppq: int | None
    sel_start_ticks: float | None
    sel_end_ticks: float | None
    mixer_track_focused: int | None
    seq: int

class FLSession:
    def live_state(self) -> LiveState | None: ...  # None when the bridge is down; callers degrade honestly
```

### 5.2 Snapshot recomposition

`SessionState` is recomposed by capability instead of by backend:

- Transport/tempo/selection: `live_state()` (bridge getters) when the bridge is up.
- Channel names/levels: the project file (already file-first in `channel_names()` - the file is the target universe and answers mid-reload).
- Mixer/channel NAME enumeration when live-only detail is asked for: a slimmed wire op `state.names` (the name/level lists from today's `_state_snapshot`, minus every field the bridge now answers).
- Note content: file, always (VERDICT Q1).

The one-backend-per-call doctrine note in `snapshot()` becomes one-backend-per-FIELD-GROUP, with the group boundaries drawn by proven capability - which is what the doctrine actually wants (route to the surface that can honor the job).

### 5.3 Wire read-plane retirement scope (what survives of `wire.py`, and why)

Doctrine: delete the superseded mechanism.
The wire's READ plane is superseded exactly where the bridge has a getter; the remainder is not superseded by anything and stays.

| Wire surface | Fate | Reason |
|---|---|---|
| `state.snapshot` composite (controller `_state_snapshot` + server parse, `session.py:684-745`) | DELETE both halves | superseded by `live_state()` + file + `state.names` |
| `mixer.getCurrentTempo` post-write readback | RETIRE; verify tempo writes via `live_state()` | the bridge getter is an INDEPENDENT surface, so the readback gets stronger, not just cheaper |
| Settle gate `_settle` (polls `state.snapshot`) | switch to bridge `get_state`/heartbeat when the bridge is present, wire `ping` otherwise | same invariant (no live op before FL answers), cheaper probe |
| `channels.selectedChannel` | KEEP | the bridge routing guard needs it and no bridge getter exists (§3.2); dies the day R3 finds a channel-selection call |
| `plugins.getParamCount/Name/Value` + `setParamValue` | KEEP | the plugin-param surface has no bridge or file equivalent (docs/24 row "Plugin params"); read and write are one surface here |
| `channels.getChannel*` readbacks for `set_channel_levels` | KEEP | the bridge has no channel-level getters; a wire write is verified by the only surface that can read it |
| `transport.start/stop`, `general.undo`, `mixer.setCurrentTempo`, `channels.setChannel*` | KEEP | the wire's WRITE plane is not superseded by anything in R2's scope |
| hello / `ping` | KEEP | the wire's own liveness gate for its surviving ops |
| `state.names` (new, slim) | ADD (net-negative against the deleted snapshot builder) | name enumeration has no bridge getter |

So `wire.py` survives as the params + control-writes wire with a name-enumeration read, and the controller script sheds its composite snapshot builder.
`wire.py` itself is already lean (224 lines); the deletions land mostly in `device_FLMCP.py` and `session.py`.
Full wire deletion is NOT in R2 scope and should not be forced: per #54 §1 "UDP may supersede the MIDI SysEx wire" only if a probe shows full coverage, and the verdict shows the opposite (no param getters, no channel-level getters, no select call).
Flag for R3: each write-side probe find (params via host calls, channel selection, mixer ops) deletes another wire row; the wire retires row by row, evidence by evidence.

### 5.4 Tests and live rung

- Differential: `bridge_spool_cli` answers `get_state` from mock-host getter values; `live_state()` returns them typed; ledger untouched by reads.
- Snapshot composition: bridge up -> transport fields from `LiveState`; bridge down -> fields `None` and channels still answer from file; no cell ever silently mixes a stale source with a live one.
- Tempo cross-verification: set via wire, confirm via `live_state()` against the same tolerance.
- Live rung: play FL, `live_state()` shows `playing=true` and an advancing position without any file save; stop, it flips; tempo change in FL's UI is visible in the next `live_state()`.

## 6. What the file lane remains FOR (not a hedge)

Two jobs the file lane is genuinely best at, with the evidence that makes them its jobs:

- **Channel-targeted writes.** `ChanNum` is never loaded by FL's id-17 handler and no dispatcher id hands back a channel object (VERDICT Q2; FINDINGS decode). The file lane is the ONLY lane that can honor a channel target, so it keeps that job until a write-side probe changes the evidence (R3).
- **Offline mode and bulk.** FL closed (headless render prep, batch composition) has no live surface at all; and >60 KB commands exceed the datagram ceiling by design (docs/22 §4). The spool + flpkit path is the canonical tool for both.

Per #54 §1, flpkit additionally stays as the verifier and the render path - "best version is judged per JOB, not per technology."

## 7. Sequencing, gates, and coordination

| Order | PR | Gate (CI is dead - local runs ARE the gate, #54 §6) |
|---|---|---|
| 1 | Phase 0: `lanes.py` breaker + quarantine + fallback tombstoning; safety ledger moved to `native/abi/` | ruff + pytest + ctest green; breaker matrix tests |
| 2 | Phase 1: router + flag deletion + pyscript deletion + receipt field | differential tests; live rung 3.6 |
| 3 | Phase 2a: `bridge_events` + subscribe + `BridgeWire` event receive | ctest (ASan/UBSan) + fake-responder tests |
| 4 | Phase 2b: `observer.py` + concurrency gates + surfacing; delete wire event machinery + `native/probe/` source | pytest; live rung 4.9; docs/24 rows updated in the same PR (maintenance rule) |
| 5 | Phase 3: `get_state` + `LiveState` + snapshot recomposition + wire read retirement + `state.names` | differential + live rung 5.4 |

Live rungs follow the RUNBOOK_BRIDGE_V2 pattern: orchestrator-executed in a supervised window; repo agents never drive FL.

Dependencies on other research areas, by name:

- **R1 (@flmcp-r1-arch):** owns the #80 `session.py` split and the overall capability-detection architecture. `lanes.py`, `observer.py`, and the router semantics defined here should land inside R1's module layout; R2 supplies the behavior contracts (§2.3, §3.1, §4.4), R1 the file boundaries. The no-flag promotion (§3.1) is the shared invariant both plans must state identically.
- **R3 (@flmcp-r3-tier2):** tier-2 write-side RE (automation, mixer, instruments). The event schema reserves nothing but tolerates everything: new `EventKind` literals (e.g. automation, clip moves) extend the union without touching the transport, and each R3 host-call find deletes a wire row (§5.3) or the selected-channel guard (§3.2).
- **#63 (RTTI scraper):** re-verifies the getter ids, the id-17 decode, and the fatal-id ledger on any future FL build; the observer and router inherit re-verification for free once it exists.

## 8. Open items carried honestly

- "No score-edit push" is inferred from absence during one live window, not positively proven (VERDICT Q3). If a future probe window finds an edit push, the checkpoint machinery gains a cheap trigger; nothing else changes.
- The selected-channel guard couples bridge eligibility to wire liveness (§3.2). Acceptable: the failure mode is a correct-but-slower file-lane write, never a wrong-channel live write.
- `_bridge_confirmed` still verifies against a file that lags the live session (§3.4). Closing it needs a readback getter that the sweep proved absent; the checkpoint diff narrows the window to the last save.
- The 2026-08 hang anomaly (FINDINGS "UNVERIFIED anomaly") remains open against a build that no longer exists; the Phase 0 crash-attribution machinery captures exactly the five evidence items a recurrence would need.
