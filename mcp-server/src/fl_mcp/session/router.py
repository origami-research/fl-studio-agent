"""Capability detection + note-lane routing + the bridge circuit breaker.

This replaces the FL_MCP_BRIDGE flag (docs/27 §2, doctrine #54): the best lane
is chosen per call from evidence, and the evidence is the probe verdict
encoded as an eligibility predicate, not a hope. There is no env var, no
global, nothing a human flips.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from fl_mcp import errors
from fl_mcp.schemas import Note, NoteWriteMode, Target, WriteReceipt
from fl_mcp.spool import Spool, spool_roots

from .link import LIVE_READ_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .link import ControlLink

BRIDGE_HEARTBEAT_FILE = "bridge_heartbeat.txt"
# The bridge rewrites its heartbeat every throttled tick (sub-second while FL
# is responsive), so a stale heartbeat means FL quit or the plugin is gone.
BRIDGE_HEARTBEAT_FRESH_SECONDS = 15.0
# The heartbeat protocol this server speaks; a heartbeat without a matching
# proto= line is refused, exactly analogous to the wire's dropped-frames
# codec check in doctor(). "Plugin loaded AND compatible" - the heartbeat IS
# the recurring hello.
BRIDGE_PROTO = 2
# The breaker's ping gate: the bridge's spool poll answers within a tick or
# two when its pumps run; a wedged GUI thread (heartbeat alive, pumps dead)
# fails this fast instead of burning the full command timeout per write.
BRIDGE_PING_TIMEOUT_SECONDS = 2.0
# A wedged bridge keeps ticking heartbeats, so an open breaker re-probes at
# most this often - one ping wait per interval, not per call (the recycle
# rate-limit precedent, link.RECYCLE_MIN_INTERVAL_SECONDS).
REOPEN_PROBE_MIN_INTERVAL_SECONDS = 30.0


class NoteLane(Protocol):
    """The lane contract (docs/27 §1). Receipts are readback, never echo.
    A lane failure RAISES; it never silently falls through - the router
    decides fallback BEFORE the call."""

    name: Literal["project-write", "bridge"]

    def write(self, target: Target, notes: list[Note], mode: NoteWriteMode) -> WriteReceipt: ...

    def clear(self, target: Target) -> WriteReceipt: ...

    def read(self, target: Target) -> list[Note]: ...


class ProfileCapabilitiesLike(Protocol):
    """The capability bits the router reads."""

    channel_targeting_live: bool


class CapabilityProfileLike(Protocol):
    """Only the profile fields L1 reads (docs/27 §4); L2 owns the full
    CapabilityProfile and this package never hard-imports it."""

    capabilities: ProfileCapabilitiesLike


@dataclass(frozen=True)
class _BuiltinCapabilities:
    # ChanNum is ignored live (PROBE_VERDICT_2026-08-30 Q2): notes land in the
    # selected channel, so channel targeting is NOT live until a profile with
    # live channel selection (R2 select-then-write) says otherwise.
    channel_targeting_live: bool = False


@dataclass(frozen=True)
class _BuiltinProfile:
    capabilities: _BuiltinCapabilities = field(default_factory=_BuiltinCapabilities)


# The warned fallback: current constants, used until L2's loader is installed.
BUILTIN_PROFILE = _BuiltinProfile()


def load_profile() -> CapabilityProfileLike:
    """L2's loader (fl_mcp.profile) when installed; the BUILTIN fallback keeps
    this package importable and testable in isolation. Once L2 lands, this
    delegates automatically - no wiring to flip."""
    try:
        from fl_mcp.profile import load_profile as real_load
    except ImportError:
        return BUILTIN_PROFILE
    return real_load()


@dataclass(frozen=True)
class Heartbeat:
    """The bridge's recurring hello: ``unix_time=`` freshness plus the
    ``proto=``/``pat=``/``chan=`` context lines (docs/27 §6). Every field
    fails CLOSED - missing or unreadable parses as inf/None, never open."""

    age: float  # seconds since the bridge last ticked; inf when unknown
    at: float | None  # the unix_time stamp itself (breaker recovery compares it)
    proto: int | None
    pattern: int | None
    channel: int | None


_NO_HEARTBEAT = Heartbeat(age=math.inf, at=None, proto=None, pattern=None, channel=None)


def read_heartbeat(path: Path) -> Heartbeat:
    """Parse one heartbeat file, failing closed on any missing/garbled field."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _NO_HEARTBEAT
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        try:
            fields[key] = int(value)
        except ValueError:
            continue
    at = fields.get("unix_time")

    def context(key: str) -> int | None:
        # The bridge stamps -1 for "unknown": chan= ALWAYS (no FL getter for
        # the selected channel exists - CI_ ids are mixer-track only, VERDICT
        # Q2), pat= until its first live write observes SetCurrentPattern.
        value = fields.get(key)
        return value if value is not None and value >= 0 else None

    return Heartbeat(
        age=max(0.0, time.time() - at) if at is not None else math.inf,
        at=float(at) if at is not None else None,
        proto=fields.get("proto"),
        pattern=context("pat"),
        channel=context("chan"),
    )


@dataclass(frozen=True)
class Capabilities:
    """What the evidence says right now (docs/27 §2); computed per operation."""

    bridge_live: bool  # fresh heartbeat + proto match (the ping gate is the breaker's)
    bridge_pattern: int | None  # the bridge's last-known current pattern (heartbeat pat=)
    bridge_channel: int | None  # the LIVE selected channel, wire-read (R2 §3.2)
    profile: CapabilityProfileLike
    bridge_heartbeat_at: float | None = None  # freshest unix_time= seen; breaker recovery


