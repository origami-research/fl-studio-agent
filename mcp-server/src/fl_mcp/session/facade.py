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
from collections.abc import Callable
from pathlib import Path

from fl_mcp import bridge_wire, errors
from fl_mcp.bridge_wire import BridgeWire
from fl_mcp.flpaths import primary_settings_dir
from fl_mcp.observer import Observer
from fl_mcp.schemas import (
    Channel,
    ChannelLevels,
    DoctorReport,
    HelloEvent,
    Note,
    NoteWriteMode,
    PluginParam,
    SessionState,
    Target,
    WriteReceipt,
)
from fl_mcp.spool import Spool, SpoolUnavailable
from fl_mcp.store import Store
from fl_mcp.wire import ControlUnavailable, MidiControl

from . import controls, lifecycle, state
from . import notes as note_ops
from .link import ControlLink
from .notes import BridgeLane, FileLane
from .router import (
    BRIDGE_PING_TIMEOUT_SECONDS,
    BridgeBreaker,
    CapabilityProfileLike,
    load_profile,
)

log = logging.getLogger(__name__)

# The project this session may rewrite (the project-write backend's target).
ENV_PROJECT = "FLMCP_AGENT_PROJECT"
DEFAULT_PROJECT_NAME = "FLMCP Agent.flp"


def create_session() -> FLSession:
    """THE factory, called once at the entry point (server lifespan or
    ``sdk.connect()``). Builds the wire (or None when python-rtmidi is missing
    or ports can't open - tools then degrade honestly), the spool, the store,
    and resolves the project file. No lazy construction hidden in getters."""
    try:
        control: MidiControl | None = MidiControl()
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

    # Carrier by capability (docs/26 A4): the bridge's UDP endpoint when its
    # advert is fresh and it answers a ping, the file spool otherwise. Both are
    # Spool-shaped (send/await_response, shared id space, one consumed-ledger),
    # so everything downstream is carrier-blind.
    return FLSession(
        control=control,
        spool=bridge_wire.connect() or Spool(),
        store=Store(project_key=_project_key(project)),
        project=project,
    )


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

    spool: Spool
    store: Store
    project: Path | None
    link: ControlLink
    breaker: BridgeBreaker
    profile: CapabilityProfileLike

    def __init__(
        self,
        *,
        control: MidiControl | None,
        spool: Spool | BridgeWire,
        store: Store,
        project: Path | None,
        control_factory: Callable[[], MidiControl] | None = None,
        profile: CapabilityProfileLike | None = None,
    ) -> None:
        self.link = ControlLink(control, factory=control_factory)
        self.spool = spool
        self.store = store
        self.project = project
        self.profile = profile if profile is not None else load_profile()
        self.breaker = BridgeBreaker(ping=self._bridge_ping)
        self.file_lane = FileLane(self)
        self.bridge_lane = BridgeLane(self)
        self.observer = Observer(project=project)

    @property
    def control(self) -> MidiControl | None:
        return self.link.control

    @control.setter
    def control(self, value: MidiControl | None) -> None:
        # sdk_fakes.FakeSession assigns ``self.control`` without calling this
        # __init__; the first assignment bootstraps its link.
        if getattr(self, "link", None) is None:
            self.link = ControlLink(value)
        else:
            self.link.control = value

    # -- notes ----------------------------------------------------------------

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        return note_ops.write_notes(self, target, notes, mode)

    def clear_notes(self, target: Target) -> WriteReceipt:
        return note_ops.clear_notes(self, target)

    def read_notes(self, target: Target) -> list[Note]:
        return note_ops.read_notes(self, target)

    # -- controls -------------------------------------------------------------

    def set_tempo(self, bpm: float) -> float:
        return controls.set_tempo(self, bpm)

    def set_channel_levels(
        self,
        channel: int,
        *,
        volume: float | None = None,
        pan: float | None = None,
        pitch_semitones: int | None = None,
    ) -> ChannelLevels:
        return controls.set_channel_levels(
            self, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
        )

    def plugin_params(
        self,
        channel: int,
        *,
        query: str | None = None,
        limit: int = controls.PLUGIN_PARAMS_DEFAULT_LIMIT,
    ) -> list[PluginParam]:
        return controls.plugin_params(self, channel, query=query, limit=limit)

    def set_plugin_param(self, channel: int, param: int | str, value: float) -> PluginParam:
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
            cmd = self.spool.send("ping")
            return self.spool.await_response(cmd, timeout=BRIDGE_PING_TIMEOUT_SECONDS).ok
        except (errors.ToolError, SpoolUnavailable):
            return False
