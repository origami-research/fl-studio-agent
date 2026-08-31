"""The DISCOVERY pipeline: one Pass runner that folds evidence into the profile.

Discovery learns the version-specific facts a fresh FL build carries. Before
this module those facts were gathered by four hand-coded probe passes; here they
are DATA - a list of :class:`Pass` specs - run by ONE generic driver
(:func:`discover`). A new probe is a new Pass spec, never a new branch in the
driver. Reading a Pass tells you exactly what it asks FL and what it learns
(docs/30 Surface 4); reading :func:`discover` (once) tells you how every pass is
run, resumed, and folded.

The native Bridge stays the executor: a Pass emits ``wp_*`` :class:`Command`\\ s
that ride the same claim-once spool/UDP carriers as a note write, and its
``fold`` reads the ``result`` the Bridge acked back. This module is pure
orchestration - it never touches FL, only the :class:`Transport`.

Cross-lane seam: :class:`Transport` and :class:`Response` are the G2 transport
surface (docs/30 Surface 2), referenced here structurally so this lane builds
and tests in isolation. :class:`Command` and the response payload are plain data
the executor already speaks (``op`` + ``args`` -> a JSON ``result`` object). The
integration manager binds the concrete G2 types; nothing here hard-imports them.

Crash-resume: the sweep is EXPECTED to take FL down on a new build's unknown
ids. Two ledgers compose. The native quarantine ledger skips the one id a
command crashed on, so a re-issued command advances (per-id, inside the Bridge).
The :class:`Ledger` here records each completed command's response, so a
restarted run replays finished commands instead of re-issuing them (per-command,
across passes). Together: a crash STRICTLY advances discovery.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fl_mcp.profile import CapabilityProfile

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


# --- the wire types (structural; the executor already speaks them) -----------


@dataclass(frozen=True)
class Command:
    """One request to the Bridge executor: a ``wp_*`` op and its args.

    The executor validates and dispatches by ``op`` and acks a JSON ``result``
    object. A Pass builds these; the Transport frames and sends them.
    """

    op: str
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Response(Protocol):
    """What the Transport hands back for one Command (G2 Surface 2).

    Only the two fields discovery reads are named here: whether the command was
    acked, and the ``result`` object the Bridge's ``wp_*`` op returned.
    """

    ok: bool
    result: dict[str, Any]


class Transport(Protocol):
    """The one send path (G2 Surface 2), referenced structurally."""

    def request(self, cmd: Command, timeout: float) -> Response: ...


@dataclass(frozen=True)
class _Recorded:
    """A response reconstructed from the ledger on resume (satisfies Response)."""

    ok: bool
    result: dict[str, Any]


# --- the Pass surface (docs/30 Surface 4) ------------------------------------


class Pass(Protocol):
    """One discovery probe, stated as data.

    ``commands`` says what to ask FL for THIS profile (an empty list is valid -
    a static pass that only reasons over the profile it was handed). ``fold``
    turns the acked responses into a new profile, carrying forward everything it
    did not learn. Both are pure; the driver owns IO and resume.
    """

    name: str

    def commands(self, profile: CapabilityProfile) -> list[Command]: ...

    def fold(self, responses: list[Response], profile: CapabilityProfile) -> CapabilityProfile: ...


# --- the resume ledger -------------------------------------------------------


class Ledger(Protocol):
    """Per-command durable record so a restarted run skips finished work."""

    def get(self, key: str) -> Response | None: ...

    def record(self, key: str, response: Response) -> None: ...


class NullLedger:
    """No persistence: every command is issued fresh. The default."""

    def get(self, key: str) -> Response | None:
        return None

    def record(self, key: str, response: Response) -> None:
        return None


class FileLedger:
    """A resume ledger backed by one JSON file under the discovery run's dir.

    Each completed command is one ``{key: {ok, result}}`` entry, flushed after
    every record so a hard FL kill leaves finished passes recorded. Reading it
    back reconstructs a :class:`Response` the driver reuses in place of a re-issue.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, dict[str, Any]] = {}
        if path.is_file():
            try:
                self._entries = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("discovery ledger %s unreadable (%s); starting fresh", path, exc)
                self._entries = {}

    def get(self, key: str) -> Response | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        return _Recorded(ok=bool(entry["ok"]), result=dict(entry["result"]))

    def record(self, key: str, response: Response) -> None:
        self._entries[key] = {"ok": response.ok, "result": response.result}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._entries, indent=2) + "\n")


# --- the one driver ----------------------------------------------------------


def discover(
    passes: list[Pass],
    transport: Transport,
    profile: CapabilityProfile,
    *,
    ledger: Ledger | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CapabilityProfile:
    """Run each pass's commands over the transport, fold results into the profile.

    One loop, no per-pass branch: for every pass we ask it for commands against
    the profile so far, issue each (or replay it from the ledger on resume),
    record the acked ones, and let the pass fold its responses into the next
    profile. Passes see each other's learnings only through that evolving
    profile, so the sequence is the whole contract - reorder or extend it by
    editing the list, never this function.

    A command the transport reports as not-acked (``ok is False``) is folded in
    as-is - a pass decides what a refusal means; the driver never guesses. It is
    NOT recorded, so a resumed run re-issues it (the native quarantine ledger
    handles an id that actually crashed).
    """
    for spec in passes:
        commands = spec.commands(profile)
        responses: list[Response] = []
        for i, cmd in enumerate(commands):
            key = f"{spec.name}/{i}"
            cached = ledger.get(key) if ledger is not None else None
            if cached is not None:
                log.info("discovery: %s resumed from ledger", key)
                responses.append(cached)
                continue
            log.info("discovery: %s -> %s", key, cmd.op)
            response = transport.request(cmd, timeout)
            responses.append(response)
            if response.ok and ledger is not None:
                ledger.record(key, response)
        profile = spec.fold(responses, profile)
    return profile


__all__ = [
    "DEFAULT_TIMEOUT",
    "Command",
    "FileLedger",
    "Ledger",
    "NullLedger",
    "Pass",
    "Response",
    "Transport",
    "discover",
]
