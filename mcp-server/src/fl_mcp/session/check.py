"""fl.check(): live proof from FL itself that each channel makes sound.

Plays a short span and polls FL's own mixer meters (``mixer.getTrackPeaks``,
API v1: the current peak of a mixer track, 1.0 = 0 dBFS), keeping each track's
loudest reading. A channel is heard on the mixer track it is routed to
(``channels.getTargetFxTrack``, carried by ``state.snapshot``). Every call is a
plain module call through the controller, so any installed controller serves it.

What the meters cannot say is reported as such, never guessed: two channels on
one mixer track are measured together, and a channel routed straight to the
master shares its meter with the whole mix. Why a channel is silent is named
only from what FL or the session's note record states (muted, a muted mixer
track, zero volume, no notes in the bars played). FL is left stopped, with no
mute or solo state touched.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from fl_mcp import errors, flp
from fl_mcp.schemas import CHECK_MAX_BARS, Channel, ChannelCheck, CheckResult, MixerTrack, Note, SessionState

from . import controls
from .controls import FL_LOOP_MODE_PATTERN, FL_LOOP_MODE_SONG, FL_SONGLENGTH_ABSTICKS, FL_STEPS_PER_BEAT

if TYPE_CHECKING:
    from .facade import FLSession

# FL's mixer track 0 is the master.
MASTER = 0
# getTrackPeaks mode: the louder of left and right (IL API stubs, midi.PEAK_LR).
PEAK_LR = 2
# The template is 4/4 and the note API speaks beats, so a bar is four beats.
BEATS_PER_BAR = 4
# At or below -60 dBFS a track is silent: the threshold render uses for a silent mix.
SILENCE_DBFS = -60.0
# Pause between meter sweeps. FL's meters fall back slowly enough that a hit
# between two sweeps still reads near its peak on the next one.
PEAK_POLL_SECONDS = 0.05
# After stopping a song that was already playing, let its meters fall to silence.
METER_DECAY_SECONDS = 0.5


def check(
    session: FLSession,
    start_bar: int,
    bars: int,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> CheckResult:
    """Play ``bars`` bars from ``start_bar`` and report which channels sounded."""
    if isinstance(start_bar, bool) or not isinstance(start_bar, int) or start_bar < 1:
        raise errors.invalid_argument("check", f"start_bar must be a whole number from 1, got {start_bar!r}")
    if isinstance(bars, bool) or not isinstance(bars, int) or not 1 <= bars <= CHECK_MAX_BARS:
        raise errors.invalid_argument("check", f"bars must be a whole number from 1 to {CHECK_MAX_BARS}, got {bars!r}")
    if session._ensure_hello() is None:
        raise errors.ToolError(
            "control_plane_unavailable",
            "FL Studio is not answering, so nothing could be played.",
            "FL may still be starting. Retry the check in a moment.",
            retryable=True,
        )
    link = session.link
    state = session.snapshot()
    if state.tempo_bpm is None or not state.ppq:
        raise errors.fl_api_error("state.snapshot", "it reported no tempo or ticks per beat")
    start_beat = (start_bar - 1) * BEATS_PER_BAR
    end_beat = start_beat + bars * BEATS_PER_BAR
    playing = _playing_notes(session, state, start_beat, end_beat)
    seconds = (end_beat - start_beat) * 60.0 / state.tempo_bpm
    tracks = sorted({MASTER} | {c.mixer_track for c in state.channels if c.mixer_track is not None})

    if state.playing:
        link.command("transport.stop")
    # Meters still falling from earlier playback (this check's own prior run,
    # a render) must not read as sound in this one.
    sleep(METER_DECAY_SECONDS)
    link.command("transport.setSongPos", [start_beat * state.ppq, FL_SONGLENGTH_ABSTICKS])
    peaks = dict.fromkeys(tracks, 0.0)
    try:
        # Inside the try: a start FL parks and applies late still meets the stop
        # below, which FL applies after it.
        link.command("transport.start")
        deadline = clock() + seconds
        while True:
            for track in tracks:
                peaks[track] = max(peaks[track], _peak(session, track))
            if clock() >= deadline:
                break
            sleep(PEAK_POLL_SECONDS)
    finally:
        link.command("transport.stop")

    levels = {track: _dbfs(peak) for track, peak in peaks.items()}
    results = _attribute(state, levels, playing, f"bars {start_bar}-{start_bar + bars - 1}")
    return CheckResult(
        start_bar=start_bar,
        bars=bars,
        seconds=round(seconds, 2),
        master_peak_dbfs=levels[MASTER],
        channels=results,
        summary=_summary(results, levels[MASTER], start_bar, bars),
    )


def _peak(session: FLSession, track: int) -> float:
    """One meter reading: FL's current peak on a mixer track, 1.0 = 0 dBFS."""
    value = session.link.call("mixer.getTrackPeaks", [track, PEAK_LR])
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise errors.fl_api_error("mixer.getTrackPeaks", f"returned {value!r}, not a level")
    return float(value)


def _dbfs(peak: float) -> float | None:
    """A meter reading in dBFS, or None for silence."""
    if peak <= 0:
        return None
    level = 20 * math.log10(peak)
    return round(level, 1) if level > SILENCE_DBFS else None


