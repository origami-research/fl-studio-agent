"""The FLP project matrix and the file-lane cycle both e2e tiers share.

Six genuinely different file shapes, sourced from FL's own bundled Data dir
(read in place, never mutated - every cycle copies to tmp first) plus one
synthetic:

* minimal-basic-with-limiter - fresh minimal template; carries the 1-byte
  event 172 that desyncs pyflp (docs/21 addendum 13).
* chord-progression - the agent project's ancestor: 4 patterns, 4 playlist
  clips, 80-byte playlist records.
* demo-80-stride-automation - picked programmatically: the first bundled demo
  (sorted) whose playlist decodes at the 80-byte stride pyflp does not know
  AND that carries automation clips.
* demo-32-stride-legacy - picked programmatically: the first bundled demo
  whose playlist decodes at the legacy 32-byte stride.
* monster-astes-308ch - Astes - Bien Duro: 308 channels, the one corpus file
  pyflp cannot parse at all (docs/21 addendum A2).
* synthetic-multi-arrangement - the chord template with a second arrangement
  spliced in, mimicking the 99/241/36/233+track-events layout FL itself
  writes (verified on the three bundled two-arrangement demos).

The picks are BY DECODED SHAPE, not by filename: if a future FL build stops
shipping a shape, the matrix member fails loudly instead of silently testing
nothing.

The cycle (``run_file_cycle``) is the full file lane: read scene -> write
notes to a fresh pattern -> place a playlist clip -> set tempo -> set channel
levels -> re-read and assert coherence -> byte sanity -> the surgical-write
invariant (every event the writers had no business touching is byte-identical).
Tier 1 (test_matrix.py) runs it headless; tier 2 (test_matrix_live.py) runs
the same cycle and then makes real FL confirm the result.

Every failure names the matrix project and the cycle step (the ``step``
context manager), so a red run reads as "which file shape, which operation".
"""

from __future__ import annotations

import math
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import flpkit as flp
import pytest

from fl_mcp import scene
from fl_mcp.fl.paths import fl_app

FL_DATA = fl_app() / "Contents/Resources/FL/Data"
_TEMPLATES = FL_DATA / "Templates"
_BASIC = _TEMPLATES / "Minimal/Basic with limiter/Basic with limiter.flp"
_CHORD = _TEMPLATES / "Utility/Create a chord progression/Create a chord progression.flp"
_ASTES = FL_DATA / "Demo projects/Demo songs/Astes - Bien Duro.flp"

# The notes every cycle writes: a C-major triad, distinctive enough to spot in
# any readback and short enough to keep renders quick.
CYCLE_NOTES = (
    flp.NoteSpec(key=60, start=0.0, length=1.0),
    flp.NoteSpec(key=64, start=1.0, length=1.0),
    flp.NoteSpec(key=67, start=2.0, length=2.0),
)
CYCLE_VOLUME = 0.5  # raw 6400; every stock template ships 10000
CYCLE_PAN = 0.25  # raw 8000; distinct from centre 6400 and chord's 6372


@dataclass(frozen=True)
class MatrixProject:
    """One matrix member. ``source`` is None when a programmatic pick found
    no bundled file of the required shape - the cycle then fails naming the
    missing shape rather than skipping (a vanished shape is a finding)."""

    id: str
    source: Path | None
    shape: str  # what this member pins, for the failure message
    synthesize_arrangement: bool = False
    write_arrangement: int = 0

    @property
    def live_levels_readback(self) -> bool:
        """Template projects are ungrouped, so FL's plain channel-index API is
        proven for them (the old live gate used it); bundled demo songs use
        display groups whose index mapping is unverified, so tier 2 limits the
        levels readback to templates rather than gamble a live window on it."""
        return self.source is not None and _TEMPLATES in self.source.parents


@dataclass(frozen=True)
class CycleOutcome:
    """What the cycle wrote, for tier 2's live readbacks."""

    project: MatrixProject
    path: Path
    fresh_pattern: int
    channel: int
    tempo: float
    clip_written: bool
    levels_written: bool


@contextmanager
def step(project_id: str, name: str):
    """Name the matrix project and cycle step in any failure."""
    try:
        yield
    except Exception as e:
        raise AssertionError(f"[{project_id} / step:{name}] {e}") from e


# -- matrix construction ---------------------------------------------------------


def _playlist_strides(path: Path) -> set[int]:
    """Decoded record stride of every non-empty playlist event in the file,
    using the same detection rule as flpkit (smallest fitting 4-aligned size
    with the pattern_base signature at every record head)."""
    _, stream = flp._chunks(path.read_bytes())
    strides: set[int] = set()
    for event_id, off, size in flp._events(stream):
        if event_id == flp.EVENT_PLAYLIST and size:
            blob = bytes(stream[off : off + size])
            found = next(
                (
                    s
                    for s in range(flp.PLAYLIST_STRIDE_MIN, flp.PLAYLIST_STRIDE_MAX + 1, 4)
                    if len(blob) % s == 0 and flp._playlist_stride_fits(blob, s)
                ),
                None,
            )
            if found is not None:
                strides.add(found)
    return strides


