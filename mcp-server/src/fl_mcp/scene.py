"""A compact, musical read of what is in the project.

Why this is not just "return the notes"
---------------------------------------
Dumping raw notes costs ~960 tokens for a 56-note project and tells a model
almost nothing it can act on - it still has to infer key, register and rhythm
from a list of `{midi, channel, start_beat}`. The same scene as a summary is
about 20 tokens *and* already answers the questions that get asked: what key is
this, what is on each channel, where is there room.

So the default is analysis, not enumeration. Note-level detail is available on
request, for the cases that genuinely need it - harmonising an existing line,
say - rather than being paid for on every read.

What is deliberately absent
---------------------------
No scale tables, no chord dictionaries, no "which notes fit Am7". A model
already knows music theory; spending tool surface on it is pure overhead. The
useful thing is *state the model cannot know*: what is actually in this project
right now. That is the whole scope.

One read of the whole project
-----------------------------
fl.scene() is the one way to see the whole project, so it is a line per channel,
pattern and mixer track, never their contents: in the arena's runs of 2026-11 the
first call printed the scene, fl.channels, fl.patterns and fl.mixer together,
23,558 characters on the starting project, and the harness's 20,000-character
cap on a tool result cut the end off. Each object's own read has the rest. tests/test_scene.py holds the
printout of a busy project to a budget well under the cap.

Key inference is offered with a confidence, never asserted. Modal, chromatic or
sparse material fools every method, and a confidently wrong key is worse than
no key at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import flp
from .schemas import Channel, MixerTrack, Note, PatternInfo, PlaylistClip


@dataclass(frozen=True)
class SceneRow:
    """One note in analysis units (beats, MIDI) - what this module reasons on."""

    midi: int
    channel: int = 0
    pattern: int = 1
    start_beat: float = 0.0
    length_beats: float = 1.0
    velocity: int = 100  # 0-127


def rows_from_notes(notes: Mapping[tuple[int, int], Sequence[Note]]) -> list[SceneRow]:
    """Rows from per-(pattern, channel) notes in tool units - the session's live view."""
    return [
        SceneRow(
            midi=n.key,
            channel=channel,
            pattern=pattern,
            start_beat=n.start,
            length_beats=n.length,
            velocity=round(n.velocity * 127),
        )
        for (pattern, channel), group in sorted(notes.items())
        for n in group
    ]


def project_rows(project: Path) -> list[SceneRow]:
    """Rows decoded from a saved project file (ticks -> beats)."""
    proj = flp.read(Path(project))
    return [
        SceneRow(
            midi=n.key,
            channel=n.channel,
            pattern=n.pattern,
            start_beat=n.position / proj.ppq,
            length_beats=n.length / proj.ppq,
            velocity=n.velocity,
        )
        for n in proj.notes
    ]


@dataclass(frozen=True)
class ClipRow:
    """One playlist clip in analysis units (beats). Exactly one of
    ``pattern``/``channel`` is set - a pattern clip or an audio/channel clip."""

    track: int
    start_beat: float
    length_beats: float
    pattern: int | None = None
    channel: int | None = None
    arrangement: int = 0


def read_clips(project: Path) -> list[ClipRow]:
    """Load playlist clips via the flp reader (ticks -> beats). The test seam."""
    proj = flp.read(Path(project))
    return [
        ClipRow(
            track=item.track,
            start_beat=item.position / proj.ppq,
            length_beats=item.length / proj.ppq,
            pattern=item.pattern,
            channel=item.channel,
            arrangement=item.arrangement,
        )
        for item in proj.playlist
    ]


@dataclass(frozen=True)
class AutomationRow:
    """One automation channel's clip, digested: FL names these channels after
    what they automate ("Volume - Master"), so the name IS the target."""

    channel: int
    name: str
    points: int
    span_beats: float
    from_value: float
    to_value: float


