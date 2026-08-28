"""Thin MCP layer. The product surface is the SDK; tools exist to (1) run
Python against it, (2) read state cheaply, (3) diagnose setup.

LANE W implements this module (docs/21). No module globals: the session is
built in the lifespan and injected into tools via the framework's Context
(verified supported by the installed MCP version). ``main()`` configures
logging to STDERR - stdout is the JSON-RPC wire, one stray line corrupts it.
"""

from __future__ import annotations

from typing import Any


def lifespan(server: Any) -> Any:
    """Async context manager: build the session (MIDI ports open BEFORE FL
    launches - docs/14 ordering), yield {"fl": sdk.connect(session)}, close
    the transport on shutdown."""
    raise NotImplementedError("Lane W")


def run(code: str, ctx: Any) -> dict[str, Any]:
    """MCP tool. Execute Python in a namespace holding ``fl`` (the SDK) and
    ``theory``. Returns {"stdout": ..., "result": repr-of-last-expression} or
    {"traceback": ...} - errors read like Python, no status-string zoo."""
    raise NotImplementedError("Lane W")


def get_notes(pattern: int, channel: int | str, ctx: Any) -> dict[str, Any]:
    """MCP tool. Read-only view of a target's notes with provenance and
    size-aware compression (full list when small, summary when large)."""
    raise NotImplementedError("Lane W")


def fl_doctor(ctx: Any) -> dict[str, Any]:
    """MCP tool. session.doctor().model_dump() - the only place the report
    becomes a dict."""
    raise NotImplementedError("Lane W")


def main() -> None:
    """Entry point: logging.basicConfig(stream=sys.stderr, level from
    $FL_MCP_LOG), construct the MCP server with the lifespan, run."""
    raise NotImplementedError("Lane W")
