# Shared surfaces: the frozen contracts for parallel lane execution

Every lane codes against THESE signatures, owns a DISJOINT file set, and never edits another lane's files. Cross-lane types are Protocols (structural), so a lane never hard-imports another lane's concrete module - it can build and test in isolation, and integration is mechanical. Detail/rationale: docs/26-master-plan.md + docs/research/r1..r4.

THE SWITCH IS REMOVED: there is no `FL_MCP_BRIDGE` env var anywhere. Lane selection is always automatic capability detection (§2).

## File ownership (disjoint - no two lanes touch the same file)

| Lane | Owns | Publishes |
|---|---|---|
| L1 session-spine | `mcp-server/src/fl_mcp/session/**`, `errors.py`, `schemas.py`, session tests | router, Lane protocol, BridgeBreaker, facade |
| L2 profile | `fl_mcp/profile.py`, `scrape_rtti.py`, `gen_layout.py`, `profiles/**`, `native/generated/**`, their tests | CapabilityProfile, load_profile, generated header |
| L3 observer | `fl_mcp/observer.py`, `bridge_wire.py`, observer tests | UserAction event stream, Observer |
| L4 native | `native/**` (bridge bulkhead, `native/abi/`, event forwarding, discovery ops, probe deletion) | bridge heartbeat/event/discovery wire |
| L5 flpkit | the flpkit repo (separate) | write_automation, detect-don't-assume, write split |

Integration: the manager merges lanes onto `integrate/parallel` in dependency order and resolves the (few, mechanical) import wirings.

## §1 The Lane protocol (L1 owns; File/Bridge lanes implement)
```python
class NoteLane(Protocol):
    name: Literal["project-write", "bridge"]
    def write(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt: ...
    def clear(self, target: Target) -> WriteReceipt: ...
    def read(self, target: Target) -> list[Note]: ...   # may raise if the lane can't read (bridge)
```
Receipts are readback, never echo (existing WriteReceipt contract). A lane failure RAISES; it never silently falls through - the router decides fallback BEFORE the call.

## §2 Capabilities + router (L1 owns) - replaces the flag
```python
@dataclass(frozen=True)
class Capabilities:
    bridge_live: bool          # fresh heartbeat + proto match + ping round-trip
    bridge_pattern: int | None # the bridge's current pattern (from heartbeat pat=)
    bridge_channel: int | None # the bridge's selected channel (from heartbeat chan=)
    profile: "CapabilityProfileLike"

def detect(link, spool, project, profile) -> Capabilities: ...  # pure, per-op, no env, no globals
def pick_note_lane(caps: Capabilities, target: Target, breaker: "BridgeBreaker") -> NoteLane: ...
    # bridge iff caps.bridge_live AND breaker.closed AND caps.bridge_channel == target.channel
    # (channel targeting is bridge-blind: VERDICT Q2); else file lane. File lane always for offline.
```
When L5/D-lane later adds live channel selection, only the eligibility predicate widens; the table shape is stable.

## §3 BridgeBreaker (L1 owns)
```python
class BridgeBreaker:               # states: closed -> open -> half_open -> closed
    FAILURES_TO_OPEN = 3
    def record_success(self) -> None: ...
    def record_soft_failure(self) -> None: ...      # timeout/refusal/stale mid-command
    def record_hard_failure(self, fingerprint: str) -> None: ...  # FL death near an unacked cmd
    @property
    def closed(self) -> bool: ...                    # False => route to file lane
```
Gate live writes on a ping round-trip per session first, then trust heartbeat between failures (R4 B2-7).

## §4 CapabilityProfile (L2 owns) - every version-specific fact
```python
@dataclass(frozen=True)
class CapabilityProfile:
    meta: ProfileMeta                    # fl_version/build/arch/os/engine_sha
    structs: dict[str, StructLayout]     # TNoteParams/TNotesParams... field offsets+types
    getters: tuple[GetterFact, ...]
    push_events: frozenset[str]
    flp: FlpFacts                        # event_size_overrides, playlist_stride, note flags
    fst: FstFacts
    ppq_default: int
    capabilities: ProfileCapabilities    # note_readback_live, pattern_targeting_live, channel_targeting_live, score_edit_push
    tick: TickFacts                      # mean_ms, max_ms, frontmost_required

def load_profile() -> CapabilityProfile:
    """FLMCP_PROFILE env > <spool>/profile.json > packaged profile matching FL version
    > BUILTIN_FALLBACK (current constants) with a logged warning naming each fallback."""
```
L1 consumes this via a `CapabilityProfileLike` Protocol (only the fields it reads), so L1 never hard-imports L2. Schema JSON is docs/research/r4-streamline-and-audit.md §A1.

## §5 Observer / UserAction event (L3 owns; L4 produces the raw pushes)
```python
@dataclass(frozen=True)
class UserAction:
    kind: Literal["transport","position","selection","focus","context","routing","score_edit"]
    at: datetime
    detail: dict[str, Any]     # kind-specific, validated at construction
class Observer(Protocol):
    def poll(self) -> list[UserAction]: ...        # lazy: called at checkpoints (before-action/after-tool/at-save)
    def drain_pushes(self) -> list[UserAction]: ...# 0-latency FL callbacks the bridge forwarded
```
Push kinds map to FL callbacks (VERDICT Q3): transport=FPD_SetPlaying, position=FPD_SongPosChanged, selection/focus=FPD_SetFocus, context=FPD_ContextInfoChanged, routing=FPD_RoutingChanged. score_edit has NO push -> poll-and-diff only. Concurrency: an agent write refuses/defers if a fresh score_edit touches the same target within the guard window.

## §6 Bridge wire additions (L4 owns) - the surfaces L1/L3 read
- Heartbeat file gains `proto=2`, `pat=<n>`, `chan=<n>` lines (L1's detect() reads these).
- Event forwarding: FL push callbacks -> `bridge_events.jsonl` (append, ring-drained), schema = §5 kinds. L3 consumes.
- Discovery ops (`wp_*`) ride the existing spool/UDP executor. Static fatal-id ledger lives at `native/abi/fl_dispatch_ledger.h` (moved out of the probe before probe deletion). Generated ABI header from L2's gen_layout at `native/generated/fl_profile_generated.h`.
- Command/ack shapes unchanged from the v2 spool + UDP (#58); one executor, one consumed-ledger, exactly-once across carriers.

## §7 Facade seam (L1 owns) - unchanged public API
`from fl_mcp.session import FLSession, create_session, NotePlaneUnavailable, WriteRejected` stays byte-identical. `sdk.py`, `server.py`, and all callers do not change. `create_session()` calls `load_profile()` once and threads it in.

## Invariants every lane obeys (doctrine #54)
- No `FL_MCP_BRIDGE`, no feature flag, no off-by-default gate. Best lane chosen per call from evidence.
- Every version-specific constant comes from the profile (§4) or is detected by an in-data invariant; hardcoded only as a warned fallback.
- Boundaries verify by readback. Lanes raise, never silently fall through. CI is down: local gate (pytest+ruff, cmake+ctest) is the merge bar.
- Net-negative where possible; delete the superseded mechanism in the same PR.