def read_automations(project: Path) -> list[AutomationRow]:
    """Load automation channels via the flp reader. The test seam."""
    proj = flp.read(Path(project))
    return [
        AutomationRow(
            channel=c.index,
            name=c.name,
            points=len(c.automation),
            span_beats=c.automation[-1].position,
            from_value=c.automation[0].value,
            to_value=c.automation[-1].value,
        )
        for c in proj.channels
        if c.automation
    ]


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Scale degrees as semitone offsets from the tonic.
MAJOR = (0, 2, 4, 5, 7, 9, 11)
MINOR = (0, 2, 3, 5, 7, 8, 10)  # natural minor

# Below this, a channel's material is treated as bass-like and reported as root
# motion - the harmonic skeleton - rather than as a pitch-class set.
BASS_REGISTER_CEILING = 55  # G3


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def _infer_key(weighted: dict[int, int]) -> tuple[str, str]:
    """Best-fitting major/minor key, with a confidence label.

    Scores each of the 24 keys by how much of the material falls inside its
    scale, weighted by how often each pitch class occurs - so a passing
    chromatic note counts for less than a repeated tonic. Confidence comes from
    the margin over the runner-up, because a near-tie means the material is
    genuinely ambiguous and should be reported as such.
    """
    total = sum(weighted.values())
    if total == 0:
        return "unknown", "none"

    scored: list[tuple[float, str]] = []
    for tonic in range(12):
        for degrees, label in ((MAJOR, "major"), (MINOR, "minor")):
            scale = {(tonic + d) % 12 for d in degrees}
            inside = sum(count for pc, count in weighted.items() if pc in scale)
            # Nudge toward keys whose tonic actually appears; otherwise all
            # rotations of a pentatonic set score identically.
            tonic_bonus = weighted.get(tonic, 0) * 0.35
            scored.append(((inside + tonic_bonus) / total, f"{NOTE_NAMES[tonic]} {label}"))

    scored.sort(reverse=True)
    best_score, best_key = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    margin = best_score - runner_up

    if best_score < 0.85:
        confidence = "low"       # a lot of the material sits outside any one scale
    elif margin >= 0.08:
        confidence = "high"
    elif margin >= 0.03:
        confidence = "medium"
    else:
        confidence = "low"       # several keys fit equally well
    return best_key, confidence


MAX_ROOT_MOTION_BARS = 8


