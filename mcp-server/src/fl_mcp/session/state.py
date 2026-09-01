"""Reads and the doctor report: snapshot, channel_names, doctor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_mcp import errors, flp
from fl_mcp.core.carriers.spool import spool_roots
from fl_mcp.fl.paths import piano_roll_scripts_dir
from fl_mcp.fl.process import fl_process_running
from fl_mcp.schemas import Channel, DoctorReport, PlaneStatus, SessionState

if TYPE_CHECKING:
    from .facade import FLSession

# state.snapshot arrives as ~10 chunked SysEx frames (measured ~4.8KB on a
# 5-channel/18-track project, addendum A5); give bigger projects headroom.
SNAPSHOT_TIMEOUT_SECONDS = 10.0

PYSCRIPT_NAME = "MCP Write Notes.pyscript"


def channel_names(session: FLSession) -> list[Channel]:
    """The channels of the TARGET universe - the project file - for name
    resolution. File-first because targets are file coordinates and the
    file answers instantly mid-reload; the live snapshot is the fallback
    for sessions without a project."""
    if session.project is not None:
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


def snapshot(session: FLSession) -> SessionState:
    """Live: ONE composite ``state.snapshot`` round trip (chunked response;
    v1's transport silently dropped it, Lane W fixed the routing - addendum
    A5). Else: decoded from the project file. One backend per call, so a
    stale file never contaminates a live view or vice versa."""
    if session._ensure_hello() is not None and session.control is not None:
        resp = session.control.send_request("state.snapshot", timeout=SNAPSHOT_TIMEOUT_SECONDS)
        if not resp.ok:
            detail = resp.err or "no detail"
            if errors.is_operation_unsafe(detail):
                raise errors.operation_unsafe("state.snapshot")
            raise errors.fl_api_error("state.snapshot", detail)
        try:
            return SessionState.from_wire(resp.ret)
        except (KeyError, TypeError, ValueError) as exc:
            raise errors.fl_api_error(
                "state.snapshot", f"malformed snapshot payload ({exc!r})"
            ) from exc

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

    raise errors.ToolError(
        "control_plane_unavailable",
        "No backend can read session state: the control plane is not connected and "
        "no project file is configured.",
        "Launch FL Studio with the FLMCP controller script assigned, or run "
        "`python -m fl_mcp.make_agent_project` / set FLMCP_AGENT_PROJECT to a .flp "
        "the agent may read.",
        retryable=True,
    )


def doctor(session: FLSession) -> DoctorReport:
    """Typed report - control plane, note plane, project file - with a
    concrete ``fix`` string per degraded plane."""
    if session.control is None:
        control_plane = PlaneStatus(
            connected=False,
            detail="no MIDI control wire: python-rtmidi is missing or the virtual "
            "ports could not be opened",
            fix="Install python-rtmidi (`uv pip install python-rtmidi`) and restart "
            "the MCP server.",
        )
    else:
        # Captured BEFORE the probe: the hello probe may recycle a silent
        # wire, and the old wire's dropped-frame count is the diagnostic.
        dropped = session.control.dropped_frames
        hello = session._ensure_hello()
        if hello is not None:
            control_plane = PlaneStatus(
                connected=True,
                detail=f"controller script {hello.script_ver} on FL {hello.fl_ver}, "
                f"ppq {hello.ppq}, dropped_frames={dropped}",
            )
        elif dropped:
            control_plane = PlaneStatus(
                connected=False,
                detail=f"ports are open and frames ARRIVE but {dropped} were dropped "
                "as malformed - a codec mismatch, not a missing connection",
                fix="Reinstall the controller script so its embedded codec matches "
                "the server (`python -m fl_mcp.fl.install`), then restart FL Studio.",
            )
        elif fl_process_running():
            control_plane = PlaneStatus(
                connected=False,
                detail="FL Studio is running but the controller script has not said hello",
                fix="Close any dialog in FL (its welcome screen blocks the controller), and "
                "confirm the FLMCP controller script is assigned in FL's MIDI settings "
                "(`python -m fl_mcp.fl.install`). Ports self-heal; launch order does not matter.",
            )
        else:
            control_plane = PlaneStatus(
                connected=False,
                detail="ports are open; FL Studio is not running",
                fix="Launch FL Studio - the FLMCP controller script connects at startup.",
            )

    scripts_dir = piano_roll_scripts_dir()
    script_installed = scripts_dir is not None and (scripts_dir / PYSCRIPT_NAME).is_file()
    if not spool_roots():
        note_plane = PlaneStatus(
            connected=False,
            detail="FL Studio's settings folder was not found, so there is no spool",
            fix="Install FL Studio (or run it once) so "
            "~/Documents/Image-Line/FL Studio/Settings exists.",
        )
    elif not script_installed:
        note_plane = PlaneStatus(
            connected=False,
            detail=f"the '{PYSCRIPT_NAME}' piano-roll script is not installed",
            fix="Run `python -m fl_mcp.fl.install`, then restart FL Studio.",
        )
    else:
        note_plane = PlaneStatus(
            connected=True,
            detail=f"spool at {spool_roots()[0]} (the bridge lane routes automatically "
            "when its heartbeat proves it live and the selected channel matches; "
            "otherwise targeted notes apply via the project file)",
        )

    if session.project is None:
        project_file = PlaneStatus(
            connected=False,
            detail="no agent project configured (project-write backend unavailable)",
            fix="Run `python -m fl_mcp.make_agent_project`, or set FLMCP_AGENT_PROJECT "
            "to a .flp the agent may rewrite.",
        )
    elif not session.project.is_file():
        project_file = PlaneStatus(
            connected=False,
            detail=f"configured project file does not exist: {session.project}",
            fix="Create it (`python -m fl_mcp.make_agent_project`) or point "
            "FLMCP_AGENT_PROJECT at a real .flp.",
        )
    else:
        project_file = PlaneStatus(connected=True, detail=str(session.project))

    all_connected = control_plane.connected and note_plane.connected and project_file.connected
    return DoctorReport(
        status="ok" if all_connected else "degraded",
        control_plane=control_plane,
        note_plane=note_plane,
        project_file=project_file,
    )
