"""The single seam every lane codes against. Every type is FIRM:
unions resolve at construction, invariants live in the fields, and a dict
exists only at the MCP boundary via ``.model_dump()``.

This file is IMPLEMENTED, not a stub - it is the contract the lanes share.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from fl_mcp.clock import now
from fl_mcp.pitch import parse_key

SCHEMA_VERSION = 2

NoteWriteMode = Literal["merge", "replace"]  # THE mode type - validated once, at a boundary

Backend = Literal["live", "project-write", "bridge"]


class Note(BaseModel):
    """One note, in musical units. C4 = MIDI 60 = middle C.

    ``key`` is firmly an int after construction: "C4"/"F#3" are accepted as
    input and resolved once by ``pitch.parse_key``. Use ``pitch.key_name`` for
    display. No downstream code ever sees a union.
    """

    key: int = Field(ge=0, le=127)
    start: float = Field(ge=0)  # beats from pattern start
    length: float = Field(gt=0, le=1024)  # beats
    velocity: float = Field(default=0.8, ge=0, le=1)
    pan: float = Field(default=0.5, ge=0, le=1)
    color: int = Field(default=0, ge=0, le=15)

    @field_validator("key", mode="before")
    @classmethod
    def _resolve_key(cls, value: object) -> int:
        return parse_key(value)

    def canonical(self) -> dict[str, Any]:
        """Normalized form; the only basis for hashing and the action log."""
        return {
            "key": self.key,
            "start": round(self.start, 6),
            "length": round(self.length, 6),
            "velocity": round(self.velocity, 4),
            "pan": round(self.pan, 4),
            "color": self.color,
        }


class Target(BaseModel):
    """A pattern/channel coordinate. Firmly ints: channel names and the
    "current" default are resolved AT THE TOOL BOUNDARY (server/sdk), never
    carried as a union or a -1 sentinel into the core."""

    pattern: int = Field(ge=1)
    channel: int = Field(ge=0)


class NoteMove(BaseModel):
    """A note paired across a diff as dragged rather than delete+insert."""

    from_: Note = Field(alias="from")
    to: Note
    confidence: float = Field(gt=0, le=1)

    model_config = {"populate_by_name": True}


class NoteDelta(BaseModel):
    """The structured change one write made - the unit of history."""

    added: list[Note] = Field(default_factory=list)
    removed: list[Note] = Field(default_factory=list)
    moved: list[NoteMove] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.moved)


class WriteReceipt(BaseModel):
    """What a write CONFIRMED (readback), never what was requested."""

    backend: Backend
    target: Target
    confirmed_notes: int  # notes on this target in the saved/live state, post-write
    delta: NoteDelta


class PluginParam(BaseModel):
    """One plugin parameter, with its normalized live value and known semantics.

    E2-verified 2026-08-29: a generator exposes its full parameter surface
    over the wire (597 named params on Sytrus), set + readback exact up to
    FL's own quantization.
    """

    index: int = Field(ge=0)
    name: str
    value: float = Field(ge=0, le=1)
    display: str
    kind: Literal["continuous", "stepped", "enum", "bool"]
    options: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    unit: str | None = None


class PluginParamSpec(BaseModel):
    """Static semantics for one parameter of one identified plugin.

    FL's documented ``plugins`` API deliberately exposes normalized values only;
    formatted text, ranges and option labels belong to a plugin-specific model
    captured from that plugin.  This type is the boundary for that evidence.
    """

    index: int = Field(ge=0)
    name: str
    kind: Literal["continuous", "stepped", "enum", "bool"]
    options: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    unit: str | None = None


class PluginParamModel(BaseModel):
    """An immutable per-plugin parameter model, selected by FL plugin name."""

    plugin: str
    parameters: tuple[PluginParamSpec, ...]


class ChannelLevels(BaseModel):
    """A channel's mix state, in tool units (readback, post-write)."""

    channel: int = Field(ge=0)
    volume: float = Field(ge=0, le=1)
    pan: float = Field(ge=-1, le=1)
    pitch_semitones: int = Field(ge=-48, le=48)


class AutomationPoint(BaseModel):
    """One automation point, in beats and FL's normalized value scale."""

    position: float = Field(ge=0)
    value: float = Field(ge=-0.001, le=1.001)
    tension: float = Field(default=0.0, ge=-1, le=1)


class AutomationReceipt(BaseModel):
    """Automation points confirmed by reparsing the saved project file."""

    backend: Literal["project-write"]
    channel: int = Field(ge=0)
    points: list[AutomationPoint]


class PlaneStatus(BaseModel):
    connected: bool
    detail: str | None = None
    fix: str | None = None


class DoctorReport(BaseModel):
    status: Literal["ok", "degraded"]
    control_plane: PlaneStatus
    note_plane: PlaneStatus
    project_file: PlaneStatus


# -- spool wire (server <-> code inside FL) ----------------------------------


