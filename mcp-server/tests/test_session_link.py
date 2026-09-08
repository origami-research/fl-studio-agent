"""The wire link and the session factory: hello/ping liveness, hot-plug
self-heal, settle, reload reconnect, and create_session."""

from __future__ import annotations

from pathlib import Path

import pytest
from carrier_fakes import control_platform
from session_fakes import (
    HELLO,
    NOTES,
    TARGET,
    FakeControl,
    FakeSpool,
    isolate_spool,
    make_session,
)

from fl_mcp import errors, flp
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION
from fl_mcp.schemas import ControlResponse
from fl_mcp.session import FLSession, create_session
from fl_mcp.session import lifecycle as lifecycle_mod
from fl_mcp.session import link as link_mod


@pytest.fixture(autouse=True)
def isolated_spool(monkeypatch):
    isolate_spool(monkeypatch)


# --- create_session ----------------------------------------------------------


def test_create_session_is_the_factory(monkeypatch):
    """Never real ports in the fast suite: a REAL MidiControl here made FL
    (when open) hot-plug onto test ports and segfault rtmidi's C layer at
    teardown - the live-found cause of the FL-open suite crashes."""
    fake = FakeControl(hello=HELLO)
    session = create_session(platform=control_platform(fake))
    assert isinstance(session, FLSession)
    assert session.control is fake
    assert "/" not in session.store.project_key


def test_create_session_ignores_a_missing_project_override(monkeypatch, tmp_path):
    missing = tmp_path / "not-created.flp"
    monkeypatch.setenv("FLMCP_AGENT_PROJECT", str(missing))

    session = create_session(platform=control_platform(FakeControl(hello=HELLO)))

    assert session.project is None


def test_project_key_is_a_single_safe_component():
    """The raw project path as a key was a live bug: pathlib's join replaces
    the base on an absolute right-hand side, pointing the store at the .flp."""
    from fl_mcp.session.facade import _project_key

    key = _project_key(Path("/Users/someone/Documents/FLMCP Agent.flp"))
    assert "/" not in key and "\\" not in key
    assert key.startswith("FLMCP Agent-")
    assert _project_key(None) == "default"
    # stable across calls, distinct across paths
    assert key == _project_key(Path("/Users/someone/Documents/FLMCP Agent.flp"))
    assert key != _project_key(Path("/elsewhere/FLMCP Agent.flp"))


def test_project_dir_rejects_path_separators():
    from fl_mcp.fl import paths

    with pytest.raises(ValueError):
        paths.project_dir("/absolute/path.flp")


# --- the hello probe and its self-heal ---------------------------------------


def test_matching_controller_hello_does_not_warn(caplog):
    session, _ = make_session(control=FakeControl(hello=HELLO))

    assert session._ensure_hello() is HELLO
    assert HELLO.script_ver == CONTROLLER_PROTOCOL_VERSION
    assert "controller protocol version mismatch" not in caplog.text


def test_stale_controller_hello_warns_once(caplog):
    stale_hello = HELLO.model_copy(update={"script_ver": "0.0.0"})
    session, _ = make_session(control=FakeControl(hello=stale_hello))

    assert session._ensure_hello() is stale_hello
    assert session._ensure_hello() is stale_hello
    assert caplog.text.count("controller protocol version mismatch") == 1
    assert CONTROLLER_PROTOCOL_VERSION in caplog.text


def test_silent_wire_recycles_ports_when_fl_is_running(monkeypatch):
    """Live-found (2026-08-29): FL binds ports that APPEAR while it runs (~2s)
    but can miss ports that existed at launch. A silent wire with FL up must
    recycle the ports and pick up the hot-plug hello."""
    silent = FakeControl(hello=None)
    silent.closed = False
    silent.close = lambda: setattr(silent, "closed", True)
    healed = FakeControl(hello=HELLO)
    session, _ = make_session(control=silent)
    session.link._control_factory = lambda: healed
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)

    assert session._ensure_hello() is HELLO
    assert silent.closed
    assert session.control is healed


def test_silent_wire_does_not_recycle_when_fl_is_closed(monkeypatch):
    silent = FakeControl(hello=None)
    calls: list[bool] = []
    session, _ = make_session(control=silent)
    session.link._control_factory = lambda: calls.append(True)
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: False)

    assert session._ensure_hello() is None
    assert calls == []


def test_recycle_is_rate_limited(monkeypatch):
    """An FL that is running but genuinely unconfigured must cost one recycle
    wait per interval, not one per tool call."""
    silent = FakeControl(hello=None)
    silent.close = lambda: None
    session, _ = make_session(control=silent)
    factory_calls: list[bool] = []

    def factory():
        factory_calls.append(True)
        still_silent = FakeControl(hello=None)
        still_silent.close = lambda: None
        return still_silent

    session.link._control_factory = factory
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)
    monkeypatch.setattr(link_mod, "RECYCLE_HELLO_WAIT_SECONDS", 0.0)

    assert session._ensure_hello() is None
    assert session._ensure_hello() is None  # immediately again - inside the interval
    assert factory_calls == [True]


