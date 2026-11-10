"""Live arrangement: patterns, their clips in the playlist, and a control's moves over time.

FL's controller API has no call that adds a playlist clip or an automation point: every
name in FL 2026's playlist, patterns and arrangement modules was listed live (API 45), and
none places, moves or removes a clip. FL's documented recording does both. While FL records
in song mode it keeps a control's moves in the selected pattern, and it adds that pattern's
clip in the playlist where playback started (FL manual, "Mouse & Controller Recording" and
"Note / MIDI Recording"). The controller runs one recording job on OnIdle (``ctl.record``,
``ctl.job``); this module asks for it and reads FL back.

A placement records the Bridge's mix level at its current value: FL keeps a clip only when
something was recorded, and the Bridge passes audio through unchanged, so its mix level is
an inert control. Live behaviour and its limits:
docs/research/arrangement-automation-live-2026-11-09.md.
"""

from __future__ import annotations

import itertools
import logging
import sys
from typing import TYPE_CHECKING

from fl_mcp import errors
from fl_mcp.errors import WriteRejected
from fl_mcp.schemas import AutomationPoint, AutomationTake, PatternInfo, PlacedClip

from .controls import BEATS_PER_BAR, FL_STEPS_PER_BEAT, _live_int, _live_read, _live_write

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# FL's patterns.findFirstNextEmptyPat flag: select the next empty pattern without asking
# for a name (IL API stubs, midi.FFNEP_DontPromptName).
FFNEP_DONT_PROMPT_NAME = 2
# The longest pattern name FL shows in full; a longer one is refused before anything changes.
PATTERN_NAME_MAX = 64
# A placement records for one beat, so FL keeps the marker however its playback starts.
PLACE_BEATS = 1
# How long the engine waits for a recording job past its own length: arming, starting,
# stopping and disarming each take a few ticks (live: 0.6 s for a one-beat placement), and
# the controller allows each of its six steps 3 s and the recording 5 s more.
JOB_OVERHEAD_SECONDS = 30.0
# After giving up, how long the engine waits for the controller to leave FL stopped.
JOB_CANCEL_SECONDS = 12.0
# Polled twice a second: the status is small, and FL answers it while it plays.
JOB_POLL_SECONDS = 0.5
# FL records once per OnIdle tick and plays a recorded value until the next one, so a played
# back value is the line asked for somewhere within one recording step of where it is read,
# to this much (live: within 0.005 on a 2-bar ramp). Read against the exact position, a dip
# on each kick was 0.55 off where the line falls steeply (fl-mcp-03, 2026-11-10). The first
# sample of a span can still hold the value recorded there before.
AUTOMATION_TOLERANCE = 0.03
SKIP_FIRST_TICKS = 1
# The automation a take reports: FL's played-back values, thinned to this many points.
REPORTED_POINTS = 17
# FL asks "What would you like to record?" before it records unless this setting is 1
# (Windows: HKCU\Software\Image-Line\FL Studio <N>\RMC, written when the box is ticked).
RECORD_QUESTION_SECTION = "RMC"
RECORD_QUESTION_SETTING = "PopupRec"

# What the controller records or samples: {"kind": "channel" | "mixer" | "effect", ...}.
ControlTarget = dict[str, object]


def _bar_beats(bar: int) -> float:
    return (bar - 1) * BEATS_PER_BAR


def _bar_of(beat: float) -> str:
    """A start in beats as the bar it falls in, with the beat when it is not a bar's first."""
    bar, within = divmod(beat, BEATS_PER_BAR)
    return f"{int(bar) + 1}" if within == 0 else f"{int(bar) + 1} (beat {within + 1:g})"


def pattern_info(session: FLSession, number: int) -> PatternInfo:
    return PatternInfo(
        number=number,
        name=str(_live_read(session, "patterns.getPatternName", [number])),
        beats=_live_int(session, "patterns.getPatternLength", [number]) / FL_STEPS_PER_BEAT,
    )


