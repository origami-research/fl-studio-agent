"""One send path over interchangeable carriers (docs/30 Surface 2).

Transport owns everything the carriers must agree on and therefore must not
each re-implement: arg validation (schemas.OP_ARGS), the sequence-numbered id
space, and the pick of the best LIVE carrier per call. A carrier owns only
its medium's framing.

Exactly-once across carriers (the #58 invariant, now generic): every command
id comes from ONE allocator resumed past the consumer's consumed-ledger and
every artifact any carrier left behind (carriers.spool.resume_next_id), and a
command that hops carriers - a send-time refusal such as an oversized
datagram - keeps its id, so the consumer's fail-closed ledger dedupes no
matter which medium delivers first.

A timeout does NOT hop: an unanswered command may still apply on a later tick
(docs/22), so re-sending it through another carrier could double-apply. The
session's breaker owns retry policy (docs/27 §3); this module only guarantees
that whatever is sent, on whichever medium, carries a never-reused id.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

from fl_mcp.schemas import OP_ARGS, SpoolCommand, SpoolOp, SpoolResponse

from .carriers import Carrier, CarrierUnavailable
from .carriers.spool import resume_next_id, spool_roots


class Transport:
    """One send path; carriers listed best-capability first (UDP before the
    file spool), each asked ``is_live()`` per call - evidence, never a flag."""

    def __init__(self, carriers: list[Carrier], *, roots: list[Path] | None = None) -> None:
        """``roots`` is the id-resume seam (tmp_path in tests); defaults to
        the real spool tree, where the consumer's ledger lives regardless of
        which carrier delivers."""
        self._carriers = carriers
        # The lock serializes id allocation: MCP dispatches tool calls on
        # concurrent threads, and two sends sharing an id correlates the wrong
        # FL response to the wrong command.
        self._lock = threading.Lock()
        self._next_id = resume_next_id(roots if roots is not None else spool_roots())
        self._sent: dict[int, Carrier] = {}  # id -> the carrier that took it

    def command(self, op: str, **args: Any) -> SpoolCommand:
        """Schema-validated (schemas.OP_ARGS), sequence-numbered. Resolve
        unions at the boundary: the consumer sees the validated, normalized
        form ("C4" is already 60), never the caller's raw input."""
        if op not in OP_ARGS:
            raise ValueError(f"unknown spool op {op!r}; expected one of {sorted(OP_ARGS)}")
        validated = OP_ARGS[op](**args).model_dump(mode="json")
        with self._lock:
            # The membership check above is what makes this cast honest.
            cmd = SpoolCommand(id=self._next_id, op=cast(SpoolOp, op), args=validated)
            self._next_id += 1
        return cmd

    def request(self, cmd: SpoolCommand, timeout: float) -> SpoolResponse:
        """docs/30 Surface 2, frozen: dispatch on the best live carrier and
        block for that carrier's response."""
        return self._dispatch(cmd).await_response(cmd.id, timeout)

    # The staged halves request() composes - today's call-sites send, record
    # bookkeeping, then block; both shapes share the one dispatch rule.

    def send(self, op: str, **args: Any) -> SpoolCommand:
        cmd = self.command(op, **args)
        self._sent[cmd.id] = self._dispatch(cmd)
        return cmd

    def await_response(self, command: SpoolCommand, timeout: float) -> SpoolResponse:
        carrier = self._sent.pop(command.id, None)
        if carrier is None:
            raise CarrierUnavailable(f"command {command.id} was not sent by this transport")
        return carrier.await_response(command.id, timeout)

    def _dispatch(self, cmd: SpoolCommand) -> Carrier:
        """The ONE carrier decision (R4 B3-1, generic): the first live carrier
        takes the command; a send-time refusal (CarrierUnavailable) falls
        through to the next WITH THE SAME ID."""
        last: CarrierUnavailable | None = None
        for carrier in self._carriers:
            if not carrier.is_live():
                continue
            try:
                carrier.send(cmd)
            except CarrierUnavailable as exc:
                last = exc
                continue
            return carrier
        raise last or CarrierUnavailable(f"no live carrier took command {cmd.id}")
