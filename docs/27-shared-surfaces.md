# Shared surfaces

These are the stable contracts still cited by code and tests. They are not a separate plan; the plan lives in [docs/26](26-master-plan.md), and the acquisition rule lives in [docs/33](33-capability-acquisition-model.md).

## 1. Lane

```python
class Lane(Protocol):
    name: str  # "project-write", "bridge" - recorded in the action log, never shown to the model
    def can_serve(self, op: Operation, caps: Capabilities) -> bool: ...
    def apply(self, op: Operation, data: Sequence[Note]) -> list[Note] | None: ...
```

`apply` returns the target's notes as FL confirmed them, never an echo.
A lane failure raises; the router decides fallback before the call.
The facade turns the confirmed notes into the model-facing `WriteResult(wrote, track, pattern, mode, total)`.

## 1a. Note state

FL has no live note getter.
PROBE_VERDICT Q1 (2026-08-30) found none, and a re-check on 2026-10-18 found nothing newer: the Bridge's `wp_*` getters read time, context, colors, and project info, and the controller's FL API has no piano-roll read.
So the engine's record of applied writes is the source of truth for the notes it wrote.

- **Write.** FL acknowledging a Bridge command means FL applied it, so the acknowledged edit is the confirmed state at that boundary: `replace` leaves exactly the sent notes, and `merge` leaves the track's known notes plus the sent ones.
  Where FL can save (licensed FL with a readable project), the lane saves and re-reads the project instead, which is stronger evidence.
  A refusal or a timeout raises and records nothing.
- **Record.** The store keeps that state per (pattern, channel), in memory for this session only: a later server must not inherit claims about an FL that has since been reset.
  The action log stays durable history.
- **Read.** `read_notes`, `get_notes`, `get_scene`, and `Track.notes()` overlay the record on the project FL loaded.
  A target this session never wrote reads from the project, which still describes it.
  A save of the project newer than a record (FL saving its live state, including a person's Cmd+S) supersedes that record, because the save holds everything FL had then.
  Tempo is read live from FL whenever FL is connected.
- **Unknown.** A merge onto a track whose earlier notes the session never saw (no project to read) has no knowable total: the result says `total=None` and nothing is recorded.
- **Known gap.** `fl.undo()` reverts FL without telling the record, so a note read after an undo can be stale.

Model-facing text (tool descriptions, results, error messages and fixes, SDK docstrings) never names these internals; `tests/test_clean_traces.py` replays the live runs that motivated this and scans every such string.

## 2. Capabilities and router

```python
@dataclass(frozen=True)
class Capabilities:
    bridge_live: bool
    bridge_pattern: int | None
    bridge_channel: int | None
    profile: "CapabilityProfileLike"

def detect(link, spool, project, profile) -> Capabilities: ...
def pick_note_lane(caps: Capabilities, target: Target, breaker: "BridgeBreaker") -> NoteLane: ...
```

There is no `FL_MCP_BRIDGE` flag. Bridge eligibility is evidence: fresh heartbeat, protocol match, ping round-trip, breaker closed, and the target constraints the bridge can actually satisfy. File lane remains first-class and always handles offline or channel-targeted writes.

## 3. BridgeBreaker

```python
class BridgeBreaker:
    def record_success(self) -> None: ...
    def record_soft_failure(self) -> None: ...
    def record_hard_failure(self, fingerprint: str) -> None: ...
    @property
    def closed(self) -> bool: ...
```

The breaker protects FL from repeated live-write failures and routes future calls to the file lane until recovery.

## 4. CapabilityProfile

```python
@dataclass(frozen=True)
class CapabilityProfile:
    meta: ProfileMeta
    dispatcher: DispatcherFacts
    structs: dict[str, StructLayout]
    getters: tuple[GetterFact, ...]
    push_events: frozenset[str]
    flp: FlpFacts
    fst: FstFacts
    ppq_default: int
    capabilities: ProfileCapabilities
    tick: TickFacts
```

The profile is the single home for version-specific facts. Loading order is explicit profile override, discovered spool profile, packaged profile, then warned builtin fallback.

## 5. Observer and UserAction

```python
@dataclass(frozen=True)
class UserAction:
    kind: Literal["transport", "position", "selection", "focus", "context", "routing", "score_edit"]
    at: datetime
    detail: dict[str, Any]

class Observer(Protocol):
    def poll(self) -> list[UserAction]: ...
    def drain_pushes(self) -> list[UserAction]: ...
```

FL push callbacks cover transport, position, focus/selection, context, and routing. Score edits have no push and are discovered by lazy poll-and-diff. An agent write refuses or defers when a fresh user edit touches the same target inside the guard window.

## 6. Bridge wire additions

- Heartbeat carries `proto=`, `pat=`, and `chan=` for detection.
- FL push callbacks append typed events to the bridge event log.
- `wp_*` discovery ops ride the same executor as ordinary commands.
- `native/abi/fl_dispatch_ledger.h` is the safety authority for fatal, hanging, modal, and pointer-deref dispatcher ids.
- Generated ABI facts belong under the profile-generated native header, with warned fallback only when a profile is missing.

## 7. Facade seam

`from fl_mcp.session import FLSession, create_session, NotePlaneUnavailable, WriteRejected` remains the public API. Internal package movement must preserve that seam.

## Invariants

- No feature flags for lane choice.
- Version facts come from the profile or data-derived detection.
- Boundaries verify by readback; where no readback exists, FL's acknowledgement is the confirmation and the session records it.
- Lanes raise; routers decide.
- Superseded mechanisms are deleted when the canonical path ships.
