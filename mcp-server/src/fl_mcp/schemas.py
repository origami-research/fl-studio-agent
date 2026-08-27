"""FL MCP schema package - the single seam between all lanes.

Every command, response, note, and log line is defined here as a pydantic
model. Lanes code against these types:

- Lane A (Bridge plugin, C++) mirrors the command/response shapes on the spool.
- Lane B (controller script) mirrors the SysEx payload shapes.
- Lane C (server core) imports these directly.
- Lane D (kernel) imports C's API which is built on these.

Wire rule (spec 11): the spool carries beats and 0..1 normalized values;
conversion to FL units happens inside FL where the real PPQ lives.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


class Note(BaseModel):
    """One note, in musical units. C4 = MIDI 60 = middle C."""

    key: str | int  # "C4" / "F#3" / MIDI 0-127
    start: float = Field(ge=0)  # beats from pattern start
    length: float = Field(gt=0, le=1024)  # beats
    velocity: float = Field(default=0.8, ge=0, le=1)
    pan: float = Field(default=0.5, ge=0, le=1)
    color: int = Field(default=0, ge=0, le=15)

    def canonical(self) -> dict[str, Any]:
        """Normalized form; the only basis for hashing and the action log."""
        return {
            "key": _resolve_key(self.key),
            "start": round(self.start, 6),
            "length": round(self.length, 6),
            "velocity": round(self.velocity, 4),
            "pan": round(self.pan, 4),
            "color": self.color,
        }


def _resolve_key(key: str | int) -> int:
    """Note name or number -> MIDI 0-127. Raises ValueError with a model-readable message."""
    if isinstance(key, bool):
        raise ValueError(f"invalid key {key!r}")
    if isinstance(key, int):
        if not 0 <= key <= 127:
            raise ValueError(f"MIDI note {key} out of range (0-127)")
        return key
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"invalid key {key!r}")
    semitones = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    text = key.strip()
    letter = text[0].upper()
    if letter not in semitones:
        raise ValueError(f"invalid note name {key!r}: must start with A-G")
    semi = semitones[letter]
    idx = 1
    while idx < len(text) and text[idx] in "#b":
        semi += 1 if text[idx] == "#" else -1
        idx += 1
    try:
        octave = int(text[idx:])
    except ValueError:
        raise ValueError(f"invalid note name {key!r}: expected octave, e.g. 'C4'") from None
    midi = semi + (octave - 4) * 12 + 60
    if not 0 <= midi <= 127:
        raise ValueError(f"note {key!r} resolves to MIDI {midi}, outside 0-127")
    return midi


# ---------------------------------------------------------------------------
# Observations (FL state rendered to the model)
#
# Every default is chosen to be the *resting / no-op* value (0 dB = unity,
# centered pan, not muted, no plugin, empty lists) so that render() with
# exclude_defaults drops it. An untouched channel serializes to just its
# index and name; the model's eye goes straight to what is actually set.
# See views.render() and docs/16.
# ---------------------------------------------------------------------------


class Effect(BaseModel):
    slot: int
    name: str


class Send(BaseModel):
    to: int
    level: float = 0.8  # FL's default send level; an inactive send is simply absent


class Channel(BaseModel):
    index: int
    name: str
    plugin: str | None = None  # None = native sampler / no generator
    volume_db: float = 0.0  # 0 dB = unity
    pan: float = 0.0  # centered
    muted: bool = False
    routed_to: int | None = None  # None = master (track 0)


class MixerTrack(BaseModel):
    index: int
    name: str
    volume_db: float = 0.0
    pan: float = 0.0
    muted: bool = False
    solo: bool = False
    effects: list[Effect] = Field(default_factory=list)
    sends: list[Send] = Field(default_factory=list)


class SessionState(BaseModel):
    """The whole live session as one observation. Rendered compactly to the model."""

    tempo_bpm: float
    ppq: int = 96
    playing: bool = False
    current_pattern: int = 0
    channels: list[Channel] = Field(default_factory=list)
    mixer: list[MixerTrack] = Field(default_factory=list)
    source: Literal["live", "project"] = "live"


class NoteSetSummary(BaseModel):
    """Lazy stand-in for a large note group: a summary instead of N notes, so
    a 5,000-note pattern doesn't flood context. The model filters, then pulls."""

    count: int
    low: str | None = None  # lowest pitch, e.g. "C2"
    high: str | None = None  # highest pitch
    density: float | None = None  # notes per beat
    id: str | None = None  # content hash (via hashing.noteset_hash)


