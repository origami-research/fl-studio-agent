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
            position=int(round(float(note["start"]) * ppq)),
            length=max(1, int(round(float(note["length"]) * ppq))),
            key=int(note["key"]),
            channel=channel,
            # Tool surface is 0..1; the file format is 0..127 / 0..128.
            velocity=max(0, min(127, int(round(float(note.get("velocity", 0.8)) * 127)))),
            pan=max(0, min(128, int(round(float(note.get("pan", 0.5)) * 128)))),
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

    written = len(blob) // NOTE_SIZE
    return WriteResult(
        project=project,
        pattern=pattern,
        channel=channel,
        notes_written=len(notes),
        notes_in_pattern=written,
    )


def clear_notes(project: Path, pattern: int = 1) -> WriteResult:
    return write_notes(project, [], pattern=pattern, mode="replace")
