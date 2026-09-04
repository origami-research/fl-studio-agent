"""Reads: snapshot (one backend per call) and the doctor report."""

from __future__ import annotations

from pathlib import Path

import pytest
from session_fakes import HELLO, FakeControl, make_session

from fl_mcp import errors, flp
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION
from fl_mcp.core.carriers.broker_client import BrokerControl

# --- snapshot ----------------------------------------------------------------


def test_snapshot_from_project_file(monkeypatch, tmp_path):
    channel = flp.FlpChannel(index=0, name="Lead", volume=10000, pan=6400, pitch_semitones=0)
    monkeypatch.setattr(
        flp,
        "read",
        lambda path: flp.FlpProject(ppq=192, tempo=140.0, channels=(channel,), notes=()),
    )
    session, _ = make_session(project=tmp_path / "agent.flp")

    state = session.snapshot()

    assert state.ppq == 192
    assert state.tempo_bpm == 140.0
    assert state.current_pattern is None, "the live pattern is unknown on the file lane"
    assert state.channels[0].name == "Lead"
    assert state.channels[0].volume == pytest.approx(0.78125)
    assert state.channels[0].pan == 0.0


SNAPSHOT_PAYLOAD = {
    "ppq": 96,
    "tempo": 128000,  # getCurrentTempo: BPM x 1000
    "playing": True,
    "song_pos": 0.0,
    "current_pattern": 3,
    "pattern_count": 4,
    "channels": [
        {"idx": 0, "name": "Kick", "muted": False, "vol": 0.78, "pan": 0.0, "target_mixer_track": 1}
    ],
    "mixer": [
        {"idx": 0, "name": "Master", "vol": 0.8, "pan": 0.0, "muted": False, "solo": False},
        {"idx": 1, "name": "Kick bus", "vol": 0.7, "pan": -0.1, "muted": True, "solo": False},
    ],
}


def test_snapshot_live_uses_one_composite_round_trip(monkeypatch):
    """Addendum A5: state.snapshot works in v2 (Lane W fixed chunk routing);
    the session must NOT read the file when the live plane answers."""
    monkeypatch.setattr(flp, "read", lambda path: pytest.fail("live snapshot must not touch the file"))
    control = FakeControl(hello=HELLO, handlers={"state.snapshot": lambda args: SNAPSHOT_PAYLOAD})
    session, _ = make_session(control=control, project=Path("/tmp/agent.flp"))

    state = session.snapshot()

    # the liveness ping precedes it (sticky-hello confirmation, A12)
    assert [c for c in control.calls if c[0] != "ping"] == [("state.snapshot", [])], (
        "exactly one composite round trip"
    )
    assert state.tempo_bpm == 128.0
    assert state.ppq == 96
    assert state.playing is True
    assert state.current_pattern == 3
    assert [c.name for c in state.channels] == ["Kick"]
    assert [t.name for t in state.mixer] == ["Master", "Kick bus"]
    assert state.mixer[1].muted is True


def test_snapshot_live_malformed_payload_is_a_typed_error():
    control = FakeControl(hello=HELLO, handlers={"state.snapshot": lambda args: {"ppq": 96}})
    session, _ = make_session(control=control)

    with pytest.raises(errors.ToolError) as excinfo:
        session.snapshot()
    assert excinfo.value.category == "fl_api_error"
    assert "malformed" in excinfo.value.message


def test_snapshot_without_live_or_project_is_an_explicit_setup_error():
    session, _ = make_session()

    with pytest.raises(errors.ToolError) as excinfo:
        session.snapshot()

    assert excinfo.value.category == "control_plane_unavailable"
    assert "No backend can read session state" in excinfo.value.message
    assert "make_agent_project" in excinfo.value.fix


# --- doctor ------------------------------------------------------------------


