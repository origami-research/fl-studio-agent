# Envisioned surfaces + the readability guarantee

These are the frozen contracts for the generic-pipeline refactor (docs/29). Shown BEFORE any spawn. The design rule that answers "will I still understand how each feature works": every capability is ONE small file that reads like a spec of that feature; the pipeline it flows through is ONE module that reads like a single algorithm. Genericness lives in the pipeline; specificity lives in the feature file. You never cross-reference more than two files to understand a capability.

## The readability guarantee (the whole point)

To understand ANY capability you read exactly two things:
1. `ops/<cap>.py` + `flpkit/formats/<cap>.py` - the feature, stated plainly.
2. the ONE pipeline it uses (`core/codec.py`, `core/router.py`, ...) - read once, understood forever.

A `docs/READING_GUIDE.md` maps every capability -> its two files. If understanding a feature ever needs a third file or a special-case branch, the pipeline is wrong (docs/29 §6).

## Surface 1: Format (the codec's contract) - flpkit

A Format IS the feature, for anything that lives in the .flp file. Reading one implementation tells you how that element is found, packed, unpacked, and checked - nothing hidden in the engine.

```python
class Format(Protocol):
    name: str            # "notes", "playlist", "automation", "levels"
    event_id: int        # the FLP event that carries it

    def locate(self, stream: memoryview, target: Target) -> SpliceSite: ...
        # WHERE this element lives (e.g. notes: the pattern's blob for this channel)
    def encode(self, data: Sequence[Any], ppq: int) -> bytes: ...
        # tool units -> file bytes (e.g. beats->ticks, 0..1 vol -> the field's real type)
    def decode(self, blob: bytes, ppq: int) -> list[Any]: ...
        # file bytes -> tool units (the inverse; detection of era-variable sizes happens here)
    def verify(self, sent: Sequence[Any], readback: Sequence[Any]) -> None: ...
        # raise WriteRejected if the saved file doesn't field-match what we sent
```

The ONE engine every writer/reader uses:

```python
def read(path: Path, fmt: Format, target: Target) -> list[Any]:
    """locate -> decode. The only read path."""

def patch(path: Path, fmt: Format, target: Target, data: Sequence[Any], mode: Mode) -> list[Any]:
    """locate -> encode -> splice + fix chunk lengths -> re-read -> fmt.verify.
    The only write path. Notes, playlist, automation, levels all flow through here."""
```

## Surface 2: Carrier / Transport - fl_mcp/core

```python
class Carrier(Protocol):
    def send(self, cmd: Command) -> None: ...
    def await_response(self, cmd_id: int, timeout: float) -> Response: ...
    def is_live(self) -> bool: ...          # capability detection, per carrier

class Transport:
    """One send path. Picks the best LIVE carrier per call (UDP if up, else file
    spool), shares one consumed-ledger => exactly-once across carriers."""
    def __init__(self, carriers: list[Carrier]) -> None: ...
    def request(self, cmd: Command, timeout: float) -> Response: ...
```

## Surface 3: Operation / Lane / route - fl_mcp/core

```python
@dataclass(frozen=True)
class Operation:
    kind: str            # "write_notes", "write_automation", "set_mixer", ...
    target: Target
    requires: Capability # what a lane must satisfy (e.g. needs_channel_match, offline_ok)

class Lane(Protocol):
    name: str            # "project-write", "bridge"
    def can_serve(self, op: Operation, caps: Capabilities) -> bool: ...
    def apply(self, op: Operation, data: Sequence[Any]) -> Receipt: ...

def route(op: Operation, caps: Capabilities, lanes: Sequence[Lane], breaker) -> Lane:
    """The ONE routing rule: the first lane that can_serve under the evidence,
    best-capability first, breaker-gated. No per-feature branch."""
```

## Surface 4: Pass - fl_mcp/core (discovery)

```python
class Pass(Protocol):
    """One discovery probe. Reading it tells you what it asks FL and what it learns."""
    name: str            # "getters", "sweep", "map_align", "mixer_probe"
    def commands(self, profile: CapabilityProfile) -> list[Command]: ...
    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile: ...

def discover(passes: Sequence[Pass], transport: Transport, profile: CapabilityProfile) -> CapabilityProfile:
    """Run passes, fold each into the profile, crash-resume via the ledger. One driver."""
```

## Surface 5: CapabilityProfile + CapabilityTag - the single fact source + provenance

```python
@dataclass(frozen=True)
class CapabilityProfile:   # docs/27 §4 - every version-specific fact, loaded once
    ...

@dataclass(frozen=True)
class CapabilityTag:
    capability: str                          # "notes.write.pattern"
    status: Literal["live","byte","oracle","planned"]
    verified_fl: str | None                  # FL version last confirmed
    evidence: str                            # runbook / verdict / test ref
    def stale(self, current_fl: str) -> bool: ...
```

## Worked trace: how automation-write reads, end to end

To implement AND to understand automation-write, a human reads TWO short files.

`flpkit/formats/automation.py` (the feature, ~35 readable lines):
```python
class AutomationFormat:
    name = "automation"
    event_id = EVENT_CHANNEL_AUTOMATION      # 234

    def locate(self, stream, target):
        # the target channel's automation blob (kind-5 channel)
        return find_channel_event(stream, target.channel, self.event_id)

    def encode(self, points, ppq):
        # points (position beats, value 0..1, tension) -> 24-byte records,
        # x_delta = this.pos - prev.pos in ticks; header+trailer carried from template
        ...
    def decode(self, blob, ppq):
        # inverse; cumulative-sum x_delta -> absolute positions
        ...
    def verify(self, sent, readback):
        # every sent point present at its position/value (tick-quantized) or raise
        ...
```

`fl_mcp/ops/automation.py` (the capability binding, ~12 lines):
```python
AUTOMATION = Operation.spec(
    kind="write_automation",
    requires=Capability.FILE,        # file lane (no live automation host call - PROBE Q)
    tag=CapabilityTag("automation.write", status="byte", verified_fl="26.1.5",
                      evidence="docs/26 D1"),
)

def set_automation(session, channel, points):
    op = AUTOMATION.at(Target(channel=channel))
    return route(op, session.caps, session.lanes, session.breaker).apply(op, points)
```

That is the WHOLE feature. `route` (read once) picks the file lane; the file lane calls `codec.patch(path, AutomationFormat(), target, points, mode)` (read once); `AutomationFormat` (above) says exactly what the bytes are. No third file, no branch, no hidden engine magic. The same trace with `NotesFormat`/`MixerFormat` swapped is notes/mixer. Genericness bought brevity WITHOUT hiding the feature.

## Why readability improves, not degrades
- Today: understanding notes-write means reading a 110-line function that mixes locate+build+splice+verify+chunk-length math. Tomorrow: `NotesFormat` states each step in its own named method, and the messy chunk-length/splice math lives once in `codec.patch`, read once.
- A new reader learns the FIVE pipelines once, then every feature is a ~40-line spec file.
- The READING_GUIDE maps capability -> its two files, so "how does X work" is always a two-file answer.
