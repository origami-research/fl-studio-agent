"""hello() under a contended wire (run 7 crash: UnboundLocalError on `hello`)."""

from __future__ import annotations

from typing import Any

from fl_mcp import errors
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION
from fl_mcp.schemas import ControlResponse, HelloEvent
from fl_mcp.session.link import ControlLink


class _BusyBeforeHello:
    """A carrier whose very first hello request finds the shared wire held."""

    dropped_frames = 0

    def is_live(self) -> bool:
        return True

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        raise errors.control_plane_busy("ctl.hello", timeout)

    def send_request(self, op: str, args: Any = None, kwargs: Any = None, timeout: float = 2.0) -> ControlResponse:
        raise AssertionError("no request may follow a busy hello")

    def drain_events(self) -> list[dict[str, Any]]:
        return []

    def add_event_listener(self, fn: Any) -> None:
        return None

    def close(self) -> None:
        return None


def test_busy_wire_before_any_hello_is_not_connected_and_never_recycles() -> None:
    factory_calls: list[int] = []
    link = ControlLink(_BusyBeforeHello(), factory=lambda: factory_calls.append(1) or _BusyBeforeHello())
    assert link.hello() is None
    assert factory_calls == [], "a busy wire is live; it must never be recycled"


def test_busy_ping_after_a_hello_trusts_that_hello() -> None:
    hello = HelloEvent(script_ver=CONTROLLER_PROTOCOL_VERSION, fl_ver="2026", ppq=96)

    class _BusyPing(_BusyBeforeHello):
        def await_hello(self, timeout: float = 5.0) -> HelloEvent:
            return hello

        def send_request(self, op: str, args: Any = None, kwargs: Any = None, timeout: float = 2.0) -> ControlResponse:
            raise errors.control_plane_busy(op, timeout)

    assert ControlLink(_BusyPing(), factory=_BusyPing).hello() is hello
