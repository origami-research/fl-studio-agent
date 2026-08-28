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

import logging
import struct
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from .schemas import ChannelLevels, Note, NoteWriteMode

log = logging.getLogger(__name__)

# -- wire facts (verified live; tests/e2e/test_flp_writer_live.py) -------------
EVENT_TEMPO = 156  # u32 payload, BPM x 1000 (set 137 -> FL reads back 137000)
EVENT_PATTERN_NEW = 65  # u16 pattern number
EVENT_PATTERN_NOTES = 224  # varint-length blob of NOTE_STRUCT records

# Event ids confirmed against pyflp's enums (docs/21 addendum A8) and, for 219,
# decoded straight out of FL 2026's own stock templates.
EVENT_VERSION = 199  # ascii "major.minor..."; >= 11.5 -> text events are UTF-16-LE
EVENT_CHANNEL_NEW = 64  # u16 channel IID; starts that channel's event block
EVENT_CHANNEL_LEVELS = 219  # 24B: pan i32, volume u32, pitch i32, filter 12B
EVENT_VOL_WORD = 72  # legacy per-channel volume (pre-Levels files)
EVENT_PAN_WORD = 73  # legacy per-channel pan
EVENT_VOL_BYTE = 2  # older still
EVENT_PAN_BYTE = 3
EVENT_NAME_LEGACY = 192  # ChannelID._Name
EVENT_NAME_INTERNAL = 201  # PluginID.InternalName (e.g. the generator name)
EVENT_NAME_USER = 203  # PluginID.Name (the user-visible rename)
EVENT_MIXER_FLAGS = 236  # InsertID.Flags; first one means the mixer section began
EVENT_CUR_GROUP_ID = 146  # ProjectID.CurGroupId; v1's anchor for a new pattern

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
    # Which pattern the note came from (FL patterns are 1-based; 0 means the
    # blob preceded any PatternID.New event, so the file did not attribute it).
    # Additive extension to the frozen skeleton, flagged to the manager: without
    # it, FlpProject.notes_in(pattern) has no pattern to filter on.
    pattern: int = 0


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
        return [
            note
            for note in self.notes
            if note.pattern == pattern and (channel is None or note.channel == channel)
        ]


# -- public API -----------------------------------------------------------------


def read(path: Path) -> FlpProject:
    """Parse the file. Raises FlpError naming the byte offset on bad input."""
    data = path.read_bytes()
    header, stream = _chunks(data)
    ppq = int.from_bytes(header[4:6], "little")

    tempo: float | None = None
    unicode_text = False  # flips when FLVersion says >= 11.5
    channels: list[dict[str, int | str]] = []  # raw fields per rack channel, in order
    current: dict[str, int | str] | None = None
    pattern = 0  # 0 = before any PatternID.New (A9: stock files do this)
    notes: list[FlpNote] = []

    for event_id, off, size in _events(stream):
        payload = bytes(stream[off : off + size])
        if event_id == EVENT_VERSION:
            unicode_text = _version_is_unicode(payload, off)
        elif event_id == EVENT_TEMPO and size == 4:
            tempo = int.from_bytes(payload, "little") / 1000
        elif event_id == EVENT_CHANNEL_NEW and size == 2:
            current = {"index": int.from_bytes(payload, "little")}
            channels.append(current)
        elif event_id == EVENT_MIXER_FLAGS:
            current = None  # mixer section began; its Plugin events are not channel names
        elif event_id == EVENT_PATTERN_NEW and size == 2:
            pattern = int.from_bytes(payload, "little")
        elif event_id == EVENT_PATTERN_NOTES:
            if size % NOTE_SIZE:
                log.warning(
                    "%s: skipping notes event of size %d (not a multiple of %d) "
                    "at stream offset %d",
                    path.name, size, NOTE_SIZE, off,
                )
                continue
            notes.extend(replace(n, pattern=pattern) for n in _decode_notes(payload))
        elif current is not None:
            _apply_channel_event(current, event_id, payload, unicode_text, off)

    return FlpProject(
        ppq=ppq,
        tempo=tempo,
        channels=tuple(_build_channel(fields) for fields in channels),
        notes=tuple(notes),
    )


