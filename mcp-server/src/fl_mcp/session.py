"""One object owning the connection to FL: control wire + note spool + project.

The division of labor is by CAPABILITY, dogfood-verified (docs/21 A12):

- **Targeted note writes -> the project file** ("project-write": raw surgery
  via flpkit + an FL reload). It is the only backend that can honor a
  pattern/channel target - the pyscript/spool lane is TARGET-BLIND (a
  piano-roll script edits whichever score is open), so routing notes through
  it stamped target-claiming receipts on writes that all landed in one roll.
  Live targeted notes arrive with the native Bridge plugin (issue #10), which
  will consume the same spool; the Spool member stays for that day.
- **Tempo, transport, mixer, reads -> the live control wire** when connected.

Every result is a READBACK, and the write-through records the same structured
delta in the store whichever backend applied.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fl_mcp import errors
from fl_mcp.flpaths import (
    fl_app,
    fl_process_running,
    piano_roll_scripts_dir,
    primary_settings_dir,
)

from . import flp
from .schemas import (
    Channel,
    ChannelLevels,
    DoctorReport,
    HelloEvent,
    MixerTrack,
    Note,
    NoteDelta,
    NoteWriteMode,
    PlaneStatus,
    PluginParam,
    SessionState,
    Target,
    WriteReceipt,
)
from .spool import Spool, spool_roots
from .store import Store
from .wire import ControlUnavailable, MidiControl

log = logging.getLogger(__name__)

# Right after a SIGKILL, LaunchServices still has the dead process registered;
# it clears in a second or two, so the relaunch waits before retrying.
RELAUNCH_SETTLE_SECONDS = 2.0


# A fast "is the controller there right now" probe - not a startup wait.
HELLO_PROBE_SECONDS = 0.2
# Confirming a sticky hello is a millisecond round trip when FL is alive.
PING_CONFIRM_SECONDS = 1.0

# Hot-plug self-heal (live-measured 2026-08-29: FL binds appearing ports in
# ~2s): how long to wait for hello after recycling, and how often a silent,
# unconfigured FL is allowed to cost that wait.
RECYCLE_HELLO_WAIT_SECONDS = 8.0
RECYCLE_MIN_INTERVAL_SECONDS = 30.0

# FL can stay "unsafe at current time" for a few seconds after a load; give the
# controller's OnIdle deferral time to land a mutating call (v1-measured).
LIVE_SET_TIMEOUT_SECONDS = 8.0
LIVE_READ_TIMEOUT_SECONDS = 2.0

# Plugin-parameter surface: FL quantizes values to each control's own grid
# (E2: set 0.42 -> stored 0.41875), and big synths are big - bound the listing.
PLUGIN_PARAMS_DEFAULT_LIMIT = 40
PLUGIN_PARAM_TOLERANCE = 0.02

# state.snapshot arrives as ~10 chunked SysEx frames (measured ~4.8KB on a
# 5-channel/18-track project, addendum A5); give bigger projects headroom.
SNAPSHOT_TIMEOUT_SECONDS = 10.0

FL_STOP_TIMEOUT_SECONDS = 20.0
FL_OPEN_ATTEMPTS = 6
# E7 (2026-08-29, 3/3 PASS): FL parks "unsafe at current time" ops for OnIdle
# retry during boot, and a parked op can land LATE, undoing a newer one
# (issue #47). Waiting until a composite read answers makes transport ops
# reliable; measured settle was ~12s, so the bound leaves headroom.
SETTLE_TIMEOUT_SECONDS = 45.0
SETTLE_POLL_SECONDS = 1.0

# The project this session may rewrite (the project-write backend's target).
ENV_PROJECT = "FLMCP_AGENT_PROJECT"
DEFAULT_PROJECT_NAME = "FLMCP Agent.flp"

# The native Bridge note path (issue #10) is staged behind a double opt-in:
# FL_MCP_BRIDGE=1 AND a fresh bridge heartbeat. OFF by default - project-write
# stays the routing until the bridge has a live verdict (RUNBOOK_BRIDGE_V2.md).
ENV_BRIDGE = "FL_MCP_BRIDGE"
BRIDGE_HEARTBEAT_FILE = "bridge_heartbeat.txt"
# The bridge rewrites its heartbeat every throttled tick (sub-second while FL
# is responsive), so a stale heartbeat means FL quit or the plugin is gone.
BRIDGE_HEARTBEAT_FRESH_SECONDS = 15.0
# The bridge applies on its next tick; its spool poll answers well within this.
BRIDGE_RESPONSE_TIMEOUT_SECONDS = 10.0

# ActionEntry.id for writes that never went through the spool.
NO_COMMAND_ID = 0

PYSCRIPT_NAME = "MCP Write Notes.pyscript"
_SETUP_FIX = (
    "Either launch FL Studio with the FLMCP controller script configured (edits then "
    f"apply via the '{PYSCRIPT_NAME.removesuffix('.pyscript')}' piano-roll script), or "
    "configure a project file the agent may rewrite: run "
    "`python -m fl_mcp.make_agent_project`, or set FLMCP_AGENT_PROJECT to a .flp path."
)


class NotePlaneUnavailable(errors.ToolError):
    """No backend can apply this change; the fix names exactly what to set up."""

    def __init__(self, message: str, fix: str = _SETUP_FIX) -> None:
        super().__init__("note_plane_unavailable", message, fix)


class WriteRejected(errors.ToolError):
    """A backend took the call but did not CONFIRM the requested state."""

    def __init__(self, message: str, fix: str, *, retryable: bool = True) -> None:
        super().__init__("fl_api_error", message, fix, retryable=retryable)


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


    return FLSession(
        control=control,
        spool=Spool(),
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


def _heartbeat_age(path: Path) -> float:
    """Seconds since the Bridge plugin last ticked, per its heartbeat file
    (the ``unix_time=<epoch>`` line bridge.cpp rewrites every poll). Missing or
    unreadable is ``inf`` - the gate fails closed, never open."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return math.inf
    for line in text.splitlines():
        if line.startswith("unix_time="):
            try:
                return max(0.0, time.time() - int(line.removeprefix("unix_time=")))
            except ValueError:
                return math.inf
    return math.inf


