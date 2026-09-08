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
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fl_mcp import errors, flp
from fl_mcp.audio_stream import AudioChunk, AudioEndpoint
from fl_mcp.contract import ENV_AGENT_PROJECT, ENV_SPOOL_ROOT
from fl_mcp.core.carriers import CarrierUnavailable
from fl_mcp.core.carriers.broker_client import BrokerControl
from fl_mcp.core.carriers.spool import SpoolCarrier
from fl_mcp.core.carriers.udp import UdpCarrier
from fl_mcp.core.carriers.wire import ControlUnavailable
from fl_mcp.core.transport import Transport
from fl_mcp.errors import NotePlaneUnavailable
from fl_mcp.fl.paths import primary_settings_dir
from fl_mcp.fl.process import bridge_pid
from fl_mcp.observer import Observer
from fl_mcp.ops import notes as ops_notes
from fl_mcp.platform import Platform, platform_for
from fl_mcp.plugin_models import load_bundled_param_models
from fl_mcp.profile import profile_gate
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
    read_heartbeat,
)
from .link import ControlCarrier, ControlLink
from .notes import BridgeLane, FileLane

log = logging.getLogger(__name__)

# The project this session may rewrite (the project-write backend's target).
ENV_PROJECT = ENV_AGENT_PROJECT
DEFAULT_PROJECT_NAME = "FLMCP Agent.flp"
ENV_AUDIO_STREAM = "FLMCP_AUDIO_STREAM"
_AUDIO_STREAM_POLL_SECONDS = 0.1
_AUDIO_STREAM_RESPONSE_TIMEOUT_SECONDS = 2.0
# Rendering depends on saved, read-back project state. A write that is already
# landing usually finishes quickly, so wait once rather than turning that
# ordinary serialization into a failed render request.
RENDER_IN_FLIGHT_WRITE_WAIT_SECONDS = 15.0