@cache
def _corpus_picks() -> tuple[Path | None, Path | None]:
    """(first 80-stride demo with automation clips, first 32-stride demo),
    scanning the bundled corpus in sorted order. Astes is excluded from the
    80-stride pick because it holds its own matrix slot."""
    pick80: Path | None = None
    pick32: Path | None = None
    for path in sorted(FL_DATA.rglob("*.flp")):
        if pick80 is not None and pick32 is not None:
            break
        try:
            strides = _playlist_strides(path)
        except flp.FlpError:
            continue
        if pick32 is None and 32 in strides:
            pick32 = path
        if pick80 is None and 80 in strides and path != _ASTES:
            channels = flp.read(path).channels
            if any(c.is_automation and c.automation for c in channels):
                pick80 = path
    return pick80, pick32


def _build_matrix() -> list[MatrixProject]:
    pick80, pick32 = _corpus_picks()
    return [
        MatrixProject(
            id="minimal-basic-with-limiter",
            source=_BASIC if _BASIC.is_file() else None,
            shape="minimal template carrying the 1-byte event 172",
        ),
        MatrixProject(
            id="chord-progression",
            source=_CHORD if _CHORD.is_file() else None,
            shape="4-pattern / 4-clip template (the agent project's ancestor)",
        ),
        MatrixProject(
            id="demo-80-stride-automation",
            source=pick80,
            shape="demo with an 80-byte-stride playlist and automation clips",
        ),
        MatrixProject(
            id="demo-32-stride-legacy",
            source=pick32,
            shape="demo with a legacy 32-byte-stride playlist",
        ),
        MatrixProject(
            id="monster-astes-308ch",
            source=_ASTES if _ASTES.is_file() else None,
            shape="308-channel demo pyflp cannot parse",
        ),
        MatrixProject(
            id="synthetic-multi-arrangement",
            source=_CHORD if _CHORD.is_file() else None,
            shape="two-arrangement project (synthesized)",
            synthesize_arrangement=True,
            write_arrangement=1,
        ),
    ]


MATRIX: list[MatrixProject] = _build_matrix() if FL_DATA.is_dir() else []
MATRIX_IDS = [p.id for p in MATRIX]

requires_fl_data = pytest.mark.skipif(
    not FL_DATA.is_dir(), reason="FL Studio's bundled Data dir not found on this machine"
)


def selected(env_var: str, default: str = "") -> list[MatrixProject]:
    """The matrix trimmed by a comma-separated id list from ``env_var``
    (unset/empty -> ``default``; empty default -> the whole matrix). An
    unknown id is an operator typo and fails loudly."""
    chosen = os.environ.get(env_var, "") or default
    if not chosen:
        return MATRIX
    ids = [s.strip() for s in chosen.split(",") if s.strip()]
    known = {p.id for p in MATRIX}
    unknown = [i for i in ids if i not in known]
    assert not unknown, f"{env_var} names unknown projects {unknown}; known: {sorted(known)}"
    return [p for p in MATRIX if p.id in ids]


# -- the synthetic build ---------------------------------------------------------

# Events observed inside an arrangement block after its playlist, on FL's own
# files: track records (238), track names (239) and their 1-byte companions.
_ARRANGEMENT_EVENTS = frozenset(
    {flp.EVENT_ARRANGEMENT_NEW, 241, 36, flp.EVENT_PLAYLIST, 238, 239, 43}
)


def synthesize_second_arrangement(path: Path) -> None:
    """Append arrangement 1 to a single-arrangement file by duplicating the
    whole arrangement-0 block (NewArrangement 99, name 241, 36, playlist 233,
    track events) with the 99 payload patched to 1 - the exact layout FL
    writes for its own second arrangements."""
    data = bytearray(path.read_bytes())
    header, stream = flp._chunks(bytes(data))
    base = 8 + len(header) + 8  # FLdt payload's file offset

    prev_end = 0
    start: int | None = None
    end: int | None = None
    for event_id, off, size in flp._events(stream):
        head = prev_end
        prev_end = off + size
        if start is None:
            if event_id == flp.EVENT_ARRANGEMENT_NEW and size == 2:
                start = head
        elif end is None and event_id not in _ARRANGEMENT_EVENTS:
            end = head
    if start is None:
        raise flp.FlpError(f"{path.name} has no arrangement block to duplicate")
    if end is None:
        end = prev_end

    block = bytes(data[base + start : base + end])
    duplicate = block[:1] + (1).to_bytes(2, "little") + block[3:]
    flp._splice(data, base + end, duplicate)
    path.write_bytes(bytes(data))

    arrangements = {item.arrangement for item in flp.read(path).playlist}
    if arrangements != {0, 1}:
        raise flp.FlpError(
            f"synthesized {path.name} decodes arrangements {sorted(arrangements)}, wanted [0, 1]"
        )


