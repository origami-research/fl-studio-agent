"""fl.check(): live proof from FL itself that each channel makes sound.

Plays a short span and polls FL's own mixer meters (``mixer.getTrackPeaks``,
API v1: the current peak of a mixer track, 1.0 = 0 dBFS), keeping each track's
loudest reading. A channel is heard on the mixer track it is routed to
(``channels.getTargetFxTrack``, carried by ``state.snapshot``). Each sweep reads
every track in one controller op (``state.peaks``, controller 0.2.5): one request
per track, 24 every 50 ms on the 23-channel default project while FL played 23
sampled instruments, left FL not answering mid-playback (#317). Playback starts
only once FL answers and its stop is sent again if FL missed it (playback.py).

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

from fl_mcp import errors
from fl_mcp.controller_version import controller_protocol_matches
from fl_mcp.schemas import CHECK_MAX_BARS, Channel, ChannelCheck, CheckResult, MixerTrack, SessionState

from . import controls, playback
from .controls import BEATS_PER_BAR, FL_SONGLENGTH_ABSTICKS

if TYPE_CHECKING:
    from .facade import FLSession

# FL's mixer track 0 is the master.
MASTER = 0
# At or below -60 dBFS a track is silent: the threshold render uses for a silent mix.
SILENCE_DBFS = -60.0
# Pause between meter sweeps. FL's meters fall back slowly enough that a hit
# between two sweeps still reads near its peak on the next one; until #317 a sweep
# came every 0.05 s.
PEAK_POLL_SECONDS = 0.15
# What a check says when FL did not confirm the stop after it played.
MAY_STILL_PLAY = "FL did not confirm it stopped after the meter check, so it may still be playing."
STOP_FIX = "Stop FL's playback (space bar in FL) if it is still playing, then retry once FL answers."
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
    hello = session._ensure_hello()
    if hello is None:
        raise errors.fl_stopped_answering("nothing could be played") if session.link.stopped_answering else errors.ToolError(
            "control_plane_unavailable",
            "FL Studio is not answering, so nothing could be played.",
            "FL may still be starting. Retry the check in a moment.",
            retryable=True,
        )
    if not controller_protocol_matches(hello.script_ver):
        # An older controller has no state.peaks: refuse before FL plays, not on the first sweep.
        raise errors.controller_outdated("state.peaks")
    link = session.link
    state = session.snapshot()
    if state.tempo_bpm is None or not state.ppq:
        raise errors.fl_api_error("state.snapshot", "it reported no tempo or ticks per beat")
    start_beat = (start_bar - 1) * BEATS_PER_BAR
    end_beat = start_beat + bars * BEATS_PER_BAR
    playing = controls.playing_notes(session, state, start_beat, end_beat, "check")
    seconds = (end_beat - start_beat) * 60.0 / state.tempo_bpm
    tracks = sorted({MASTER} | {c.mixer_track for c in state.channels if c.mixer_track is not None})

    if state.playing:
        link.command("transport.stop")
    # Meters still falling from earlier playback (this check's own prior run,
    # a render) must not read as sound in this one.
    sleep(METER_DECAY_SECONDS)
    link.command("transport.setSongPos", [start_beat * state.ppq, FL_SONGLENGTH_ABSTICKS])
    playback.confirm_answering(session, "control_plane_unavailable", "the meter check")
    peaks = dict.fromkeys(tracks, 0.0)
    try:
        # Inside the try: a start FL parks and applies late still meets the stop
        # below, which FL applies after it.
        link.command("transport.start")
        deadline = clock() + seconds
        while True:
            for track, peak in _peaks(session, tracks).items():
                peaks[track] = max(peaks[track], peak)
            if clock() >= deadline:
                break
            sleep(PEAK_POLL_SECONDS)
    except errors.ToolError as failed:
        # A sweep FL did not answer (#317) is when the stop is most likely missed too: say so.
        if playback.stop(session) is None:
            raise
        raise errors.ToolError(
            failed.category, f"{failed.message} {MAY_STILL_PLAY}", STOP_FIX, retryable=failed.retryable
        ) from failed
    except BaseException:
        playback.stop(session)
        raise
    stop_error = playback.stop(session)
    if stop_error is not None:
        raise errors.ToolError(
            stop_error.category, f"{stop_error.message} {MAY_STILL_PLAY}", STOP_FIX, retryable=True
        )

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


def _peaks(session: FLSession, tracks: Sequence[int]) -> dict[int, float]:
    """One meter sweep in one round trip: FL's current peak on each mixer track, 1.0 = 0 dBFS."""
    answer = session.link.call("state.peaks", [list(tracks)])
    try:
        peaks = {int(track): value for track, value in answer}
    except (TypeError, ValueError) as exc:
        raise errors.fl_api_error("state.peaks", f"malformed answer {answer!r}") from exc
    bad = {t: v for t, v in peaks.items() if isinstance(v, bool) or not isinstance(v, int | float)}
    if bad or sorted(peaks) != sorted(tracks):
        raise errors.fl_api_error("state.peaks", f"returned {answer!r}, not a level for each of tracks {list(tracks)}")
    return {track: float(value) for track, value in peaks.items()}


def _dbfs(peak: float) -> float | None:
    """A meter reading in dBFS, or None for silence."""
    if peak <= 0:
        return None
    level = 20 * math.log10(peak)
    return round(level, 1) if level > SILENCE_DBFS else None


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