class FLSession:
    """Everything below the SDK talks to FL through this one object."""

    control: MidiControl | None
    spool: Spool
    store: Store
    project: Path | None

    def __init__(
        self,
        *,
        control: MidiControl | None,
        spool: Spool,
        store: Store,
        project: Path | None,
        control_factory: Callable[[], MidiControl] | None = None,
    ) -> None:
        self.control = control
        self.spool = spool
        self.store = store
        self.project = project
        self._control_factory = control_factory if control_factory is not None else MidiControl
        self._last_recycle = 0.0


    def channel_names(self) -> list[Channel]:
        """The channels of the TARGET universe - the project file - for name
        resolution. File-first because targets are file coordinates and the
        file answers instantly mid-reload; the live snapshot is the fallback
        for sessions without a project."""
        if self.project is not None:
            proj = flp.read(self.project)
            return [
                Channel(
                    index=c.index,
                    name=c.name or f"Channel {c.index}",
                    volume=c.volume / flp.LEVEL_MAX,
                    pan=(c.pan - flp.PAN_CENTRE) / flp.PAN_CENTRE,
                )
                for c in proj.channels
            ]
        return self.snapshot().channels

    def _reconnect_wire(self) -> None:
        """Fresh ports after an FL relaunch we caused: the old wire died with
        the killed process (and its sticky hello would lie), while ports that
        APPEAR after FL is up hit its reliable hot-plug bind (docs/21 A12)."""
        if self.control is None:
            return
        self.control.close()
        try:
            self.control = self._control_factory()
        except ControlUnavailable:
            log.exception("wire re-open after reload failed; live ops degrade")
            self.control = None
        self._last_recycle = 0.0

    def _record(
        self,
        target: Target,
        notes: list[Note],
        mode: NoteWriteMode,
        backend: str,
        command_id: int,
    ) -> NoteDelta:
        """Write-through to the store for an edit that ALREADY landed.

        The edit was applied and verified before this runs, so a recording
        failure must not fail the write - but it must not be silent either
        (found live: a store-path bug made every applied write report a
        traceback). Logged with the traceback; the receipt then carries an
        empty delta, meaning "applied, history unavailable"."""
        try:
            return self.store.record_write(target, notes, mode, backend, command_id)
        except Exception:
            log.exception("write-through recording failed after the %s backend applied", backend)
            return NoteDelta()

    def _hello(self) -> HelloEvent | None:
        """The ONE connectivity probe: the controller's hello, or None. Every
        backend choice keys off this so "connected" cannot drift between
        methods (one canonical function per rule).

        Self-healing: FL 2026 reliably binds our virtual ports when they APPEAR
        while it is running (hot-plug, ~2s live-measured) but can miss ports
        that already existed at launch. So when FL is up and the wire is silent,
        the ports are recycled to force a hot-plug bind - making the launch
        order of server and FL irrelevant to the user. Rate-limited so an FL
        that is genuinely unconfigured costs one wait per interval, not per call.
        """
        if self.control is None:
            return None
        try:
            hello = self.control.await_hello(timeout=HELLO_PROBE_SECONDS)
            # The stored hello is STICKY across an FL death (dogfood A12): a
            # stale "connected" routes live ops into a void. A ping (answered
            # from the SysEx callback, milliseconds when alive) confirms the
            # wire is current before anything trusts it.
            if self.control.send_request("ping", timeout=PING_CONFIRM_SECONDS).ok:
                return hello
        except errors.ToolError:
            pass
        if not fl_process_running() or time.monotonic() - self._last_recycle < RECYCLE_MIN_INTERVAL_SECONDS:
            return None
        self._last_recycle = time.monotonic()
        log.info("FL is running but the wire is silent; recycling ports for a hot-plug bind")
        self.control.close()
        try:
            self.control = self._control_factory()
        except ControlUnavailable:
            log.exception("port recycle failed; control plane stays down")
            self.control = None
            return None
        try:
            return self.control.await_hello(timeout=RECYCLE_HELLO_WAIT_SECONDS)
        except errors.ToolError:
            return None

    def write_notes(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt:
        """Apply to the PROJECT FILE, verify by readback, record the delta.
        Raises (NotePlaneUnavailable / WriteRejected) on failure - never
        returns a maybe.

        Targeted note writes deliberately do NOT use the pyscript/spool lane
        even when the control plane is connected: a piano-roll script can only
        edit the score it was invoked on, so it is TARGET-BLIND - dogfood-found
        2026-08-29, three "targeted" writes all landed in whichever piano roll
        was open, each replace destroying the previous part, while the receipts
        claimed their targets. The file is the one backend that can honor a
        target (and it keeps file, store, and get_scene coherent). Live
        targeted notes arrive with the native Bridge plugin (issue #10); until
        its live verdict that path exists only behind _bridge_ready()."""
        if self.project is not None:
            if self._bridge_ready():
                return self._bridge_write(self.project, target, notes, mode)
            try:
                written = flp.write_notes(
                    self.project, notes, pattern=target.pattern, channel=target.channel, mode=mode
                )
            except flp.FlpError as exc:
                raise WriteRejected(
                    f"project-file note write did not verify: {exc}",
                    fix="The project file was restored by the readback check failing; "
                    "inspect the .flp and retry.",
                ) from exc
            delta = self._record(target, notes, mode, "project-write", NO_COMMAND_ID)
            self._reload_fl()
            return WriteReceipt(
                backend="project-write",
                target=target,
                confirmed_notes=len(written),
                delta=delta,
            )

        raise NotePlaneUnavailable(
            "No backend can apply targeted notes: no project file is configured. "
            "(The pyscript lane cannot target a channel - see write_notes.)"
        )

    def clear_notes(self, target: Target) -> WriteReceipt:
        """Project-file (or, behind the flag, bridge) - same routing as write_notes."""
        if self.project is not None:
            if self._bridge_ready():
                return self._bridge_clear(self.project, target)
            try:
                written = flp.write_notes(
                    self.project, [], pattern=target.pattern, channel=target.channel, mode="replace"
                )
            except flp.FlpError as exc:
                raise WriteRejected(
                    f"project-file clear did not verify: {exc}",
                    fix="Inspect the .flp and retry.",
                ) from exc
            delta = self._record(target, [], "replace", "project-write", NO_COMMAND_ID)
            self._reload_fl()
            return WriteReceipt(
                backend="project-write",
                target=target,
                confirmed_notes=len(written),
                delta=delta,
            )

        raise NotePlaneUnavailable(
            "No backend can clear notes: the control plane is not connected and no "
            "project file is configured."
        )

    # -- the flagged bridge note path (issue #10; live-unverified) ---------------
    #
    # Targeted LIVE notes via the native Bridge plugin, which consumes the same
    # v2 spool as the pyscript but is not target-blind: as an effect it can set
    # ChanNum/PatNum on FHD_AddNotesToPR (fp_plugclass.h). Staged behind a
    # double opt-in until the live runbook (native/bridge/RUNBOOK_BRIDGE_V2.md)
    # delivers a verdict; the runbook's ladder is the only intended caller.

    def _bridge_ready(self) -> bool:
        """FL_MCP_BRIDGE=1 AND a heartbeat fresh enough to prove the Bridge
        plugin is ticking right now. Anything less routes to project-write."""
        if os.environ.get(ENV_BRIDGE) != "1":
            return False
        return any(
            _heartbeat_age(root / BRIDGE_HEARTBEAT_FILE) <= BRIDGE_HEARTBEAT_FRESH_SECONDS
            for root in spool_roots()
        )

    def _spool_ppq(self, project: Path) -> int | None:
        """The PPQ stamp for spooled note commands: the target universe's ppq
        (the project file), else the live hello's. None lets the consumer fall
        back (the bridge assumes 96 and flags it in its response summary)."""
        try:
            return flp.read(project).ppq
        except flp.FlpError:
            log.exception("could not read ppq from %s; trying the live hello", project)
        hello = self._hello()
        return hello.ppq if hello is not None else None

    def _bridge_send(self, op: str, **args: Any) -> int:
        """Spool one command and block for the bridge's response; returns the
        command id. A refusal or timeout raises - never a maybe."""
        cmd = self.spool.send(op, **args)
        resp = self.spool.await_response(cmd, timeout=BRIDGE_RESPONSE_TIMEOUT_SECONDS)
        if not resp.ok:
            raise WriteRejected(
                f"the bridge refused spool command {cmd.id} ({op}): {resp.err}",
                fix="Read bridge_status.txt / bridge_heartbeat.txt under the spool "
                "and retry; unset FL_MCP_BRIDGE to fall back to project-write.",
            )
        return cmd.id

    def _bridge_confirmed(self, project: Path, target: Target, command_id: int) -> int:
        """Readback-only count for a bridge receipt - never the bridge's echo
        (FHD_AddNotesToPR returns nothing, so its notes_written is what it
        SENT). The only read of record today is the project file, which lags
        the live session until FL saves - so this count may exclude the very
        write it confirms. That gap is deliberate until a live note readback
        exists (the readback-probe lane); the runbook's scene evidence is the
        live verdict."""
        try:
            return len(flp.notes_at(project, target.pattern, target.channel))
        except flp.FlpError as exc:
            raise WriteRejected(
                f"the bridge applied command {command_id} but the file readback "
                f"failed: {exc}; the write is UNCONFIRMED.",
                fix="Inspect the project file and the bridge status files, then retry.",
            ) from exc

    def _bridge_write(
        self, project: Path, target: Target, notes: list[Note], mode: NoteWriteMode
    ) -> WriteReceipt:
        command_id = self._bridge_send(
            "add_notes", target=target, notes=notes, mode=mode, ppq=self._spool_ppq(project)
        )
        delta = self._record(target, notes, mode, "bridge", command_id)
        return WriteReceipt(
            backend="bridge",
            target=target,
            confirmed_notes=self._bridge_confirmed(project, target, command_id),
            delta=delta,
        )

    def _bridge_clear(self, project: Path, target: Target) -> WriteReceipt:
        command_id = self._bridge_send("clear_notes", target=target)
        delta = self._record(target, [], "replace", "bridge", command_id)
        return WriteReceipt(
            backend="bridge",
            target=target,
            confirmed_notes=self._bridge_confirmed(project, target, command_id),
            delta=delta,
        )

    def read_notes(self, target: Target) -> list[Note]:
        """Store first (with provenance); project-file readback on demand."""
        stored = self.store.notes_at(target)
        if stored is not None:
            return stored

        if self.project is not None:
            ppq = flp.read(self.project).ppq
            notes = [
                Note(
                    key=n.key,
                    start=n.position / ppq,
                    length=n.length / ppq,
                    velocity=n.velocity / 127,
                    pan=n.pan / 128,
                )
                for n in flp.notes_at(self.project, target.pattern, target.channel)
            ]
            self.store.mark_ground_truth(target, notes)
            return notes

        raise NotePlaneUnavailable(
            f"Nothing recorded for pattern {target.pattern} channel {target.channel} and "
            "no project file is configured to read from."
        )

    def set_tempo(self, bpm: float) -> float:
        """Live-first (confirmed via getCurrentTempo readback, BPM x 1000);
        else the project file (flp.set_tempo) + reload. Returns the readback."""
        if self._hello() is not None and self.control is not None:
            resp = self.control.send_request(
                "mixer.setCurrentTempo", args=[bpm, 1], timeout=LIVE_SET_TIMEOUT_SECONDS
            )
            if not resp.ok:
                # Unsafe-at-current-time means busy (warm-up window after a
                # load, addendum A6) - retryable, and NEVER a fallthrough.
                if errors.is_operation_unsafe(resp.err or ""):
                    raise errors.operation_unsafe("mixer.setCurrentTempo")
                raise WriteRejected(
                    f"mixer.setCurrentTempo({bpm}) failed inside FL: {resp.err}",
                    fix="Check the value (10-999 BPM) and retry.",
                )
            readback = self.control.send_request(
                "mixer.getCurrentTempo", timeout=LIVE_READ_TIMEOUT_SECONDS
            )
            if not readback.ok or readback.ret is None:
                raise WriteRejected(
                    f"tempo was sent but the readback did not answer ({readback.err}); "
                    "the change is UNCONFIRMED.",
                    fix="Run fl_doctor, then retry.",
                )
            return float(readback.ret) / 1000.0

        if self.project is not None:
            confirmed = flp.set_tempo(self.project, bpm)
            self._reload_fl()
            return confirmed

        raise NotePlaneUnavailable(
            "No backend can set the tempo: the control plane is not connected and no "
            "project file is configured."
        )

    def set_channel_levels(
        self,
        channel: int,
        *,
        volume: float | None = None,
        pan: float | None = None,
        pitch_semitones: int | None = None,
    ) -> ChannelLevels:
        """All-or-nothing per call: a field that landed is never silently
        discarded by a fallback (the v1 partial-then-reload bug)."""
        if volume is None and pan is None and pitch_semitones is None:
            raise ValueError("nothing to change - pass volume, pan and/or pitch_semitones")

        if self._hello() is not None and self.control is not None:
            # ONE backend for the whole call. A mid-call failure raises with
            # what already landed - it never falls through to the file path,
            # which would reload FL and discard the fields that landed live.
            requested = [
                ("volume", "channels.setChannelVolume", [channel, volume]),
                ("pan", "channels.setChannelPan", [channel, pan]),
                ("pitch_semitones", "channels.setChannelPitch", [channel, pitch_semitones, 1]),
            ]
            applied: list[str] = []
            for field_name, op, args in requested:
                if args[1] is None:
                    continue
                resp = self.control.send_request(op, args=args, timeout=LIVE_SET_TIMEOUT_SECONDS)
                if not resp.ok:
                    if errors.is_operation_unsafe(resp.err or ""):
                        raise errors.operation_unsafe(op)  # busy, not broken (A6)
                    raise WriteRejected(
                        f"{op} failed inside FL: {resp.err}. "
                        f"Already applied live and KEPT: {', '.join(applied) or 'nothing'}.",
                        fix="Retry the failed field; the fields that landed were not rolled back.",
                    )
                applied.append(field_name)

            readbacks = {}
            for field_name, op, args in (
                ("volume", "channels.getChannelVolume", [channel]),
                ("pan", "channels.getChannelPan", [channel]),
                ("pitch_semitones", "channels.getChannelPitch", [channel, 1]),
            ):
                resp = self.control.send_request(op, args=args, timeout=LIVE_READ_TIMEOUT_SECONDS)
                if not resp.ok or resp.ret is None:
                    raise WriteRejected(
                        f"levels were applied but {op} did not answer ({resp.err}); "
                        "the result is UNCONFIRMED.",
                        fix="Run fl_doctor, then retry.",
                    )
                readbacks[field_name] = resp.ret
            return ChannelLevels(
                channel=channel,
                volume=float(readbacks["volume"]),
                pan=float(readbacks["pan"]),
                pitch_semitones=round(float(readbacks["pitch_semitones"])),
            )

        if self.project is not None:
            confirmed = flp.set_channel_levels(
                self.project, channel, volume=volume, pan=pan, pitch_semitones=pitch_semitones
            )
            self._reload_fl()
            return confirmed

        raise NotePlaneUnavailable(
            "No backend can set channel levels: the control plane is not connected and "
            "no project file is configured."
        )

    # -- plugin parameters (live-only; E2-verified ops) --------------------------
    #
    # A generator's parameters exist only inside the running plugin - there is
    # no file representation we can honestly edit (plugin state is an opaque
    # vendor blob). So this surface is live-or-error, never a file fallback.

    def _plugin_read(self, op: str, args: list) -> object:
        if self._hello() is None or self.control is None:
            raise errors.control_plane_unavailable(op, 0.0)
        resp = self.control.send_request(op, args=args, timeout=LIVE_READ_TIMEOUT_SECONDS)
        if not resp.ok:
            detail = resp.err or "no detail"
            if errors.is_operation_unsafe(detail):
                raise errors.operation_unsafe(op)
            raise errors.fl_api_error(op, detail)
        return resp.ret

    def plugin_params(
        self,
        channel: int,
        *,
        query: str | None = None,
        limit: int = PLUGIN_PARAMS_DEFAULT_LIMIT,
    ) -> list[PluginParam]:
        """The channel generator's parameters (name + normalized value).

        ``query`` filters by case-insensitive substring; ``limit`` bounds the
        result because big synths are BIG (Sytrus: 597 params) and every name
        is a wire round trip."""
        count = int(self._plugin_read("plugins.getParamCount", [channel, -1]) or 0)
        out: list[PluginParam] = []
        for index in range(count):
            if len(out) >= limit:
                break
            name = str(self._plugin_read("plugins.getParamName", [index, channel, -1]))
            if query is not None and query.lower() not in name.lower():
                continue
            value = float(self._plugin_read("plugins.getParamValue", [index, channel, -1]))
            out.append(PluginParam(index=index, name=name, value=min(max(value, 0.0), 1.0)))
        return out

    def set_plugin_param(self, channel: int, param: int | str, value: float) -> PluginParam:
        """Set one parameter (by index, or by unique name/substring) and return
        the READBACK. FL quantizes to the plugin's own step grid, so the
        confirmed value may differ slightly from the request - beyond
        PLUGIN_PARAM_TOLERANCE it raises instead of pretending."""
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"plugin param values are normalized 0..1, got {value}")
        if isinstance(param, str):
            matches = self.plugin_params(channel, query=param, limit=8)
            exact = [p for p in matches if p.name.lower() == param.lower()]
            chosen = exact or matches
            if len(chosen) != 1:
                names = ", ".join(p.name for p in matches) or "(no matches)"
                raise errors.fl_api_error(
                    "plugins.setParamValue",
                    f"param {param!r} is {'ambiguous' if matches else 'unknown'} on channel "
                    f"{channel}: {names}",
                )
            index = chosen[0].index
        else:
            index = param

        if self.control is None:
            raise errors.control_plane_unavailable("plugins.setParamValue", 0.0)
        resp = self.control.send_request(
            "plugins.setParamValue", args=[value, index, channel, -1], timeout=LIVE_SET_TIMEOUT_SECONDS
        )
        if not resp.ok:
            raise errors.fl_api_error("plugins.setParamValue", resp.err or "no detail")

        name = str(self._plugin_read("plugins.getParamName", [index, channel, -1]))
        confirmed = float(self._plugin_read("plugins.getParamValue", [index, channel, -1]))
        if abs(confirmed - value) > PLUGIN_PARAM_TOLERANCE:
            raise WriteRejected(
                f"param {name!r} readback {confirmed} is not the requested {value}",
                fix="The plugin may quantize this control coarsely; read plugin_params "
                "and pick a supported value.",
            )
        return PluginParam(index=index, name=name, value=confirmed)

    def snapshot(self) -> SessionState:
        """Live: ONE composite ``state.snapshot`` round trip (chunked response;
        v1's transport silently dropped it, Lane W fixed the routing - addendum
        A5). Else: decoded from the project file. One backend per call, so a
        stale file never contaminates a live view or vice versa."""
        if self._hello() is not None and self.control is not None:
            resp = self.control.send_request("state.snapshot", timeout=SNAPSHOT_TIMEOUT_SECONDS)
            if not resp.ok:
                detail = resp.err or "no detail"
                if errors.is_operation_unsafe(detail):
                    raise errors.operation_unsafe("state.snapshot")
                raise errors.fl_api_error("state.snapshot", detail)
            snap = resp.ret
            try:
                return SessionState(
                    tempo_bpm=float(snap["tempo"]) / 1000.0,  # getCurrentTempo: BPM x 1000
                    ppq=int(snap["ppq"]),
                    playing=bool(snap["playing"]),
                    current_pattern=int(snap["current_pattern"]),
                    channels=[
                        Channel(
                            index=c["idx"],
                            name=c["name"],
                            volume=float(c["vol"]),
                            pan=float(c["pan"]),
                            muted=bool(c["muted"]),
                        )
                        for c in snap["channels"]
                    ],
                    mixer=[
                        MixerTrack(
                            index=t["idx"],
                            name=t["name"],
                            volume=float(t["vol"]),
                            pan=float(t["pan"]),
                            muted=bool(t["muted"]),
                        )
                        for t in snap["mixer"]
                    ],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise errors.fl_api_error(
                    "state.snapshot", f"malformed snapshot payload ({exc!r})"
                ) from exc

        if self.project is not None:
            project = flp.read(self.project)
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

        return SessionState()

    # -- transport (manager sign-off, addendum A11) -------------------------------
    #
    # These exist so the SDK never reaches through to the wire. Each sends the
    # control op and RAISES on failure - ToolError(control_plane_unavailable) when
    # there is no control plane, ToolError(operation_unsafe) when FL refused the
    # call as unsafe at current time (retryable; see addendum A6). Returning a
    # ControlResponse here would just move the same conversion into every caller.

    def play(self) -> None:
        """``transport.start``."""
        self._control_command("transport.start")

    def stop(self) -> None:
        """``transport.stop``."""
        self._control_command("transport.stop")

    def undo(self) -> None:
        """``general.undo``."""
        self._control_command("general.undo")

    def _control_command(self, op: str) -> None:
        """THE call-to-policy conversion (addendum A11), written once: no wire
        -> control_plane_unavailable; FL's unsafe-at-current-time refusal ->
        operation_unsafe (retryable - it means busy, not absent, addendum A6);
        any other refusal -> fl_api_error with FL's own detail. A send_request
        timeout raises control_plane_unavailable from the wire and propagates."""
        if self.control is None:
            raise errors.control_plane_unavailable(op, 0.0)
        resp = self.control.send_request(op, timeout=LIVE_SET_TIMEOUT_SECONDS)
        if not resp.ok:
            detail = resp.err or "no detail"
            if errors.is_operation_unsafe(detail):
                raise errors.operation_unsafe(op)
            raise errors.fl_api_error(op, detail)

    def doctor(self) -> DoctorReport:
        """Typed report - control plane, note plane, project file - with a
        concrete ``fix`` string per degraded plane."""
        if self.control is None:
            control_plane = PlaneStatus(
                connected=False,
                detail="no MIDI control wire: python-rtmidi is missing or the virtual "
                "ports could not be opened",
                fix="Install python-rtmidi (`uv pip install python-rtmidi`) and restart "
                "the MCP server.",
            )
        else:
            # Captured BEFORE the probe: _hello() may recycle a silent wire,
            # and the old wire's dropped-frame count is the diagnostic.
            dropped = self.control.dropped_frames
            hello = self._hello()
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
                    "the server (`python -m fl_mcp.install`), then restart FL Studio.",
                )
            elif fl_process_running():
                control_plane = PlaneStatus(
                    connected=False,
                    detail="FL Studio is running but the controller script has not said hello",
                    fix="Close any dialog in FL (its welcome screen blocks the controller), and "
                    "confirm the FLMCP controller script is assigned in FL's MIDI settings "
                    "(`python -m fl_mcp.install`). Ports self-heal; launch order does not matter.",
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
                fix="Run `python -m fl_mcp.install`, then restart FL Studio.",
            )
        else:
            note_plane = PlaneStatus(
                connected=True,
                detail=f"spool at {spool_roots()[0]} (reserved for the native Bridge "
                "plugin; targeted notes currently apply via the project file)",
            )

        if self.project is None:
            project_file = PlaneStatus(
                connected=False,
                detail="no agent project configured (project-write backend unavailable)",
                fix="Run `python -m fl_mcp.make_agent_project`, or set FLMCP_AGENT_PROJECT "
                "to a .flp the agent may rewrite.",
            )
        elif not self.project.is_file():
            project_file = PlaneStatus(
                connected=False,
                detail=f"configured project file does not exist: {self.project}",
                fix="Create it (`python -m fl_mcp.make_agent_project`) or point "
                "FLMCP_AGENT_PROJECT at a real .flp.",
            )
        else:
            project_file = PlaneStatus(connected=True, detail=str(self.project))

        all_connected = control_plane.connected and note_plane.connected and project_file.connected
        return DoctorReport(
            status="ok" if all_connected else "degraded",
            control_plane=control_plane,
            note_plane=note_plane,
            project_file=project_file,
        )

    # -- the one helper ---------------------------------------------------------

    def _settle(self) -> None:
        """Block until FL answers a composite read, so no live op lands in the
        boot window where FL parks it for a LATE OnIdle retry (issue #47,
        settled by experiment E7). A timeout is logged, not raised: the reload
        itself succeeded, and the next live op will surface its own error."""
        if self.control is None:
            return
        deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if self.control.send_request("state.snapshot", timeout=5.0).ok:
                    return
            except errors.ToolError:
                pass
            time.sleep(SETTLE_POLL_SECONDS)
        log.warning("FL did not settle within %ss of relaunch", SETTLE_TIMEOUT_SECONDS)

    def _reload_fl(self) -> None:
        """UGLY(U5), quarantined: FL has no 'reopen this file' API and a polite
        quit blocks on save dialogs. SIGKILL, wait RELAUNCH_SETTLE_SECONDS,
        then ``open`` with bounded retries."""
        if self.project is None:
            raise ValueError("_reload_fl requires a resolved project file")

        if fl_process_running():
            # SIGKILL, because a polite quit prompts to save and would hang
            # unattended. Only used for the project-write backend, where the
            # file on disk - not the in-memory session - is the source of truth.
            subprocess.run(["pkill", "-9", "-f", "FL Studio"], capture_output=True)
            deadline = time.monotonic() + FL_STOP_TIMEOUT_SECONDS
            while time.monotonic() < deadline and fl_process_running():
                time.sleep(0.2)
            time.sleep(RELAUNCH_SETTLE_SECONDS)

        last = ""
        for attempt in range(FL_OPEN_ATTEMPTS):
            result = subprocess.run(
                ["open", "-a", str(fl_app()), str(self.project)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self._reconnect_wire()
                self._settle()
                return
            last = (result.stderr or "").strip()
            time.sleep(1.0 + attempt)
        # The edit already landed in the file and was recorded; only the
        # relaunch failed - say exactly that, never "the write failed".
        raise errors.ToolError(
            "fl_api_error",
            f"The change IS in {self.project.name}, but FL Studio could not be "
            f"relaunched to show it after {FL_OPEN_ATTEMPTS} attempts: {last}",
            "Open the project in FL Studio manually, then run fl_doctor.",
            retryable=True,
        )