# ---------------------------------------------------------------------------
# Deltas (structured edits: what changed, not a replay of action strings)
# ---------------------------------------------------------------------------


class NoteMove(BaseModel):
    """A note whose value changed, paired from a removed hash to its new form."""

    from_hash: str
    to: Note
    confidence: float = Field(default=1.0, ge=0, le=1)


class NoteDelta(BaseModel):
    """A content-addressed diff between two note groups. Fold, don't replay."""

    added: list[Note] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)  # hashes of gone notes
    moved: list[NoteMove] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.moved)


# ---------------------------------------------------------------------------
# Targets and references
# ---------------------------------------------------------------------------


class Target(BaseModel):
    """Where a command applies. Names resolve via the manifest; -1 = current."""

    pattern: int | str = -1
    channel: int | str = -1


class NoteSetRef(BaseModel):
    """A server-side reference to a group of notes (FL has no note identity)."""

    id: str  # sha256 of schema_version + canonical notes
    provenance: Literal["shadow", "project", "model"]
    as_of: datetime
    confidence: float = Field(default=1.0, ge=0, le=1)
    stale: bool = False


class TrackRef(BaseModel):
    """A live handle to a channel. Guarded by the project generation."""

    channel: int
    name: str
    generation: int


# ---------------------------------------------------------------------------
# Spool commands (server -> Bridge plugin)
# ---------------------------------------------------------------------------


class SpoolCommand(BaseModel):
    """Envelope for every command file on the spool."""

    id: int
    op: str
    gen: int = 0  # project generation at issue time
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    args: dict[str, Any] = Field(default_factory=dict)


class AddNotesArgs(BaseModel):
    target: Target
    mode: Literal["merge", "replace"] = "merge"
    notes: list[Note] = Field(min_length=1, max_length=5000)


class ClearNotesArgs(BaseModel):
    target: Target


class PreviewNoteArgs(BaseModel):
    channel: int | str
    notes: list[Note] = Field(min_length=1, max_length=64)


class PingArgs(BaseModel):
    pass


OP_ARGS: dict[str, type[BaseModel]] = {
    "add_notes": AddNotesArgs,
    "clear_notes": ClearNotesArgs,
    "preview_note": PreviewNoteArgs,
    "ping": PingArgs,
}


# ---------------------------------------------------------------------------
# Spool responses (Bridge plugin -> server)
# ---------------------------------------------------------------------------


class SpoolResponse(BaseModel):
    id: int
    ok: bool
    op: str = ""
    notes_written: int | None = None
    err: str | None = None
    plugin_ver: str = ""


# ---------------------------------------------------------------------------
# Action log (append-only, oldest first; read in reverse for history)
# ---------------------------------------------------------------------------


class ActionEntry(BaseModel):
    ts: datetime
    id: int
    gen: int
    op: str
    target: Target | None = None
    mode: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    before: Any = None  # scalar before-value for param ops
    after: Any = None
    undo_step: str | None = None


# ---------------------------------------------------------------------------
# SysEx control-plane payloads (server <-> controller script)
# ---------------------------------------------------------------------------


class ControlRequest(BaseModel):
    """JSON payload inside a SysEx frame (base64 of UTF-8 JSON on the wire)."""

    id: int
    op: str  # "<module>.<function>" mirroring the FL Python API 1:1
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class ControlResponse(BaseModel):
    id: int
    ok: bool
    ret: Any = None
    err: str | None = None
    trace: str | None = None


class HelloEvent(BaseModel):
    """Controller script -> server on attach."""

    script_ver: str
    fl_ver: str
    ppq: int
    tempo: float | None = None
