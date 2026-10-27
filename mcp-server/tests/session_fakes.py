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

import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from fl_mcp import errors
from fl_mcp.contract import FLLicense
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION
from fl_mcp.core.carriers.wire import MidiControl
from fl_mcp.core.transport import Transport
from fl_mcp.platform import Platform
from fl_mcp.schemas import (
    OP_ARGS,
    ControlResponse,
    HelloEvent,
    Note,
    NoteDelta,
    PluginParamModel,
    RenderSpan,
    SpoolCommand,
    SpoolResponse,
    Target,
)
from fl_mcp.session import FLSession
from fl_mcp.session import detect as detect_mod
from fl_mcp.session.detect import CapabilityProfileLike
from fl_mcp.store import Store


def arrangement(
    *, loop_mode: int = 0, tempo_bpm: float = 130.0, song_last_tick: int = 1535, pattern: int = 1, steps: int = 16
) -> dict[str, Callable[[list[Any]], Any]]:
    """FL's live arrangement as the controller answers it, in the units measured
    on FL 2026 (see controls.py): the playlist's last tick, the selected
    pattern's length in 16th steps, tempo as BPM x 1000."""
    return {
        "transport.getLoopMode": lambda args: loop_mode,
        "mixer.getCurrentTempo": lambda args: round(tempo_bpm * 1000),
        "general.getRecPPQ": lambda args: 96,
        "transport.getSongLength": lambda args: song_last_tick if args == [2] else None,
        "patterns.patternNumber": lambda args: pattern,
        "patterns.getPatternLength": lambda args: steps if args == [pattern] else None,
    }


# What a render of the template's four-bar pattern at 130 BPM reports.
RENDER_SPAN = RenderSpan(
    source="pattern",
    pattern=1,
    beats=16.0,
    tempo_bpm=130.0,
    seconds=16 * 60 / 130,
)
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


class FakePlatform:
    """The session's OS seam without the host OS: the fast suite runs on linux
    CI, where the real platform_for() fails closed. Only what the suite drives
    is implemented; anything else fails loudly instead of touching the host."""

    render_executable = Path("/fake/FL/render")

    def __init__(self, control: FakeControl | None = None) -> None:
        self._control = control if control is not None else FakeControl(hello=None)

    def control_factory(self) -> Callable[[], FakeControl]:
        return lambda: self._control

    def render_command(self, project: Path) -> Sequence[str]:
        return (str(self.render_executable), "-R", str(project))


class FakeSpool:
    """Session<->spool contract without the filesystem: scripted responses."""

    def __init__(self, responses: dict[str, SpoolResponse | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._id = 0

    def send(self, op: str, **args: Any) -> SpoolCommand:
        OP_ARGS[op](**args)  # the real Transport validates every send the same way
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


class FakeStore(Store):
    """The real Store (on a throwaway root) that also lists each write it was
    handed, so tests can assert what the session recorded and by which lane."""

    def __init__(self) -> None:
        super().__init__("fake", root=Path(tempfile.mkdtemp(prefix="flmcp-fake-store-")))
        self.writes: list[tuple[Target, list[Note], list[Note], str, str, int]] = []

    def record_write(self, target, before, after, mode, backend, command_id) -> NoteDelta:
        self.writes.append((target, before, after, mode, backend, command_id))
        return super().record_write(target, before, after, mode, backend, command_id)


def make_session(
    *,
    control: FakeControl | None = None,
    transport: FakeSpool | Transport | None = None,
    store: FakeStore | None = None,
    project: Path | None = None,
    session_spool_root: Path | None = None,
    profile: CapabilityProfileLike | None = None,
    plugin_param_models: tuple[PluginParamModel, ...] = (),
    platform: FakePlatform | None = None,
    fl_license: FLLicense = FLLicense.LICENSED,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = lambda seconds: None,
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
        platform=cast(Platform, platform or FakePlatform()),
        fl_license=fl_license,
        clock=clock,
        # The suite never really waits: a retry or a check's polling pause is
        # recorded by the caller's clock double when it needs to observe it.
        monotonic=monotonic,
        sleep=sleep,
    )
    return session, store


def live_untargeted_bridge(monkeypatch, root: Path) -> None:
    """A live Bridge (fresh, compatible heartbeat under ``root``) whose target channel
    the wire cannot verify, so note writes meet the Bridge requirement and the file
    lane serves them: how the file lane's own tests reach it."""
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [root])
    heartbeat = f"unix_time={int(time.time())}\nproto={detect_mod.BRIDGE_PROTO}\nabi=ok\npat=-1\nchan=-1\n"
    (root / detect_mod.BRIDGE_HEARTBEAT_FILE).write_text(heartbeat, encoding="utf-8")


def isolate_spool(monkeypatch) -> None:
    """detect() reads bridge heartbeats under the real spool roots; the fast
    suite must never see this machine's. Call from an autouse fixture; bridge
    tests re-point spool_roots at tmp_path instead."""
    monkeypatch.setattr(detect_mod, "spool_roots", lambda: [])