def notes_at(path: Path, pattern: int, channel: int) -> list[FlpNote]:
    """The notes actually in the SAVED file - THE readback for verification."""
    return read(path).notes_in(pattern, channel)


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
    data = bytearray(path.read_bytes())
    header, stream = _chunks(bytes(data))
    base = 8 + len(header) + 8  # FLdt payload's file offset
    ppq = int.from_bytes(header[4:6], "little")
    blob = _encode_notes(notes, channel, ppq)

    # One walk locates everything the splice needs: the end of the target
    # pattern's PatternID.New event, the extent of that pattern's existing
    # notes event (v1 semantics, with attribution fixed: only a 224 while the
    # target pattern is current counts), and v1's anchor for a new pattern.
    current = 0
    prev_end = 0
    pattern_end: int | None = None
    existing: tuple[int, int, int] | None = None  # (event head, payload off, size)
    anchor_end: int | None = None
    for event_id, off, size in _events(stream):
        head = prev_end
        prev_end = off + size
        if event_id == EVENT_PATTERN_NEW and size == 2:
            current = int.from_bytes(stream[off : off + size], "little")
            if current == pattern and pattern_end is None:
                pattern_end = off + size
        elif event_id == EVENT_PATTERN_NOTES and current == pattern and existing is None:
            existing = (head, off, size)
        elif event_id == EVENT_CUR_GROUP_ID and anchor_end is None:
            anchor_end = off + size

    if mode == "merge" and existing is not None:
        head, off, size = existing
        blob = bytes(stream[off : off + size]) + blob

    length = bytearray()
    n = len(blob)
    while True:  # varint length, 7 bits per byte, high bit = continue
        length.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            break
    notes_event = bytes([EVENT_PATTERN_NOTES]) + bytes(length) + blob

    if existing is not None:
        head, off, size = existing
        old_extent = (off + size) - head
        data[base + head : base + off + size] = notes_event
        dt_len_off = 8 + len(header) + 4
        old_len = int.from_bytes(data[dt_len_off : dt_len_off + 4], "little")
        new_len = old_len + len(notes_event) - old_extent
        data[dt_len_off : dt_len_off + 4] = new_len.to_bytes(4, "little")
    elif pattern_end is not None:
        _splice(data, base + pattern_end, notes_event)
    else:
        # No such pattern yet - create it where FL itself writes patterns,
        # after the display-group block (v1's anchor; stream start if absent).
        new_pattern = bytes([EVENT_PATTERN_NEW]) + pattern.to_bytes(2, "little")
        at = base + (anchor_end if anchor_end is not None else 0)
        _splice(data, at, new_pattern + notes_event)

    path.write_bytes(bytes(data))

    # The readback IS the contract: every sent note must be found in the saved
    # file, field-matched after the same tool-unit -> file-unit quantization.
    result = notes_at(path, pattern, channel)
    expected = _decode_notes(_encode_notes(notes, channel, ppq))
    pool = list(result)
    for want in expected:
        found = next(
            (
                got
                for got in pool
                if (got.position, got.length, got.key, got.channel, got.pan)
                == (want.position, want.length, want.key, want.channel, want.pan)
                and abs(got.velocity - want.velocity) <= 1
            ),
            None,
        )
        if found is None:
            raise FlpError(
                f"readback of {path.name} pattern {pattern} channel {channel} "
                f"is missing a sent note: {want}"
            )
        pool.remove(found)
    if mode == "replace" and pool:
        raise FlpError(
            f"readback of {path.name} pattern {pattern} channel {channel} has "
            f"{len(pool)} notes beyond the replace set, e.g. {pool[0]}"
        )
    return result


