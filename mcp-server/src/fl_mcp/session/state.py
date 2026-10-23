"""Reads and the doctor report: snapshot, channel_names, doctor."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from fl_mcp import errors, flp
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION, controller_protocol_matches
from fl_mcp.core.carriers.spool import spool_roots
from fl_mcp.fl.paths import piano_roll_scripts_dir
from fl_mcp.fl.process import bridge_pid, fl_process_running, pid_alive
from fl_mcp.profile import installed_bridge_build, profile_gate
from fl_mcp.schemas import BridgeInstall, Channel, DoctorReport, PlaneStatus, SessionState

from . import controls
from .detect import BRIDGE_HEARTBEAT_FILE, BRIDGE_HEARTBEAT_FRESH_SECONDS, read_heartbeat

if TYPE_CHECKING:
    from .facade import FLSession

log = logging.getLogger(__name__)

# state.snapshot arrives as ~10 chunked SysEx frames (measured ~4.8KB on a
# 5-channel/18-track project, addendum A5); give bigger projects headroom.
SNAPSHOT_TIMEOUT_SECONDS = 10.0

PYSCRIPT_NAME = "MCP Write Notes.pyscript"


def channel_names(session: FLSession) -> list[Channel]:
    """The channels of the TARGET universe - the project file - for name
    resolution. File-first because targets are file coordinates and the
    file answers instantly mid-reload; the live snapshot is the fallback
    for sessions without a project."""
    if session.project is not None and session.project.is_file():
        proj = flp.read(session.project)
        return [
            Channel(
                index=c.index,
                name=c.name or f"Channel {c.index}",
                volume=c.volume / flp.LEVEL_MAX,
                pan=(c.pan - flp.PAN_CENTRE) / flp.PAN_CENTRE,
            )
            for c in proj.channels
        ]
    return session.snapshot().channels


def tempo(session: FLSession) -> float | None:
    """FL's tempo now - THE tempo read behind fl.tempo and get_scene.

    Live from FL whenever FL is connected, so a set_tempo shows at once;
    the project's saved tempo only when FL is not connected (a trial FL
    never saves, so its file keeps the tempo it opened with)."""
    if session._ensure_hello() is not None and session.control is not None:
        return controls._live_int(session, "mixer.getCurrentTempo") / 1000.0
    if session.project is not None and session.project.is_file():
        return flp.read(session.project).tempo
    return None


def snapshot(session: FLSession) -> SessionState:
    """Live: ONE composite ``state.snapshot`` round trip (chunked response;
    v1's transport silently dropped it, Lane W fixed the routing - addendum
    A5). Else: decoded from the project file. One backend per call, so a
    stale file never contaminates a live view or vice versa."""
    if session._ensure_hello() is not None and session.control is not None:
        snap = session.link.call("state.snapshot", timeout=SNAPSHOT_TIMEOUT_SECONDS)
        try:
            return SessionState.from_wire(snap)
        except (KeyError, TypeError, ValueError) as exc:
            raise errors.fl_api_error("state.snapshot", f"malformed snapshot payload ({exc!r})") from exc

    if session.project is not None:
        project = flp.read(session.project)
        return SessionState(
            ppq=project.ppq,
            tempo_bpm=project.tempo,
            channels=[
                Channel(
                    index=c.index,
                    name=c.name,
                    volume=c.volume / flp.LEVEL_MAX,
                    pan=(c.pan - flp.PAN_CENTRE) / flp.PAN_CENTRE,
                )
                for c in project.channels
            ],
        )

    log.warning("snapshot: the control wire is not connected and no project file is configured")
    raise errors.ToolError(
        "control_plane_unavailable",
        "FL Studio is not answering, so its state cannot be read right now.",
        "FL may still be starting. Retry in a moment.",
        retryable=True,
    )


def doctor(session: FLSession, *, clock: Callable[[], float] = time.time) -> DoctorReport:
    """Typed report - control plane, note plane, project file - with a
    concrete ``fix`` string per degraded plane."""

    def failed_probe(plane: str, exc: Exception) -> PlaneStatus:
        detail = f"{plane} probe failed: {exc}"
        return PlaneStatus(
            connected=False,
            detail=detail,
            fix=f"{detail}. Restore that component, then run fl-mcp-doctor again.",
        )

    try:
        fl_pid = bridge_pid(session.session_spool_root) if session.session_spool_root else None
    except Exception as exc:  # noqa: BLE001 - doctor must report failed probes
        fl_pid = None
        liveness_error: Exception | None = exc
    else:
        liveness_error = None
    try:
        fl_pid_alive = pid_alive(fl_pid) if session.session_spool_root else None
    except Exception as exc:  # noqa: BLE001 - doctor must report failed probes
        fl_pid_alive = None
        liveness_error = liveness_error or exc
    liveness = {"fl_pid": fl_pid, "fl_pid_alive": fl_pid_alive}

    try:
        control_plane = _control_plane_status(session, liveness)
        if liveness_error is not None:
            control_plane = failed_probe("bridge PID liveness", liveness_error)
    except Exception as exc:  # noqa: BLE001 - doctor must report failed probes
        control_plane = failed_probe("control plane", exc)

    try:
        note_plane = _note_plane_status(clock=clock)
    except Exception as exc:  # noqa: BLE001 - doctor must report failed probes
        note_plane = failed_probe("note plane", exc)
    supported, profile_detail = profile_gate()
    if not supported and profile_detail.startswith("unsupported "):
        note_plane = PlaneStatus(
            connected=False,
            detail=profile_detail,
            fix="Capture and review this build's profile with `python -m fl_mcp.discover`, then install the rebuilt Bridge.",
        )
    installed_for = installed_bridge_build()
    if installed_for is not None and supported and installed_for != profile_detail:
        detail = f"Bridge installed for {installed_for}, running {profile_detail}"
        note_plane = PlaneStatus(
            connected=False,
            detail=detail,
            fix="Rerun fl-mcp-install after an FL update; if the Bridge still does not answer, "
            "build it for this FL build (SETUP.md, 'Build the Bridge yourself').",
        )

    try:
        project_file = _project_file_status(session)
    except Exception as exc:  # noqa: BLE001 - doctor must report failed probes
        project_file = failed_probe("project file", exc)

    all_connected = (
        control_plane.connected
        and note_plane.connected
        and project_file.connected
        and (fl_pid_alive is not False)
    )
    return DoctorReport(
        status="ok" if all_connected else "degraded",
        control_plane=control_plane,
        note_plane=note_plane,
        project_file=project_file,
        bridge=_bridge_install(),
    )


def _bridge_install() -> BridgeInstall:
    """Presence and source of the Bridge in FL's plugin folder; a failed read is reported, not raised."""
    from fl_mcp.fl import bridge

    try:
        return bridge.installed()
    except Exception as exc:  # noqa: BLE001 - doctor must report failed probes
        return BridgeInstall(installed=False, path="unknown", source=f"probe failed: {exc}")


