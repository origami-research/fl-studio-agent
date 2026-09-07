"""Shared doubles for the session suite (split along the package seams).

The wire and store are injected fakes (their real implementations are other
lanes' work); the spool is the real one where the protocol matters. The flp
module is patched at its module seam for the project-write backend - Lane F's
real implementation slots in without changing these tests' claims, and the
live gates (G3/G4) exercise the unpatched path.

Fixtures (``no_reload``, the spool isolation) live in the test modules that
use them - importing fixtures trips F811 when they appear as parameters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fl_mcp import errors
from fl_mcp.clock import now
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION
from fl_mcp.core.carriers.wire import MidiControl
from fl_mcp.core.transport import Transport
from fl_mcp.schemas import (
    ControlResponse,
    HelloEvent,
    Note,
    NoteDelta,
    NoteSetRef,
    PluginParamModel,
    SpoolCommand,
    SpoolResponse,
    Target,
)
from fl_mcp.session import FLSession
from fl_mcp.session import detect as detect_mod
from fl_mcp.session.detect import CapabilityProfileLike
from fl_mcp.store import Store

HELLO = HelloEvent(script_ver=CONTROLLER_PROTOCOL_VERSION, fl_ver="2026", ppq=96)
TARGET = Target(pattern=1, channel=0)
NOTES = [Note(key=60, start=0, length=1)]


class FakeControl:
    """The MidiControl surface the session consumes, with scripted answers."""

    def __init__(
        self,
        hello: HelloEvent | None = None,
        handlers: dict[str, Any] | None = None,
        dropped: int = 0,
    ) -> None:
        self.hello = hello
        self.handlers = handlers or {}
        self.dropped_frames = dropped
        self.calls: list[tuple[str, list[Any]]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def await_hello(self, timeout: float = 5.0) -> HelloEvent:
        if self.hello is None:
            raise errors.control_plane_unavailable("hello", timeout)
        return self.hello

    def send_request(
        self,
        op: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> ControlResponse:
        self.calls.append((op, args or []))
        if op == "ping" and "ping" not in self.handlers:
            # Like the real controller: ping answers whenever the wire is alive,
            # which for the fake means "whenever a hello was scripted".
            return ControlResponse(
                id=len(self.calls), ok=self.hello is not None, err=None if self.hello else "dead wire"
            )
        if op == "ctl.save" and op not in self.handlers:
            return ControlResponse(id=len(self.calls), ok=True)
        if op not in self.handlers:
            return ControlResponse(id=len(self.calls), ok=False, err=f"no handler for {op}")
        ret = self.handlers[op](args or [])
        # A handler may script a full failure response (e.g. unsafe-at-current-time).
        if isinstance(ret, ControlResponse):
            return ret
        return ControlResponse(id=len(self.calls), ok=True, ret=ret)


class FakeSpool:
    """Session<->spool contract without the filesystem: scripted responses."""

    def __init__(self, responses: dict[str, SpoolResponse | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._id = 0

    def send(self, op: str, **args: Any) -> SpoolCommand:
        self._id += 1
        self.sent.append((op, args))
        return SpoolCommand(id=self._id, op=op, args={})

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        outcome = self.responses.get(command.op)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise errors.note_plane_unavailable(command.id, timeout)
        return outcome


class FakeStore:
    def __init__(self) -> None:
        self.writes: list[tuple[Target, list[Note], str, str, int]] = []
        self.ground_truths: list[tuple[Target, list[Note]]] = []
        self.preloaded: dict[tuple[int, int], list[Note]] = {}

    def record_write(self, target, notes, mode, backend, command_id) -> NoteDelta:
        self.writes.append((target, notes, mode, backend, command_id))
        return NoteDelta(added=list(notes))

    def notes_at(self, target) -> list[Note] | None:
        return self.preloaded.get((target.pattern, target.channel))

    def mark_ground_truth(self, target, notes) -> NoteSetRef:
        self.ground_truths.append((target, notes))
        return NoteSetRef(id="gt", target=target, provenance="project", as_of=now())


def make_session(
    *,
    control: FakeControl | None = None,
    transport: FakeSpool | Transport | None = None,
    store: FakeStore | None = None,
    project: Path | None = None,
    session_spool_root: Path | None = None,
    profile: CapabilityProfileLike | None = None,
    plugin_param_models: tuple[PluginParamModel, ...] = (),
    platform: Any | None = None,
) -> tuple[FLSession, FakeStore]:
    store = store or FakeStore()
    # The fakes implement exactly the surface FLSession consumes; the casts are
    # honest because every method the session calls exists on the doubles.
    session = FLSession(
        control=cast(MidiControl, control),
        transport=cast(Transport, transport or FakeSpool()),
        store=cast(Store, store),
        project=project,
        session_spool_root=session_spool_root,
        # NEVER the real MidiControl in the fast suite (real ports + open FL
        # segfault rtmidi); a recycle in a test yields another silent fake.
        control_factory=cast(Any, lambda: FakeControl(hello=None)),
        profile=profile,
        plugin_param_models=plugin_param_models,
        platform=platform,
    )
    return session, store


def isolate_spool(monkeypatch) -> None:
    """detect() reads bridge heartbeats under the real spool roots; the fast
    suite must never see this machine's. Call from an autouse fixture; bridge
    tests re-point spool_roots at tmp_path instead."""
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [])
