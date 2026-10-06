"""Live control ops: tempo, channel levels, plugin parameters.

Every result is a READBACK; a mid-call failure raises with what already
landed - it never falls through to the file path (the v1 cascade bug).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import TYPE_CHECKING

from fl_mcp import audio, errors, flp
from fl_mcp.contract import ENV_AGENT_PROJECT, ENV_SPOOL_ROOT, FLLicense
from fl_mcp.errors import NotePlaneUnavailable, WriteRejected
from fl_mcp.schemas import (
    AutomationPoint,
    AutomationReceipt,
    ChannelLevels,
    PluginParam,
    PluginParamModel,
    PluginParamSpec,
    RenderReceipt,
)

from .link import LIVE_READ_TIMEOUT_SECONDS, LIVE_SET_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .facade import FLSession

# Plugin-parameter surface: FL quantizes values to each control's own grid
# (E2: set 0.42 -> stored 0.41875), and big synths are big - bound the listing.
PLUGIN_PARAMS_DEFAULT_LIMIT = 40
PLUGIN_PARAM_TOLERANCE = 0.02
SAVE_BUSY_RETRIES = 2
SAVE_BUSY_RETRY_SECONDS = 0.25
RENDER_TIMEOUT_SECONDS = 300.0
RENDER_TAIL_SECONDS = 0.5

log = logging.getLogger(__name__)

# FL's plugins.* convention for "the channel's own generator, not a mixer
# effect slot" (the slot argument every call here passes).
GENERATOR_SLOT = -1
_NUMBER = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*$")


def save_project(session: FLSession) -> None:
    """Save the live project to its .flp via FL's FPT_Save transport command.

    Live-verified 2026-09-02: live tempo 145 in RAM then save_project made the
    on-disk .flp reparse to 145 (was 130). FL's globalTransport return for
    FPT_Save is a non-flag internal int that varies per call, so it is NOT
    surfaced - success is by-effect; a refusal raises below.

    Never under a trial license: live box 2026-10-05, the first trial save
    opened FL's modal Buy dialog and every later controller call timed out.
    """
    if session.fl_license is FLLicense.TRIAL:
        raise errors.fl_trial_cannot_save("saving the project")
    if session._ensure_hello() is None or session.control is None:
        # A failed liveness probe does not shrink the save's documented
        # budget.  In session mode that probe crosses the broker first; using
        # 0 here made the resulting ctl.save error claim an expired request
        # even though no save request had received a zero-second timeout.
        raise errors.control_plane_unavailable("ctl.save", LIVE_SET_TIMEOUT_SECONDS)
    for attempt in range(SAVE_BUSY_RETRIES + 1):
        try:
            response = session.control.send_request("ctl.save", timeout=LIVE_SET_TIMEOUT_SECONDS)
            break
        except errors.ToolError as exc:
            if exc.category != "control_plane_busy" or attempt == SAVE_BUSY_RETRIES:
                raise
            time.sleep(SAVE_BUSY_RETRY_SECONDS)
    if not response.ok:
        detail = response.err or "no detail"
        if errors.is_operation_unsafe(detail):
            raise errors.operation_unsafe("ctl.save")
        raise errors.fl_api_error("ctl.save", detail)


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


def _render_span_seconds(project: Path) -> float:
    """One arrangement pass plus a short decay tail, from saved authoritative state."""
    parsed = flp.read(project)
    end_ticks = max((item.position + item.length for item in parsed.playlist), default=parsed.ppq * 16)
    beats = end_ticks / parsed.ppq
    return beats * 60.0 / parsed.tempo + RENDER_TAIL_SECONDS


def _live_render(session: FLSession, output: Path, seconds: float) -> Path:
    """Play the saved arrangement once while the master Bridge records it."""
    response_timeout = min(10.0, max(2.0, seconds / 2.0 + 2.0))
    with session._transport_use():
        started = session.transport.send("capture_start", seconds=seconds)
        start = session.transport.await_response(started, timeout=response_timeout)
    if not start.ok:
        raise errors.ToolError(
            "render_failed",
            start.err or "Bridge refused capture_start",
            "Check the Bridge heartbeat and retry.",
            retryable=True,
        )
    stop_error: errors.ToolError | None = None
    try:
        # Position is a controller operation, so it remains on the control wire
        # rather than reaching from the Bridge into FL on a mixer callback.
        if session.control is None:
            raise errors.control_plane_unavailable("transport.setSongPos", 0.0)
        positioned = session.control.send_request(
            "transport.setSongPos", args=[0], timeout=LIVE_SET_TIMEOUT_SECONDS
        )
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
            session.link.command("transport.stop")
        except errors.ToolError as exc:
            stop_error = exc
        finally:
            with session._transport_use():
                stopped_cmd = session.transport.send("capture_stop")
                stopped = session.transport.await_response(stopped_cmd, timeout=response_timeout)
        if stop_error is not None:
            try:
                session.link.command("transport.stop")
            except errors.ToolError as exc:
                stop_error = exc
            else:
                stop_error = None

    stop_detail = (
        f"; transport.stop was unacknowledged: {stop_error.message}"
        if stop_error is not None and stop_error.category == "control_plane_unavailable"
        else f"; transport.stop failed: {stop_error.message}"
        if stop_error is not None
        else ""
    )
    if not stopped.ok or not stopped.path or stopped.frames_written is None:
        raise errors.ToolError(
            "render_failed",
            (stopped.err or "Bridge wrote no audio frames") + stop_detail,
            "Confirm FL audio output and retry.",
            retryable=True,
        )
    captured = Path(stopped.path)
    if not captured.is_file() or captured.stat().st_size <= 44:
        raise errors.ToolError(
            "render_failed",
            "Bridge capture WAV is missing or empty" + stop_detail,
            "Check the spool root is writable and retry.",
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
        raise errors.ToolError(
            "render_failed",
            "Bridge capture did not verify: "
            f"requested={expected_frames}, written={stopped.frames_written}, rate={stopped.sample_rate}, "
            f"drops={stopped.drop_count}{stop_detail}",
            "Confirm the Bridge is last on Master and FL audio is running, then retry.",
            retryable=True,
        )
    try:
        samples, rate = audio._read_wav(captured)
        measured = audio.analyze_wav(captured)
    except (OSError, ValueError) as exc:
        raise errors.ToolError(
            "render_failed",
            f"Bridge WAV could not be read: {exc}{stop_detail}",
            "Retry the capture after checking the spool disk.",
            retryable=True,
        ) from exc
    if (
        rate != start.sample_rate
        or abs(len(samples) - expected_frames) > expected_frames * 0.01
        or measured.integrated_lufs <= -60
    ):
        raise errors.ToolError(
            "render_failed",
            f"Bridge WAV did not verify: frames={len(samples)}/{expected_frames}, rate={rate}/{start.sample_rate}, "
            f"lufs={measured.integrated_lufs:.1f}{stop_detail}",
            "Confirm FL audio output and Master Bridge routing, then retry.",
            retryable=True,
        )
    if stop_error is not None:
        log.warning(
            "transport.stop was unacknowledged after two attempts; FL may still be playing; "
            "the next transport command re-seats it: %s",
            stop_error.message,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(captured, output)
    return output


def _save_before_render(session: FLSession) -> tuple[bool, str | None]:
    """Save so the render length reflects the live arrangement.

    Licensed: a failed save fails the render, as before. Trial: FL is never
    asked to save (save_project), because its Buy dialog would freeze the very
    transport the capture needs. The Bridge capture does not need the saved
    file, so the render runs and the receipt says the project was not saved.
    """
    if session.fl_license is FLLicense.TRIAL:
        return False, (
            "not saved: a trial FL answers Save with a modal Buy dialog that freezes the controller. "
            "The render length came from the last saved project file."
        )
    save_project(session)
    return True, None


def render_wav(session: FLSession, output: Path) -> RenderReceipt:
    """Play the loop once and record the live mix, about the loop's length.

    The Bridge and FL audio output device are required. It fails loudly when
    that live path is unavailable rather than masking it with an offline export.
    """
    if session.project is None:
        raise errors.ToolError(
            "render_failed",
            "Cannot render without a configured saved FL project.",
            "Configure an existing .flp project, then retry.",
        )
    output = output.expanduser().resolve()
    project = session.project.expanduser().resolve()
    saved, save_detail = _save_before_render(session)
    if not session.caps.bridge_live:
        raise errors.ToolError(
            "render_failed",
            "Bridge is not live; render needs the Master-insert Bridge and FL audio output.",
            "Start FL with an audio output device and confirm bridge_heartbeat.txt, then retry.",
            retryable=True,
        )
    path = _live_render(session, output, _render_span_seconds(project))
    return RenderReceipt(path=path, project_saved=saved, project_save_detail=save_detail)


def export_offline(session: FLSession, output: Path) -> Path:
    """Boot a second FL and export with -R, for offline deliverables only."""
    if session.fl_license is FLLicense.TRIAL:
        raise errors.fl_trial_cannot_reopen("export_offline (a second FL rendering the saved .flp)")
    if session.project is None:
        raise errors.ToolError(
            "render_failed",
            "Cannot export without a configured saved FL project.",
            "Configure an existing .flp project, then retry.",
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
        readback = session.control.send_request("mixer.getCurrentTempo", timeout=LIVE_READ_TIMEOUT_SECONDS)
        if not readback.ok or readback.ret is None:
            raise WriteRejected(
                f"tempo was sent but the readback did not answer ({readback.err}); "
                "the change is UNCONFIRMED.",
                fix="Run fl_doctor, then retry.",
            )
        return float(readback.ret) / 1000.0

    if session.project is not None:
        session._prepare_file_write()
        confirmed = flp.set_tempo(session.project, bpm)
        session._reload_fl()
        return confirmed

    raise NotePlaneUnavailable(
        "No backend can set the tempo: the control plane is not connected and no project file is configured."
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
                    f"levels were applied but {op} did not answer ({resp.err}); the result is UNCONFIRMED.",
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
        session._prepare_file_write()
        confirmed = flp.set_channel_levels(
            session.project, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
        )
        session._reload_fl()
        return confirmed

    raise NotePlaneUnavailable(
        "No backend can set channel levels: the control plane is not connected and "
        "no project file is configured."
    )


def set_automation(session: FLSession, channel: int, points: list[AutomationPoint]) -> AutomationReceipt:
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
        session._prepare_file_write()
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
        points=[AutomationPoint(position=p.position, value=p.value, tension=p.tension) for p in confirmed],
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


def _plugin_param_count(session: FLSession, channel: int) -> int:
    return int(_plugin_read(session, "plugins.getParamCount", [channel, GENERATOR_SLOT]) or 0)


def _plugin_model(session: FLSession, channel: int) -> PluginParamModel | None:
    """Select static semantics only when the session was explicitly given them."""
    if not session.plugin_param_models:
        return None
    plugin = str(_plugin_read(session, "plugins.getPluginName", [channel, GENERATOR_SLOT]))
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
        name = str(_plugin_read(session, "plugins.getParamName", [index, channel, GENERATOR_SLOT]))
        if query is not None and query.lower() not in name.lower():
            continue
        value = float(_plugin_read(session, "plugins.getParamValue", [index, channel, GENERATOR_SLOT]))
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

    name = str(_plugin_read(session, "plugins.getParamName", [index, channel, GENERATOR_SLOT]))
    requested = _resolve_value(value, _spec(model, index, name))

    if session.control is None:
        raise errors.control_plane_unavailable("plugins.setParamValue", 0.0)
    resp = session.control.send_request(
        "plugins.setParamValue",
        # FL's generic plugin API is positional: value first, then the index
        # advertised by getParamCount/getParamName/getParamValue.
        args=[requested, index, channel, GENERATOR_SLOT],
        timeout=LIVE_SET_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise errors.fl_api_error("plugins.setParamValue", resp.err or "no detail")

    confirmed = float(_plugin_read(session, "plugins.getParamValue", [index, channel, GENERATOR_SLOT]))
    if abs(confirmed - requested) > PLUGIN_PARAM_TOLERANCE:
        raise WriteRejected(
            f"param {name!r} readback {confirmed} is not the requested {requested}",
            fix="The plugin may quantize this control coarsely; read plugin_params "
            "and pick a supported value.",
        )
    return _param(index, name, confirmed, _spec(model, index, name))