def resolve_pattern(session: FLSession, call: str, pattern: int | str) -> int:
    """A pattern by number, exact name or a unique part of its name, as FL names it now."""
    if isinstance(pattern, int):
        last = _live_int(session, "patterns.patternMax")
        if 1 <= pattern <= last:
            return pattern
        raise errors.invalid_argument(call, f"pattern {pattern} does not exist; FL has patterns 1 to {last}")
    known = session.patterns()
    exact = [p for p in known if p.name == pattern]
    partial = [p for p in known if pattern.lower() in p.name.lower()]
    chosen = exact if len(exact) == 1 else partial
    if len(chosen) == 1:
        return chosen[0].number
    listing = [f"{p.number}: {p.name}" for p in known]
    raise errors.unknown_target("pattern", pattern, listing)


def _check_name(call: str, name: str) -> str:
    name = name.strip()
    if not name or len(name) > PATTERN_NAME_MAX:
        raise errors.invalid_argument(call, f"a pattern name is 1 to {PATTERN_NAME_MAX} characters")
    return name


def _rename(session: FLSession, number: int, name: str) -> PatternInfo:
    _live_write(session, "patterns.setPatternName", [number, name])
    info = pattern_info(session, number)
    if info.name != name:
        raise WriteRejected(f"FL Studio named pattern {number} {info.name!r}, not {name!r}.", fix="Rename it again.")
    return info


def new_pattern(session: FLSession, name: str, copy_of: int | None = None) -> PatternInfo:
    """A new pattern: FL's next empty one, named ``name``, holding a copy of ``copy_of``."""
    name = _check_name("fl.new_pattern", name)
    _live_write(session, "patterns.findFirstNextEmptyPat", [FFNEP_DONT_PROMPT_NAME])
    number = _live_int(session, "patterns.patternNumber")
    if not _live_read(session, "patterns.isPatternDefault", [number]):
        raise WriteRejected(f"FL Studio selected pattern {number}, which is not empty.", fix="Try again.")
    if copy_of is not None:
        _live_write(session, "patterns.clonePattern", [copy_of, number])
        source = pattern_info(session, copy_of)
        copied = pattern_info(session, number)
        if _live_read(session, "patterns.isPatternDefault", [number]) or copied.beats != source.beats:
            raise WriteRejected(
                f"FL Studio did not copy pattern {copy_of} into pattern {number}.",
                fix="Copy it again; the new pattern is still empty.",
            )
        session.copy_pattern_notes(copy_of, number)
    return _rename(session, number, name)


def rename_pattern(session: FLSession, number: int, name: str) -> PatternInfo:
    return _rename(session, number, _check_name("fl.rename_pattern", name))


# -- recording jobs ---------------------------------------------------------------


def _require_record_question_off(session: FLSession) -> None:
    """Refuse before FL would ask what to record: the question is modal, so a recording
    started while it is on waits for a person (and froze FL when asked from a MIDI call)."""
    if sys.platform != "win32":
        return  # where FL keeps the setting on macOS is unchecked; the job times out instead
    from fl_mcp.fl.install import fl_settings

    if fl_settings(RECORD_QUESTION_SECTION).get(RECORD_QUESTION_SETTING) == "1":
        return
    raise errors.ToolError(
        "fl_api_error",
        "FL Studio asks what to record each time it records, and placing clips and automation records.",
        "Ask the user to press Record in FL Studio once, choose 'Notes and automation' and tick "
        "'Don't show this in the future', then stop FL and try again.",
    )


def _run_job(session: FLSession, spec: dict[str, object], seconds: float, what: str) -> dict[str, object]:
    """Start one controller job and wait for its end; a failure raises with FL's reason."""
    try:
        started = _live_write(session, "ctl.record", [spec])
    except errors.ToolError as exc:
        _cancel(session)  # an answer that comes late would leave a job running
        raise WriteRejected(f"FL Studio did not start {what}: {exc.message}", fix=exc.fix) from exc
    job = started.get("job") if isinstance(started, dict) else None
    deadline = session._monotonic() + seconds + JOB_OVERHEAD_SECONDS
    status: object = None
    while session._monotonic() < deadline:
        status = _live_read(session, "ctl.job", [])
        if isinstance(status, dict) and status.get("id") == job and status.get("phase") in ("done", "failed"):
            if status["phase"] == "failed":
                raise WriteRejected(
                    f"FL Studio stopped {what}: {status.get('error')}.",
                    fix="FL is stopped and not recording; try again.",
                )
            return status
        session._sleep(JOB_POLL_SECONDS)
    _cancel(session)
    raise WriteRejected(
        f"FL Studio did not finish {what} in {seconds + JOB_OVERHEAD_SECONDS:.0f} s.",
        fix="FL may be showing a question or a dialog; ask the user to close it, then try again.",
        retryable=False,
    )