def detect(
    link: ControlLink,
    spool: Spool,
    project: Path | None,
    profile: CapabilityProfileLike,
) -> Capabilities:
    """Pure, per-op capability detection: no env var, no globals, nothing a
    human flips. The heartbeat proves the bridge is ticking; the wire proves
    which channel is selected (the bridge cannot - see _selected_channel).
    ``spool``/``project`` are the detection surface for future capabilities
    (spool health, file-lane state)."""
    best = _NO_HEARTBEAT
    for root in spool_roots():
        candidate = read_heartbeat(root / BRIDGE_HEARTBEAT_FILE)
        if candidate.age < best.age:
            best = candidate
    live = best.age <= BRIDGE_HEARTBEAT_FRESH_SECONDS and best.proto == BRIDGE_PROTO
    return Capabilities(
        bridge_live=live,
        bridge_pattern=best.pattern if live else None,
        bridge_channel=_selected_channel(link) if live else None,
        profile=profile,
        bridge_heartbeat_at=best.at if live else None,
    )


def _selected_channel(link: ControlLink) -> int | None:
    """The R2 §3.2 selected-channel guard: notes a bridge write applies land
    in the first SELECTED channel, always (VERDICT Q2), and the bridge has no
    getter for it (its heartbeat stamps chan=-1). The controller wire does:
    one ``channels.selectedChannel()`` round trip is the only channel-match
    evidence. Wire down or read refused -> None -> the file lane (correctness
    beats liveness)."""
    if link.hello() is None or link.control is None:
        return None
    try:
        resp = link.control.send_request(
            "channels.selectedChannel", timeout=LIVE_READ_TIMEOUT_SECONDS
        )
    except errors.ToolError:
        return None
    if not resp.ok:
        return None
    try:
        channel = int(resp.ret)
    except (TypeError, ValueError):
        return None
    return channel if channel >= 0 else None


class BridgeBreaker:
    """closed -> open -> half_open -> closed (docs/27 §3).

    Live writes are gated on a spool ping round-trip once per session
    (half_open is the starting state), then the heartbeat is trusted between
    failures (R4 B2-7). A failure never falls through mid-call - the failed
    call raises, the breaker records, and the NEXT call routes around the
    bridge: half_open after a soft failure (re-ping before reuse), open after
    FAILURES_TO_OPEN in a row or one hard failure (FL death near an unacked
    command). An open breaker re-arms to half_open only when a heartbeat
    NEWER than the failure appears, rate-limited so a wedged-but-ticking
    bridge costs one ping wait per interval."""

    FAILURES_TO_OPEN = 3

    def __init__(
        self,
        ping: Callable[[], bool],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Wall clock, not monotonic: recovery compares against the
        # heartbeat's own unix_time stamps.
        self._ping = ping
        self._clock = clock
        self._state: Literal["closed", "open", "half_open"] = "half_open"
        self._soft_failures = 0
        self._opened_at: float | None = None
        self._last_probe: float | None = None
        self.last_hard_fingerprint: str | None = None

    @property
    def closed(self) -> bool:
        """False => route to the file lane."""
        return self._state == "closed"

    def record_success(self) -> None:
        self._state = "closed"
        self._soft_failures = 0
        self._opened_at = None

    def record_soft_failure(self) -> None:
        """A timeout/refusal/stale mid-command: re-ping before the next use,
        and open outright after FAILURES_TO_OPEN in a row."""
        self._soft_failures += 1
        if self._soft_failures >= self.FAILURES_TO_OPEN:
            self._open()
        else:
            self._state = "half_open"

    def record_hard_failure(self, fingerprint: str) -> None:
        """FL death near an unacked command - the command may have been the
        trigger, so the bridge is not retried until fresh evidence appears."""
        self.last_hard_fingerprint = fingerprint
        self._open()

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = self._clock()

    def admit(self, heartbeat_at: float | None) -> bool:
        """Whether the bridge may take the NEXT call. closed: yes. open: a
        heartbeat newer than the failure re-arms to half_open (rate-limited).
        half_open: one ping round-trip decides, and its outcome is recorded."""
        if self._state == "open":
            if (
                heartbeat_at is None
                or self._opened_at is None
                or heartbeat_at <= self._opened_at
                or (
                    self._last_probe is not None
                    and self._clock() - self._last_probe < REOPEN_PROBE_MIN_INTERVAL_SECONDS
                )
            ):
                return False
            self._state = "half_open"
        if self._state == "half_open":
            self._last_probe = self._clock()
            if self._ping():
                self.record_success()
            else:
                self.record_soft_failure()
        return self.closed


def pick_note_lane(
    caps: Capabilities,
    target: Target,
    breaker: BridgeBreaker,
    *,
    bridge: NoteLane,
    file: NoteLane,
) -> NoteLane:
    """The routing predicate (docs/27 §2): bridge iff the heartbeat proves it
    live AND the write's channel constraint is verifiably satisfied AND the
    breaker admits it (closed, or a ping round-trip just closed it).

    Channel targeting is bridge-blind (PROBE_VERDICT Q2: ChanNum is ignored,
    notes land in the selected channel), so the constraint is the wire-read
    selected channel equalling the target - until a profile reports live
    channel selection, which widens only this predicate. The file lane honors
    both coordinates by construction and takes everything else."""
    if (
        caps.bridge_live
        and (
            caps.profile.capabilities.channel_targeting_live
            or caps.bridge_channel == target.channel
        )
        and breaker.admit(caps.bridge_heartbeat_at)
    ):
        return bridge
    return file
