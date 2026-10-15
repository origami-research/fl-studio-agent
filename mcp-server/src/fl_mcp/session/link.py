"""The control wire, owned: liveness probe, hot-plug self-heal, settle, and
the one call-to-policy conversion every live op shares."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from fl_mcp import errors
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION, controller_protocol_matches
from fl_mcp.core.carriers.wire import ControlUnavailable
from fl_mcp.fl.process import FL_BOOT_TIMEOUT_SECONDS, fl_process_running
from fl_mcp.schemas import ControlResponse, HelloEvent


class ControlCarrier(Protocol):
    """The surface the link needs; satisfied by MidiControl (wire) and
    BrokerControl (session mode, pid-addressed via the machine-wide broker, #158)."""

    def is_live(self) -> bool: ...
    def await_hello(self, timeout: float = ...) -> HelloEvent: ...
    def send_request(self, op: str, args: list[Any] | None = ..., kwargs: dict[str, Any] | None = ..., timeout: float = ...) -> ControlResponse: ...
    def drain_events(self) -> list[dict[str, Any]]: ...
    def add_event_listener(self, fn: Any) -> None: ...
    @property
    def dropped_frames(self) -> int: ...
    def close(self) -> None: ...

log = logging.getLogger(__name__)

# A fast "is the controller there right now" probe - not a startup wait.
HELLO_PROBE_SECONDS = 0.2
# Confirming a sticky hello is a millisecond round trip when FL is alive.
PING_CONFIRM_SECONDS = 1.0

# Hot-plug self-heal (live-measured 2026-08-29: FL binds appearing ports in
# ~2s): how long to wait for hello after recycling, and how often a silent,
# unconfigured FL is allowed to cost that wait. A cold FL boot can take well
# beyond the first controller request, so this is a startup deadline rather
# than one request timeout.
RECYCLE_HELLO_WAIT_SECONDS = 120.0
RECYCLE_HELLO_RETRY_INITIAL_SECONDS = 1.0
RECYCLE_HELLO_RETRY_MAX_SECONDS = 8.0
RECYCLE_MIN_INTERVAL_SECONDS = 30.0

# FL can stay "unsafe at current time" for a few seconds after a load; give the
# controller's OnIdle deferral time to land a mutating call (v1-measured).
LIVE_SET_TIMEOUT_SECONDS = 8.0
LIVE_READ_TIMEOUT_SECONDS = 2.0

# E7 (2026-08-29, 3/3 PASS): FL parks "unsafe at current time" ops for OnIdle
# retry during boot, and a parked op can land LATE, undoing a newer one
# (issue #47). Waiting until a composite read answers makes transport ops
# reliable; measured settle was ~12s, so the bound leaves headroom.
SETTLE_POLL_SECONDS = 1.0


class ControlLink:
    """Owns the control wire, its factory, and the recycle clock. Everything
    else asks the link, never the raw wire."""

    control: ControlCarrier | None

    def __init__(
        self,
        control: ControlCarrier | None,
        factory: Callable[[], ControlCarrier],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.control = control
        self._control_factory = factory
        self._clock = clock
        self._sleep = sleep
        # -inf, not 0.0: the monotonic clock counts from boot, so 0.0 would
        # refuse the first recycle on a host booted under the interval ago.
        self._last_recycle = float("-inf")
        self._warned_controller_versions: set[str] = set()

    def _check_controller_protocol(self, hello: HelloEvent) -> HelloEvent:
        """Warn once for each incompatible controller that announces itself."""
        if not controller_protocol_matches(hello.script_ver) and hello.script_ver not in self._warned_controller_versions:
            self._warned_controller_versions.add(hello.script_ver)
            log.warning(
                "controller protocol version mismatch: controller reports %s, server expects %s; "
                "installed controller is stale vs this server - run fl-mcp-install and restart FL",
                hello.script_ver,
                CONTROLLER_PROTOCOL_VERSION,
            )
        return hello

    def hello(self) -> HelloEvent | None:
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
        hello: HelloEvent | None = None
        try:
            hello = self.control.await_hello(timeout=HELLO_PROBE_SECONDS)
            # The stored hello is STICKY across an FL death (dogfood A12): a
            # stale "connected" routes live ops into a void. A ping (answered
            # from the SysEx callback, milliseconds when alive) confirms the
            # wire is current before anything trusts it.
            if self.control.send_request("ping", timeout=PING_CONFIRM_SECONDS).ok:
                return self._check_controller_protocol(hello)
        except errors.ToolError as exc:
            # A contended shared wire is LIVE, not dead: another session's op
            # held it. Recycling here (reopening ports) would be wrong and would
            # make every control op flap under load. Trust the hello we have.
            if exc.category == "control_plane_busy":
                if hello is not None:
                    return self._check_controller_protocol(hello)
                # The wire was held before this session ever heard a hello
                # (two sessions starting at once, run 7). Live, not dead:
                # report "not connected yet" and let the caller retry; never
                # recycle, and never crash the server on an unbound name.
                log.debug("control wire busy before the first hello: %s", exc.message)
                return None
            log.debug("control hello probe failed: %s", exc.message)
        return self._recycle()

    def _recycle(self) -> HelloEvent | None:
        """Recycle a silent wire for a hot-plug bind; ``self.control`` is
        non-None (the caller's guard in :meth:`hello`)."""
        if not fl_process_running():
            return None  # nothing to bind to - FL is not even running
        now = self._clock()
        if now - self._last_recycle < RECYCLE_MIN_INTERVAL_SECONDS:
            return None  # recycled too recently - one wait per interval, not per call
        self._last_recycle = now
        log.info("FL is running but the wire is silent; recycling ports for a hot-plug bind")
        self.control.close()
        try:
            self.control = self._control_factory()
        except ControlUnavailable:
            log.exception("port recycle failed; control plane stays down")
            self.control = None
            return None
        deadline = self._clock() + RECYCLE_HELLO_WAIT_SECONDS
        retry_wait = RECYCLE_HELLO_RETRY_INITIAL_SECONDS
        attempt = 0
        last_error: errors.ToolError | None = None
        while True:
            attempt += 1
            remaining = max(0.0, deadline - self._clock())
            log.info(
                "awaiting FL hello after port recycle (attempt %d, %.1fs remaining)",
                attempt,
                remaining,
            )
            try:
                return self._check_controller_protocol(
                    self.control.await_hello(timeout=min(retry_wait, remaining))
                )
            except errors.ToolError as exc:
                last_error = exc
            remaining = deadline - self._clock()
            if remaining <= 0:
                detail = last_error.message if last_error is not None else "no detail"
                log.error(
                    "port recycle completed but FL did not answer hello within %gs after %d attempts: %s",
                    RECYCLE_HELLO_WAIT_SECONDS,
                    attempt,
                    detail,
                )
                return None
            pause = min(retry_wait, remaining)
            log.info(
                "FL has not answered hello after port recycle (attempt %d); retrying in %.1fs: %s",
                attempt,
                pause,
                last_error.message if last_error is not None else "no detail",
            )
            self._sleep(pause)
            retry_wait = min(retry_wait * 2, RECYCLE_HELLO_RETRY_MAX_SECONDS)

    def reconnect(self) -> None:
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
        self._last_recycle = float("-inf")

    def request(
        self,
        op: str,
        args: list[Any] | None = None,
        *,
        timeout: float,
    ) -> ControlResponse:
        """Forward to the wire; no wire raises control_plane_unavailable."""
        if self.control is None:
            raise errors.control_plane_unavailable(op, 0.0)
        return self.control.send_request(op, args=args, timeout=timeout)

    def command(self, op: str) -> None:
        """THE call-to-policy conversion (addendum A11), written once: no wire
        -> control_plane_unavailable; FL's unsafe-at-current-time refusal ->
        operation_unsafe (retryable - it means busy, not absent, addendum A6);
        any other refusal -> fl_api_error with FL's own detail. A send_request
        timeout raises control_plane_unavailable from the wire and propagates."""
        resp = self.request(op, timeout=LIVE_SET_TIMEOUT_SECONDS)
        if not resp.ok:
            detail = resp.err or "no detail"
            if errors.is_operation_unsafe(detail):
                raise errors.operation_unsafe(op)
            raise errors.fl_api_error(op, detail)

    def settle(self) -> None:
        """Block until FL answers a composite read, so no live op lands in the
        boot window where FL parks it for a LATE OnIdle retry (issue #47,
        settled by experiment E7). A timeout is logged, not raised: the reload
        itself succeeded, and the next live op will surface its own error."""
        if self.control is None:
            return
        deadline = self._clock() + FL_BOOT_TIMEOUT_SECONDS
        while self._clock() < deadline:
            try:
                if self.control.send_request("state.snapshot", timeout=5.0).ok:
                    return
            except errors.ToolError as exc:
                log.debug("settle snapshot probe failed: %s", exc.message)
            self._sleep(SETTLE_POLL_SECONDS)
        log.warning("FL did not settle within %ss of relaunch", FL_BOOT_TIMEOUT_SECONDS)