def set_tempo(path: Path, bpm: float) -> float:
    """Patch the tempo event's u32 in place, or APPEND one at the end of the
    event stream when FL omitted it (end-of-stream wins by sequential
    application - live-verified; mid-stream splices load-but-ignore or reject
    depending on position). Returns the readback. Port the live-verified v1
    implementation from flp_writer.set_tempo unchanged."""
    if not 10.0 <= bpm <= 999.0:
        raise ValueError(f"tempo {bpm} out of range (10-999 BPM)")

    data = bytearray(path.read_bytes())
    header, stream = _chunks(bytes(data))
    base = 8 + len(header) + 8  # FLdt payload's file offset
    payload = round(bpm * 1000).to_bytes(4, "little")

    patched = False
    for event_id, off, size in _events(stream):
        if event_id == EVENT_TEMPO and size == 4:
            data[base + off : base + off + 4] = payload
            patched = True
            break

    if not patched:
        _splice(data, base + len(stream), bytes([EVENT_TEMPO]) + payload)

    path.write_bytes(bytes(data))

    stored = read(path).tempo
    if stored is None or round(stored * 1000) != round(bpm * 1000):
        raise FlpError(f"tempo readback {stored!r} does not match requested {bpm}")
    return stored


def set_channel_levels(
    path: Path,
    channel: int,
    *,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
) -> ChannelLevels:
    """Patch the channel's levels in place; None leaves a value alone. Returns
    the readback. The discovery task docs/21 assigned here is CLOSED (addendum
    A8): levels are ONE event, ChannelID.Levels 219, fixed 24-byte payload -
    pan i32 [0:4], volume u32 [4:8], pitch i32 [8:12] - verified by decoding
    FL 2026's own stock templates. So this patches three int32s inside the
    existing event. A legacy project whose channel has no 219 event is refused
    (FlpError) rather than written with guessed units."""
    if volume is not None and not 0.0 <= volume <= 1.0:
        raise ValueError(f"volume {volume} out of range (0.0-1.0)")
    if pan is not None and not -1.0 <= pan <= 1.0:
        raise ValueError(f"pan {pan} out of range (-1.0 left to 1.0 right)")
    if pitch_semitones is not None and not -48 <= pitch_semitones <= 48:
        raise ValueError(f"pitch {pitch_semitones} out of range (-48 to 48)")

    data = bytearray(path.read_bytes())
    header, stream = _chunks(bytes(data))
    base = 8 + len(header) + 8  # FLdt payload's file offset

    current: int | None = None
    seen: set[int] = set()
    levels_off: int | None = None
    for event_id, off, size in _events(stream):
        if event_id == EVENT_CHANNEL_NEW and size == 2:
            current = int.from_bytes(stream[off : off + size], "little")
            seen.add(current)
        elif event_id == EVENT_MIXER_FLAGS:
            current = None
        elif (
            event_id == EVENT_CHANNEL_LEVELS
            and size == 24
            and current == channel
            and levels_off is None
        ):
            levels_off = off

    if channel not in seen:
        raise IndexError(f"no channel {channel}; project has {len(seen)}")
    if levels_off is None:
        raise FlpError(
            f"channel {channel} has no Levels event (id 219); refusing to write "
            "a legacy-format project with guessed units"
        )

    at = base + levels_off
    if pan is not None:
        data[at : at + 4] = struct.pack("<i", round(PAN_CENTRE + pan * PAN_CENTRE))
    if volume is not None:
        data[at + 4 : at + 8] = struct.pack("<I", round(volume * LEVEL_MAX))
    if pitch_semitones is not None:
        data[at + 8 : at + 12] = struct.pack("<i", pitch_semitones)
    path.write_bytes(bytes(data))

    # Report what the saved file contains, then confirm the requested fields
    # landed exactly (the write is deterministic byte surgery; any drift is a
    # writer bug, not tolerance).
    saved = next(c for c in read(path).channels if c.index == channel)
    mismatches = [
        name
        for name, requested, stored in (
            ("volume", volume if volume is None else round(volume * LEVEL_MAX), saved.volume),
            ("pan", pan if pan is None else round(PAN_CENTRE + pan * PAN_CENTRE), saved.pan),
            ("pitch_semitones", pitch_semitones, saved.pitch_semitones),
        )
        if requested is not None and requested != stored
    ]
    if mismatches:
        raise FlpError(
            f"channel {channel} readback does not match the write: {mismatches}"
        )
    return ChannelLevels(
        channel=channel,
        volume=saved.volume / LEVEL_MAX,
        pan=(saved.pan - PAN_CENTRE) / PAN_CENTRE,
        pitch_semitones=saved.pitch_semitones,
    )


