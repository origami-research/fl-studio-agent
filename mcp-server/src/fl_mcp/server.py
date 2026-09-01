"""Thin MCP layer. The product surface is the SDK; tools exist to (1) run
Python against it, (2) read state cheaply, (3) diagnose setup.

LANE W implements this module (docs/21). No module globals: the session is
built in the lifespan and injected into tools via the framework's Context
(verified supported by the installed MCP version). ``main()`` configures
logging to STDERR - stdout is the JSON-RPC wire, one stray line corrupts it.
"""

from __future__ import annotations

import ast
import contextlib
import io
import logging
import os
import sys
import traceback
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError

from fl_mcp import theory
from fl_mcp.errors import ToolError

from . import flp, scene, sdk
from .schemas import AutomationPoint, Note, Target
from .session import create_session

log = logging.getLogger(__name__)

SERVER_NAME = "fl-studio"
LOG_LEVEL_ENV = "FL_MCP_LOG"
# Above this, get_notes returns a summary instead of the full list - a model
# should not have to page through 4000 notes to learn the shape of a part.
GET_NOTES_FULL_LIST_MAX = 200
GET_NOTES_SAMPLE_SIZE = 20


@contextlib.asynccontextmanager
async def lifespan(server: Any) -> Any:
    """Async context manager: build the session (MIDI ports open BEFORE FL
    launches - docs/14 ordering), yield {"fl": sdk.connect(session)}, close
    the transport on shutdown."""
    session = create_session()
    fl = sdk.connect(session)
    # "session" rides along because doctor() and provenance reads are session
    # surface, deliberately not exposed on the SDK object the model drives.
    try:
        yield {"fl": fl, "session": session}
    finally:
        if session.control is not None:
            session.control.close()


def run(code: str, ctx: Context) -> dict[str, Any]:
    """Execute Python against the live FL Studio session. The namespace holds
    ``fl`` (the SDK), ``theory``, ``Note``, and ``AutomationPoint``. A trailing
    expression is repr'd like a REPL. Returns {"stdout", "result"} or, on any
    error, {"stdout", "traceback"} - read the traceback and fix the call.

    The full working surface (no introspection needed):
    - fl.set_tempo(140.0) -> confirmed BPM; fl.tempo reads it back.
    - fl.channels -> [Channel(index, name), ...].
    - fl.track("Bass") / fl.track(2, pattern=1) -> Track (exact name, index, or
      unique substring; pattern defaults to 1).
    - Track.write([Note(...), ...], mode="merge"|"replace") -> receipt with
      confirmed_notes; Track.notes() -> confirmed state; Track.clear().
    - Note(key=60 or "C4"/"F#3", start=0.0, length=1.0, velocity=0.8, pan=0.5)
      with start/length in beats from pattern start.
    - Track.set_automation([AutomationPoint(position=0.0, value=0.5,
      tension=0.0)]) - position in beats, value normalized 0..1.
    - Track.params(query="cutoff") / Track.set_param("cutoff", 0.55) - live
      generator plugin parameters, values normalized 0..1.
    - fl.render("out.wav") -> Path; fl.analyze(wav) -> bpm/key/LUFS/spectral
      balance; fl.read_wav(path) -> (samples, sample_rate).
    - fl.snapshot() -> tempo + channels.
    - theory.chord("C", "minor") / theory.scale("E", "minor") -> MIDI ints;
      theory.drums("x...x...x...x...", key="C1", step_beats=0.25) -> a
      ready-to-write [Note] step grid ('x' = hit, '.' = rest).

    Every write returns FL's confirmed readback - trust it over your intent.
    """
    # Engineering notes (not part of the model-facing description above):
    # ``Note``/``AutomationPoint`` are in the namespace because ``Track.write``
    # takes ``list[Note]`` and ``Track.set_automation`` takes
    # ``list[AutomationPoint]`` - without them writes are inexpressible.
    # ``theory.drums`` returns v2 ``schemas.Note`` and composes directly with
    # ``Track.write`` (an earlier claim here that v2 rejects it was refuted by
    # direct construction+write on 2026-09-01).
    fl = ctx.request_context.lifespan_context["fl"]
    namespace: dict[str, Any] = {
        "fl": fl,
        "theory": theory,
        "Note": Note,
        "AutomationPoint": AutomationPoint,
    }
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return {"traceback": "".join(traceback.format_exception_only(exc))}

    # REPL semantics: a trailing expression is evaluated and repr'd, so
    # `fl.tempo` answers like Python instead of printing nothing.
    last_expr: ast.Expression | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)

    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(tree, "<fl>", "exec"), namespace)
            result = eval(compile(last_expr, "<fl>", "eval"), namespace) if last_expr else None
    except BaseException as exc:  # noqa: BLE001 - the REPL boundary: user code (incl. sys.exit) must never kill the server; its error IS the result
        frames = exc.__traceback__.tb_next if exc.__traceback__ else None
        return {
            "stdout": stdout.getvalue(),
            "traceback": "".join(traceback.format_exception(type(exc), exc, frames)),
        }
    return {
        "stdout": stdout.getvalue(),
        "result": None if result is None else repr(result),
    }


