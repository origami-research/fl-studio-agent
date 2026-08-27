"""Compressed rendering of observations for the model.

The pydantic models are the full, lossless truth. What the model *reads* should
be minimal: only the fields that differ from their resting default, and for
large note groups a summary instead of thousands of objects. This module is the
one place that turns a model into its compact wire form, so the compression
policy lives in exactly one spot.

Rule: `exclude_defaults` is for *views* (describing state - default-valued state
is uninteresting). It is NOT used for durable commands, where "explicitly set to
the default" must be distinguishable from "unset". Commands dump in full.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from . import hashing
from .schemas import Note, NoteSetSummary, SessionState, _resolve_key


def render(model: BaseModel) -> dict[str, Any]:
    """Compact dict: drop fields equal to their default and any None.

    A resting channel -> {"index": 1, "name": "808 Kick"}; only what someone
    actually changed survives.
    """
    return model.model_dump(mode="json", exclude_defaults=True, exclude_none=True)


def render_session(state: SessionState) -> dict[str, Any]:
    """The whole session, compacted. Empty channel/mixer lists drop out too."""
    return render(state)


# How many notes we hand over in full before switching to a summary. Above this,
# the model gets a NoteSetSummary and filters (server-side, no round trip) before
# materializing the slice it wants.
LAZY_THRESHOLD = 50


def _midi_to_name(midi: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[midi % 12]}{midi // 12 - 1}"


def note_view(notes: list[Note], threshold: int = LAZY_THRESHOLD) -> dict[str, Any] | list[dict[str, Any]]:
    """Full compact list when small; a summary when large.

    Small: [{"key":"C4","start":0,"length":1}, ...] (defaults dropped per note).
    Large: {"count":5000,"low":"C2","high":"C6","density":8.0,"id":"..."}.
    """
    if len(notes) <= threshold:
        return [render(n) for n in notes]
    return summarize_notes(notes).model_dump(exclude_none=True)


def summarize_notes(notes: list[Note]) -> NoteSetSummary:
    """Cheap census of a note group for the lazy path."""
    if not notes:
        return NoteSetSummary(count=0)
    midis = [_resolve_key(n.key) for n in notes]
    span_beats = max((n.start + n.length) for n in notes)
    density = round(len(notes) / span_beats, 2) if span_beats > 0 else None
    return NoteSetSummary(
        count=len(notes),
        low=_midi_to_name(min(midis)),
        high=_midi_to_name(max(midis)),
        density=density,
        id=hashing.noteset_hash(notes),
    )
