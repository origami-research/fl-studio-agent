"""Live control ops: tempo, channel levels, plugin parameters.

Every result is a READBACK; a mid-call failure raises with what already
landed - it never falls through to the file path (the v1 cascade bug).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fl_mcp import errors, flp
from fl_mcp.errors import NotePlaneUnavailable, WriteRejected
from fl_mcp.schemas import AutomationPoint, AutomationReceipt, ChannelLevels, PluginParam

from .link import LIVE_READ_TIMEOUT_SECONDS, LIVE_SET_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .facade import FLSession

# Plugin-parameter surface: FL quantizes values to each control's own grid
# (E2: set 0.42 -> stored 0.41875), and big synths are big - bound the listing.
PLUGIN_PARAMS_DEFAULT_LIMIT = 40
PLUGIN_PARAM_TOLERANCE = 0.02

# FL's plugins.* convention for "the channel's own generator, not a mixer
# effect slot" (the slot argument every call here passes).
GENERATOR_SLOT = -1


def render_wav(session: FLSession, output: Path) -> Path:
    """Ask the live controller to export WAV, then confirm its artifact exists.

    Rendering is necessarily live-only: an FL project file cannot render itself,
    and success means an actual WAV appeared at the requested location.
    """
    output = output.expanduser().resolve()
    if session._ensure_hello() is None or session.control is None:
        raise errors.control_plane_unavailable("ui.exportWav", 0.0)
    response = session.control.send_request(
        "ui.exportWav", args=[str(output)], timeout=LIVE_SET_TIMEOUT_SECONDS
    )
    if not response.ok:
        detail = response.err or "no detail"
        if errors.is_operation_unsafe(detail):
            raise errors.operation_unsafe("ui.exportWav")
        raise errors.fl_api_error("ui.exportWav", detail)
    if not output.is_file() or output.stat().st_size == 0:
        raise errors.ToolError(
            "render_failed",
            f"FL accepted the WAV export but did not create {output}.",
            "Check FL's export settings and retry; a render is successful only when its WAV exists.",
            retryable=True,
        )
    return output


def set_tempo(session: FLSession, bpm: float) -> float:
    """Live-first (confirmed via getCurrentTempo readback, BPM x 1000);
    else the project file (flp.set_tempo) + reload. Returns the readback."""
    if session._ensure_hello() is not None and session.control is not None:
        resp = session.control.send_request(
            "mixer.setCurrentTempo", args=[bpm, 1], timeout=LIVE_SET_TIMEOUT_SECONDS
        )
        if not resp.ok:
            # Unsafe-at-current-time means busy (warm-up window after a
            # load, addendum A6) - retryable, and NEVER a fallthrough.
            if errors.is_operation_unsafe(resp.err or ""):
                raise errors.operation_unsafe("mixer.setCurrentTempo")
            raise WriteRejected(
                f"mixer.setCurrentTempo({bpm}) failed inside FL: {resp.err}",
                fix="Check the value (10-999 BPM) and retry.",
            )
        readback = session.control.send_request(
            "mixer.getCurrentTempo", timeout=LIVE_READ_TIMEOUT_SECONDS
        )
        if not readback.ok or readback.ret is None:
            raise WriteRejected(
                f"tempo was sent but the readback did not answer ({readback.err}); "
                "the change is UNCONFIRMED.",
                fix="Run fl_doctor, then retry.",
            )
        return float(readback.ret) / 1000.0

    if session.project is not None:
        confirmed = flp.set_tempo(session.project, bpm)
        session._reload_fl()
        return confirmed

    raise NotePlaneUnavailable(
        "No backend can set the tempo: the control plane is not connected and no "
        "project file is configured."
    )


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
            ("volume", "channels.setChannelVolume", [channel, volume]),
            ("pan", "channels.setChannelPan", [channel, pan]),
            ("pitch_semitones", "channels.setChannelPitch", [channel, pitch_semitones, 1]),
        ]
        applied: list[str] = []
        for field_name, op, args in requested:
            if args[1] is None:
                continue
            resp = session.control.send_request(op, args=args, timeout=LIVE_SET_TIMEOUT_SECONDS)
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
            ("volume", "channels.getChannelVolume", [channel]),
            ("pan", "channels.getChannelPan", [channel]),
            ("pitch_semitones", "channels.getChannelPitch", [channel, 1]),
        ):
            resp = session.control.send_request(op, args=args, timeout=LIVE_READ_TIMEOUT_SECONDS)
            if not resp.ok or resp.ret is None:
                raise WriteRejected(
                    f"levels were applied but {op} did not answer ({resp.err}); "
                    "the result is UNCONFIRMED.",
                    fix="Run fl_doctor, then retry.",
                )
            readbacks[field_name] = resp.ret
        return ChannelLevels(
            channel=channel,
            volume=float(readbacks["volume"]),
            pan=float(readbacks["pan"]),
            pitch_semitones=round(float(readbacks["pitch_semitones"])),
        )

    if session.project is not None:
        confirmed = flp.set_channel_levels(
            session.project, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
        )
        session._reload_fl()
        return confirmed

    raise NotePlaneUnavailable(
        "No backend can set channel levels: the control plane is not connected and "
        "no project file is configured."
    )


def set_automation(
    session: FLSession, channel: int, points: list[AutomationPoint]
) -> AutomationReceipt:
    """Replace an existing automation clip and return its saved-file readback.

    There is no verified live automation writer.  ``flpkit`` carries the
    channel's opaque header/trailer and verifies its own splice; this second,
    independent project parse is the session boundary's confirmed result.
    """
    if session.project is None:
        raise NotePlaneUnavailable(
            "No backend can write automation: no project file is configured. "
            "Automation clips currently require an existing FL-authored clip."
        )
    try:
        saved = flp.write_automation(session.project, channel, points)
        project = flp.read(session.project)
        confirmed = next(item.automation for item in project.channels if item.index == channel)
    except (flp.FlpError, IndexError, StopIteration) as exc:
        raise WriteRejected(
            f"project-file automation write did not verify: {exc}",
            fix="Use an existing automation channel and inspect the project file before retrying.",
        ) from exc
    if confirmed != saved:
        raise WriteRejected(
            "project-file automation write read back differently on the session parse; "
            "the result is UNCONFIRMED.",
            fix="Inspect the project file before retrying.",
        )
    session._reload_fl()
    return AutomationReceipt(
        backend="project-write",
        channel=channel,
        points=[
            AutomationPoint(position=p.position, value=p.value, tension=p.tension)
            for p in confirmed
        ],
    )


# -- plugin parameters (live-only; E2-verified ops) --------------------------
#
# A generator's parameters exist only inside the running plugin - there is
# no file representation we can honestly edit (plugin state is an opaque
# vendor blob). So this surface is live-or-error, never a file fallback.


def _plugin_read(session: FLSession, op: str, args: list) -> object:
    if session._ensure_hello() is None or session.control is None:
        raise errors.control_plane_unavailable(op, 0.0)
    resp = session.control.send_request(op, args=args, timeout=LIVE_READ_TIMEOUT_SECONDS)
    if not resp.ok:
        detail = resp.err or "no detail"
        if errors.is_operation_unsafe(detail):
            raise errors.operation_unsafe(op)
        raise errors.fl_api_error(op, detail)
    return resp.ret


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
    count = int(_plugin_read(session, "plugins.getParamCount", [channel, GENERATOR_SLOT]) or 0)
    out: list[PluginParam] = []
    for index in range(count):
        if len(out) >= limit:
            break
        name = str(_plugin_read(session, "plugins.getParamName", [index, channel, GENERATOR_SLOT]))
        if query is not None and query.lower() not in name.lower():
            continue
        value = float(
            _plugin_read(session, "plugins.getParamValue", [index, channel, GENERATOR_SLOT])
        )
        out.append(PluginParam(index=index, name=name, value=min(max(value, 0.0), 1.0)))
    return out


def set_plugin_param(
    session: FLSession, channel: int, param: int | str, value: float
) -> PluginParam:
    """Set one parameter (by index, or by unique name/substring) and return
    the READBACK. FL quantizes to the plugin's own step grid, so the
    confirmed value may differ slightly from the request - beyond
    PLUGIN_PARAM_TOLERANCE it raises instead of pretending."""
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"plugin param values are normalized 0..1, got {value}")
    if isinstance(param, str):
        matches = plugin_params(session, channel, query=param, limit=8)
        exact = [p for p in matches if p.name.lower() == param.lower()]
        chosen = exact or matches
        if len(chosen) != 1:
            names = ", ".join(p.name for p in matches) or "(no matches)"
            raise errors.fl_api_error(
                "plugins.setParamValue",
                f"param {param!r} is {'ambiguous' if matches else 'unknown'} on channel "
                f"{channel}: {names}",
            )
        index = chosen[0].index
    else:
        index = param

    if session.control is None:
        raise errors.control_plane_unavailable("plugins.setParamValue", 0.0)
    resp = session.control.send_request(
        "plugins.setParamValue",
        args=[value, index, channel, GENERATOR_SLOT],
        timeout=LIVE_SET_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise errors.fl_api_error("plugins.setParamValue", resp.err or "no detail")

    name = str(_plugin_read(session, "plugins.getParamName", [index, channel, GENERATOR_SLOT]))
    confirmed = float(
        _plugin_read(session, "plugins.getParamValue", [index, channel, GENERATOR_SLOT])
    )
    if abs(confirmed - value) > PLUGIN_PARAM_TOLERANCE:
        raise WriteRejected(
            f"param {name!r} readback {confirmed} is not the requested {value}",
            fix="The plugin may quantize this control coarsely; read plugin_params "
            "and pick a supported value.",
        )
    return PluginParam(index=index, name=name, value=confirmed)