def _root_motion(rows: list) -> list[str]:
    """The lowest note in each bar - a bass line's harmonic skeleton.

    Reported instead of chord names on purpose: naming chords from sparse or
    arpeggiated material guesses wrong often, and a wrong label is worse than a
    plain fact. The lowest note per bar is simply true.
    """
    by_bar: dict[int, int] = {}
    for row in rows:
        bar = int(row.start_beat // 4)
        if bar not in by_bar or row.midi < by_bar[bar]:
            by_bar[bar] = row.midi
    bars = sorted(by_bar)
    roots = [NOTE_NAMES[by_bar[bar] % 12] for bar in bars]
    # A 32-bar dump is the noise this module exists to remove. Eight bars is
    # enough to show the harmonic pattern; the rest is usually repetition.
    if len(roots) > MAX_ROOT_MOTION_BARS:
        return [*roots[:MAX_ROOT_MOTION_BARS], f"...+{len(roots) - MAX_ROOT_MOTION_BARS} bars"]
    return roots


def summarize(
    rows: Sequence[SceneRow],
    channels: Sequence[Channel] = (),
    *,
    patterns: Sequence[PatternInfo] | None = (),
    clips: Sequence[PlaylistClip] = (),
    mixer: Sequence[MixerTrack] | None = (),
    automations: Sequence[AutomationRow] = (),
    tempo: float | None = None,
    project_notes: str | None = None,
    unanswered: str | None = None,
) -> dict[str, Any]:
    """The whole project as SceneSummary's fields: one digest per channel, pattern and
    mixer track in use, the playlist's extent and the automation, from what the session
    read. A channel or pattern with no notes is listed too, so empty reads as empty, not
    missing. The digest says where things are and what they hold; each object's own
    read (fl.track(...).notes(), fl.playlist, fl.mixer) has the rest. ``patterns`` or
    ``mixer`` None: FL did not answer that read (``unanswered`` says why), so the digest
    leaves it out rather than guessing."""
    by_channel: dict[int, list[SceneRow]] = {}
    by_pattern: dict[int, list[SceneRow]] = {}
    for row in rows:
        by_channel.setdefault(row.channel, []).append(row)
        by_pattern.setdefault(row.pattern, []).append(row)
    listed = {c.index: c for c in channels}
    clip_channels = {c.channel for c in clips if c.channel is not None}
    unlisted = (by_channel.keys() | clip_channels) - listed.keys()
    arrangements = _arrangements(clips)

    weighted: dict[int, int] = {}
    for row in rows:
        weighted[row.midi % 12] = weighted.get(row.midi % 12, 0) + 1
    key, confidence = _infer_key(weighted) if rows else (None, None)

    return {
        "tempo": tempo,
        "key": key,
        "key_confidence": confidence,
        "note_count": len(rows),
        "project_notes": project_notes or None,
        "playlist": _playlist(clips, arrangements),
        "patterns": None if patterns is None else _patterns(patterns, by_pattern, clips, arrangements),
        "channels": [
            _channel(listed[i], by_channel.get(i, []), clips, arrangements) for i in sorted(listed)
        ] + [_unlisted_channel(i, by_channel.get(i, []), clips, arrangements) for i in sorted(unlisted)],
        "mixer": None if mixer is None else [_mixer_track(track) for track in mixer],
        "unanswered": unanswered if patterns is None or mixer is None else None,
        "automations": [
            {
                "channel": a.channel,
                "name": a.name,
                "points": a.points,
                "span_beats": a.span_beats,
                "values": [a.from_value, a.to_value],
            }
            for a in automations
        ],
    }


def _arrangements(clips: Sequence[PlaylistClip]) -> list[int | None]:
    """The song's arrangements: its one (every clip is in it, those added this session
    too), or each of several, with None for the clips added this session, which FL adds
    to the arrangement it shows without saying which."""
    numbers: list[int | None] = sorted({c.arrangement for c in clips if c.arrangement is not None})
    if len(numbers) <= 1:
        return numbers or [None]
    return numbers + ([None] if any(c.arrangement is None for c in clips) else [])


def _in(clip: PlaylistClip, arrangement: int | None, arrangements: list[int | None]) -> bool:
    return len(arrangements) == 1 or clip.arrangement == arrangement


def _placements(clips: Sequence[PlaylistClip], arrangements: list[int | None]) -> list[dict[str, Any]]:
    """Where ``clips`` start, in beats, by arrangement."""
    placed = []
    for arrangement in arrangements:
        beats = sorted({c.start for c in clips if _in(c, arrangement, arrangements)})
        if beats:
            placed.append({"arrangement": arrangement, "beats": beats})
    return placed


def _playlist(clips: Sequence[PlaylistClip], arrangements: list[int | None]) -> dict[str, Any]:
    """The arrangement's extent (each arrangement's, when there are several); where each
    pattern plays is in its own digest."""
    if not clips:
        return {"clip_count": 0}
    digest: dict[str, Any] = {
        "clip_count": len(clips),
        "tracks_used": len({c.track for c in clips if c.track is not None}),
    }
    untracked = sum(c.track is None for c in clips)
    if untracked:
        digest["untracked"] = untracked
    digest["arrangements"] = [
        {
            "arrangement": arrangement,
            "clip_count": len(group),
            "span_beats": [min(c.start for c in group), max(c.start + c.length for c in group)],
        }
        for arrangement in arrangements
        if (group := [c for c in clips if _in(c, arrangement, arrangements)])
    ]
    return digest


def _patterns(
    patterns: Sequence[PatternInfo],
    by_pattern: Mapping[int, list[SceneRow]],
    clips: Sequence[PlaylistClip],
    arrangements: list[int | None],
) -> list[dict[str, Any]]:
    """Each pattern: its length, where the playlist plays it, and which channels hold how
    many notes in it. A pattern FL did not list but notes or clips name is kept, unnamed."""
    known = {p.number: p for p in patterns}
    numbers = known.keys() | by_pattern.keys() | {c.pattern for c in clips if c.pattern is not None}
    digests = []
    for number in sorted(numbers):
        info = known.get(number)
        group = by_pattern.get(number, [])
        digests.append({
            "number": number,
            "name": info.name if info else None,
            "beats": info.beats if info else None,
            "at": _placements([c for c in clips if c.pattern == number], arrangements),
            "notes": len(group),
            "channels": sorted({r.channel for r in group}),
        })
    return digests


def _channel(
    channel: Channel, group: list[SceneRow], clips: Sequence[PlaylistClip], arrangements: list[int | None]
) -> dict[str, Any]:
    """One channel: where it plays, its levels, and what its notes span (the lowest note
    per bar too, for a bass-register part), with what the project's notes say it is for."""
    digest: dict[str, Any] = {
        "index": channel.index,
        "name": channel.name,
        "mixer_track": channel.mixer_track,
        "volume": channel.volume,
        "pan": channel.pan,
        "muted": channel.muted,
        **_notes(group),
    }
    placed = _placements([c for c in clips if c.channel == channel.index], arrangements)
    if placed:
        digest["clips_at"] = placed
    if channel.about:
        digest["about"] = channel.about
    return digest


def _unlisted_channel(
    index: int, group: list[SceneRow], clips: Sequence[PlaylistClip], arrangements: list[int | None]
) -> dict[str, Any]:
    """A channel that notes or clips name but FL's channel list does not: its levels and
    mixer track are unknown, so the digest says None rather than a default."""
    digest = _channel(Channel(index=index, name=f"Channel {index}"), group, clips, arrangements)
    return digest | {"mixer_track": None, "volume": None, "pan": None, "muted": None}


def _notes(group: list[SceneRow]) -> dict[str, Any]:
    if not group:
        return {"notes": 0}
    low = min(r.midi for r in group)
    digest: dict[str, Any] = {
        "notes": len(group),
        "patterns": sorted({r.pattern for r in group}),
        "beats": [min(r.start_beat for r in group), max(r.start_beat + r.length_beats for r in group)],
        "range": [note_name(low), note_name(max(r.midi for r in group))],
        "pitch_classes": [NOTE_NAMES[p] for p in sorted({r.midi % 12 for r in group})],
    }
    if low < BASS_REGISTER_CEILING:
        digest["root_motion"] = _root_motion(group)
    return digest


def _mixer_track(track: MixerTrack) -> dict[str, Any]:
    return {
        "index": track.index,
        "name": track.name,
        "volume": track.volume,
        "pan": track.pan,
        "muted": track.muted,
        "solo": track.solo,
        "effects": [{"slot": e.slot, "plugin": e.plugin} for e in track.effects],
        "sends": [{"target": s.target, "level": s.level} for s in track.sends if s.on],
    }


def notes_detail(
    rows: Sequence[SceneRow], channel: int | None = None, pattern: int | None = None
) -> list[dict[str, Any]]:
    """Note-level data, for the cases that genuinely need it."""

    return [
        {
            "channel": r.channel,
            "pattern": r.pattern,
            "key": note_name(r.midi),
            "midi": r.midi,
            "start": r.start_beat,
            "length": r.length_beats,
            "velocity": round(r.velocity / 127, 2),  # SceneRow velocity is 0-127
        }
        for r in rows
        if (channel is None or r.channel == channel) and (pattern is None or r.pattern == pattern)
    ]