def _control_plane_status(session: FLSession, liveness: dict[str, int | bool | None]) -> PlaneStatus:
    if session.control is None:
        windows = type(session.platform).__name__ == "WindowsPlatform"
        return PlaneStatus(
            connected=False,
            detail="no MIDI control wire: python-rtmidi is missing or the MIDI ports could not be opened",
            fix=(
                "Install loopMIDI, provision 'FLMCP Req' and 'FLMCP Res', then restart the MCP server."
                if windows
                else "Install python-rtmidi (`uv pip install python-rtmidi`) and restart the MCP server."
            ),
            **liveness,
        )
    # Captured BEFORE the probe: the hello probe may recycle a silent wire,
    # and the old wire's dropped-frame count is the diagnostic.
    dropped = session.control.dropped_frames
    hello = session._ensure_hello()
    if hello is not None:
        stale_controller = not controller_protocol_matches(hello.script_ver)
        return PlaneStatus(
            connected=True,
            detail=(
                f"installed controller is stale vs this server "
                f"(controller {hello.script_ver}, expected {CONTROLLER_PROTOCOL_VERSION}); "
                if stale_controller
                else ""
            )
            + f"controller script {hello.script_ver} on FL {hello.fl_ver}, "
            f"ppq {hello.ppq}, dropped_frames={dropped}",
            fix=(
                "installed controller is stale vs this server - run fl-mcp-install and restart FL"
                if stale_controller
                else None
            ),
            **liveness,
        )
    if dropped:
        return PlaneStatus(
            connected=False,
            detail=f"ports are open and frames ARRIVE but {dropped} were dropped "
            "as malformed - a codec mismatch, not a missing connection",
            fix="Reinstall the controller script so its embedded codec matches "
            "the server (`python -m fl_mcp.fl.install`), then restart FL Studio.",
            **liveness,
        )
    if fl_process_running():
        return PlaneStatus(
            connected=False,
            detail="FL Studio is running but the controller script has not said hello",
            fix="Close any dialog in FL (its welcome screen blocks the controller), and "
            "confirm the FLMCP controller script is assigned in FL's MIDI settings "
            "(`python -m fl_mcp.fl.install`). Ports self-heal; launch order does not matter.",
            **liveness,
        )
    return PlaneStatus(
        connected=False,
        detail="ports are open; FL Studio is not running",
        fix="Launch FL Studio - the FLMCP controller script connects at startup.",
        **liveness,
    )


