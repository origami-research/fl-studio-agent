"""Hear an instrument preset before choosing it: fl.audition.

A sample needs no FL to be heard (run code reads it with fl.read_wav); a preset is a
plugin's state, so it sounds only when FL plays it. The agent project keeps one
channel for that, "Audition", which nothing in the song plays: the preset is loaded
there, the notes are written into the pattern that starts the arrangement, that
channel alone is rendered (the render's own solo), and its notes are written back as
they were, also when the render failed. The song's notes, mutes and every other
channel end as they began; the Audition channel keeps the preset last heard and its
name, so the next audition finds it.

The notes go where a render from bar 1 plays them: in pattern mode the selected
pattern, in song mode the pattern whose clip starts the playlist (the agent project's
Pattern 1).
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from fl_mcp import errors
from fl_mcp import sounds as stock_sounds
from fl_mcp.ops import notes as ops_notes
from fl_mcp.schemas import Audition, Note, Target

from . import controls, loading
from . import notes as note_ops
from .controls import BEATS_PER_BAR, live_arrangement_span

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

AUDITION_CHANNEL = "Audition"
# A two-bar phrase across the middle of the keyboard: a low root, a rising line, a held chord.
DEFAULT_NOTES = (
    Note(key=48, start=0.0, length=1.0, velocity=0.8),
    Note(key=60, start=1.0, length=0.5, velocity=0.8),
    Note(key=64, start=1.5, length=0.5, velocity=0.8),
    Note(key=67, start=2.0, length=0.5, velocity=0.8),
    Note(key=72, start=2.5, length=1.5, velocity=0.8),
    Note(key=60, start=4.0, length=4.0, velocity=0.8),
    Note(key=64, start=4.0, length=4.0, velocity=0.8),
    Note(key=67, start=4.0, length=4.0, velocity=0.8),
)
# Long enough for any phrase worth judging a sound by, short enough to keep the render quick.
MAX_BARS = 8


def audition(session: FLSession, preset: str, notes: Sequence[Note] | None, output: Path) -> Audition:
    """Load ``preset`` on the Audition channel, play ``notes`` (DEFAULT_NOTES) on it alone,
    and record it to ``output``; the notes are written back as they were."""
    try:
        # A name that is no preset file (a plugin's own preset, a guess) is refused naming the closest real ones.
        stock_sounds.resolve(stock_sounds.library(), preset, "preset")
        role, _ = stock_sounds.preset_role(preset)
    except ValueError as exc:
        raise errors.invalid_argument("fl.audition", str(exc)) from None
    if role != "instrument":
        raise errors.invalid_argument(
            "fl.audition", f"{preset!r} is an effect preset; audition an instrument preset (Plugin presets/Generators/...)"
        )
    phrase = list(DEFAULT_NOTES if notes is None else notes)
    if not phrase:
        raise errors.invalid_argument("fl.audition", "give at least one note, or none for the default phrase")
    end = max(n.start + n.length for n in phrase)
    bars = max(1, math.ceil(end / BEATS_PER_BAR - 1e-9))
    if bars > MAX_BARS:
        raise errors.invalid_argument("fl.audition", f"the notes run {bars} bars; an audition plays at most {MAX_BARS}")
    channel = loading.channels_named(session, (AUDITION_CHANNEL,), "fl.audition")[AUDITION_CHANNEL]
    target = _target(session, channel)
    session._require_note_path()
    before = note_ops.read_notes(session, target)  # raises when unknown: what to write back must be known first
    try:
        sound = loading.load_sound(session, channel, preset, "preset")
    except errors.ToolError:
        # FL may have loaded it anyway and renamed the channel after it: name it back, or the
        # next audition finds no Audition channel (box build 2026-11-13: SimSynth presets).
        with contextlib.suppress(errors.ToolError):
            loading.name_channel(session, channel, AUDITION_CHANNEL)
        raise
    sound = sound.model_copy(update={"name": loading.name_channel(session, channel, AUDITION_CHANNEL)})
    try:
        # Inside the try: a write FL applied but did not confirm must still be put back.
        ops_notes.write_notes(session, target, phrase, "replace")
        render = controls.render_wav(session, output, start_bar=1, bars=bars, solo=(channel,))
    finally:
        if before:
            ops_notes.write_notes(session, target, before, "replace")
        else:
            ops_notes.clear_notes(session, target)
    log.info("auditioned %s on channel %d (pattern %d)", preset, channel, target.pattern)
    return Audition(sound=sound, render=render)


def _target(session: FLSession, channel: int) -> Target:
    """Where a render from bar 1 plays the channel's notes (see the module docstring)."""
    span = live_arrangement_span(session)
    if span.pattern is not None:
        return Target(pattern=span.pattern, channel=channel)
    first = [c.pattern for c in session.playlist() if c.start == 0 and c.pattern is not None]
    if not first:
        raise errors.invalid_argument(
            "fl.audition", "no pattern starts the song at bar 1, so a render from bar 1 would not play the audition"
        )
    return Target(pattern=min(first), channel=channel)