# -- the shared file-lane cycle --------------------------------------------------


def _pattern_ids(raw: bytes) -> set[int]:
    _, stream = flp._chunks(raw)
    return {
        int.from_bytes(stream[off : off + size], "little")
        for event_id, off, size in flp._events(stream)
        if event_id == flp.EVENT_PATTERN_NEW and size == 2
    }


def _assert_walks_cleanly(raw: bytes) -> None:
    """FLhd magic present, FLdt framing consistent, and the event walk lands
    exactly on the declared stream end."""
    assert raw[:4] == b"FLhd", "FLhd magic missing"
    _, stream = flp._chunks(raw)  # raises FlpError on bad framing
    consumed = 0
    for _event_id, off, size in flp._events(stream):  # raises FlpError on overrun
        consumed = off + size
    assert consumed == len(stream), (
        f"event walk consumed {consumed} of {len(stream)} stream bytes"
    )


def _untouched_events(
    raw: bytes, *, pattern: int, channel: int, arrangement: int
) -> list[tuple[int, bytes]]:
    """Every (id, payload) event the cycle's writers had NO business touching.

    The writers' contract is surgery: write_notes may create the fresh
    pattern's New(65)/Notes(224), write_playlist may rewrite the target
    arrangement's playlist(233), set_tempo the tempo(156), and
    set_channel_levels the target channel's Levels(219). Filtering exactly
    those out of before and after must leave byte-identical event lists -
    that IS the surgical-write invariant.

    Attribution mirrors flpkit's own rules: channel 0 is implicit, New(64)
    switches, the mixer section (first 236) ends channel attribution;
    New(65)/NewArrangement(99) switch pattern/arrangement."""
    _, stream = flp._chunks(raw)
    current_pattern = 0
    current_arr = 0
    current_channel: int | None = None
    mixer_seen = False
    kept: list[tuple[int, bytes]] = []
    for event_id, off, size in flp._events(stream):
        payload = bytes(stream[off : off + size])
        if event_id == flp.EVENT_CHANNEL_NEW and size == 2 and not mixer_seen:
            current_channel = int.from_bytes(payload, "little")
        elif event_id == flp.EVENT_MIXER_FLAGS:
            mixer_seen = True
            current_channel = None
        elif not mixer_seen and current_channel is None and flp._is_channel_scoped(event_id):
            current_channel = 0
        if event_id == flp.EVENT_PATTERN_NEW and size == 2:
            current_pattern = int.from_bytes(payload, "little")
            if current_pattern == pattern:
                continue
        elif event_id == flp.EVENT_ARRANGEMENT_NEW and size == 2:
            current_arr = int.from_bytes(payload, "little")
        if event_id == flp.EVENT_PATTERN_NOTES and current_pattern == pattern:
            continue
        if event_id == flp.EVENT_PLAYLIST and current_arr == arrangement:
            continue
        if event_id == flp.EVENT_TEMPO:
            continue
        if (
            event_id == flp.EVENT_CHANNEL_LEVELS
            and not mixer_seen
            and current_channel == channel
        ):
            continue
        kept.append((event_id, payload))
    return kept