def _note_plane_status(*, clock: Callable[[], float] = time.time) -> PlaneStatus:
    scripts_dir = piano_roll_scripts_dir()
    script_installed = scripts_dir is not None and (scripts_dir / PYSCRIPT_NAME).is_file()
    roots = spool_roots()
    if not roots:
        return PlaneStatus(
            connected=False,
            detail="FL Studio's settings folder was not found, so there is no spool",
            fix="Install FL Studio (or run it once) so ~/Documents/Image-Line/FL Studio/Settings exists.",
        )
    if not script_installed:
        return PlaneStatus(
            connected=False,
            detail=f"the '{PYSCRIPT_NAME}' piano-roll script is not installed",
            fix="Run `python -m fl_mcp.fl.install`, then restart FL Studio.",
        )
    for root in roots:
        pid = bridge_pid(root)
        if pid_alive(pid):
            heartbeat = read_heartbeat(root / BRIDGE_HEARTBEAT_FILE, clock=clock)
            if heartbeat.abi == "mismatch":
                detail = "The FL MCP Bridge ABI handshake failed; host dispatch is disabled."
                return PlaneStatus(
                    connected=False,
                    detail=detail,
                    fix=f"{detail} Capture a profile for this FL build and install the rebuilt Bridge.",
                )
            if heartbeat.at is not None and heartbeat.age > BRIDGE_HEARTBEAT_FRESH_SECONDS:
                age = int(heartbeat.age)
                detail = (
                    f"The FL MCP Bridge is loaded but has not ticked for {age} s: "
                    "FL's GUI thread is not pumping."
                )
                return PlaneStatus(
                    connected=False,
                    detail=detail,
                    fix=(
                        f"{detail} On macOS this happens while the screen is locked; "
                        "unlock the Mac, or bring FL out of a modal dialog."
                    ),
                )
            if heartbeat.abi == "ok" and heartbeat.render_rate == 0:
                detail = "FL's audio engine is not running (no output device?)."
                return PlaneStatus(
                    connected=False,
                    detail=detail,
                    fix=f"{detail} Select a persistent audio output device in FL Studio, then confirm render_rate is above zero.",
                )
            stream = _stream_detail(heartbeat)
            if stream is not None:
                return PlaneStatus(connected=True, detail=f"spool at {root}; {stream}")
    return PlaneStatus(connected=True, detail=f"spool at {roots[0]}")


def _stream_detail(heartbeat: object) -> str | None:
    """Keep stream observability in doctor without changing its published shape."""
    streaming = getattr(heartbeat, "streaming", None)
    if streaming is None:
        return None
    chunks = getattr(heartbeat, "stream_chunks_sent", None)
    drops = getattr(heartbeat, "stream_chunks_dropped", None)
    rate = getattr(heartbeat, "stream_sample_rate", None)
    state = "streaming" if streaming else "not streaming"
    return f"live audio {state}; chunks={chunks or 0}, drops={drops or 0}, sample_rate={rate or 0}"


def _project_file_status(session: FLSession) -> PlaneStatus:
    if session.project is None:
        return PlaneStatus(
            connected=False,
            detail="no agent project configured (project-write backend unavailable)",
            fix="Run `python -m fl_mcp.make_agent_project`, or set FLMCP_AGENT_PROJECT "
            "to a .flp the agent may rewrite.",
        )
    if not session.project.is_file():
        return PlaneStatus(
            connected=False,
            detail=f"configured project file does not exist: {session.project}",
            fix="Create it (`python -m fl_mcp.make_agent_project`) or point "
            "FLMCP_AGENT_PROJECT at a real .flp.",
        )
    with session.project.open("rb") as project_file:
        project_file.read(1)
    return PlaneStatus(connected=True, detail=str(session.project))
