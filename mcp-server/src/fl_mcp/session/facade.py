"""FLSession, the one object everything below the SDK talks to FL through,
plus create_session(), THE factory.

Every method is a thin delegation; the behavior lives in the sibling modules
(link, lifecycle, router, notes, controls, state). The division of labor is by
CAPABILITY, chosen per call from evidence (docs/27 §2) - there is no flag.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fl_mcp import errors
from fl_mcp.core.carriers import CarrierUnavailable
from fl_mcp.core.carriers.ctlspool import CtlSpoolControl
from fl_mcp.core.carriers.spool import SpoolCarrier
from fl_mcp.core.carriers.udp import UdpCarrier
from fl_mcp.core.carriers.wire import ControlUnavailable, MidiControl
from fl_mcp.core.transport import Transport
from fl_mcp.errors import NotePlaneUnavailable
from fl_mcp.fl.paths import primary_settings_dir
from fl_mcp.observer import Observer
from fl_mcp.ops import notes as ops_notes
from fl_mcp.schemas import (
    AutomationPoint,
    AutomationReceipt,
    Channel,
    ChannelLevels,
    DoctorReport,
    HelloEvent,
    Note,
    NoteWriteMode,
    PluginParam,
    PluginParamModel,
    SessionState,
    Target,
    WriteReceipt,
)
from fl_mcp.store import Store

from . import controls, lifecycle, state
from . import notes as note_ops
from .detect import (
    BRIDGE_PING_TIMEOUT_SECONDS,
    BridgeBreaker,
    Capabilities,
    CapabilityProfileLike,
    detect,
    load_profile,
)
from .link import ControlCarrier, ControlLink
from .notes import BridgeLane, FileLane

log = logging.getLogger(__name__)

# The project this session may rewrite (the project-write backend's target).
ENV_PROJECT = "FLMCP_AGENT_PROJECT"
DEFAULT_PROJECT_NAME = "FLMCP Agent.flp"


def create_session() -> FLSession:
    """THE factory, called once at the entry point (server lifespan or
    ``sdk.connect()``). Builds the wire (or None when python-rtmidi is missing
    or ports can't open - tools then degrade honestly), the spool, the store,
    and resolves the project file. No lazy construction hidden in getters."""
    # Session mode (FLMCP_SPOOL_ROOT set, e.g. an arena contestant): control
    # rides the pid-addressed ctl file lane (#150). The MIDI wire is never
    # opened - its port names are fixed constants, so two sessions' wires
    # cross-connect nondeterministically, which is worse than no wire.
    spool_override = os.environ.get("FLMCP_SPOOL_ROOT")
    control: ControlCarrier | None
    control_factory: Callable[[], ControlCarrier] | None = None
    if spool_override:
        session_root = Path(spool_override)
        control = CtlSpoolControl(session_root)
        control_factory = lambda: CtlSpoolControl(session_root)  # noqa: E731 - recycle = fresh carrier, same session
    else:
        try:
            control = MidiControl()
        except ControlUnavailable:
            log.exception("control wire unavailable; tools degrade to file/spool backends")
            control = None

    override = os.environ.get(ENV_PROJECT)
    if override:
        project: Path | None = Path(override).expanduser()
    else:
        settings = primary_settings_dir()
        candidate = (
            settings.parent / "Projects" / DEFAULT_PROJECT_NAME if settings is not None else None
        )
        project = candidate if candidate is not None and candidate.is_file() else None

    # Carrier by capability (docs/26 A4, now per CALL): one Transport over the
    # UDP carrier and the file spool, best-capability first, sharing one id
    # space and one consumed-ledger - everything downstream is carrier-blind.
    session = FLSession(
        control=control,
        control_factory=control_factory,
        transport=Transport([UdpCarrier(), SpoolCarrier()]),
        store=Store(project_key=_project_key(project)),
        project=project,
    )
    session.prewarm()  # a live bridge is used from the first write, not the second (#93)
    return session


def _project_key(project: Path | None) -> str:
    """A filesystem-safe, stable store key for a project path.

    Single path component by construction: stem for readability plus a short
    hash of the full path for uniqueness. Passing the raw path was a live bug -
    pathlib's join replaces the base on an absolute right-hand side, pointing
    the store at the .flp file itself."""
    if project is None:
        return "default"
    digest = hashlib.sha1(str(project).encode()).hexdigest()[:8]
    return f"{project.stem}-{digest}"


class FLSession:
    """Everything below the SDK talks to FL through this one object."""

    transport: Transport
    store: Store
    project: Path | None
    link: ControlLink
    breaker: BridgeBreaker
    profile: CapabilityProfileLike
    plugin_param_models: tuple[PluginParamModel, ...]

    def __init__(
        self,
        *,
        control: ControlCarrier | None,
        transport: Transport,
        store: Store,
        project: Path | None,
        control_factory: Callable[[], ControlCarrier] | None = None,
        profile: CapabilityProfileLike | None = None,
        plugin_param_models: tuple[PluginParamModel, ...] = (),
    ) -> None:
        self.link = ControlLink(control, factory=control_factory)
        self.plugin_param_models = plugin_param_models
        self.transport = transport
        self.store = store
        self.project = project
        self.profile = profile if profile is not None else load_profile()
        self.breaker = BridgeBreaker(ping=self._bridge_ping)
        self.file_lane = FileLane(self)
        self.bridge_lane = BridgeLane(self)
        self.observer = Observer(project=project)
        self._write_lock = threading.Lock()

    @property
    def control(self) -> ControlCarrier | None:
        return self.link.control

    @control.setter
    def control(self, value: ControlCarrier | None) -> None:
        # sdk_fakes.FakeSession assigns ``self.control`` without calling this
        # __init__; the first assignment bootstraps its link.
        if getattr(self, "link", None) is None:
            self.link = ControlLink(value)
        else:
            self.link.control = value

    # -- the Routable surface (docs/30 Surface 3) -----------------------------

    @property
    def caps(self) -> Capabilities:
        """Fresh per-op evidence; route() consumes it once per operation."""
        return detect(self.link, self.transport, self.project, self.profile)

    def prewarm(self) -> None:
        """Close the breaker up front when the bridge is already live, so the
        FIRST note write uses the bridge instead of safely-but-needlessly the
        file lane (#93). Non-blocking by construction: detect() only touches
        the wire when a fresh heartbeat says the bridge is up, so an offline
        session never waits."""
        caps = self.caps
        if caps.bridge_live:
            self.breaker.admit(caps.bridge_heartbeat_at)

    @property
    def lanes(self) -> tuple[BridgeLane, FileLane]:
        """Best-capability first: the live bridge, then the file lane."""
        return (self.bridge_lane, self.file_lane)

    # -- notes ----------------------------------------------------------------

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        with self._write_guard("write_notes"):
            if self.project is None:
                raise NotePlaneUnavailable(
                    "No backend can apply targeted notes: no project file is configured, "
                    "and bridge writes require the project file for readback. "
                    "(The pyscript lane cannot target a channel - see write_notes.)"
                )
            # The concurrency rule (docs/27 §5): never clobber a mid-edit user
            # note. Merge writes are purely additive, so they skip the guard.
            if mode != "merge":
                self.observer.guard_write(target)
            return ops_notes.write_notes(self, target, notes, mode)

    def clear_notes(self, target: Target) -> WriteReceipt:
        with self._write_guard("clear_notes"):
            if self.project is None:
                raise NotePlaneUnavailable(
                    "No backend can clear targeted notes: no project file is configured, "
                    "and bridge clears require the project file for readback."
                )
            self.observer.guard_write(target)
            return ops_notes.clear_notes(self, target)

    def read_notes(self, target: Target) -> list[Note]:
        return note_ops.read_notes(self, target)

    # -- controls -------------------------------------------------------------

    def set_tempo(self, bpm: float) -> float:
        with self._write_guard("set_tempo"):
            return controls.set_tempo(self, bpm)

    def render_wav(self, output: Path) -> Path:
        with self._write_guard("render_wav"):
            return controls.render_wav(self, output)

    def set_channel_levels(
        self,
        channel: int,
        *,
        volume: float | None = None,
        pan: float | None = None,
        pitch_semitones: int | None = None,
    ) -> ChannelLevels:
        with self._write_guard("set_channel_levels"):
            return controls.set_channel_levels(
                self, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
            )

    def set_automation(
        self, channel: int, points: list[AutomationPoint]
    ) -> AutomationReceipt:
        with self._write_guard("set_automation"):
            return controls.set_automation(self, channel, points)

    def plugin_params(
        self,
        channel: int,
        *,
        query: str | None = None,
        limit: int = controls.PLUGIN_PARAMS_DEFAULT_LIMIT,
    ) -> list[PluginParam]:
        return controls.plugin_params(self, channel, query=query, limit=limit)

    def set_plugin_param(self, channel: int, param: int | str, value: float | str) -> PluginParam:
        with self._write_guard("set_plugin_param"):
            return controls.set_plugin_param(self, channel, param, value)

    # -- transport (manager sign-off, addendum A11) ---------------------------
    #
    # These exist so the SDK never reaches through to the wire. Each sends the
    # control op and RAISES on failure (link.command is the one conversion).

    def play(self) -> None:
        """``transport.start``."""
        self.link.command("transport.start")

    def stop(self) -> None:
        """``transport.stop``."""
        self.link.command("transport.stop")

    def undo(self) -> None:
        """``general.undo``."""
        self.link.command("general.undo")

    # -- reads ----------------------------------------------------------------

    def snapshot(self) -> SessionState:
        return state.snapshot(self)

    def channel_names(self) -> list[Channel]:
        return state.channel_names(self)

    def doctor(self) -> DoctorReport:
        return state.doctor(self)

    # -- internals shared by the sibling modules ------------------------------

    def _ensure_hello(self) -> HelloEvent | None:
        return self.link.hello()

    def _reload_fl(self) -> None:
        lifecycle.reload_fl(self.project, self.link)

    def _bridge_ping(self) -> bool:
        """The breaker's gate: one stateless spool round trip proving the
        bridge's pumps run (a heartbeat alone cannot - R4 B2-7)."""
        try:
            cmd = self.transport.send("ping")
            return self.transport.await_response(cmd, timeout=BRIDGE_PING_TIMEOUT_SECONDS).ok
        except (errors.ToolError, CarrierUnavailable):
            return False

    @contextmanager
    def _write_guard(self, action: str) -> Iterator[None]:
        if not self._write_lock.acquire(blocking=False):
            raise errors.concurrent_write(action)
        try:
            yield
        finally:
            self._write_lock.release()