def get_notes(pattern: int, channel: int | str, ctx: Context) -> dict[str, Any]:
    """MCP tool. Read-only view of a target's notes with provenance and
    size-aware compression (full list when small, summary when large).

    ``channel`` is an index (a numeric string is tolerated - JSON models pass
    "3"); names resolve in the SDK, via ``run`` with ``fl.track("Lead")``.
    """
    session = ctx.request_context.lifespan_context["session"]
    try:
        target = Target(pattern=pattern, channel=channel)  # pydantic coerces "3" -> 3
        notes = session.read_notes(target)
    except ValidationError as exc:
        return {
            "error": str(exc),
            "fix": 'pattern >= 1 and channel >= 0, as integers. For a channel by name, use run: fl.track("Lead").notes()',
        }
    except ToolError as exc:
        return exc.to_dict()

    ref = session.store.ref_at(target)
    result: dict[str, Any] = {
        "target": target.model_dump(),
        "provenance": None if ref is None else ref.provenance,
        "as_of": None if ref is None else ref.as_of.isoformat(),
        "note_count": len(notes),
    }
    if len(notes) <= GET_NOTES_FULL_LIST_MAX:
        result["notes"] = [n.canonical() for n in notes]
        return result
    result["summary"] = {
        "keys": {"min": min(n.key for n in notes), "max": max(n.key for n in notes)},
        "span_beats": {
            "start": min(n.start for n in notes),
            "end": max(n.start + n.length for n in notes),
        },
        "sample": [n.canonical() for n in sorted(notes, key=lambda n: n.start)[:GET_NOTES_SAMPLE_SIZE]],
        "note": f"{len(notes)} notes is too many to list; this is a summary. Filter in run() for detail.",
    }
    return result


def fl_doctor(ctx: Context) -> dict[str, Any]:
    """MCP tool. session.doctor().model_dump() - the only place the report
    becomes a dict."""
    session = ctx.request_context.lifespan_context["session"]
    return session.doctor().model_dump()


def get_scene(ctx: Context, detail: str = "summary", channel: int | None = None) -> dict[str, Any]:
    """Read what is currently in the project - key, tempo, and what each channel has.

    Call this before writing, so additions fit what is already there. It returns
    an analysis rather than a note dump: a 56-note project summarises to about
    20 tokens instead of ~960, and the summary answers the questions that
    actually get asked.

    Args:
        detail: "summary" (default) for tempo, inferred key and a per-channel
            digest - note count, span in beats, register, pitch classes, and for
            bass-register parts the root motion. Use "notes" only when you need
            individual notes, such as harmonising an existing line.
        channel: With detail="notes", limit to one channel index.

    Returns:
        For "summary", a `summary` string is included that is already formatted
        for reading. Key inference carries a confidence and can be wrong on
        modal or chromatic material - treat "low" as "no reliable key".
    """
    session = ctx.request_context.lifespan_context["session"]
    project = session.project
    if project is None:
        return {"error": "No project file configured. Run: python -m fl_mcp.make_agent_project"}

    if detail == "notes":
        return {"notes": scene.notes_detail(project, channel)}
    if detail != "summary":
        return {"error": f'detail must be "summary" or "notes", got {detail!r}'}

    proj = flp.read(project)
    names = {c.index: c.name for c in proj.channels}
    return scene.summarize(project, names, tempo=proj.tempo)


def main() -> None:
    """Entry point: logging.basicConfig(stream=sys.stderr, level from
    $FL_MCP_LOG), construct the MCP server with the lifespan, run."""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get(LOG_LEVEL_ENV, "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,  # stdout is the JSON-RPC wire; no prior handler may hold it
    )
    server = MCPServer(SERVER_NAME, lifespan=lifespan)
    for tool in (run, get_notes, get_scene, fl_doctor):
        server.add_tool(tool)
    server.run()
