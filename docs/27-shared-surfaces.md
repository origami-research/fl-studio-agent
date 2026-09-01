# Shared surfaces

These are the stable contracts still cited by code and tests. They are not a separate plan; the plan lives in [docs/26](26-master-plan.md), and the acquisition rule lives in [docs/33](33-capability-acquisition-model.md).

## 1. Lane

```python
class NoteLane(Protocol):
    name: Literal["project-write", "bridge"]
    def write(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt: ...
    def clear(self, target: Target) -> WriteReceipt: ...
    def read(self, target: Target) -> list[Note]: ...
```

Receipts report confirmed readback, never an echo. A lane failure raises; the router decides fallback before the call.

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
- Boundaries verify by readback.
- Lanes raise; routers decide.
- Superseded mechanisms are deleted when the canonical path ships.