def _playing_notes(session: FLSession, state: SessionState, start: float, end: float) -> dict[int, bool] | None:
    """Per channel: does it have notes (or an audio clip) in the beats about to
    play? None when unknown. Raises when the span starts past the end.

    Pattern mode loops the selected pattern; song mode plays the playlist. The
    playlist and the notes nobody wrote this session come from the song FL
    opened (the SDK cannot change the playlist), so without that song the
    answer is unknown, never assumed.
    """
    loop_mode = controls._live_int(session, "transport.getLoopMode")
    if loop_mode == FL_LOOP_MODE_PATTERN:
        pattern = state.current_pattern
        if pattern is None:
            return None
        length = max(controls._live_int(session, "patterns.getPatternLength", [pattern]), 1) / FL_STEPS_PER_BEAT
        if start >= length:
            bars = math.ceil(length / BEATS_PER_BAR)
            raise errors.invalid_argument(
                "check", f"pattern {pattern} is {bars} bar{'s' if bars != 1 else ''} long; start at bar 1 to {bars}"
            )
        if session.project is None or not session.project.is_file():
            return None
        notes = session.all_notes()
        return {
            c.index: any(_overlaps_looped(n, start, end, length) for n in notes.get((pattern, c.index), []))
            for c in state.channels
        }
    if loop_mode != FL_LOOP_MODE_SONG:
        raise errors.fl_api_error("transport.getLoopMode", f"returned unknown loop mode {loop_mode}")
    last_tick = controls._live_int(session, "transport.getSongLength", [FL_SONGLENGTH_ABSTICKS])
    if last_tick <= 0:
        raise errors.ToolError(
            "invalid_argument",
            "FL is in song mode and its playlist is empty, so nothing would play.",
            "Place a pattern on the playlist, or switch FL to pattern mode, then check again.",
        )
    ppq = state.ppq or 1
    if start * ppq > last_tick:
        bars = math.ceil((last_tick + 1) / ppq / BEATS_PER_BAR)
        raise errors.invalid_argument("check", f"the song is {bars} bars long; start at bar 1 to {bars}")
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
    audio = {clip.channel for clip in clips if clip.channel is not None}
    return {
        c.index: c.index in audio or any(notes.get((p, c.index)) for p in patterns) for c in state.channels
    }


def _overlaps_looped(note: Note, start: float, end: float, length: float) -> bool:
    """Whether a note in a pattern of ``length`` beats, looping, sounds in [start, end)."""
    first = math.floor((start - note.start - note.length) / length)
    return any(
        note.start + k * length < end and note.start + note.length + k * length > start
        for k in range(first, math.ceil((end - note.start) / length) + 1)
    )


def _attribute(
    state: SessionState, levels: dict[int, float | None], playing: dict[int, bool] | None, span: str
) -> list[ChannelCheck]:
    """Turn mixer-track levels into a per-channel verdict, saying what is shared.

    FL's own state (a mute, a zero volume) settles a channel as silent outright.
    The session's note record ("no notes in these bars") only decides between
    channels that share a meter; a channel alone on a track that sounded is
    taken at the meter's word.
    """
    mixer = {t.index: t for t in state.mixer}

    def track_of(channel: Channel) -> int:
        return channel.mixer_track if channel.mixer_track is not None else MASTER

    fl_silenced = {c.index: _fl_silence(c, mixer.get(track_of(c))) for c in state.channels}
    no_notes = {
        c.index: f"it has no notes in {span}" if playing is not None and not playing.get(c.index) else None
        for c in state.channels
    }
    results = []
    for channel in state.channels:
        track = track_of(channel)
        level = levels.get(track)
        # The master's meter carries every channel; any other track, its own.
        sharing = [
            other.name
            for other in state.channels
            if other.index != channel.index
            and track in (MASTER, track_of(other))
            and fl_silenced[other.index] is None
            and no_notes[other.index] is None
        ]
        sounded: bool | None
        if level is None:
            sounded = False
            reason = (
                fl_silenced[channel.index]
                or no_notes[channel.index]
                or "it made no sound; check its instrument and levels"
            )
        elif fl_silenced[channel.index] is not None:
            sounded, level, reason = False, None, fl_silenced[channel.index]
        elif no_notes[channel.index] is not None and sharing:
            sounded, level, reason = False, None, no_notes[channel.index]
        elif track == MASTER:
            sounded, level = None, None  # the master's level is the whole mix, not this channel's
            reason = (
                "it plays straight into the master with the rest of the mix, so it cannot be "
                "measured on its own; route it to its own mixer track to check it"
            )
        else:
            sounded = True
            reason = f"measured together with {', '.join(sharing)} on mixer track {track}" if sharing else None
        results.append(
            ChannelCheck(
                channel=channel.index,
                name=channel.name,
                sounded=sounded,
                peak_dbfs=level,
                mixer_track=track,
                reason=reason,
            )
        )
    return results


def _fl_silence(channel: Channel, track: MixerTrack | None) -> str | None:
    """A setting in FL that keeps this channel silent, if there is one."""
    if channel.muted:
        return "it is muted"
    if channel.volume <= 0:
        return "its volume is 0"
    if track is not None and track.muted:
        return f"its mixer track {track.index} ({track.name}) is muted"
    return None


def _summary(results: Sequence[ChannelCheck], master: float | None, start_bar: int, bars: int) -> str:
    span = f"bars {start_bar}-{start_bar + bars - 1}"
    heard = [r for r in results if r.sounded]
    silent = [r for r in results if r.sounded is False]
    shared = [r for r in results if r.sounded is None]
    parts = [f"{len(heard)} of {len(results)} channels sounded in {span}"]
    parts[0] += f"; the master peaked at {master:g} dBFS." if master is not None else "; the master stayed silent."
    if silent:
        parts.append("Silent: " + "; ".join(f"{r.name} ({r.reason})" for r in silent) + ".")
    if shared:
        parts.append("Not measurable on their own (they play straight into the master): "
                     + ", ".join(r.name for r in shared) + ".")
    if master is None and heard:
        parts.append("Their mixer tracks play but nothing reaches the master: check the master volume and routing.")
    return " ".join(parts)
