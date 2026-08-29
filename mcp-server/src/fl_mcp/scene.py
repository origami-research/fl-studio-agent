"""A compact, musical read of what is in the project.

Why this is not just "return the notes"
---------------------------------------
Dumping raw notes costs ~960 tokens for a 56-note project and tells a model
almost nothing it can act on — it still has to infer key, register and rhythm
from a list of `{midi, channel, start_beat}`. The same scene as a summary is
about 20 tokens *and* already answers the questions that get asked: what key is
this, what is on each channel, where is there room.

So the default is analysis, not enumeration. Note-level detail is available on
request, for the cases that genuinely need it — harmonising an existing line,
say — rather than being paid for on every read.

What is deliberately absent
---------------------------
No scale tables, no chord dictionaries, no "which notes fit Am7". A model
already knows music theory; spending tool surface on it is pure overhead. The
useful thing is *state the model cannot know*: what is actually in this project
right now. That is the whole scope.

Key inference is offered with a confidence, never asserted. Modal, chromatic or
sparse material fools every method, and a confidently wrong key is worse than
no key at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Scale degrees as semitone offsets from the tonic.
MAJOR = (0, 2, 4, 5, 7, 9, 11)
MINOR = (0, 2, 3, 5, 7, 8, 10)  # natural minor

# Below this, a channel's material is treated as bass-like and reported as root
# motion — the harmonic skeleton — rather than as a pitch-class set.
BASS_REGISTER_CEILING = 55  # G3


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


@dataclass
class ChannelSummary:
    index: int
    name: str
    note_count: int
    first_beat: float
    last_beat: float
    low: int
    high: int
    pitch_classes: list[int]
    root_motion: list[str] = field(default_factory=list)

    def describe(self) -> str:
        pcs = " ".join(NOTE_NAMES[p] for p in self.pitch_classes)
        text = (
            f"{self.name} (ch{self.index}): {self.note_count} notes, "
            f"beats {self.first_beat:g}-{self.last_beat:g}, "
            f"range {note_name(self.low)}-{note_name(self.high)}, "
            f"pitches {pcs}"
        )
        if self.root_motion:
            text += f", root motion {' '.join(self.root_motion)}"
        return text


def _infer_key(weighted: dict[int, int]) -> tuple[str, str]:
    """Best-fitting major/minor key, with a confidence label.

    Scores each of the 24 keys by how much of the material falls inside its
    scale, weighted by how often each pitch class occurs — so a passing
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
    """The lowest note in each bar — a bass line's harmonic skeleton.

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
    project: Path,
    channel_names: dict[int, str] | None = None,
    tempo: float | None = None,
) -> dict[str, Any]:
    """Analyse the project into something a model can reason about directly."""
    from harness.verify import read_notes

    rows = read_notes(project)
    names = channel_names or {}

    if not rows:
        return {
            "tempo": tempo,
            "key": None,
            "channels": [],
            "note_count": 0,
            "summary": f"tempo {tempo:g}, no notes yet" if tempo else "empty project",
        }

    by_channel: dict[int, list] = {}
    for row in rows:
        by_channel.setdefault(row.channel, []).append(row)

    weighted: dict[int, int] = {}
    for row in rows:
        weighted[row.midi % 12] = weighted.get(row.midi % 12, 0) + 1
    key, confidence = _infer_key(weighted)

    summaries: list[ChannelSummary] = []
    for index in sorted(by_channel):
        group = by_channel[index]
        low = min(r.midi for r in group)
        summary = ChannelSummary(
            index=index,
            name=names.get(index, f"Channel {index}"),
            note_count=len(group),
            first_beat=min(r.start_beat for r in group),
            last_beat=max(r.start_beat + r.length_beats for r in group),
            low=low,
            high=max(r.midi for r in group),
            pitch_classes=sorted({r.midi % 12 for r in group}),
            root_motion=_root_motion(group) if low < BASS_REGISTER_CEILING else [],
        )
        summaries.append(summary)

    lines = []
    if tempo:
        lines.append(f"tempo {tempo:g}")
    lines.append(f"key {key} (confidence: {confidence})")
    lines += [s.describe() for s in summaries]

    return {
        "tempo": tempo,
        "key": key,
        "key_confidence": confidence,
        "note_count": len(rows),
        "channels": [
            {
                "index": s.index,
                "name": s.name,
                "notes": s.note_count,
                "beats": [s.first_beat, s.last_beat],
                "range": [note_name(s.low), note_name(s.high)],
                "pitch_classes": [NOTE_NAMES[p] for p in s.pitch_classes],
                **({"root_motion": s.root_motion} if s.root_motion else {}),
            }
            for s in summaries
        ],
        "summary": "\n".join(lines),
    }


def notes_detail(project: Path, channel: int | None = None) -> list[dict[str, Any]]:
    """Note-level data, for the cases that genuinely need it."""
    from harness.verify import read_notes

    return [
        {
            "channel": r.channel,
            "key": note_name(r.midi),
            "midi": r.midi,
            "start": r.start_beat,
            "length": r.length_beats,
            "velocity": round(r.velocity / 128, 2),
        }
        for r in read_notes(project)
        if channel is None or r.channel == channel
    ]