def run_file_cycle(project: MatrixProject, workdir: Path, *, tempo: float) -> CycleOutcome:
    """The full file-lane cycle against a tmp copy; asserts as it goes."""
    if project.source is None:
        pytest.fail(
            f"[{project.id}] no bundled project matches this shape ({project.shape}); "
            "the FL corpus on this machine changed - re-pin the matrix"
        )

    target = workdir / f"{project.id}.flp"
    with step(project.id, "copy-to-tmp"):
        shutil.copy2(project.source, target)
        target.chmod(0o644)  # copy2 preserves the bundle's read-only mode
        if project.synthesize_arrangement:
            synthesize_second_arrangement(target)

    with step(project.id, "read-scene"):
        before_bytes = target.read_bytes()
        before = flp.read(target)
        assert before.channels, "reader found no channels"
        before_summary = scene.summarize(target, tempo=before.tempo)
        assert before_summary["note_count"] == len(before.notes)

    fresh = max(_pattern_ids(before_bytes), default=0) + 1
    channel = next((c.index for c in before.channels if not c.is_automation), 0)

    with step(project.id, "write-notes"):
        written = flp.write_notes(
            target, CYCLE_NOTES, pattern=fresh, channel=channel, mode="replace"
        )
        assert sorted(n.key for n in written) == [60, 64, 67]

    arrangement = project.write_arrangement
    before_clips = [i for i in before.playlist if i.arrangement == arrangement]
    with step(project.id, "write-clips"):
        if before_clips:
            song_end = max(i.position + i.length for i in before_clips) / before.ppq
            clip = flp.ClipSpec(
                pattern=fresh, track=1, start=math.ceil(song_end / 4) * 4, length=4.0
            )
            items = flp.write_playlist(
                target, [clip], mode="merge", arrangement=arrangement
            )
            assert any(
                i.pattern == fresh and i.position == round(clip.start * before.ppq)
                for i in items
            ), f"written clip missing from readback: {items}"
            clip_written = True
        else:
            # No existing record means no honest byte template for the
            # era-specific tail; flpkit must refuse, not invent bytes.
            with pytest.raises(flp.FlpError):
                flp.write_playlist(
                    target,
                    [flp.ClipSpec(pattern=fresh, track=1, start=0.0, length=4.0)],
                    arrangement=arrangement,
                )
            clip_written = False

    with step(project.id, "set-tempo"):
        assert flp.set_tempo(target, tempo) == pytest.approx(tempo)

    with step(project.id, "set-levels"):
        try:
            levels = flp.set_channel_levels(
                target, channel, volume=CYCLE_VOLUME, pan=CYCLE_PAN
            )
            assert levels.volume == pytest.approx(CYCLE_VOLUME)
            assert levels.pan == pytest.approx(CYCLE_PAN)
            levels_written = True
        except flp.FlpError as e:
            # A legacy project whose channel has no Levels(219) event is
            # refused rather than written with guessed units - the refusal is
            # the contract, so pin its reason.
            assert "Levels event" in str(e), e
            levels_written = False

    with step(project.id, "reread-coherence"):
        after = flp.read(target)
        assert after.ppq == before.ppq, "PPQ changed"
        assert after.tempo == pytest.approx(tempo), f"tempo readback {after.tempo}"
        assert [c.name for c in after.channels] == [c.name for c in before.channels], (
            "channel names changed"
        )
        assert {c.index: c.automation for c in after.channels} == {
            c.index: c.automation for c in before.channels
        }, "automation points changed"
        assert len(after.notes) == len(before.notes) + len(CYCLE_NOTES), (
            f"note count {len(before.notes)} -> {len(after.notes)}"
        )
        assert sorted(n.key for n in after.notes_in(fresh, channel)) == [60, 64, 67]
        after_clips = [i for i in after.playlist if i.arrangement == arrangement]
        ours = [i for i in after_clips if i.pattern == fresh]
        assert len(ours) == (1 if clip_written else 0)
        assert [i for i in after_clips if i.pattern != fresh] == before_clips, (
            "pre-existing clips of the target arrangement moved"
        )
        assert [i for i in after.playlist if i.arrangement != arrangement] == [
            i for i in before.playlist if i.arrangement != arrangement
        ], "clips of an untargeted arrangement changed"
        if levels_written:
            saved = next(c for c in after.channels if c.index == channel)
            assert saved.volume == round(CYCLE_VOLUME * flp.LEVEL_MAX)
            assert saved.pan == round(flp.PAN_CENTRE + CYCLE_PAN * flp.PAN_CENTRE)
        after_summary = scene.summarize(target, tempo=after.tempo)
        assert after_summary["note_count"] == before_summary["note_count"] + len(CYCLE_NOTES)

    after_bytes = target.read_bytes()
    with step(project.id, "byte-sanity"):
        _assert_walks_cleanly(after_bytes)
        assert flp._chunks(after_bytes)[0] == flp._chunks(before_bytes)[0], (
            "FLhd header payload changed"
        )

    with step(project.id, "surgical-invariant"):
        untouched_before = _untouched_events(
            before_bytes, pattern=fresh, channel=channel, arrangement=arrangement
        )
        untouched_after = _untouched_events(
            after_bytes, pattern=fresh, channel=channel, arrangement=arrangement
        )
        assert len(untouched_before) == len(untouched_after), (
            f"event count outside the write set changed: "
            f"{len(untouched_before)} -> {len(untouched_after)}"
        )
        for i, (b, a) in enumerate(zip(untouched_before, untouched_after)):
            assert b == a, (
                f"event #{i} outside the write set changed: "
                f"id {b[0]} ({len(b[1])}B) -> id {a[0]} ({len(a[1])}B)"
            )

    return CycleOutcome(
        project=project,
        path=target,
        fresh_pattern=fresh,
        channel=channel,
        tempo=tempo,
        clip_written=clip_written,
        levels_written=levels_written,
    )
