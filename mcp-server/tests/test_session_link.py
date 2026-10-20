"""The wire link and the session factory: hello/ping liveness, hot-plug
self-heal, settle, reload reconnect, and create_session."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from session_fakes import (
    HELLO,
    NOTES,
    TARGET,
    FakeControl,
    FakePlatform,
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
from fl_mcp.session.link import ControlLink
from fl_mcp.template import template_path


@pytest.fixture(autouse=True)
def isolated_spool(monkeypatch):
    isolate_spool(monkeypatch)


class FakeClock:
    """Injectable monotonic time: starts just after host boot (monotonic counts
    from boot) and only moves when the code under test sleeps."""

    def __init__(self, now: float = 5.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- create_session ----------------------------------------------------------


def test_create_session_is_the_factory(monkeypatch):
    """Never real ports in the fast suite: a REAL MidiControl here made FL
    (when open) hot-plug onto test ports and segfault rtmidi's C layer at
    teardown - the live-found cause of the FL-open suite crashes."""
    fake = FakeControl(hello=HELLO)
    session = create_session(platform=FakePlatform(fake))
    assert isinstance(session, FLSession)
    assert session.control is fake
    assert "/" not in session.store.project_key


class _TwoControlPlatform(FakePlatform):
    """Hands out a silent wire first, then a live one: what a recycle reopens."""

    def __init__(self, first: FakeControl, then: FakeControl) -> None:
        super().__init__(first)
        self._queue = [first, then]

    def control_factory(self):
        return lambda: self._queue.pop(0)


def test_create_session_recycles_through_the_platform_wire(monkeypatch):
    """Every reopen comes from the platform: on Windows, virtual ports (MidiControl's
    default) raise in WinMM."""
    silent, live = FakeControl(hello=None), FakeControl(hello=HELLO)
    silent.close = lambda: None
    session = create_session(platform=_TwoControlPlatform(silent, live))
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)

    assert session._ensure_hello() is HELLO
    assert session.control is live


def test_create_session_ignores_a_missing_project_override(monkeypatch, tmp_path):
    missing = tmp_path / "not-created.flp"
    monkeypatch.setenv("FLMCP_AGENT_PROJECT", str(missing))

    session = create_session(platform=FakePlatform(FakeControl(hello=HELLO)))

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


def test_recycle_is_rate_limited_but_never_refuses_the_first(monkeypatch):
    """An FL that is running but genuinely unconfigured must cost one recycle
    wait per interval, not one per tool call. The FIRST recycle is never
    refused, even 5s after host boot: a 0.0 "never recycled" sentinel read as
    "recycled at boot" and silently skipped it on freshly booted hosts."""
    clock = FakeClock(now=5.0)
    factory_calls: list[float] = []

    def factory():
        factory_calls.append(clock())
        return FakeControl(hello=None)

    link = ControlLink(FakeControl(hello=None), factory=factory, clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)
    monkeypatch.setattr(link_mod, "RECYCLE_HELLO_WAIT_SECONDS", 0.0)

    assert link.hello() is None
    assert link.hello() is None  # immediately again - inside the interval
    assert factory_calls == [5.0]
    clock.now += link_mod.RECYCLE_MIN_INTERVAL_SECONDS
    assert link.hello() is None
    assert factory_calls == [5.0, 35.0]


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

    delayed = DelayedHello()
    clock = FakeClock()
    link = ControlLink(FakeControl(hello=None), factory=lambda: delayed, clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(link_mod, "fl_process_running", lambda: True)

    with caplog.at_level("INFO"):
        assert link.hello() is HELLO

    assert delayed.attempts == 3
    assert delayed.timeouts == [1.0, 2.0, 4.0]
    assert clock.sleeps == [1.0, 2.0]
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
    project = tmp_path / "agent.flp"
    shutil.copyfile(template_path(), project)
    session, _ = make_session(control=old, transport=FakeSpool(), project=project)
    session.link._control_factory = lambda: fresh
    session.link._last_recycle = 12345.0

    session.write_notes(TARGET, NOTES, "merge")

    assert old.closed and session.control is fresh
    assert session.link._last_recycle == float("-inf")


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


# --- transient FL refusals are absorbed by the link ---------------------------

UNSAFE = ControlResponse(id=1, ok=False, err="RuntimeError: Operation unsafe at current time")


def _flaky(failures: list[ControlResponse | Exception], answer: object) -> Any:
    """A controller handler that fails with each scripted failure in turn, then answers."""
    pending = list(failures)

    def handler(args: list[Any]) -> object:
        if pending:
            failure = pending.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        return answer

    return handler


def _link(handlers: dict[str, Any]) -> tuple[ControlLink, FakeControl, FakeClock]:
    clock = FakeClock()
    control = FakeControl(hello=HELLO, handlers=handlers)
    return ControlLink(control, factory=lambda: control, clock=clock, sleep=clock.sleep), control, clock


def test_an_unsafe_refusal_is_retried_with_backoff_until_fl_takes_it():
    """FL's 'unsafe at current time' applied nothing, so the engine retries it
    and the caller never sees the hiccup (owner, 2026-10-18)."""
    link, control, clock = _link({"state.snapshot": _flaky([UNSAFE, UNSAFE], "snap")})
    assert link.call("state.snapshot") == "snap"
    assert [op for op, _ in control.calls] == ["state.snapshot"] * 3
    assert clock.sleeps == list(link_mod.TRANSIENT_RETRY_DELAYS[:2])


def test_a_busy_wire_is_retried_because_the_request_was_never_sent():
    busy = errors.control_plane_busy("mixer.getCurrentTempo", 2.0)
    link, control, clock = _link({"mixer.getCurrentTempo": _flaky([busy], 120_000)})
    assert link.call("mixer.getCurrentTempo") == 120_000
    assert len(control.calls) == 2 and clock.sleeps == [link_mod.TRANSIENT_RETRY_DELAYS[0]]


def test_retries_are_bounded_and_then_one_plain_error_surfaces():
    link, control, clock = _link({"general.undo": lambda args: UNSAFE})
    with pytest.raises(errors.ToolError) as excinfo:
        link.command("general.undo")
    assert excinfo.value.category == "operation_unsafe"
    assert excinfo.value.fix == "Retry it in a few seconds."
    assert len(control.calls) == len(link_mod.TRANSIENT_RETRY_DELAYS) + 1
    assert clock.sleeps == list(link_mod.TRANSIENT_RETRY_DELAYS)
    assert sum(clock.sleeps) < 10, "bounded: a model waits seconds, not minutes"


def test_a_timeout_is_never_retried_because_fl_may_still_apply_it():
    """Issue #47: a parked call can land late, so repeating it could apply twice."""
    timeout = errors.control_plane_unavailable("mixer.setCurrentTempo", 8.0)
    link, control, clock = _link({"mixer.setCurrentTempo": _flaky([timeout], None)})
    with pytest.raises(errors.ToolError) as excinfo:
        link.call("mixer.setCurrentTempo", [100, 1])
    assert excinfo.value.category == "control_plane_unavailable"
    assert len(control.calls) == 1 and clock.sleeps == []


