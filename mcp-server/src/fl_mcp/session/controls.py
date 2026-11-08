"""Live control ops: tempo, channel levels, plugin parameters.

Every result is a READBACK; a mid-call failure raises with what already
landed - it never falls through to the file path (the v1 cascade bug).
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fl_mcp import audio, errors, flp
from fl_mcp.contract import ENV_AGENT_PROJECT, ENV_SPOOL_ROOT, FLLicense
from fl_mcp.core.carriers import CarrierUnavailable
from fl_mcp.errors import NotePlaneUnavailable, WriteRejected
from fl_mcp.schemas import (
    CAPTURE_MAX_SECONDS,
    AutomationPoint,
    AutomationResult,
    ChannelLevels,
    Note,
    PluginParam,
    PluginParamModel,
    PluginParamSpec,
    RenderReceipt,
    RenderSpan,
    SessionState,
    SpoolResponse,
    StemsReceipt,
)

from . import playback
from .audition import Rack, audition
from .link import LIVE_READ_TIMEOUT_SECONDS, LIVE_SET_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .facade import FLSession

# Plugin-parameter surface: FL quantizes values to each control's own grid
# (E2: set 0.42 -> stored 0.41875), and big synths are big - bound the listing.
PLUGIN_PARAMS_DEFAULT_LIMIT = 40
PLUGIN_PARAM_TOLERANCE = 0.02
RENDER_TIMEOUT_SECONDS = 300.0
# A render's capture, this decay tail included, stops at CAPTURE_MAX_SECONDS
# because it holds the tool call for its whole length.
RENDER_TAIL_SECONDS = 0.5

# FL's MIDI-scripting vocabulary for the live span (IL FL-Studio-API-Stubs).
# Live-measured on FL 2026: getSongLength(ABSTICKS) is the
# playlist's LAST tick (1535 for a 1536-tick clip; 0 for an empty playlist) in
# either loop mode; getPatternLength counts 16th steps, not the beats the stubs
# document (32 beats of notes -> 128); getSongLength(MS) keeps its old value after
# a tempo change until the next edit, so seconds come from ticks and live tempo.
FL_LOOP_MODE_PATTERN = 0
FL_LOOP_MODE_SONG = 1
FL_SONGLENGTH_ABSTICKS = 2
FL_STEPS_PER_BEAT = 4
# The template is 4/4 and the note API speaks beats, so a bar is four beats.
BEATS_PER_BAR = 4

log = logging.getLogger(__name__)

# FL's plugins.* convention for "the channel's own generator, not a mixer
# effect slot" (the slot argument every call here passes).
GENERATOR_SLOT = -1
# FL's PIM_None: a set applies the value at once, with no controller pickup.
PICKUP_NONE = 0
# getChannelPitch / setChannelPitch's mode for whole semitones.
PITCH_SEMITONES = 1
_NUMBER = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*$")


def save_project(session: FLSession) -> None:
    """Save the live project to its .flp via FL's FPT_Save transport command.

    Live-verified 2026-09-02: live tempo 145 in RAM then save_project made the
    on-disk .flp reparse to 145 (was 130). FL's globalTransport return for
    FPT_Save is a non-flag internal int that varies per call, so it is NOT
    surfaced - success is by-effect; a refusal raises below. Never under a
    trial license (errors.fl_trial_unsupported).
    """
    if session.fl_license is FLLicense.TRIAL:
        raise errors.fl_trial_unsupported("saving")
    if session._ensure_hello() is None or session.control is None:
        # A failed liveness probe does not shrink the save's documented
        # budget.  In session mode that probe crosses the broker first; using
        # 0 here made the resulting ctl.save error claim an expired request
        # even though no save request had received a zero-second timeout.
        raise session.not_answering("ctl.save", LIVE_SET_TIMEOUT_SECONDS, "nothing was saved")
    session.link.command("ctl.save")


def _render_saved_project(session: FLSession, project: Path) -> Path:
    """Run FL's blocking macOS project renderer.

    Live-verified (#169): FL's bundle executable accepts ``-R <project.flp>``
    and writes ``<project>.wav`` beside it. Keep this process boundary small so
    its command spelling is straightforward to verify and adjust live.
    """
    command = list(session.platform.render_command(project))
    try:
        subprocess.run(
            command,
            check=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            env=_render_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise errors.ToolError(
            "render_failed",
            f"FL CLI could not render {project}: {exc}",
            "Check the FL CLI render invocation and retry.",
            retryable=True,
        ) from exc
    return project.with_suffix(".wav")


def _render_env() -> dict[str, str]:
    """The render child must never join the live session.

    FL's ``-R`` render is a second FL process that loads the same project, so it
    loads the Bridge too. Inheriting this session's ``FLMCP_SPOOL_ROOT`` made
    that render-only FL publish ITS pid into the live session's port file, then
    exit: the broker addressed a dead pid ("No response to ctl.save"), and the
    arena's liveness watcher declared the producer's FL lost (live run 9,
    2026-09-04). The render gets a throwaway spool root and no agent project.
    """
    env = os.environ.copy()
    env.pop(ENV_AGENT_PROJECT, None)
    env[ENV_SPOOL_ROOT] = tempfile.mkdtemp(prefix="flmcp-render-spool-")
    return env


def _live_read(session: FLSession, op: str, args: list) -> object:
    """One live controller read: FL's answer, or a typed error naming why not."""
    if session._ensure_hello() is None or session.control is None:
        raise session.not_answering(op, LIVE_READ_TIMEOUT_SECONDS, "nothing could be read")
    return session.link.call(op, args, timeout=LIVE_READ_TIMEOUT_SECONDS)


def _live_write(session: FLSession, op: str, args: list) -> object:
    """One live controller write: FL's answer, or a typed error naming why not.
    Never a file fallback: the caller reads the change back from FL."""
    if session._ensure_hello() is None or session.control is None:
        raise session.not_answering(op, LIVE_SET_TIMEOUT_SECONDS, "nothing was changed")
    return session.link.call(op, args, timeout=LIVE_SET_TIMEOUT_SECONDS)


def _live_int(session: FLSession, op: str, args: list | None = None) -> int:
    value = _live_read(session, op, args or [])
    if isinstance(value, bool) or not isinstance(value, int):
        raise errors.fl_api_error(op, f"returned {value!r}, not an integer")
    return value


def live_arrangement_span(session: FLSession) -> RenderSpan:
    """How long FL's live arrangement plays from its start, in its current loop mode.

    THE answer to "how long is the arrangement": song mode reads the playlist's
    end, pattern mode the selected pattern's length, both at FL's live tempo.
    The saved .flp is never consulted: a trial FL never saves.
    """
    loop_mode = _live_int(session, "transport.getLoopMode")
    tempo_bpm = _live_int(session, "mixer.getCurrentTempo") / 1000.0
    pattern: int | None = None
    source: Literal["song", "pattern"]
    if loop_mode == FL_LOOP_MODE_SONG:
        source = "song"
        ppq = _live_int(session, "general.getRecPPQ")
        if ppq <= 0:
            raise errors.fl_api_error("general.getRecPPQ", f"returned {ppq} ticks per beat")
        last_tick = _live_int(session, "transport.getSongLength", [FL_SONGLENGTH_ABSTICKS])
        if last_tick <= 0:
            raise errors.ToolError(
                "render_failed",
                "FL is in song mode and its playlist is empty, so playing it records only silence.",
                "Place a pattern on the playlist, or switch FL to pattern mode, then render again.",
            )
        beats = (last_tick + 1) / ppq
    elif loop_mode == FL_LOOP_MODE_PATTERN:
        source = "pattern"
        pattern = _live_int(session, "patterns.patternNumber")
        beats = _live_int(session, "patterns.getPatternLength", [pattern]) / FL_STEPS_PER_BEAT
    else:
        raise errors.fl_api_error("transport.getLoopMode", f"returned unknown loop mode {loop_mode}")

    return RenderSpan(
        source=source,
        pattern=pattern,
        beats=beats,
        tempo_bpm=tempo_bpm,
        seconds=beats * 60.0 / tempo_bpm,
    )


def bar_range(span: RenderSpan, start_bar: int, bars: int | None) -> RenderSpan:
    """The part of a live span from ``start_bar`` for ``bars`` bars (to its end when None).

    A range past the end is cut at the end, so the span says what actually plays; a
    start past the end is refused. Bars are four beats (BEATS_PER_BAR).
    """
    if isinstance(start_bar, bool) or not isinstance(start_bar, int) or start_bar < 1:
        raise errors.invalid_argument("fl.render", f"start_bar must be a whole number from 1, got {start_bar!r}")
    if bars is not None and (isinstance(bars, bool) or not isinstance(bars, int) or bars < 1):
        raise errors.invalid_argument("fl.render", f"bars must be a whole number from 1, or None, got {bars!r}")
    start = (start_bar - 1) * BEATS_PER_BAR
    if start >= span.beats:
        total = max(1, math.ceil(span.beats / BEATS_PER_BAR - 1e-9))
        what = "song" if span.source == "song" else f"pattern {span.pattern}"
        raise errors.invalid_argument(
            "fl.render", f"the {what} is {total} bar{'s' if total != 1 else ''} long; start at bar 1 to {total}"
        )
    end = span.beats if bars is None else min(span.beats, start + bars * BEATS_PER_BAR)
    beats = end - start
    return span.model_copy(update={"start_bar": start_bar, "beats": beats, "seconds": beats * 60.0 / span.tempo_bpm})


def playing_notes(
    session: FLSession, state: SessionState, start: float, end: float, tool: str
) -> dict[int, bool] | None:
    """Per channel: does it have notes (or an audio clip) in the beats [start, end)
    about to play? None when unknown. Raises when the span starts past the end.

    Pattern mode loops the selected pattern; song mode plays the playlist. The
    playlist and the notes nobody wrote this session come from the song FL
    opened (the SDK cannot change the playlist), so without that song the
    answer is unknown, never assumed. ``tool`` names the caller in a refusal.
    """
    loop_mode = _live_int(session, "transport.getLoopMode")
    if loop_mode == FL_LOOP_MODE_PATTERN:
        pattern = state.current_pattern
        if pattern is None:
            return None
        length = max(_live_int(session, "patterns.getPatternLength", [pattern]), 1) / FL_STEPS_PER_BEAT
        if start >= length:
            bars = math.ceil(length / BEATS_PER_BAR)
            raise errors.invalid_argument(
                tool, f"pattern {pattern} is {bars} bar{'s' if bars != 1 else ''} long; start at bar 1 to {bars}"
            )
        if session.project is None or not session.project.is_file():
            return None
        notes = session.all_notes()
        return {
            c.index: any(overlaps_looped(n, start, end, length) for n in notes.get((pattern, c.index), []))
            for c in state.channels
        }
    if loop_mode != FL_LOOP_MODE_SONG:
        raise errors.fl_api_error("transport.getLoopMode", f"returned unknown loop mode {loop_mode}")
    last_tick = _live_int(session, "transport.getSongLength", [FL_SONGLENGTH_ABSTICKS])
    if last_tick <= 0:
        raise errors.ToolError(
            "invalid_argument",
            "FL is in song mode and its playlist is empty, so nothing would play.",
            f"Place a pattern on the playlist, or switch FL to pattern mode, then {tool} again.",
        )
    ppq = state.ppq or 1
    if start * ppq > last_tick:
        bars = math.ceil((last_tick + 1) / ppq / BEATS_PER_BAR)
        raise errors.invalid_argument(tool, f"the song is {bars} bars long; start at bar 1 to {bars}")
    if session.project is None or not session.project.is_file():
        return None
    project = flp.read(session.project)
    notes = session.all_notes()
    clips = [
        clip
        for clip in project.playlist
        if clip.position / project.ppq < end and (clip.position + clip.length) / project.ppq > start
    ]
    patterns = {clip.pattern for clip in clips if clip.pattern is not None}
    audio_clips = {clip.channel for clip in clips if clip.channel is not None}
    return {
        c.index: c.index in audio_clips or any(notes.get((p, c.index)) for p in patterns) for c in state.channels
    }


def overlaps_looped(note: Note, start: float, end: float, length: float) -> bool:
    """Whether a note in a pattern of ``length`` beats, looping, sounds in [start, end)."""
    first = math.floor((start - note.start - note.length) / length)
    return any(
        note.start + k * length < end and note.start + note.length + k * length > start
        for k in range(first, math.ceil((end - note.start) / length) + 1)
    )


def _check_capture_length(span: RenderSpan) -> None:
    if span.seconds + RENDER_TAIL_SECONDS > CAPTURE_MAX_SECONDS:
        raise errors.ToolError(
            "render_failed",
            f"The live {span.source} is {span.beats:g} beats at {span.tempo_bpm:g} BPM ({span.seconds:.1f} s); "
            f"a render records at most {CAPTURE_MAX_SECONDS:g} s including its "
            f"{RENDER_TAIL_SECONDS:g} s tail.",
            "Render fewer bars (bars=, start_bar=), shorten the arrangement or raise the tempo.",
        )


def _capture_start(session: FLSession, seconds: float, timeout: float) -> SpoolResponse:
    """Arm the Bridge's capture, first stopping whatever capture it still has armed.

    The Bridge holds one capture and refuses a start while one is armed; only a
    capture_stop disarms it. A start it acknowledged after this side gave up, or
    a server that died mid-render, left it armed with nobody to stop it, and every
    later render failed "could not start recording" until FL was restarted (box
    take base-phonk-opus, 2026-11-02: ten renders over seven minutes). Renders are
    single-flight per FL, so whatever is armed at start time is a leftover: one
    rule, a stop before every start, clears it (with nothing armed the Bridge
    answers "no active capture" and nothing changes).
    """
    with session._transport_use():
        try:
            stopped = session.transport.send("capture_stop")
            session.transport.await_response(stopped, timeout=timeout)
        except (errors.ToolError, CarrierUnavailable) as exc:
            log.warning("could not clear the Bridge's capture before a render: %s", exc)
        started = session.transport.send("capture_start", seconds=seconds)
        return session.transport.await_response(started, timeout=timeout)


def _live_render(
    session: FLSession, output: Path, seconds: float, *, start_tick: int = 0, refuse_silence: bool = True
) -> Path:
    """Play the live arrangement from ``start_tick`` while the master Bridge records it.

    The Bridge starts recording when FL starts playing, wherever it plays from. A
    silent recording of the whole mix means FL's audio path is broken, so it is
    refused; a range or a soloed render may be silent and is returned as it is.
    """
    response_timeout = min(10.0, max(2.0, seconds / 2.0 + 2.0))
    playback.confirm_answering(session, "render_failed", "the render")
    start = _capture_start(session, seconds, response_timeout)
    if not start.ok:
        log.warning("the Bridge refused capture_start: %s", start.err)
        raise errors.ToolError(
            "render_failed",
            "FL Studio could not start recording.",
            "Retry the render in a moment.",
            retryable=True,
        )
    stop_error: errors.ToolError | None = None
    try:
        # Position is a controller operation, so it remains on the control wire
        # rather than reaching from the Bridge into FL on a mixer callback.
        # The song's start in FL's default mode (a fraction of the song), as every render
        # until 2026-11-03; any other bar in absolute ticks, as fl.check positions.
        position = [0] if start_tick == 0 else [start_tick, FL_SONGLENGTH_ABSTICKS]
        positioned = session.link.request("transport.setSongPos", position, timeout=LIVE_SET_TIMEOUT_SECONDS)
        if not positioned.ok:
            raise errors.ToolError(
                "render_failed",
                positioned.err or "FL refused loop start",
                "Wait for FL to settle, then retry.",
                retryable=True,
            )
        session.link.command("transport.start")
        # The Bridge ends recording at its requested frame count. This is only
        # the bounded transport wait before we ask it to finalize the WAV.
        time.sleep(seconds + 1.0)
    finally:
        try:
            stop_error = playback.stop(session)
        finally:
            with session._transport_use():
                stopped_cmd = session.transport.send("capture_stop")
                stopped = session.transport.await_response(stopped_cmd, timeout=response_timeout)

    stop_detail = (
        f"; transport.stop was unacknowledged: {stop_error.message}"
        if stop_error is not None and stop_error.category == "control_plane_unavailable"
        else f"; transport.stop failed: {stop_error.message}"
        if stop_error is not None
        else ""
    )
    if not stopped.ok or not stopped.path or stopped.frames_written is None:
        log.warning("Bridge capture_stop failed: %s%s", stopped.err, stop_detail)
        raise errors.ToolError(
            "render_failed",
            "The render recorded no audio.",
            "FL's audio output may be unavailable. Retry the render.",
            retryable=True,
        )
    captured = Path(stopped.path)
    if not captured.is_file() or captured.stat().st_size <= 44:
        log.warning("Bridge capture WAV %s is missing or empty%s", captured, stop_detail)
        raise errors.ToolError(
            "render_failed",
            "The render's recording is missing or empty.",
            "Retry the render.",
            retryable=True,
        )
    expected_frames = start.frames_requested
    if (
        start.sample_rate is None
        or expected_frames is None
        or stopped.sample_rate != start.sample_rate
        or stopped.drop_count
        or abs(stopped.frames_written - expected_frames) > expected_frames * 0.01
    ):
        log.warning(
            "Bridge capture did not verify: requested=%s written=%s rate=%s drops=%s%s",
            expected_frames, stopped.frames_written, stopped.sample_rate, stopped.drop_count, stop_detail,
        )
        raise errors.ToolError(
            "render_failed",
            "The render's recording was incomplete (audio dropped out while recording).",
            "Retry the render.",
            retryable=True,
        )
    try:
        frames, rate = audio.wav_format(captured)
        lufs = audio.integrated_lufs(captured)
    except (OSError, ValueError) as exc:
        log.warning("Bridge WAV %s could not be read: %s%s", captured, exc, stop_detail)
        raise errors.ToolError(
            "render_failed",
            f"The render's recording could not be read: {exc}.",
            "Retry the render.",
            retryable=True,
        ) from exc
    if (
        rate != start.sample_rate
        or abs(frames - expected_frames) > expected_frames * 0.01
        or (refuse_silence and lufs <= -60)
    ):
        log.warning(
            "Bridge WAV did not verify: frames=%s/%s rate=%s/%s lufs=%.1f%s",
            frames, expected_frames, rate, start.sample_rate, lufs, stop_detail,
        )
        silent = refuse_silence and lufs <= -60
        raise errors.ToolError(
            "render_failed",
            "The render recorded silence." if silent else "The render's recording was incomplete.",
            "Check that the song has notes on channels that make sound, then render again."
            if silent
            else "Retry the render.",
            retryable=True,
        )
    if stop_error is not None:
        log.warning(
            "transport.stop was unacknowledged, also once FL was given time to answer; FL may still "
            "be playing; the next transport command re-seats it: %s",
            stop_error.message,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(captured, output)
    return output


def _prepare_render(session: FLSession, start_bar: int, bars: int | None) -> tuple[RenderSpan, RenderSpan, int]:
    """The live span, the part of it to record, and the absolute tick it starts at."""
    if not session.caps.bridge_live:
        log.warning("render refused: the Master-insert Bridge is not live (check bridge_heartbeat.txt)")
        raise errors.fl_not_prepared("render_failed", "nothing was recorded")
    whole = live_arrangement_span(session)
    span = bar_range(whole, start_bar, bars)
    start_tick = 0
    if start_bar > 1:
        ppq = _live_int(session, "general.getRecPPQ")
        if ppq <= 0:
            raise errors.fl_api_error("general.getRecPPQ", f"returned {ppq} ticks per beat")
        start_tick = (start_bar - 1) * BEATS_PER_BAR * ppq
    return whole, span, start_tick


def render_wav(
    session: FLSession,
    output: Path,
    *,
    start_bar: int = 1,
    bars: int | None = None,
    solo: Sequence[int] = (),
    mute: Sequence[int] = (),
) -> RenderReceipt:
    """Play the live arrangement once, or ``bars`` bars of it from ``start_bar``, and
    record the live mix; with ``solo``/``mute``, only those channels sound (audition).

    The length is FL's own, read live just before the capture
    (live_arrangement_span); nothing is saved first, so a trial FL renders
    exactly like a licensed one. The Bridge and FL audio output device are
    required: it fails loudly when that live path is unavailable rather than
    masking it with an offline export.
    """
    whole, span, start_tick = _prepare_render(session, start_bar, bars)
    _check_capture_length(span)
    everything = span.beats == whole.beats and not solo and not mute
    with audition(session, solo, mute):
        path = _live_render(
            session,
            output.expanduser().resolve(),
            span.seconds + RENDER_TAIL_SECONDS,
            start_tick=start_tick,
            refuse_silence=everything,
        )
    return RenderReceipt(path=path, span=span, solo=sorted(solo), mute=sorted(mute))


def render_stems(
    session: FLSession,
    output: Path,
    by: str,
    *,
    start_bar: int = 1,
    bars: int | None = None,
    channels: Sequence[int] = (),
) -> StemsReceipt:
    """One render per channel (``by="channels"``) or per mixer track, soloing the
    channels routed to it (``by="mixer"``): the Bridge records only the master, so
    each stem is its own play. Files are named ``<output stem>-<label>.wav`` beside
    ``output``.

    Only channels with notes (or an audio clip) in the bars played are recorded, and
    only the mixer tracks they feed: a silent stem costs a full play (box run
    2026-11-03: 20 of 23 stems were empty and took most of 6.5 minutes). The rest
    are reported as ``skipped``. When the session cannot tell (no copy of the song
    FL opened), every channel is recorded. ``channels`` names the channels to record
    instead, each alone, notes or not.

    A stem may still be silent, so none is refused for silence; the whole set is
    bounded like one render, at CAPTURE_MAX_SECONDS of recording.
    """
    if by not in ("channels", "mixer"):
        raise errors.invalid_argument("fl.render", f'stems must be "channels" or "mixer", got {by!r}')
    if channels and by != "channels":
        raise errors.invalid_argument("fl.render", "a list of stems names channels; it cannot be mixer stems")
    state = session.snapshot()
    rack = {c.index: c for c in state.channels}
    if not rack:
        raise errors.invalid_argument("fl.render", "FL has no channels to record stems of")
    missing = [c for c in channels if c not in rack]
    if missing:
        raise errors.invalid_argument("fl.render", f"no channel at index {', '.join(map(str, missing))}")
    _, span, start_tick = _prepare_render(session, start_bar, bars)
    if channels:
        heard, skipped = list(dict.fromkeys(channels)), []
    else:
        start = (span.start_bar - 1) * BEATS_PER_BAR
        playing = playing_notes(session, state, start, start + span.beats, "fl.render")
        heard = [i for i in rack if playing is None or playing.get(i)]
        skipped = [i for i in rack if i not in heard]
    if by == "channels":
        groups = [(f"{i}-{rack[i].name}", (i,)) for i in heard]
    else:
        names = {t.index: t.name for t in state.mixer}
        tracks = sorted({rack[i].mixer_track for i in heard if rack[i].mixer_track is not None})
        groups = [
            (f"mixer{t}-{names.get(t, '')}", tuple(i for i in heard if rack[i].mixer_track == t)) for t in tracks
        ]
    if not groups:
        last = span.start_bar + max(1, math.ceil(span.beats / BEATS_PER_BAR - 1e-9)) - 1
        where = f"bar {last}" if last == span.start_bar else f"bars {span.start_bar}-{last}"
        raise errors.ToolError(
            "invalid_argument",
            f"No channel has notes in {where}, so there are no stems to record.",
            'Render bars that have notes, or name the channels to record: stems=["Bass", "Kick"].',
        )
    recorded = len(groups) * (span.seconds + RENDER_TAIL_SECONDS)
    if recorded > CAPTURE_MAX_SECONDS:
        raise errors.ToolError(
            "render_failed",
            f"{len(groups)} stems of {span.seconds:.1f} s each record {recorded:.0f} s; "
            f"one call records at most {CAPTURE_MAX_SECONDS:g} s.",
            "Render fewer bars (bars=, start_bar=), or name fewer channels (stems=[...]).",
        )
    receipts = []
    with Rack(session) as mutes:
        for label, solo in groups:
            mutes.hear(solo)
            path = _live_render(
                session,
                output.expanduser().resolve().with_name(f"{output.stem}-{_label(label)}.wav"),
                span.seconds + RENDER_TAIL_SECONDS,
                start_tick=start_tick,
                refuse_silence=False,
            )
            receipts.append(RenderReceipt(path=path, span=span, solo=list(solo)))
    return StemsReceipt(stems=receipts, skipped=skipped)


def _label(text: str) -> str:
    """A file-name-safe stem label: letters, digits and dashes, at most 40 characters."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "stem"


def export_offline(session: FLSession, output: Path) -> Path:
    """Boot a second FL and export with -R, for offline deliverables only."""
    if session.fl_license is FLLicense.TRIAL:
        raise errors.fl_trial_unsupported("offline export")
    if session.project is None:
        raise errors.ToolError(
            "render_failed",
            "This session has no saved song to export.",
            "Use render to record the song instead.",
        )
    output = output.expanduser().resolve()
    project = session.project.expanduser().resolve()
    save_project(session)
    rendered = _render_saved_project(session, project)
    if not rendered.is_file() or rendered.stat().st_size == 0:
        raise errors.ToolError(
            "render_failed",
            f"FL CLI did not create a non-empty WAV at {rendered}.",
            "Check FL's CLI render settings and retry; a render is successful only when its WAV exists.",
            retryable=True,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if rendered != output:
        shutil.copyfile(rendered, output)
    return output


def set_tempo(session: FLSession, bpm: float) -> float:
    """Live-first (confirmed via getCurrentTempo readback, BPM x 1000);
    else the project file (flp.set_tempo) + reload. Returns the readback."""
    if session._ensure_hello() is not None and session.control is not None:
        resp = session.link.request("mixer.setCurrentTempo", [bpm, 1], timeout=LIVE_SET_TIMEOUT_SECONDS)
        if not resp.ok:
            # Unsafe-at-current-time means busy (warm-up window after a
            # load, addendum A6) - retryable, and NEVER a fallthrough.
            if errors.is_operation_unsafe(resp.err or ""):
                raise errors.operation_unsafe("mixer.setCurrentTempo")
            raise WriteRejected(
                f"mixer.setCurrentTempo({bpm}) failed inside FL: {resp.err}",
                fix="Check the value (10-999 BPM) and retry.",
            )
        readback = session.link.request("mixer.getCurrentTempo", timeout=LIVE_READ_TIMEOUT_SECONDS)
        if not readback.ok or readback.ret is None:
            raise WriteRejected(
                f"FL Studio took the tempo but did not report it back ({readback.err}).",
                fix="Read fl.tempo, and set the tempo again if it is not what you want.",
            )
        return float(readback.ret) / 1000.0

    if session.project is not None:
        session._prepare_file_write()
        confirmed = flp.set_tempo(session.project, bpm)
        session._reload_fl()
        return confirmed

    log.warning("set_tempo: the control wire is not connected and no project file is configured")
    raise NotePlaneUnavailable("FL Studio is not accepting a tempo change right now.")


def set_channel_levels(
    session: FLSession,
    channel: int,
    *,
    volume: float | None = None,
    pan: float | None = None,
    pitch_semitones: int | None = None,
) -> ChannelLevels:
    """All-or-nothing per call: a field that landed is never silently
    discarded by a fallback (the v1 partial-then-reload bug)."""
    if volume is None and pan is None and pitch_semitones is None:
        raise ValueError("nothing to change - pass volume, pan and/or pitch_semitones")

    if session._ensure_hello() is not None and session.control is not None:
        # ONE backend for the whole call. A mid-call failure raises with
        # what already landed - it never falls through to the file path,
        # which would reload FL and discard the fields that landed live.
        requested = [
            ("volume", "channels.setChannelVolume", [channel, volume, PICKUP_NONE]),
            ("pan", "channels.setChannelPan", [channel, pan, PICKUP_NONE]),
            ("pitch_semitones", "channels.setChannelPitch", [channel, pitch_semitones, PITCH_SEMITONES, PICKUP_NONE]),
        ]
        applied: list[str] = []
        for field_name, op, args in requested:
            if args[1] is None:
                continue
            resp = session.link.request(op, args, timeout=LIVE_SET_TIMEOUT_SECONDS)
            if not resp.ok:
                if errors.is_operation_unsafe(resp.err or ""):
                    raise errors.operation_unsafe(op)  # busy, not broken (A6)
                raise WriteRejected(
                    f"{op} failed inside FL: {resp.err}. "
                    f"Already applied live and KEPT: {', '.join(applied) or 'nothing'}.",
                    fix="Retry the failed field; the fields that landed were not rolled back.",
                )
            applied.append(field_name)

        readbacks = {}
        for field_name, op, args in (
            ("volume", "channels.getChannelVolume", [channel, False]),  # 0..1, not dB
            ("pan", "channels.getChannelPan", [channel]),
            ("pitch_semitones", "channels.getChannelPitch", [channel, PITCH_SEMITONES]),
        ):
            resp = session.link.request(op, args, timeout=LIVE_READ_TIMEOUT_SECONDS)
            if not resp.ok or resp.ret is None:
                raise WriteRejected(
                    f"FL Studio took the levels but did not report {op} back ({resp.err}).",
                    fix="Set the levels again to be sure they are what you want.",
                )
            readbacks[field_name] = resp.ret
        return ChannelLevels(
            channel=channel,
            volume=float(readbacks["volume"]),
            pan=float(readbacks["pan"]),
            pitch_semitones=round(float(readbacks["pitch_semitones"])),
        )

    if session.project is not None:
        session._prepare_file_write()
        confirmed = flp.set_channel_levels(
            session.project, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
        )
        session._reload_fl()
        return confirmed

    log.warning("set_channel_levels: the control wire is not connected and no project file is configured")
    raise NotePlaneUnavailable("FL Studio is not accepting a level change right now.")


def set_automation(session: FLSession, channel: int, points: list[AutomationPoint]) -> AutomationResult:
    """Replace an existing automation clip and return its saved-file readback.

    There is no verified live automation writer.  ``flpkit`` carries the
    channel's opaque header/trailer and verifies its own splice; this second,
    independent project parse is the session boundary's confirmed result.
    """
    if session.project is None:
        raise NotePlaneUnavailable(
            "Automation clips cannot be written in this session: there is no saved song to edit.",
            fix="Shape the sound with Track.set_param or levels instead.",
            retryable=False,
        )
    try:
        session._prepare_file_write()
        saved = flp.write_automation(session.project, channel, points)
        project = flp.read(session.project)
        confirmed = next(item.automation for item in project.channels if item.index == channel)
    except (flp.FlpError, IndexError, StopIteration) as exc:
        raise WriteRejected(
            f"The automation clip could not be written: {exc}",
            fix="Use a channel that already has an automation clip, then retry.",
        ) from exc
    if confirmed != saved:
        log.warning("automation write on channel %d read back differently on the session parse", channel)
        raise WriteRejected(
            "The automation clip did not save as written, so FL was not reopened.",
            fix="Retry the write.",
        )
    session._reload_fl()
    return AutomationResult(
        channel=channel,
        points=[AutomationPoint(position=p.position, value=p.value, tension=p.tension) for p in confirmed],
    )


# -- plugin parameters (live-only; E2-verified ops) --------------------------
#
# A generator's parameters exist only inside the running plugin - there is
# no file representation we can honestly edit (plugin state is an opaque
# vendor blob). So this surface is live-or-error, never a file fallback.


def _plugin_param_count(session: FLSession, channel: int) -> int:
    return int(_live_read(session, "plugins.getParamCount", [channel, GENERATOR_SLOT]) or 0)


def _plugin_model(session: FLSession, channel: int) -> PluginParamModel | None:
    """Select static semantics only when the session was explicitly given them."""
    if not session.plugin_param_models:
        return None
    plugin = str(_live_read(session, "plugins.getPluginName", [channel, GENERATOR_SLOT, False]))
    return next((model for model in session.plugin_param_models if model.plugin == plugin), None)


def _spec(model: PluginParamModel | None, index: int, name: str) -> PluginParamSpec | None:
    if model is None:
        return None
    return next((item for item in model.parameters if item.index == index and item.name == name), None)


def _option_index(spec: PluginParamSpec, value: float) -> int:
    assert spec.options is not None
    if len(spec.options) == 1:
        return 0
    return round(value * (len(spec.options) - 1))


def _display(value: float, spec: PluginParamSpec | None) -> str:
    if spec is None:
        return f"{value:g}"
    if spec.options is not None:
        return spec.options[_option_index(spec, value)]
    if spec.minimum is not None and spec.maximum is not None:
        shown = spec.minimum + value * (spec.maximum - spec.minimum)
        if spec.step is not None:
            shown = round((shown - spec.minimum) / spec.step) * spec.step + spec.minimum
        suffix = f" {spec.unit}" if spec.unit else ""
        return f"{shown:g}{suffix}"
    return f"{value:g}"


def _param(index: int, name: str, value: float, spec: PluginParamSpec | None) -> PluginParam:
    return PluginParam(
        index=index,
        name=name,
        value=min(max(value, 0.0), 1.0),
        display=_display(value, spec),
        kind=spec.kind if spec is not None else "continuous",
        options=list(spec.options) if spec is not None and spec.options is not None else None,
        minimum=spec.minimum if spec is not None else None,
        maximum=spec.maximum if spec is not None else None,
        step=spec.step if spec is not None else None,
        unit=spec.unit if spec is not None else None,
    )


def _resolve_value(value: float | str, spec: PluginParamSpec | None) -> float:
    if not isinstance(value, str):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError(f"plugin param value must be a finite normalized number, got {value!r}")
        normalized = float(value)
        if not isfinite(normalized):
            raise ValueError(f"plugin param value must be a finite normalized number, got {value!r}")
        if not 0.0 <= normalized <= 1.0:
            raise ValueError(f"plugin param values are normalized 0..1, got {value}")
        return normalized
    if spec is None:
        raise ValueError(
            "display or option values require a captured per-plugin parameter model; "
            "pass a normalized float instead"
        )
    if spec.options is not None:
        option = next(
            (index for index, label in enumerate(spec.options) if label.casefold() == value.casefold()),
            None,
        )
        if option is None:
            choices = ", ".join(spec.options)
            raise ValueError(f"invalid option {value!r} for {spec.name!r}; choose one of: {choices}")
        return option / max(len(spec.options) - 1, 1)
    if spec.minimum is None or spec.maximum is None:
        raise ValueError(f"{spec.name!r} has no captured display range; pass a normalized float instead")
    suffix = f" {spec.unit}" if spec.unit else ""
    text = value.removesuffix(suffix).strip() if suffix else value.strip()
    match = _NUMBER.fullmatch(text)
    if match is None:
        expected = f"number{suffix}" if suffix else "number"
        raise ValueError(f"invalid display value {value!r} for {spec.name!r}; expected {expected}")
    shown = float(match.group(1))
    if not spec.minimum <= shown <= spec.maximum:
        raise ValueError(f"display value {value!r} is outside {spec.minimum:g}..{spec.maximum:g}")
    return (shown - spec.minimum) / (spec.maximum - spec.minimum)


def plugin_params(
    session: FLSession,
    channel: int,
    *,
    query: str | None = None,
    limit: int = PLUGIN_PARAMS_DEFAULT_LIMIT,
) -> list[PluginParam]:
    """The channel generator's parameters (name + normalized value).

    ``query`` filters by case-insensitive substring; ``limit`` bounds the
    result because big synths are BIG (Sytrus: 597 params) and every name
    is a wire round trip."""
    count = _plugin_param_count(session, channel)
    model = _plugin_model(session, channel)
    out: list[PluginParam] = []
    for index in range(count):
        if len(out) >= limit:
            break
        name = str(_live_read(session, "plugins.getParamName", [index, channel, GENERATOR_SLOT]))
        if query is not None and query.lower() not in name.lower():
            continue
        value = float(_live_read(session, "plugins.getParamValue", [index, channel, GENERATOR_SLOT]))
        out.append(_param(index, name, value, _spec(model, index, name)))
    return out


def set_plugin_param(session: FLSession, channel: int, param: int | str, value: float | str) -> PluginParam:
    """Set one parameter (by index, or by unique name/substring) and return
    the READBACK. FL quantizes to the plugin's own step grid, so the
    confirmed value may differ slightly from the request - beyond
    PLUGIN_PARAM_TOLERANCE it raises instead of pretending."""
    model = _plugin_model(session, channel)
    if isinstance(param, str):
        matches = plugin_params(session, channel, query=param, limit=8)
        exact = [p for p in matches if p.name.lower() == param.lower()]
        chosen = exact or matches
        if len(chosen) != 1:
            names = ", ".join(p.name for p in matches) or "(no matches)"
            raise errors.fl_api_error(
                "plugins.setParamValue",
                f"param {param!r} is {'ambiguous' if matches else 'unknown'} on channel {channel}: {names}",
            )
        index = chosen[0].index
    else:
        if isinstance(param, bool) or not isinstance(param, int):
            raise TypeError(
                f"plugin param index must be a non-negative integer returned by track.params(), got {param!r}"
            )
        index = param
        count = _plugin_param_count(session, channel)
        if not 0 <= index < count:
            raise ValueError(
                f"plugin param index {index} is outside this generator's 0..{count - 1} range; "
                "get a fresh index from track.params()"
            )

    name = str(_live_read(session, "plugins.getParamName", [index, channel, GENERATOR_SLOT]))
    requested = _resolve_value(value, _spec(model, index, name))

    # FL's generic plugin API is positional: value first, then the index
    # advertised by getParamCount/getParamName/getParamValue.
    session.link.call(
        "plugins.setParamValue",
        [requested, index, channel, GENERATOR_SLOT, PICKUP_NONE],
        timeout=LIVE_SET_TIMEOUT_SECONDS,
    )

    confirmed = float(_live_read(session, "plugins.getParamValue", [index, channel, GENERATOR_SLOT]))
    if abs(confirmed - requested) > PLUGIN_PARAM_TOLERANCE:
        raise WriteRejected(
            f"param {name!r} readback {confirmed} is not the requested {requested}",
            fix="The plugin may quantize this control coarsely; read plugin_params "
            "and pick a supported value.",
        )
    return _param(index, name, confirmed, _spec(model, index, name))
