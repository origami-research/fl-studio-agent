"""Write notes directly into a .flp project file.

This is the autonomous backend. Every other route to FL's piano roll needs a
human at some point:

* the piano roll .pyscript only runs from FL's Scripting menu;
* triggering that menu by keystroke needs macOS automation consent (SIP-
  protected, cannot be granted programmatically) *and* a hotkey bound inside FL;
* a native plugin needing no trigger at all is impossible — FL refuses to load
  third-party binaries (docs/11).

Writing the project file sidesteps all of it. Notes go straight into the `.flp`,
and `open -a "FL Studio" project.flp` — which needs no permission — makes FL
show them. The cost is that FL reloads the project rather than editing it live,
so this is for agent-driven composition, not for editing alongside a user.

Note layout is a `PatternID.Notes` event holding packed 24-byte records, matching
PyFLP's `NotesEvent.STRUCT`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NOTE_STRUCT = "<IHHIHHBBBBBBBB"
NOTE_SIZE = 24
PPQ = 96

# Neutral values FL writes for an ordinary note; wrong defaults here show up as
# notes that play but behave oddly (silent, hard-panned, pitch-shifted).
FINE_PITCH_CENTER = 120
RELEASE_DEFAULT = 64
PAN_CENTER = 64
MOD_DEFAULT = 128


@dataclass
class WriteResult:
    project: Path
    pattern: int
    channel: int
    notes_written: int
    notes_in_pattern: int


def _pack_note(
    position: int,
    length: int,
    key: int,
    channel: int,
    velocity: int,
    pan: int,
) -> bytes:
    return struct.pack(
        NOTE_STRUCT,
        position,          # position, ticks
        0,                 # flags
        channel,           # rack_channel
        length,            # length, ticks
        key,               # key, MIDI number
        0,                 # group
        FINE_PITCH_CENTER,
        0,                 # _u1
        RELEASE_DEFAULT,
        0,                 # midi_channel
        pan,
        velocity,
        MOD_DEFAULT,
        MOD_DEFAULT,
    )


def _blob_from_notes(notes: list[dict[str, Any]], channel: int, ppq: int) -> bytes:
    packed = b""
    for note in notes:
        packed += _pack_note(
            position=round(float(note["start"]) * ppq),
            length=max(1, round(float(note["length"]) * ppq)),
            key=int(note["key"]),
            channel=channel,
            # Tool surface is 0..1; the file format is 0..127 / 0..128.
            velocity=max(0, min(127, round(float(note.get("velocity", 0.8)) * 127))),
            pan=max(0, min(128, round(float(note.get("pan", 0.5)) * 128))),
        )
    return packed


def write_notes(
    project: Path,
    notes: list[dict[str, Any]],
    pattern: int = 1,
    channel: int = 0,
    mode: str = "merge",
    ppq: int = PPQ,
) -> WriteResult:
    """Add notes to `project`, in place. Returns what actually landed.

    Re-reads the saved file before returning rather than trusting the write, so
    a malformed event surfaces here instead of as an empty piano roll.
    """
    # Imported lazily: the compat shim must be applied before pyflp is used, and
    # this module is importable in environments without pyflp at all.
    from harness.flp_compat import import_pyflp, parse
    from pyflp._events import U16Event
    from pyflp.pattern import NotesEvent, PatternID

    pyflp = import_pyflp()
    parsed = parse(project)
    events = list(parsed.events)

    # NotesEvent is a UserList of parsed note records, not raw bytes, so an
    # existing event is updated in place. Removing and re-inserting would mean
    # tracking indices that shift underneath us.
    existing_event = None
    pattern_index: int | None = None
    seen_pattern = False

    for i, event in enumerate(events):
        name = str(event.id)
        if name == "PatternID.New" and int(event.value) == pattern:
            pattern_index = i
            seen_pattern = True
        elif name == "PatternID.Notes" and seen_pattern and existing_event is None:
            existing_event = event

    blob = _blob_from_notes(notes, channel, ppq)
    if mode == "merge" and existing_event is not None and len(existing_event.data):
        blob = NotesEvent.STRUCT.build(existing_event.data) + blob

    if existing_event is not None:
        # Rebuild rather than mutate: assigning to the parsed .data view does
        # not survive serialisation, so the old event is dropped and a fresh one
        # constructed from the combined bytes.
        parsed.events.remove(PatternID.Notes, 0)
        insert_at = (pattern_index + 1) if pattern_index is not None else 0
        parsed.events.insert(insert_at, NotesEvent(PatternID.Notes, blob))
    elif pattern_index is not None:
        parsed.events.insert(pattern_index + 1, NotesEvent(PatternID.Notes, blob))
    else:
        # No such pattern yet — create it. Patterns live after the display-group
        # block, which is where FL itself writes them.
        anchor = next(
            (i for i, e in enumerate(events) if str(e.id) == "ProjectID.CurGroupId"),
            0,
        )
        parsed.events.insert(anchor + 1, NotesEvent(PatternID.Notes, blob))
        parsed.events.insert(
            anchor + 1, U16Event(PatternID.New, pattern.to_bytes(2, "little"))
        )

    pyflp.save(parsed, project)

    # Report what the saved file actually contains, not what we intended to
    # write: re-read via the same reader the round-trip verification uses, so a
    # malformed event surfaces here as a wrong count instead of as an empty
    # piano roll discovered later. (set_tempo and set_channel_levels already
    # re-read; this path was the gap.)
    from harness.verify import read_notes

    persisted = sum(1 for row in read_notes(project) if row.pattern == pattern)
    return WriteResult(
        project=project,
        pattern=pattern,
        channel=channel,
        notes_written=len(notes),
        notes_in_pattern=persisted,
    )


def clear_notes(project: Path, pattern: int = 1) -> WriteResult:
    return write_notes(project, [], pattern=pattern, mode="replace")


def set_tempo(project: Path, bpm: float) -> float:
    """Set the project tempo, in place. Returns the tempo actually stored.

    FL stores tempo as ProjectID.Tempo, an integer of BPM x 1000, so 130 BPM is
    130000. Writing the float directly silently produces a 130 BPM project at
    0.13 BPM, which is the kind of failure that looks like the write did
    nothing.

    Re-reads the saved file before returning rather than trusting the write.
    """
    from harness.flp_compat import import_pyflp, parse
    from pyflp._events import U32Event
    from pyflp.project import ProjectID

    if not 10.0 <= bpm <= 999.0:
        raise ValueError(f"tempo {bpm} out of range (10-999 BPM)")

    pyflp = import_pyflp()
    parsed = parse(project)

    # FL stores tempo as a U32 event of BPM x 1000. A project left at the default
    # tempo has NO such event (FL omits it), so mutate it when present and insert
    # one otherwise - the same way write_notes inserts a missing pattern event.
    found = False
    for event in parsed.events:
        if event.id == ProjectID.Tempo:
            event.value = round(bpm * 1000)
            found = True
    if not found:
        events = list(parsed.events)
        anchor = next((i for i, e in enumerate(events) if e.id == ProjectID.FLVersion), 0)
        parsed.events.insert(anchor + 1, U32Event(ProjectID.Tempo, round(bpm * 1000).to_bytes(4, "little")))

    pyflp.save(parsed, project)
    return float(parse(project).tempo)


# FL's channel levels are raw 0..12800 integers. Volume defaults to 10000 and
# pan is bipolar around 6400. The tool surface uses 0..1 and -1..1 so a model
# never has to know that.
LEVEL_MAX = 12800
PAN_CENTRE = 6400
VOLUME_DEFAULT = 10000


def set_channel_levels(
    project: Path,
    channel: int,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
) -> dict[str, Any]:
    """Set a channel's volume, pan and/or pitch, in place.

    Only the parameters passed are touched; None leaves a value alone, so
    setting pan does not reset a volume the user already adjusted.

    Returns what the saved file actually contains, re-read rather than echoed.
    """
    from harness.flp_compat import import_pyflp, parse

    pyflp = import_pyflp()
    parsed = parse(project)
    channels = list(parsed.channels)
    if not 0 <= channel < len(channels):
        raise IndexError(f"no channel {channel}; project has {len(channels)}")
    target = channels[channel]

    if volume is not None:
        if not 0.0 <= volume <= 1.0:
            raise ValueError(f"volume {volume} out of range (0.0-1.0)")
        target.volume = round(volume * LEVEL_MAX)
    if pan is not None:
        if not -1.0 <= pan <= 1.0:
            raise ValueError(f"pan {pan} out of range (-1.0 left to 1.0 right)")
        target.pan = round(PAN_CENTRE + pan * PAN_CENTRE)
    if pitch_semitones is not None:
        if not -48 <= pitch_semitones <= 48:
            raise ValueError(f"pitch {pitch_semitones} out of range (-48 to 48)")
        target.pitch_shift = int(pitch_semitones)

    pyflp.save(parsed, project)

    saved = list(parse(project).channels)[channel]
    # Explicit None checks, not `or`: hard-left pan is raw 0 and silence is
    # volume 0, both falsy. `saved.pan or PAN_CENTRE` reported a hard-left
    # channel as centred.
    raw_volume = saved.volume if saved.volume is not None else VOLUME_DEFAULT
    raw_pan = saved.pan if saved.pan is not None else PAN_CENTRE
    return {
        "channel": channel,
        "volume": round(raw_volume / LEVEL_MAX, 3),
        "pan": round((raw_pan - PAN_CENTRE) / PAN_CENTRE, 3),
        "pitch_semitones": saved.pitch_shift,
    }
