"""FLP file access, via flpkit - the extracted, open-source FLP library.

The single source of truth for the FLP wire format now lives at
https://github.com/clarkipeng/flpkit (extracted from this repo, where every
byte-level fact was corpus- and live-FL-verified). This module is the thin
seam between it and our typed world:

- ``schemas.Note`` satisfies flpkit's ``NoteLike`` protocol (key/start/length/
  velocity/pan in the same tool units), so note writes pass straight through.
- Only ``set_channel_levels`` re-wraps its result into the pydantic
  ``schemas.ChannelLevels`` the session's typed surface promises.

Format-level tests live in flpkit itself; this repo keeps the corpus
differential, the live-FL gates, and the tests of this seam.
"""

from __future__ import annotations

from pathlib import Path

import flpkit
from flpkit import (
    LEVEL_MAX,
    PAN_CENTRE,
    VOLUME_DEFAULT,
    FlpChannel,
    FlpError,
    FlpNote,
    FlpProject,
    notes_at,
    read,
    set_tempo,
    write_notes,
)

from .schemas import ChannelLevels

__all__ = [
    "LEVEL_MAX",
    "PAN_CENTRE",
    "VOLUME_DEFAULT",
    "ChannelLevels",
    "FlpChannel",
    "FlpError",
    "FlpNote",
    "FlpProject",
    "notes_at",
    "read",
    "set_channel_levels",
    "set_tempo",
    "write_notes",
]


def set_channel_levels(
    path: Path,
    channel: int,
    *,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
) -> ChannelLevels:
    """flpkit's patch-and-verify, re-wrapped into the typed schemas model."""
    levels = flpkit.set_channel_levels(
        path, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
    )
    return ChannelLevels(
        channel=levels.channel,
        volume=levels.volume,
        pan=levels.pan,
        pitch_semitones=levels.pitch_semitones,
    )