class AddNotesArgs(BaseModel):
    target: Target
    notes: list[Note] = Field(min_length=1, max_length=5000)
    mode: NoteWriteMode = "merge"
    # The project's PPQ, stamped by the session (project file first, hello as
    # fallback). Notes travel in beats; a consumer with its own PPQ source (the
    # pyscript reads flp.score.PPQ) may ignore the stamp, but the native Bridge
    # has no PPQ source of its own and falls back to 96 with a logged warning
    # when the stamp is absent.
    ppq: int | None = Field(default=None, ge=1)


class ClearNotesArgs(BaseModel):
    target: Target


class PingArgs(BaseModel):
    pass


# -- discovery passes (native bridge_discover.cpp dispatches wp_* by op) ------


class WpGettersArgs(BaseModel):
    pass


class WpTimingArgs(BaseModel):
    iters: int = 200


class WpTargetStep(BaseModel):
    pattern: int
    channel: int


class WpTargetArgs(BaseModel):
    ppq: int = 96
    steps: list[WpTargetStep]


class WpSweepArgs(BaseModel):
    ids: list[int]


SpoolOp = Literal[
    "add_notes",
    "clear_notes",
    "ping",
    "wp_getters",
    "wp_timing",
    "wp_target",
    "wp_sweep",
]

OP_ARGS: dict[str, type[BaseModel]] = {
    "add_notes": AddNotesArgs,
    "clear_notes": ClearNotesArgs,
    "ping": PingArgs,
    "wp_getters": WpGettersArgs,
    "wp_timing": WpTimingArgs,
    "wp_target": WpTargetArgs,
    "wp_sweep": WpSweepArgs,
}


class SpoolCommand(BaseModel):
    proto: int = SCHEMA_VERSION
    id: int
    op: SpoolOp
    args: dict[str, Any]  # validated against OP_ARGS[op] at send time
    ts: datetime = Field(default_factory=now)


class SpoolResponse(BaseModel):
    id: int
    ok: bool
    op: SpoolOp | None = None
    notes_written: int | None = None
    err: str | None = None
    # The wp_* ops ack a JSON evidence object; Pass.fold reads it (docs/30
    # Surface 4). None for ops that ack without evidence (add_notes, ping).
    result: dict[str, Any] | None = None


# -- control wire (server <-> controller script over SysEx) ------------------


class ControlRequest(BaseModel):
    id: int
    op: str  # dotted FL API name, e.g. "mixer.getCurrentTempo"
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    # Target FL process id: the shared MIDI wire broadcasts to every FL, and
    # each controller executes only the request whose pid matches its own
    # os.getpid(). None = broadcast to all (single-session dev).
    pid: int | None = None


class ControlResponse(BaseModel):
    id: int
    ok: bool
    ret: Any = None
    err: str | None = None


class HelloEvent(BaseModel):
    """Controller script -> server on attach."""

    script_ver: str
    fl_ver: str
    ppq: int
    tempo: float | None = None


# -- observations (FL state rendered to the model) ----------------------------


class Channel(BaseModel):
    index: int
    name: str
    volume: float = 0.78
    pan: float = 0.0
    muted: bool = False


class MixerTrack(BaseModel):
    index: int
    name: str
    volume: float = 0.8
    pan: float = 0.0
    muted: bool = False


class SessionState(BaseModel):
    tempo_bpm: float | None = None
    ppq: int | None = None
    playing: bool = False
    # None on the file lane, where the live pattern is unknown - a consumer
    # must never mistake a default for a fact.
    current_pattern: int | None = None
    channels: list[Channel] = Field(default_factory=list)
    mixer: list[MixerTrack] = Field(default_factory=list)

    @classmethod
    def from_wire(cls, snap: Any) -> SessionState:
        """The live ``state.snapshot`` payload, exactly as the controller sends
        it (tempo is getCurrentTempo's BPM x 1000). Raises KeyError/TypeError/
        ValueError on a malformed payload; the session wraps that into its
        typed error."""
        return cls(
            tempo_bpm=float(snap["tempo"]) / 1000.0,
            ppq=int(snap["ppq"]),
            playing=bool(snap["playing"]),
            current_pattern=int(snap["current_pattern"]),
            channels=[
                Channel(
                    index=c["idx"],
                    name=c["name"],
                    volume=float(c["vol"]),
                    pan=float(c["pan"]),
                    muted=bool(c["muted"]),
                )
                for c in snap["channels"]
            ],
            mixer=[
                MixerTrack(
                    index=t["idx"],
                    name=t["name"],
                    volume=float(t["vol"]),
                    pan=float(t["pan"]),
                    muted=bool(t["muted"]),
                )
                for t in snap["mixer"]
            ],
        )


# -- history ------------------------------------------------------------------


class NoteSetRef(BaseModel):
    """Content-addressed handle to a note set in the store."""

    id: str
    target: Target
    provenance: Literal["shadow", "project"]
    as_of: datetime


class ActionEntry(BaseModel):
    """One applied action in the durable log."""

    ts: datetime = Field(default_factory=now)
    id: int
    op: str
    target: Target | None = None
    mode: NoteWriteMode | None = None
    backend: Backend | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    delta: NoteDelta | None = None
    summary: str | None = None
