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

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import flpkit
from flpkit import (
    LEVEL_MAX,
    PAN_CENTRE,
    VOLUME_DEFAULT,
    FlpAutomationPoint,
    FlpChannel,
    FlpError,
    FlpNote,
    FlpPlaylistItem,
    FlpProject,
    codec,
    notes_at,
    read,
    set_tempo,
    write_notes,
)

from .schemas import AutomationPoint, ChannelLevels

__all__ = [
    "LEVEL_MAX",
    "PAN_CENTRE",
    "VOLUME_DEFAULT",
    "ChannelLevels",
    "FlpAutomationPoint",
    "FlpChannel",
    "FlpError",
    "FlpNote",
    "FlpPlaylistItem",
    "FlpProject",
    "ProjectGuide",
    "notes_at",
    "project_guide",
    "read",
    "set_channel_levels",
    "set_tempo",
    "write_automation",
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


def write_automation(
    path: Path, channel: int, points: Sequence[AutomationPoint]
) -> tuple[FlpAutomationPoint, ...]:
    """Patch through flpkit, then return flpkit's saved-file readback.

    ``flpkit.write_automation`` is the canonical codec and performs the
    splice's own encode/save/reparse equality check.  This seam exposes the
    resulting parsed points so the session can independently reparse and
    compare its boundary readback before it reloads FL.
    """
    flpkit.write_automation(path, channel, points)
    project = read(path)
    for item in project.channels:
        if item.index == channel:
            if item.automation is None:
                raise FlpError(f"channel {channel} has no automation readback after save")
            return item.automation
    raise FlpError(f"channel {channel} is absent from the saved-file automation readback")


# The project's own comments (FL's File > Project info): text, UTF-16 since FL 11.5.
EVENT_COMMENTS = 195
EVENT_VERSION = 199


@dataclass(frozen=True)
class ProjectGuide:
    """What the project's comments say: per channel name, what it is for; and the rest.

    One rule: a line ``<channel name>: <text>`` describes that channel (the packaged
    template writes its palette this way, docs/07); every other line is a project note.
    """

    about: dict[str, str] = field(default_factory=dict)
    notes: str | None = None


def project_guide(path: Path, channel_names: Sequence[str]) -> ProjectGuide:
    _header, raw = codec.chunks(path.read_bytes())
    stream = codec.Stream(raw)
    text, unicode = None, True
    for event_id, offset, size in stream:
        if event_id == EVENT_VERSION:
            version = bytes(stream[offset : offset + size]).decode("ascii", "replace").rstrip("\0")
            unicode = tuple(int(x) for x in version.split(".")[:2] if x.isdigit()) >= (11, 5)
        elif event_id == EVENT_COMMENTS:
            payload = bytes(stream[offset : offset + size])
            text = (payload.decode("utf-16-le", "replace") if unicode else payload.decode("latin-1")).rstrip("\0")
            break
    if not text:
        return ProjectGuide()
    names = set(channel_names)
    about: dict[str, str] = {}
    notes = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        name, sep, rest = line.partition(": ")
        if sep and name in names and name not in about:
            about[name] = rest.strip()
        elif line.strip():
            notes.append(line.strip())
    return ProjectGuide(about=about, notes="\n".join(notes) or None)
