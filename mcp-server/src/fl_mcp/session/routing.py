"""Live mixer routing: the mixer track a channel plays through, and sends
between mixer tracks.

Documented controller calls only, live-verified on FL 2026 (controller API 45,
docs/research/routing-presets-live-2026-11-02.md): ``channels.setTargetFxTrack``
reads back through ``getTargetFxTrack``; ``mixer.setRouteTo`` takes effect after
``mixer.afterRoutingChanged`` and reads back through ``getRouteSendActive``;
``mixer.setRouteToLevel`` reads back through ``getRouteToLevel``. Every result
is FL's readback, and a call that stops partway raises with what already landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_mcp import errors
from fl_mcp.errors import WriteRejected
from fl_mcp.schemas import ChannelRoute, MixerSend

from .controls import _live_int, _live_read, _live_write

if TYPE_CHECKING:
    from .facade import FLSession

# FL stored a 0.5 send level as exactly 0.5 live; a wider gap is not what was asked.
SEND_LEVEL_TOLERANCE = 0.01


def _where(track: int) -> str:
    return "the master" if track == 0 else f"mixer track {track}"


def _require_mixer_track(session: FLSession, call: str, track: int) -> None:
    """Refuse a mixer track FL does not have before anything is written."""
    count = _live_int(session, "mixer.trackCount")
    if not 0 <= track < count:
        raise errors.invalid_argument(
            call, f"mixer track {track} does not exist; FL has mixer tracks 0 (the master) to {count - 1}"
        )


def route_channel(session: FLSession, channel: int, mixer_track: int) -> ChannelRoute:
    """Send a channel's sound to ``mixer_track`` and return the track FL now routes it to."""
    _require_mixer_track(session, "fl.route", mixer_track)
    _live_write(session, "channels.setTargetFxTrack", [channel, mixer_track])
    confirmed = _live_int(session, "channels.getTargetFxTrack", [channel])
    if confirmed != mixer_track:
        raise WriteRejected(
            f"FL Studio routes channel {channel} to {_where(confirmed)}, not {_where(mixer_track)}.",
            fix="Route it again; if FL keeps the old track, pick another mixer track.",
        )
    return ChannelRoute(channel=channel, mixer_track=confirmed)


def set_send(
    session: FLSession, source: int, target: int, *, on: bool, level: float | None = None
) -> MixerSend:
    """Turn the send from ``source`` to ``target`` on or off, optionally at
    ``level`` (normalized 0..1), and return FL's readback of that send."""
    for track in (source, target):
        _require_mixer_track(session, "fl.send", track)
    _live_write(session, "mixer.setRouteTo", [source, target, on])
    _live_write(session, "mixer.afterRoutingChanged", [])
    if level is not None:
        try:
            _live_write(session, "mixer.setRouteToLevel", [source, target, level])
        except errors.ToolError as exc:
            raise WriteRejected(
                f"FL Studio turned {'on' if on else 'off'} the send from {_where(source)} to "
                f"{_where(target)} but did not take its level: {exc.message}",
                fix="Set the level again; the send keeps the state FL already took.",
            ) from exc
    active = _live_read(session, "mixer.getRouteSendActive", [source, target])
    confirmed_level = _live_read(session, "mixer.getRouteToLevel", [source, target])
    if not isinstance(active, (bool, int)) or not isinstance(confirmed_level, (int, float)):
        raise errors.fl_api_error(
            "mixer.getRouteSendActive", f"reported the send as {active!r} at level {confirmed_level!r}"
        )
    sent = MixerSend(source=source, target=target, on=bool(active), level=float(confirmed_level))
    if sent.on != on:
        raise WriteRejected(
            f"FL Studio left the send from {_where(source)} to {_where(target)} {'on' if sent.on else 'off'}.",
            fix="Check both mixer tracks, then send again.",
            retryable=False,
        )
    if level is not None and abs(sent.level - level) > SEND_LEVEL_TOLERANCE:
        raise WriteRejected(
            f"FL Studio set the send from {_where(source)} to {_where(target)} to level "
            f"{sent.level:.2f}, not {level:.2f}.",
            fix="Set the level again.",
        )
    return sent