# -- helpers (each = exactly one wire-format job) --------------------------------


def _chunks(data: bytes) -> tuple[bytes, memoryview]:
    """Split FLhd header / FLdt event stream; raises FlpError on a non-FLP file.

    Layout: b"FLhd" + u32 len(6) + (u16 format, u16 nChannels, u16 ppq),
    then b"FLdt" + u32 len + the event stream.
    """
    if data[:4] != b"FLhd":
        raise FlpError("not an FLP file: no FLhd magic at offset 0")
    if len(data) < 8:
        raise FlpError(f"truncated FLhd chunk: file is only {len(data)} bytes")
    hd_len = int.from_bytes(data[4:8], "little")
    if hd_len < 6 or 8 + hd_len > len(data):
        raise FlpError(f"bad FLhd length {hd_len} at offset 4")
    header = data[8 : 8 + hd_len]
    dt_off = 8 + hd_len
    if data[dt_off : dt_off + 4] != b"FLdt":
        raise FlpError(f"no FLdt magic at offset {dt_off}")
    if dt_off + 8 > len(data):
        raise FlpError(f"truncated FLdt chunk header at offset {dt_off}")
    dt_len = int.from_bytes(data[dt_off + 4 : dt_off + 8], "little")
    start = dt_off + 8
    if start + dt_len > len(data):
        raise FlpError(
            f"FLdt length {dt_len} at offset {dt_off + 4} overruns the file "
            f"({len(data)} bytes)"
        )
    return header, memoryview(data)[start : start + dt_len]


def _events(stream: memoryview) -> Iterator[tuple[int, int, int]]:
    """Yield (event_id, payload_offset, payload_size). Encoding: id < 64 ->
    1 data byte, 64-127 -> 2, 128-191 -> 4, 192+ -> varint length + payload.

    Offsets are relative to the event stream (add the FLdt payload's file
    offset for an absolute position). Raises FlpError on truncation.
    """
    end = len(stream)
    pos = 0
    while pos < end:
        head = pos
        event_id = stream[pos]
        pos += 1
        if event_id < 192:
            size = 1 if event_id < 64 else 2 if event_id < 128 else 4
        else:
            size = 0
            shift = 0
            while True:
                if pos >= end:
                    raise FlpError(
                        f"truncated varint for event {event_id} at stream offset {head}"
                    )
                byte = stream[pos]
                pos += 1
                size |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
        if pos + size > end:
            raise FlpError(
                f"event {event_id} payload of {size} bytes at stream offset {head} "
                f"overruns the stream ({end} bytes)"
            )
        yield event_id, pos, size
        pos += size


def _splice(data: bytearray, at: int, event: bytes) -> None:
    """Insert event bytes at ``at`` and bump the FLdt chunk length to match."""
    data[at:at] = event
    hd_len = int.from_bytes(data[4:8], "little")
    dt_len_off = 8 + hd_len + 4
    old_len = int.from_bytes(data[dt_len_off : dt_len_off + 4], "little")
    data[dt_len_off : dt_len_off + 4] = (old_len + len(event)).to_bytes(4, "little")


def _decode_notes(blob: bytes) -> list[FlpNote]:
    """Unpack NOTE_STRUCT records; raises FlpError on a truncated blob."""
    if len(blob) % NOTE_SIZE:
        raise FlpError(
            f"notes blob of {len(blob)} bytes is not a multiple of {NOTE_SIZE}"
        )
    notes = []
    for record in NOTE_STRUCT.iter_unpack(blob):
        position, _flags, rack_channel, length, key, _group = record[:6]
        velocity = record[11]
        pan = record[10]
        notes.append(
            FlpNote(
                position=position,
                length=length,
                key=key,
                channel=rack_channel,
                velocity=velocity,
                pan=pan,
            )
        )
    return notes


def _version_is_unicode(payload: bytes, offset: int) -> bool:
    """Decode an FLVersion (ascii) payload; True when >= 11.5, the version at
    which FL switched text events to UTF-16-LE."""
    try:
        text = payload.decode("ascii").rstrip("\0")
        parts = [int(part) for part in text.split(".")]
    except (UnicodeDecodeError, ValueError) as exc:
        raise FlpError(f"malformed FLVersion event at stream offset {offset}: {exc}") from exc
    return parts[:2] >= [11, 5]