def test_doctor_all_degraded_names_a_fix_per_plane(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    session, _ = make_session()

    report = session.doctor()

    assert report.status == "degraded"
    assert report.control_plane.connected is False
    assert "python-rtmidi" in report.control_plane.fix
    assert report.note_plane.connected is False
    assert report.note_plane.fix
    assert report.project_file.connected is False
    assert "make_agent_project" in report.project_file.fix


def test_doctor_reports_probe_failures_instead_of_raising(tmp_path):
    """Doctor remains usable when the control wire and project file are both broken."""

    class ExplodingControl:
        @property
        def dropped_frames(self):
            raise RuntimeError("control link exploded")

        def await_hello(self, timeout=5.0):
            raise RuntimeError("control link exploded")

        def send_request(self, *args, **kwargs):
            raise RuntimeError("control link exploded")

        def close(self):
            raise RuntimeError("control link exploded")

    session, _ = make_session(control=ExplodingControl(), project=tmp_path / "missing.flp")  # type: ignore[arg-type]

    report = session.doctor()

    assert report.status == "degraded"
    assert report.control_plane.connected is False
    assert "control link exploded" in report.control_plane.fix
    assert report.project_file.connected is False
    assert "does not exist" in report.project_file.detail


def test_doctor_all_connected(tmp_path, monkeypatch):
    settings = tmp_path / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    scripts = settings / "Piano roll scripts"
    scripts.mkdir(parents=True)
    (scripts / "MCP Write Notes.pyscript").write_text("# installed", encoding="utf-8")
    project = tmp_path / "agent.flp"
    project.write_bytes(b"FLhd")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    session, _ = make_session(control=FakeControl(hello=HELLO, dropped=3), project=project)
    report = session.doctor()

    assert report.status == "ok"
    assert report.control_plane.connected is True
    assert "dropped_frames=3" in report.control_plane.detail
    assert report.note_plane.connected is True
    assert report.project_file.detail == str(project)


def test_doctor_reports_a_stale_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    stale_hello = HELLO.model_copy(update={"script_ver": "0.0.0"})
    session, _ = make_session(control=FakeControl(hello=stale_hello))

    report = session.doctor()

    assert report.control_plane.connected is True
    assert CONTROLLER_PROTOCOL_VERSION in report.control_plane.detail
    assert report.control_plane.fix == (
        "installed controller is stale vs this server - run fl-mcp-install and restart FL"
    )


def test_doctor_dropped_frames_without_hello_points_at_the_codec(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    session, _ = make_session(control=FakeControl(hello=None, dropped=17))

    report = session.doctor()

    assert report.control_plane.connected is False
    assert "17" in report.control_plane.detail
    assert "codec" in report.control_plane.detail or "codec" in report.control_plane.fix


def test_doctor_session_mode_silent_hello_is_not_a_codec_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    control = BrokerControl(tmp_path / "session")
    session, _ = make_session(control=control)  # type: ignore[arg-type]

    report = session.doctor()

    assert report.control_plane.connected is False
    assert "codec" not in report.control_plane.detail.lower()


def test_doctor_reports_a_dead_session_bridge_pid(tmp_path, monkeypatch):
    """The port file is evidence, not a cached control-carrier PID."""
    from fl_mcp.session import state as state_mod

    session, _ = make_session()
    session.session_spool_root = tmp_path
    (tmp_path / "bridge_udp.json").write_text('{"pid": 4321}')
    monkeypatch.setattr(state_mod, "pid_alive", lambda pid: False)

    report = session.doctor()

    assert report.control_plane.fl_pid == 4321
    assert report.control_plane.fl_pid_alive is False
    assert report.status != "ok", "a dead FL pid must never report an ok session"

def test_doctor_silent_wire_fix_names_the_dialog_not_a_restart(tmp_path, monkeypatch):
    """Ports self-heal now (hot-plug, docs/21 A12), so 'restart FL' is no longer
    the advice - the remaining silent-wire causes are a blocking dialog or a
    missing controller assignment."""
    from fl_mcp.session import link as link_mod
    from fl_mcp.session import state as state_mod

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(state_mod, "fl_process_running", lambda: True)
    # The probe recycles the silent wire (FL "running"); make_session's
    # factory hands back another silent fake, exactly like the old monolith.
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)
    session, _ = make_session(control=FakeControl(hello=None))

    report = session.doctor()

    assert report.control_plane.connected is False
    assert "dialog" in report.control_plane.fix.lower()
    assert "launch order does not matter" in report.control_plane.fix.lower()