def create_session(
    *, plugin_param_models: tuple[PluginParamModel, ...] | None = None, platform: Platform | None = None
) -> FLSession:
    """THE factory, called once at the entry point (server lifespan or
    ``sdk.connect()``). Builds the wire (or None when python-rtmidi is missing
    or ports can't open - tools then degrade honestly), the spool, the store,
    and resolves the project file. No lazy construction hidden in getters."""
    # Session mode (FLMCP_SPOOL_ROOT set, e.g. an arena contestant) uses
    # BrokerControl: the machine-wide MIDI wire broadcasts pid-addressed
    # requests through one bind-wins broker.
    platform = platform or platform_for(sys.platform)
    spool_override = os.environ.get(ENV_SPOOL_ROOT)
    control: ControlCarrier | None
    control_factory: Callable[[], ControlCarrier] | None = None
    if spool_override:
        session_root = Path(spool_override)
        endpoint = platform.broker_endpoint()
        control = BrokerControl(session_root, endpoint)
        control_factory = lambda: BrokerControl(session_root, endpoint)  # noqa: E731 - recycle = fresh carrier, same session
    else:
        try:
            control = platform.control_factory()()
        except ControlUnavailable:
            log.exception("control wire unavailable; tools degrade to file/spool backends")
            control = None

    override = os.environ.get(ENV_PROJECT)
    if override:
        candidate = Path(override).expanduser()
        project = candidate if candidate.is_file() else None
    else:
        settings = primary_settings_dir()
        candidate = settings.parent / "Projects" / DEFAULT_PROJECT_NAME if settings is not None else None
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
        plugin_param_models=(
            load_bundled_param_models() if plugin_param_models is None else plugin_param_models
        ),
        session_spool_root=Path(spool_override) if spool_override else None,
        platform=platform,
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
        session_spool_root: Path | None = None,
        platform: Platform | None = None,
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
        self.session_spool_root = session_spool_root
        self.platform = platform or platform_for(sys.platform)
        self._profile_supported, self._profile_gate_detail = profile_gate()
        if not self._profile_supported:
            log.warning("%s", self._profile_gate_detail)
        self._write_lock = threading.Lock()
        # A session has one Bridge transport.  Its staged send/await API must
        # stay one transaction at this boundary: the optional audio worker is
        # a second client of that transport, not an independent connection.
        # This keeps a lifecycle ack from competing with prewarm or a tool.
        self._transport_lock = threading.Lock()
        self._write_state_lock = threading.Lock()
        self._write_action: str | None = None
        self._write_owner_thread: int | None = None
        self._run_refusals = 0
        # Development-mode file-edit batching: file-lane ops write the .flp
        # but defer the FL reload so N edits cost ONE reboot, not N. Session
        # mode refuses this lane before a file mutation because FL has no
        # verified in-place project reload API.
        self._reload_batch_depth = 0
        self._reload_pending = False
        self._file_write_prepared = False
        self._audio_stream_stop = threading.Event()
        self._audio_stream_thread: threading.Thread | None = None
        self._audio_endpoint: AudioEndpoint | None = None
        self._audio_bridge_pid: int | None = None

    @property
    def run_refusals(self) -> int:
        with self._write_state_lock:
            return self._run_refusals

    def record_run_refusal(self) -> None:
        with self._write_state_lock:
            self._run_refusals += 1

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

    def start_audio_stream_lifecycle(self) -> None:
        """Publish session audio once a live Bridge is ready, when enabled."""
        if os.environ.get(ENV_AUDIO_STREAM) != "1" or self.session_spool_root is None:
            return
        if self._audio_stream_thread is not None:
            return
        self._audio_stream_thread = threading.Thread(
            target=self._run_audio_stream_lifecycle, name="fl-mcp-audio-lifecycle", daemon=True
        )
        self._audio_stream_thread.start()

    def close(self) -> None:
        """Stop session-owned services before closing the control connection."""
        self._audio_stream_stop.set()
        if self._audio_stream_thread is not None:
            self._audio_stream_thread.join(timeout=_AUDIO_STREAM_RESPONSE_TIMEOUT_SECONDS + 1)
        self._stop_audio_stream()
        if self.control is not None:
            self.control.close()

    def _run_audio_stream_lifecycle(self) -> None:
        while not self._audio_stream_stop.wait(_AUDIO_STREAM_POLL_SECONDS):
            root = self.session_spool_root
            if root is None:
                return
            heartbeat = read_heartbeat(root / "bridge_heartbeat.txt")
            pid = bridge_pid(root)
            if self._audio_endpoint is not None and pid != self._audio_bridge_pid:
                self._stop_audio_stream(send_stop=False)
            live = (
                pid is not None and heartbeat.age <= 10.0 and heartbeat.proto == 2 and heartbeat.abi == "ok"
            )
            if not live:
                continue
            if self._audio_endpoint is None:
                self._start_audio_stream(pid)

    def _start_audio_stream(self, pid: int) -> None:
        root = self.session_spool_root
        if root is None:
            return
        endpoint = AudioEndpoint(root)
        try:
            endpoint.start(publish=False)
            with self._transport_use():
                command = self.transport.send("stream_start", port=endpoint.udp_port)
                response = self.transport.await_response(command, timeout=_AUDIO_STREAM_RESPONSE_TIMEOUT_SECONDS)
            if not response.ok:
                endpoint.close()
                log.info("live audio stream start refused: %s", response.err or "Bridge refused stream_start")
                return
            if self._audio_stream_stop.is_set():
                endpoint.close()
                return
            endpoint.publish(response.sample_rate or 44_100)
            self._audio_endpoint = endpoint
            self._audio_bridge_pid = pid
            log.info("live audio stream published for Bridge pid %d", pid)
        except Exception:
            endpoint.close()
            log.info("live audio stream start deferred", exc_info=True)

    def _stop_audio_stream(self, *, send_stop: bool = True) -> None:
        endpoint, self._audio_endpoint = self._audio_endpoint, None
        self._audio_bridge_pid = None
        if endpoint is None:
            return
        if send_stop:
            try:
                with self._transport_use():
                    command = self.transport.send("stream_stop")
                    self.transport.await_response(command, timeout=_AUDIO_STREAM_RESPONSE_TIMEOUT_SECONDS)
            except Exception:
                log.info("live audio stream stop was not acknowledged", exc_info=True)
        endpoint.close()

    @property
    def lanes(self) -> tuple[BridgeLane, FileLane]:
        """Best-capability first: the live bridge, then the file lane."""
        return (self.bridge_lane, self.file_lane)

    # -- notes ----------------------------------------------------------------

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        with self._write_guard("write_notes"):
            if self.project is None and not self.caps.bridge_live:
                raise NotePlaneUnavailable(
                    "No backend can apply targeted notes: no project file is configured and the Bridge is not live. "
                    "(The pyscript lane cannot target a channel - see write_notes.)"
                )
            # The concurrency rule (docs/27 §5): never clobber a mid-edit user
            # note. Merge writes are purely additive, so they skip the guard.
            if mode != "merge":
                self.observer.guard_write(target)
            return ops_notes.write_notes(self, target, notes, mode)

    def clear_notes(self, target: Target) -> WriteReceipt:
        with self._write_guard("clear_notes"):
            if self.project is None and not self.caps.bridge_live:
                raise NotePlaneUnavailable(
                    "No backend can clear targeted notes: no project file is configured and the Bridge is not live."
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

    def export_offline(self, output: Path) -> Path:
        with self._write_guard("export_offline"):
            return controls.export_offline(self, output)

    @contextmanager
    def audio_stream(self) -> Iterator[Iterator[AudioChunk]]:
        """Yield live master-mix chunks; every gap remains visible in metadata."""
        if self.session_spool_root is None:
            raise errors.ToolError(
                "audio_stream_unavailable", "Live streaming requires a session spool root."
            )
        endpoint = AudioEndpoint(self.session_spool_root)
        endpoint.start(publish=False)
        with self._transport_use():
            command = self.transport.send("stream_start", port=endpoint.udp_port)
            response = self.transport.await_response(command, timeout=5)
        if not response.ok:
            endpoint.close()
            raise errors.ToolError(
                "audio_stream_unavailable", response.err or "Bridge refused stream_start", retryable=True
            )
        endpoint.publish(response.sample_rate or 44_100)
        try:
            yield endpoint.chunks()
        finally:
            try:
                with self._transport_use():
                    command = self.transport.send("stream_stop")
                    self.transport.await_response(command, timeout=5)
            finally:
                endpoint.close()

    def save_project(self) -> int | None:
        with self._write_guard("save_project"):
            return controls.save_project(self)

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

    def add_effect(self, insert_index: int, plugin_name: str) -> tuple[flp.Effect, ...]:
        """Add an FL-authored native effect record and return its file readback."""
        with self._write_guard("add_effect"):
            if self.project is None:
                raise NotePlaneUnavailable(
                    "No project file is configured for adding an effect. "
                    "Effect records can only be written through the project file."
                )
            self._prepare_file_write()
            confirmed = tuple(flp.add_effect(self.project, insert_index, plugin_name))
            self._reload_fl()
            return confirmed

    def set_automation(self, channel: int, points: list[AutomationPoint]) -> AutomationReceipt:
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
        # Inside a file-edit batch, coalesce: each edit already wrote the .flp;
        # the single reload on batch exit syncs FL once. A reload is a SIGKILL
        # + relaunch, so doing it per edit would reboot the DAW N times.
        if self._reload_batch_depth > 0:
            self._reload_pending = True
            return
        self._require_file_lane()
        lifecycle.reload_fl(self.project, self.link)

    def _require_file_lane(self) -> None:
        """Refuse before mutating a session project that cannot reload in place."""
        if self.session_spool_root is not None:
            raise errors.file_lane_requires_reload()

    def _prepare_file_write(self) -> None:
        """Save live FL state before a project-file mutation can reload it.

        Saving after a .flp mutation is unsafe: FL's in-memory project can
        overwrite that mutation. One save covers every write in a file_edit
        batch; standalone writes prepare independently.
        """
        self._require_file_lane()
        if self._file_write_prepared:
            return
        if self.control is None or self._ensure_hello() is None:
            return
        try:
            controls.save_project(self)
        except errors.ToolError as exc:
            if exc.category != "control_plane_unavailable":
                raise
            return
        if self._reload_batch_depth > 0:
            self._file_write_prepared = True

    @contextmanager
    def file_edit(self) -> Iterator[None]:
        """Batch development-mode file-lane edits into ONE reload.

        File-lane ops (automation, channel levels, added plugins, and the file
        fallback for notes/tempo) each rewrite the .flp and normally reload FL -
        a full SIGKILL+relaunch. Inside this block the edits still apply to the
        .flp but the reload is deferred to a single reboot on exit, so a batch
        of edits costs one reload instead of N. The first file write saves
        live bridge work before it mutates the .flp. Nestable."""
        self._require_file_lane()
        if self._reload_batch_depth == 0:
            self._file_write_prepared = False
        self._reload_batch_depth += 1
        try:
            yield
        finally:
            self._reload_batch_depth -= 1
            if self._reload_batch_depth == 0 and self._reload_pending:
                self._reload_pending = False
                self._reload_fl()
            if self._reload_batch_depth == 0:
                self._file_write_prepared = False

    def _bridge_ping(self) -> bool:
        """The breaker's gate: one stateless spool round trip proving the
        bridge's pumps run (a heartbeat alone cannot - R4 B2-7)."""
        try:
            with self._transport_use():
                cmd = self.transport.send("ping")
                return self.transport.await_response(cmd, timeout=BRIDGE_PING_TIMEOUT_SECONDS).ok
        except (errors.ToolError, CarrierUnavailable):
            return False

    @contextmanager
    def _transport_use(self) -> Iterator[None]:
        """Keep a session command's send and wait halves together.

        Transport intentionally exposes staged calls for callers that need the
        command id.  At the session boundary, however, a background lifecycle
        and an MCP request share one Bridge, so neither may interleave those
        halves.  The carrier still correlates by id; this lock also prevents
        concurrent socket receive loops during startup.
        """
        with self._transport_lock:
            yield

    @contextmanager
    def _write_guard(self, action: str) -> Iterator[None]:
        if self.session_spool_root is not None and not self._profile_supported:
            raise errors.ToolError(
                "unsupported_fl_build",
                f"Live operation '{action}' refused: {self._profile_gate_detail}",
                "Run the capture command on this FL install, ship its reviewed profile, then retry.",
            )
        owns_active_write = False
        if not self._write_lock.acquire(blocking=False):
            with self._write_state_lock:
                if self._write_owner_thread == threading.get_ident():
                    # render_wav saves through controls.save_project() while
                    # holding this guard. Re-entering from that same request
                    # is serialization, not a competing write.
                    owns_active_write = True
                remaining_action = self._write_action or "another FL MCP write"
            if not owns_active_write and (
                action != "render_wav"
                or not self._write_lock.acquire(timeout=RENDER_IN_FLIGHT_WRITE_WAIT_SECONDS)
            ):
                if action == "render_wav":
                    with self._write_state_lock:
                        remaining_action = self._write_action or remaining_action
                    raise errors.concurrent_write_wait_expired(
                        action, remaining_action, RENDER_IN_FLIGHT_WRITE_WAIT_SECONDS
                    )
                raise errors.concurrent_write(action)
        if owns_active_write:
            yield
            return
        with self._write_state_lock:
            self._write_action = action
            self._write_owner_thread = threading.get_ident()
        try:
            yield
        finally:
            with self._write_state_lock:
                self._write_action = None
                self._write_owner_thread = None
            self._write_lock.release()