def _text(payload: bytes, unicode_text: bool, offset: int) -> str:
    """Decode one text event payload; NUL-terminated, encoding per FLVersion."""
    codec = "utf-16-le" if unicode_text else "latin-1"
    try:
        return payload.decode(codec).rstrip("\0")
    except UnicodeDecodeError as exc:
        raise FlpError(f"undecodable {codec} text event at stream offset {offset}: {exc}") from exc


# Raw-field keys a channel block accumulates before _build_channel resolves them.
_CHANNEL_EVENT_FIELDS = {
    EVENT_NAME_USER: "name_user",
    EVENT_NAME_LEGACY: "name_legacy",
    EVENT_NAME_INTERNAL: "name_internal",
    EVENT_VOL_WORD: "vol_word",
    EVENT_PAN_WORD: "pan_word",
    EVENT_VOL_BYTE: "vol_byte",
    EVENT_PAN_BYTE: "pan_byte",
}


def _apply_channel_event(
    fields: dict[str, int | str],
    event_id: int,
    payload: bytes,
    unicode_text: bool,
    offset: int,
) -> None:
    """Decode one channel-scoped event into the channel's raw-field dict."""
    if event_id == EVENT_CHANNEL_LEVELS:
        if len(payload) != 24:
            log.warning(
                "skipping Levels event of size %d (expected 24) at stream offset %d",
                len(payload), offset,
            )
            return
        pan, volume, pitch = struct.unpack_from("<iIi", payload)
        fields.update(levels_pan=pan, levels_volume=volume, levels_pitch=pitch)
    elif event_id in (EVENT_NAME_USER, EVENT_NAME_LEGACY, EVENT_NAME_INTERNAL):
        fields[_CHANNEL_EVENT_FIELDS[event_id]] = _text(payload, unicode_text, offset)
    elif event_id in (EVENT_VOL_WORD, EVENT_PAN_WORD, EVENT_VOL_BYTE, EVENT_PAN_BYTE):
        fields[_CHANNEL_EVENT_FIELDS[event_id]] = int.from_bytes(payload, "little")


def _build_channel(fields: dict[str, int | str]) -> FlpChannel:
    """Resolve a channel's raw fields: Levels wins over the legacy word/byte
    events; report a default only when the file stored nothing at all. Name
    priority mirrors pyflp's display_name: user rename, else legacy name event,
    else the plugin's internal name."""
    name = fields["name_user"] if "name_user" in fields else fields.get("name_legacy", "")
    name = name or fields.get("name_internal", "")
    volume = fields.get("levels_volume", fields.get("vol_word", fields.get("vol_byte")))
    pan = fields.get("levels_pan", fields.get("pan_word", fields.get("pan_byte")))
    return FlpChannel(
        index=int(fields["index"]),
        name=str(name),
        volume=int(volume) if volume is not None else VOLUME_DEFAULT,
        pan=int(pan) if pan is not None else PAN_CENTRE,
        pitch_semitones=int(fields.get("levels_pitch", 0)),
    )


def _encode_notes(notes: list[Note], channel: int, ppq: int) -> bytes:
    """Pack tool-unit notes into NOTE_STRUCT records (ticks, 0-127 velocity)."""
    packed = bytearray()
    for note in notes:
        packed += NOTE_STRUCT.pack(
            round(note.start * ppq),  # position, ticks
            0,  # flags
            channel,  # rack_channel
            max(1, round(note.length * ppq)),  # length, ticks (never 0)
            note.key,  # MIDI number, firm int (schemas resolved any name)
            0,  # group
            FINE_PITCH_CENTER,
            0,  # _u1
            RELEASE_DEFAULT,
            0,  # midi_channel
            max(0, min(128, round(note.pan * 128))),
            max(0, min(127, round(note.velocity * 127))),
            MOD_DEFAULT,
            MOD_DEFAULT,
        )
    return bytes(packed)