def _cancel(session: FLSession) -> None:
    """Ask the controller to stop a job the engine no longer waits for, and wait until it
    has left FL stopped and not recording; a controller that does not answer is logged."""
    try:
        if _live_write(session, "ctl.cancel", []) is None:
            return
        deadline = session._monotonic() + JOB_CANCEL_SECONDS
        while session._monotonic() < deadline:
            status = _live_read(session, "ctl.job", [])
            if not isinstance(status, dict) or status.get("phase") in ("done", "failed"):
                return
            session._sleep(JOB_POLL_SECONDS)
    except errors.ToolError as exc:
        log.warning("could not stop the recording job: %s", exc.message)


def _seconds(beats: float, session: FLSession) -> float:
    tempo = session.tempo() or 120.0
    return beats * 60.0 / tempo


def _active_tracks(session: FLSession, start: int, ppq: int) -> set[int]:
    """The playlist tracks FL reports playing a clip at ``start``: FL answers only while it
    plays, and only for clips of notes (live)."""
    spec = {"mode": "play", "start": start + 2, "length": max(1, ppq // 2), "seconds": 0.5, "tracks": True}
    status = _run_job(session, spec, 0.5, "reading the playlist")
    return set(status.get("tracks") or [])


def place(session: FLSession, number: int, bars: list[int]) -> list[PlacedClip]:
    """Add a clip of pattern ``number`` at each of ``bars``; FL picks each clip's track.

    FL adds a pattern's clip while recording only where no clip of that pattern starts at
    or before the recording (live: 11 of 11 added, 4 of 4 after an earlier clip not), so a pattern's clips go
    in from the last bar back, and a bar after one of its existing clips is refused."""
    _require_record_question_off(session)
    info = pattern_info(session, number)
    first = session.first_clip_beat(number)
    for bar in bars:
        if first is not None and _bar_beats(bar) >= first:
            raise errors.ToolError(
                "invalid_argument",
                f"Pattern {number} ({info.name}) already has a clip at bar {_bar_of(first)}; FL adds a "
                f"pattern's clip only before its first one, so it cannot add one at bar {bar}.",
                f"Place that bar from a copy: fl.new_pattern('{info.name} 2', copy_of={number}).",
            )
    ppq = _live_int(session, "general.getRecPPQ")
    placed: list[PlacedClip] = []
    for bar in sorted(set(bars), reverse=True):
        start = round(_bar_beats(bar) * ppq)
        before = _active_tracks(session, start, ppq)
        spec = {
            "mode": "record", "pattern": number, "start": start, "length": PLACE_BEATS * ppq,
            "seconds": _seconds(PLACE_BEATS, session), "target": {"kind": "bridge"}, "marker": info.name,
        }
        status = _run_job(session, spec, spec["seconds"], f"placing {info.name} at bar {bar}")
        if not status.get("events"):
            raise WriteRejected(
                f"FL Studio recorded nothing at bar {bar}, so it added no clip of {info.name} there.",
                fix="Place it again.",
            )
        new = sorted(_active_tracks(session, start, ppq) - before)
        clip = PlacedClip(
            pattern=number, name=info.name, bar=bar, beats=info.beats, track=new[0] if len(new) == 1 else None
        )
        session.record_clip(clip)
        placed.append(clip)
    return sorted(placed, key=lambda c: c.bar)


def _sampled_error(
    rows: list[list[float]], start: int, length: int, ppq: int, points: list[AutomationPoint], gaps: list[int]
) -> float:
    """How far FL's playback is from the line: each value against the line's range within one
    recording step of where it was read. ``gaps`` are the widest steps (ticks) the recording
    took in each of equal spans of the take; a read uses its span's and its neighbours'."""
    worst = 0.0
    for tick, value in rows[SKIP_FIRST_TICKS:]:
        beat = (tick - start) / ppq
        step = _local_gap(gaps, tick - start, length) / ppq
        low, high = _line_range(points, beat - step, beat + step)
        worst = max(worst, low - value, value - high)
    return worst


def _local_gap(gaps: list[int], offset: int, length: int) -> int:
    """The recording step around ``offset``: its span's and its neighbours'. A span no tick
    ended in (spans shorter than a step) holds the step that ended next, after it."""
    if not gaps:
        return 0
    span = min(len(gaps) - 1, max(0, offset * len(gaps) // max(1, length)))
    after = next((g for g in gaps[span + 1 :] if g), 0)
    before = next((g for g in reversed(gaps[:span]) if g), 0)
    return max(gaps[span], after, before)


def _line_range(points: list[AutomationPoint], first: float, last: float) -> tuple[float, float]:
    """The lowest and highest value of the line between two beats."""
    values = [_value_at(points, first), _value_at(points, last)]
    values += [p.value for p in points if first < p.position < last]
    return min(values), max(values)


def _value_at(points: list[AutomationPoint], beat: float) -> float:
    if beat <= points[0].position:
        return points[0].value
    for a, b in itertools.pairwise(points):
        if beat <= b.position:
            return a.value + (b.value - a.value) * (beat - a.position) / max(1e-9, b.position - a.position)
    return points[-1].value


def _thin(rows: list[list[float]], start: int, ppq: int) -> list[AutomationPoint]:
    step = max(1, (len(rows) - 1) // (REPORTED_POINTS - 1)) if rows else 1
    kept = rows[::step]
    if rows and kept[-1] is not rows[-1]:
        kept.append(rows[-1])
    return [
        AutomationPoint(position=round((t - start) / ppq, 3), value=round(min(1.0, max(0.0, v)), 4)) for t, v in kept
    ]


def automate(
    session: FLSession,
    target: ControlTarget,
    label: str,
    points: list[AutomationPoint],
    bar: int,
    pattern: int | None,
) -> AutomationTake:
    """Record ``points`` (beats from ``bar``, 0..1) into a pattern at ``bar`` and play them back.

    FL keeps recorded moves in the pattern, and a later recording over the same span of the
    pattern replaces them there (live). The take reads FL's own playback of the control."""
    _require_record_question_off(session)
    beats = points[-1].position
    ppq = _live_int(session, "general.getRecPPQ")
    start = round(_bar_beats(bar) * ppq)
    if pattern is None:
        number = new_pattern(session, label).number
    else:
        number = pattern
        first = session.first_clip_beat(number)
        if first is not None and first != _bar_beats(bar):
            raise errors.ToolError(
                "invalid_argument",
                f"Pattern {number} starts at bar {_bar_of(first)}, not bar {bar}.",
                "Automate it where its first clip starts, or leave out pattern to record into a new one.",
            )
    seconds = _seconds(beats, session)
    spec = {
        "mode": "record", "pattern": number, "start": start, "length": round(beats * ppq), "seconds": seconds,
        "target": target, "points": [[round(p.position * ppq), p.value] for p in points],
        "marker": label,
    }
    recorded = _run_job(session, spec, seconds, f"recording the automation of {label}")
    if not recorded.get("events"):
        raise WriteRejected(f"FL Studio recorded none of the automation of {label}.", fix="Automate it again.")
    if session.first_clip_beat(number) is None:
        info = pattern_info(session, number)
        session.record_clip(PlacedClip(pattern=number, name=info.name, bar=bar, beats=info.beats, track=None))
    played = _run_job(
        session,
        {"mode": "play", "start": start, "length": round(beats * ppq), "seconds": seconds, "target": target},
        seconds,
        f"playing back the automation of {label}",
    )
    rows = [list(r) for r in played.get("rows") or []]
    if len(rows) <= SKIP_FIRST_TICKS:
        raise WriteRejected(f"FL Studio played none of the automation of {label} back.", fix="Automate it again.")
    gaps = [int(g) for g in recorded.get("gaps") or []]
    worst = _sampled_error(rows, start, round(beats * ppq), ppq, points, gaps)
    if worst > AUTOMATION_TOLERANCE:
        raise WriteRejected(
            f"FL Studio plays the automation of {label} back up to {worst:.2f} away from the values asked for.",
            fix="Check that Automation is ticked in FL's recording filter (right-click Record), then automate again.",
        )
    return AutomationTake(
        target=label,
        pattern=pattern_info(session, number),
        bar=bar,
        beats=beats,
        points=_thin(rows, start, ppq),
        max_error=round(worst, 4),
    )


def automation_target(
    session: FLSession,
    *,
    channel: int | None,
    mixer_track: int | None,
    slot: int | None,
    param: int | str,
) -> tuple[ControlTarget, str]:
    """The controller's description of a control and a label for it, checked against FL."""
    call = "fl.automate"
    if (channel is None) == (mixer_track is None):
        raise errors.invalid_argument(call, "pass exactly one of channel and mixer_track")
    if channel is not None:
        name = session.track_name(channel)
        if slot is not None:
            raise errors.invalid_argument(call, "slot is for a mixer track's effect; leave it out for a channel")
        if param in ("volume", "pan"):
            return {"kind": "channel", "channel": channel, "param": param}, f"{name} {param}"
        index, pname = _plugin_param(session, call, channel, -1, param, name)
        return {"kind": "channel", "channel": channel, "param": index}, f"{name} {pname}"
    assert mixer_track is not None
    count = _live_int(session, "mixer.trackCount")
    if not 0 <= mixer_track < count:
        raise errors.invalid_argument(call, f"mixer track {mixer_track} does not exist; FL has 0 to {count - 1}")
    where = "master" if mixer_track == 0 else f"mixer track {mixer_track}"
    if slot is None:
        if param not in ("volume", "pan"):
            raise errors.invalid_argument(call, 'a mixer track automates "volume" or "pan"; pass slot for an effect')
        return {"kind": "mixer", "track": mixer_track, "param": param}, f"{where} {param}"
    if not _live_read(session, "mixer.isTrackPluginValid", [mixer_track, slot]):
        raise errors.invalid_argument(call, f"{where} has no effect in slot {slot}")
    plugin = str(_live_read(session, "plugins.getPluginName", [mixer_track, slot, False]))
    index, pname = _plugin_param(session, call, mixer_track, slot, param, f"{plugin} on {where}")
    return {"kind": "effect", "track": mixer_track, "slot": slot, "param": index}, f"{plugin} {pname} on {where}"


def _plugin_param(
    session: FLSession, call: str, index: int, slot: int, param: int | str, owner: str
) -> tuple[int, str]:
    """A plugin parameter by index, exact name or unique part of its name (any case)."""
    try:
        count = int(_live_read(session, "plugins.getParamCount", [index, slot]) or 0)
    except errors.ToolError as exc:
        raise errors.invalid_argument(call, f"{owner} has no plugin parameters ({exc.message})") from exc
    if isinstance(param, int):
        if not 0 <= param < count:
            raise errors.invalid_argument(call, f"{owner} has parameters 0 to {count - 1}, not {param}")
        return param, str(_live_read(session, "plugins.getParamName", [param, index, slot]))
    names = [str(_live_read(session, "plugins.getParamName", [i, index, slot])) for i in range(count)]
    exact = [i for i, n in enumerate(names) if n.lower() == param.lower()]
    partial = [i for i, n in enumerate(names) if param.lower() in n.lower()]
    chosen = exact if len(exact) == 1 else partial
    if len(chosen) == 1:
        return chosen[0], names[chosen[0]]
    shown = [names[i] for i in partial][:12] or [n for n in names if n][:12]
    raise errors.unknown_target("parameter", param, shown)