def test_a_real_refusal_is_not_retried():
    boom = ControlResponse(id=1, ok=False, err="TypeError: bad argument")
    link, control, clock = _link({"channels.getChannelName": lambda args: boom})
    with pytest.raises(errors.ToolError) as excinfo:
        link.call("channels.getChannelName", [99])
    assert excinfo.value.category == "fl_api_error"
    assert len(control.calls) == 1 and clock.sleeps == []


def test_set_tempo_lands_through_a_transient_refusal():
    """The whole session path: the model's set_tempo returns FL's tempo, not an error."""
    tempo = {"value": 130_000}
    attempts: list[list[Any]] = []

    def set_tempo(args: list[Any]) -> ControlResponse | None:
        attempts.append(args)
        if len(attempts) == 1:
            return UNSAFE
        tempo["value"] = round(args[0] * 1000)
        return None

    control = FakeControl(
        hello=HELLO,
        handlers={"mixer.setCurrentTempo": set_tempo, "mixer.getCurrentTempo": lambda args: tempo["value"]},
    )
    session, _ = make_session(control=control)
    assert session.set_tempo(100.0) == 100.0
    assert len(attempts) == 2


def test_a_busy_wire_that_waits_out_each_timeout_is_bounded_by_the_window():
    """A busy answer comes only after the call waited its whole timeout for the
    wire, so retries are bounded by elapsed time, not attempts: a model waits
    seconds, never a minute (review of this change, 2026-10-18)."""
    clock = FakeClock()

    def held(args: list[Any]) -> object:
        clock.now += 2.0  # the lock wait a busy wire costs a 2 s read
        raise errors.control_plane_busy("mixer.getCurrentTempo", 2.0)

    control = FakeControl(hello=HELLO, handlers={"mixer.getCurrentTempo": held})
    link = ControlLink(control, factory=lambda: control, clock=clock, sleep=clock.sleep)
    started = clock.now
    with pytest.raises(errors.ToolError) as excinfo:
        link.call("mixer.getCurrentTempo")
    assert excinfo.value.category == "control_plane_busy"
    assert len(control.calls) == 3
    assert clock.now - started <= link_mod.TRANSIENT_RETRY_WINDOW_SECONDS + 2.0
