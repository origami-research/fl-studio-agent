# Surfaces and readability

This is the readability contract for the generic pipelines in [docs/29](29-generic-pipelines.md). The navigable map is [docs/READING_GUIDE.md](READING_GUIDE.md).

## Rule

To understand a capability, read exactly two things:

1. The feature file: `fl_mcp/ops/<capability>.py` and, for file-backed capabilities, `flpkit/formats/<capability>.py`.
2. The one pipeline it flows through: codec, transport, router, discovery, reverse, or provenance.

If a feature needs a third explanatory file or a special-case branch, fix the pipeline or the feature shape.

## Surface 1: Format / codec

```python
class Format(Protocol):
    name: str
    event_id: int
    def locate(self, stream: memoryview, target: Target) -> SpliceSite: ...
    def encode(self, data: Sequence[Any], ppq: int) -> bytes: ...
    def decode(self, blob: bytes, ppq: int) -> list[Any]: ...
    def verify(self, sent: Sequence[Any], readback: Sequence[Any]) -> None: ...

def read(path: Path, fmt: Format, target: Target) -> list[Any]: ...
def patch(path: Path, fmt: Format, target: Target, data: Sequence[Any], mode: Mode) -> list[Any]: ...
```

## Surface 2: Carrier / transport

```python
class Carrier(Protocol):
    def send(self, cmd: Command) -> None: ...
    def await_response(self, cmd_id: int, timeout: float) -> Response: ...
    def is_live(self) -> bool: ...

class Transport:
    def request(self, cmd: Command, timeout: float) -> Response: ...
```

## Surface 3: Operation / lane / router

```python
@dataclass(frozen=True)
class Operation:
    kind: str
    target: Target
    requires: Capability

class Lane(Protocol):
    name: str
    def can_serve(self, op: Operation, caps: Capabilities) -> bool: ...
    def apply(self, op: Operation, data: Sequence[Any]) -> Receipt: ...

def route(op: Operation, caps: Capabilities, lanes: Sequence[Lane], breaker) -> Lane: ...
```

## Surface 4: Discovery pass

```python
class Pass(Protocol):
    name: str
    def commands(self, profile: CapabilityProfile) -> list[Command]: ...
    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile: ...

def discover(passes: Sequence[Pass], transport: Transport, profile: CapabilityProfile) -> CapabilityProfile: ...
```

## Surface 5: Profile and provenance

```python
@dataclass(frozen=True)
class CapabilityProfile:
    ...

@dataclass(frozen=True)
class CapabilityTag:
    capability: str
    status: Literal["live", "byte", "oracle", "planned"]
    verified_fl: str | None
    evidence: str
    def stale(self, current_fl: str) -> bool: ...
```

## Example trace

Automation write is a file-backed capability. `flpkit/formats/automation.py` describes the automation bytes and verifier. `fl_mcp/ops/automation.py` binds that format to the operation and provenance tag. The router chooses the file lane, and the file lane calls `codec.patch`. No capability-specific writer is allowed outside that path.