def test_recycle_hello_failure_is_logged(monkeypatch, caplog):
    """A hot-plug rebind that never receives hello degrades, but is never silent."""
    silent = FakeControl(hello=None)
    session, _ = make_session(control=silent)
    session.link._control_factory = lambda: FakeControl(hello=None)
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)
    monkeypatch.setattr(link_mod, "RECYCLE_HELLO_WAIT_SECONDS", 0.0)

    with caplog.at_level("ERROR"):
        assert session._ensure_hello() is None

    assert any("FL did not answer hello" in record.message for record in caplog.records)


def test_recycle_retries_hello_until_fl_finishes_booting(monkeypatch, caplog):
    """A cold FL boot must not leave a session disconnected after one hello race."""

    class DelayedHello(FakeControl):
        def __init__(self):
            super().__init__(hello=HELLO)
            self.attempts = 0
            self.timeouts: list[float] = []

        def await_hello(self, timeout=5.0):
            self.attempts += 1
            self.timeouts.append(timeout)
            if self.attempts < 3:
                raise errors.control_plane_unavailable("ctl.hello", timeout)
            return HELLO

    silent = FakeControl(hello=None)
    delayed = DelayedHello()
    session, _ = make_session(control=silent)
    session.link._control_factory = lambda: delayed
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)
    monkeypatch.setattr(link_mod.time, "sleep", lambda _seconds: None)

    with caplog.at_level("INFO"):
        assert session._ensure_hello() is HELLO

    assert delayed.attempts == 3
    assert delayed.timeouts == [1.0, 2.0, 4.0]
    assert sum("awaiting FL hello after port recycle" in r.message for r in caplog.records) == 3
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_sticky_hello_is_not_trusted_without_a_live_ping(monkeypatch):
    """After FL dies, the wire still holds its last hello - trusting it routed
    live ops into a void (dogfood A12). A dead ping must read as disconnected."""

    class DeadWire(FakeControl):
        def send_request(self, op, args=None, kwargs=None, timeout=2.0):
            return ControlResponse(id=1, ok=False, err="no response")

    session, _ = make_session(control=DeadWire(hello=HELLO))
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: False)

    assert session._ensure_hello() is None


def test_reload_reconnects_the_wire_fresh(monkeypatch, tmp_path):
    """Our own reload kills FL and the old ports; a fresh wire after the
    relaunch hits FL's reliable hot-plug bind and must replace the dead one."""
    monkeypatch.setattr(flp, "write_notes", lambda path, notes, *, pattern, channel, mode: list(notes))
    monkeypatch.setattr(lifecycle_mod, "fl_process_running", lambda: False)  # skip the pkill branch
    monkeypatch.setattr(lifecycle_mod, "launch_fl", lambda project: None)
    monkeypatch.setattr(link_mod, "FL_BOOT_TIMEOUT_SECONDS", 0.0)  # no FL to settle on
    old = FakeControl(hello=HELLO)
    fresh = FakeControl(hello=HELLO)
    session, _ = make_session(control=old, transport=FakeSpool(), project=tmp_path / "agent.flp")
    session.link._control_factory = lambda: fresh
    session.link._last_recycle = 12345.0

    session.write_notes(TARGET, NOTES, "merge")

    assert old.closed and session.control is fresh
    assert session.link._last_recycle == 0.0


# --- settle after reload (issue #47 / experiment E7) --------------------------


def test_settle_returns_once_snapshot_answers(monkeypatch):
    answers = iter([False, False, True])
    control = FakeControl(
        handlers={"state.snapshot": lambda args: ControlResponse(id=1, ok=next(answers), err=None)},
    )
    session, _ = make_session(control=control)
    monkeypatch.setattr(link_mod, "SETTLE_POLL_SECONDS", 0.0)
    session.link.settle()
    snapshot_calls = [c for c in control.calls if c[0] == "state.snapshot"]
    assert len(snapshot_calls) == 3


def test_settle_times_out_quietly_when_fl_never_answers(monkeypatch, caplog):
    control = FakeControl(handlers={})  # snapshot always errors
    session, _ = make_session(control=control)
    monkeypatch.setattr(link_mod, "SETTLE_POLL_SECONDS", 0.0)
    monkeypatch.setattr(link_mod, "FL_BOOT_TIMEOUT_SECONDS", 0.05)
    session.link.settle()  # must not raise: the reload itself succeeded
    assert any("settle" in r.message for r in caplog.records)
