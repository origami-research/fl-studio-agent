"""Read and write FL Studio .flp files. Our own library; pyflp is dev-only.

LANE F implements this module (docs/21). READING decodes only what the product
consumes: ppq, tempo, channels (names + levels), and notes per pattern/channel.
WRITING is raw byte surgery - patch or append exactly the bytes that express
the change, never reserialize the whole file. Live FL rejects third-party
reserialization: verified 2026-08-27 (pyflp.save wrote a wrong FLhd channel
count and mangled a text event; our parser read the corrupt file back happily
while FL refused to load it).

UGLY(U1), quarantined here and nowhere else: the FLP wire format. Every
constant below is a reverse-engineered fact and names its evidence.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .schemas import ChannelLevels, Note, NoteWriteMode

# -- wire facts (verified live; tests/e2e/test_flp_writer_live.py) -------------
EVENT_TEMPO = 156  # u32 payload, BPM x 1000 (set 137 -> FL reads back 137000)
EVENT_PATTERN_NEW = 65  # u16 pattern number
EVENT_PATTERN_NOTES = 224  # varint-length blob of NOTE_STRUCT records

# 24-byte packed note record inside a PatternID.Notes blob (matches what FL
# writes; field semantics documented in _pack_note's implementation).
NOTE_STRUCT = struct.Struct("<IHHIHHBBBBBBBB")
NOTE_SIZE = 24
PPQ_DEFAULT = 96

# Channel mix raw ranges (FL stores fixed-point ints; the tool surface is 0..1
# volume and -1..1 pan so a model never has to know that).
LEVEL_MAX = 12800
PAN_CENTRE = 6400
VOLUME_DEFAULT = 10000

# Neutral per-note values FL writes for an ordinary note; wrong defaults show
# up as notes that play but behave oddly (silent, hard-panned, pitch-shifted).
FINE_PITCH_CENTER = 120
RELEASE_DEFAULT = 64
NOTE_PAN_CENTER = 64
MOD_DEFAULT = 128


class FlpError(ValueError):
    """Malformed .flp input or a write whose readback did not match."""


@dataclass(frozen=True)
class FlpNote:
    """One decoded note, in file units (ticks), plus tool-unit conversions."""

    position: int  # ticks
    length: int  # ticks
    key: int  # MIDI 0-127
    channel: int  # rack channel index
    velocity: int  # 0-127
    pan: int  # 0-128, 64 = centre


@dataclass(frozen=True)
class FlpChannel:
    """One decoded channel: identity plus mix levels in raw file units."""

    index: int
    name: str
    volume: int  # 0..LEVEL_MAX, VOLUME_DEFAULT if the file omits it
    pan: int  # 0..LEVEL_MAX, PAN_CENTRE if omitted
    pitch_semitones: int


@dataclass(frozen=True)
class FlpProject:
    """Decoded read-only view of a project file."""

    ppq: int
    tempo: float | None  # None = FL's default (the file omits the event)
    channels: tuple[FlpChannel, ...]
    notes: tuple[FlpNote, ...]  # all patterns; filter by pattern via notes_at

    def notes_in(self, pattern: int, channel: int | None = None) -> list[FlpNote]:
        """Notes in one pattern, optionally one channel."""
        raise NotImplementedError("Lane F")


# -- public API -----------------------------------------------------------------


def read(path: Path) -> FlpProject:
    """Parse the file. Raises FlpError naming the byte offset on bad input."""
    raise NotImplementedError("Lane F")


def notes_at(path: Path, pattern: int, channel: int) -> list[FlpNote]:
    """The notes actually in the SAVED file - THE readback for verification."""
    raise NotImplementedError("Lane F")


def write_notes(
    path: Path,
    notes: list[Note],
    *,
    pattern: int,
    channel: int,
    mode: NoteWriteMode,
) -> list[FlpNote]:
    """Splice the pattern's notes blob (raw surgery); return ``notes_at`` of
    the RESULT. Raises FlpError when the readback does not field-match what was
    sent (tick-quantized positions; velocity within 1/127)."""
    raise NotImplementedError("Lane F")


def set_tempo(path: Path, bpm: float) -> float:
    """Patch the tempo event's u32 in place, or APPEND one at the end of the
    event stream when FL omitted it (end-of-stream wins by sequential
    application - live-verified; mid-stream splices load-but-ignore or reject
    depending on position). Returns the readback. Port the live-verified v1
    implementation from flp_writer.set_tempo unchanged."""
    raise NotImplementedError("Lane F")


def set_channel_levels(
    path: Path,
    channel: int,
    *,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
) -> ChannelLevels:
    """Patch the channel's level events in place; None leaves a value alone.
    Returns the readback. DISCOVERY TASK (docs/21 Lane F): find the per-channel
    level event ids by byte-diffing a template before/after pyflp mutates them,
    then live-verify against real FL before trusting the map."""
    raise NotImplementedError("Lane F")


# -- helpers (each = exactly one wire-format job) --------------------------------


def _chunks(data: bytes) -> tuple[bytes, memoryview]:
    """Split FLhd header / FLdt event stream; raises FlpError on a non-FLP file.

    Layout: b"FLhd" + u32 len(6) + (u16 format, u16 nChannels, u16 ppq),
    then b"FLdt" + u32 len + the event stream.
    """
    raise NotImplementedError("Lane F")


def _events(stream: memoryview) -> Iterator[tuple[int, int, int]]:
    """Yield (event_id, payload_offset, payload_size). Encoding: id < 64 ->
    1 data byte, 64-127 -> 2, 128-191 -> 4, 192+ -> varint length + payload."""
    raise NotImplementedError("Lane F")


def _splice(data: bytearray, at: int, event: bytes) -> None:
    """Insert event bytes at ``at`` and bump the FLdt chunk length to match."""
    raise NotImplementedError("Lane F")


def _decode_notes(blob: bytes) -> list[FlpNote]:
    """Unpack NOTE_STRUCT records; raises FlpError on a truncated blob."""
    raise NotImplementedError("Lane F")


def _encode_notes(notes: list[Note], channel: int, ppq: int) -> bytes:
    """Pack tool-unit notes into NOTE_STRUCT records (ticks, 0-127 velocity)."""
    raise NotImplementedError("Lane F")
