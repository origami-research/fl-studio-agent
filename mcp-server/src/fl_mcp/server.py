"""Thin MCP layer. The product surface is the SDK; tools exist to (1) run
Python against it, (2) read state cheaply, (3) diagnose setup.

LANE W implements this module (docs/21). No module globals: the session is
built in the lifespan and injected into tools via the framework's Context
(verified supported by the installed MCP version). ``main()`` configures
logging to STDERR - stdout is the JSON-RPC wire, one stray line corrupts it.
"""

from __future__ import annotations

import ast
import base64
import builtins
import contextlib
import functools
import io
import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from fl_mcp import __version__, theory
from fl_mcp.audio import AudioAnalysis
from fl_mcp.contract import (
    GetNotesResult,
    GetSceneResult,
    RenderResult,
    RunResult,
    SceneNotesResult,
    SceneSummary,
    ToolErrorResult,
    build_version,
)
from fl_mcp.errors import ToolError, restricted_namespace

from . import flp, scene, sdk
from .schemas import AutomationPoint, DoctorReport, Note, Target
from .session import create_session

log = logging.getLogger(__name__)

SERVER_NAME = "fl-studio"
LOG_LEVEL_ENV = "FL_MCP_LOG"
# Above this, get_notes returns a summary instead of the full list - a model
# should not have to page through 4000 notes to learn the shape of a part.
GET_NOTES_FULL_LIST_MAX = 200
GET_NOTES_SAMPLE_SIZE = 20

_RUN_STDLIB_MODULES = frozenset(
    {
        "math",
        "random",
        "itertools",
        "functools",
        "statistics",
        "json",
        "re",
        "dataclasses",
        "typing",
        "collections",
    }
)
# Named so the refusal names the offender: getattr/type/vars would let a
# string reach the dunder attributes the AST guard refuses syntactically.
# dir() only returns names (it cannot reach an attribute), and producers use it
# to discover the SDK (#198), so it stays available.
_RUN_REFUSED_BUILTINS = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "globals",
        "locals",
        "input",
        "breakpoint",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "type",
        "object",
        "super",
        "help",
        "memoryview",
    }
)
_RUN_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        # values, containers, iteration
        "dir",
        "abs",
        "all",
        "any",
        "bool",
        "callable",
        "chr",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "hash",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        # exceptions, so ordinary try/except music code does not NameError
        "Exception",
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "IndexError",
        "KeyError",
        "LookupError",
        "NameError",
        "RuntimeError",
        "StopIteration",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
    )
}


def _run_import(
    name: str, globals: Any = None, locals: Any = None, fromlist: Any = (), level: int = 0
) -> Any:
    """The import hook used only by producer code in ``run``."""
    root = name.split(".", 1)[0]
    if level or root not in _RUN_STDLIB_MODULES:
        raise restricted_namespace(f"import {name!r}")
    return builtins.__import__(name, globals, locals, fromlist, level)


class _RunNamespaceGuard(ast.NodeVisitor):
    """Reject source that can escape the deliberately small producer namespace."""

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._check_import(node.module or ".", node.level)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _RUN_REFUSED_BUILTINS or node.id == "__builtins__":
            raise restricted_namespace(f"builtin {node.id!r}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            raise restricted_namespace(f"attribute {node.attr!r}")
        self.generic_visit(node)

    @staticmethod
    def _check_import(name: str, level: int = 0) -> None:
        if level or name.split(".", 1)[0] not in _RUN_STDLIB_MODULES:
            raise restricted_namespace(f"import {name!r}")


@contextlib.asynccontextmanager
async def lifespan(server: Any) -> Any:
    """Async context manager: build the session after FL launches so MIDI ports
    hot-plug into the running host, yield {"fl": sdk.connect(session)}, close
    the transport on shutdown."""
    session = create_session()
    session.start_audio_stream_lifecycle()
    fl = sdk.connect(session)
    # "session" rides along because doctor() and provenance reads are session
    # surface, deliberately not exposed on the SDK object the model drives.
    try:
        yield {"fl": fl, "session": session}
    finally:
        log.info("run session closed with %d refusals", session.run_refusals)
        session.close()


def _tool_boundary(fn: Any) -> Any:
    """Keep every failure at the MCP boundary structurally parseable."""

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return _tool_error_response(_tool_error_result(exc))
        except Exception as exc:
            log.exception("unexpected exception executing tool %s", fn.__name__)
            return _tool_error_response(
                ToolErrorResult(
                    error="internal_error",
                    message=str(exc),
                    fix="Report this; the engine kept running.",
                    retryable=False,
                )
            )

    return wrapped


@_tool_boundary
def run(code: str, ctx: Context) -> RunResult:
    """Execute Python against the live FL Studio session. The namespace holds
    ``fl`` (the SDK), ``theory``, ``Note``, ``AutomationPoint``, and the allowlisted modules ``math``,
    ``random``, ``itertools``, ``functools``, ``statistics``, ``json``, ``re``,
    ``dataclasses``, ``typing``, and ``collections``. A trailing expression is
    repr'd like a REPL. Returns {"stdout", "result"} or, on any error,
    {"stdout", "traceback"} - read the traceback and fix the call.

    The full working surface (no introspection needed):
    - fl.set_tempo(140.0) -> confirmed BPM; fl.tempo reads it back.
    - fl.channels -> [Channel(index, name), ...].
    - fl.track("Bass") / fl.track(2, pattern=1) -> Track (exact name, index, or
      unique substring; pattern defaults to 1).
    - Track.write([Note(...), ...], mode="merge"|"replace") writes to the
      channel named by Track, selecting and verifying it live when needed;
      it returns a receipt with confirmed_notes. Track.notes() -> confirmed
      state; Track.clear() clears that named channel.
    - Note(key=60 or "C4"/"F#3", start=0.0, length=1.0, velocity=0.8, pan=0.5)
      with start/length in beats from pattern start.
    - Track.set_channel_levels(volume=0.9, pan=-0.15, pitch=0) - project-file
      channel levels; volume 0..1, pan -1..1, pitch whole semitones -48..48.
    - Track.set_automation([AutomationPoint(position=0.0, value=0.5,
      tension=0.0)]) - position in beats, value normalized 0..1 (file lane;
      unavailable in session mode).
    - Track.params(query="cutoff") / Track.set_param("cutoff", 0.55) - live
      generator plugin parameters, values normalized 0..1.
    - fl.add_effect(0, "Fruity Parametric EQ 2") -> tuple[Effect, ...] -
      project-file only; proven only for empty Master slot 0.
    - fl.render("out.wav") -> Path; fl.analyze(wav) -> bpm/key/LUFS/spectral
      balance; fl.read_wav(path) -> (samples, sample_rate).
      A render takes about a minute and produces the real mix, so render once
      when the pass is done; the harness listens back after you say you are
      done; use get_notes/get_scene (cheap, live) to check work instead.
    - fl.snapshot() -> tempo + channels.
    - Session mode (FLMCP_SPOOL_ROOT set, including a match) never reboots FL.
      Use notes through the Bridge and tempo, params, or levels through the control
      wire. `fl.file_edit()` is unavailable there because automation clips and adding
      effects still need a project reload. Outside session mode, `with fl.file_edit():`
      batches those file-lane edits into one reload.
    - theory.chord("C", "minor") / theory.scale("E", "minor") -> MIDI ints;
      theory.drums("x...x...x...x...", key="C1", step_beats=0.25) -> a
      ready-to-write [Note] step grid ('x' = hit, '.' = rest).

    Every write returns FL's confirmed readback when available. If a bridge
    command applied but live state could not be read back, its receipt has
    ``unconfirmed_write=True`` and ``confirmed_notes=None``.
    """
    # Engineering notes (not part of the model-facing description above):
    # ``Note`` is in the namespace because ``Track.write`` takes ``list[Note]``;
    # without it the primary write path is inexpressible.
    # ``theory.drums`` returns v2 ``schemas.Note`` and composes directly with
    # ``Track.write`` (an earlier claim here that v2 rejects it was refuted by
    # direct construction+write on 2026-09-01).
    fl = ctx.request_context.lifespan_context["fl"]
    namespace: dict[str, Any] = {
        "fl": fl,
        "theory": theory,
        "Note": Note,
        "AutomationPoint": AutomationPoint,
        "__builtins__": {**_RUN_BUILTINS, "__import__": _run_import},
    }
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return RunResult(stdout="", traceback="".join(traceback.format_exception_only(exc)))

    # REPL semantics: a trailing expression is evaluated and repr'd, so
    # `fl.tempo` answers like Python instead of printing nothing.
    last_expr: ast.Expression | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)

    try:
        _RunNamespaceGuard().visit(tree)
        if last_expr is not None:
            _RunNamespaceGuard().visit(last_expr)
    except ToolError:
        ctx.request_context.lifespan_context["session"].record_run_refusal()
        raise

    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(tree, "<fl>", "exec"), namespace)
            result = eval(compile(last_expr, "<fl>", "eval"), namespace) if last_expr else None
    except BaseException as exc:  # noqa: BLE001 - the REPL boundary: user code (incl. sys.exit) must never kill the server; its error IS the result
        frames = exc.__traceback__.tb_next if exc.__traceback__ else None
        return RunResult(
            stdout=stdout.getvalue(),
            traceback="".join(traceback.format_exception(type(exc), exc, frames)),
        )
    return RunResult(stdout=stdout.getvalue(), result=None if result is None else repr(result))


@_tool_boundary
def get_notes(pattern: int, channel: int | str, ctx: Context) -> GetNotesResult:
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
        return _tool_error_response(
            ToolErrorResult(
                error="invalid_argument",
                message=str(exc),
                fix='pattern >= 1 and channel >= 0, as integers. For a channel by name, use run: fl.track("Lead").notes()',
                retryable=False,
            )
        )
    except ToolError as exc:
        return _tool_error_response(_tool_error_result(exc))

    ref = session.store.ref_at(target)
    result: dict[str, Any] = {
        "target": target.model_dump(),
        "provenance": None if ref is None else ref.provenance,
        "as_of": None if ref is None else ref.as_of.isoformat(),
        "note_count": len(notes),
    }
    if len(notes) <= GET_NOTES_FULL_LIST_MAX:
        result["notes"] = [n.canonical() for n in notes]
        return GetNotesResult.model_validate(result)
    result["summary"] = {
        "keys": {"min": min(n.key for n in notes), "max": max(n.key for n in notes)},
        "span_beats": {
            "start": min(n.start for n in notes),
            "end": max(n.start + n.length for n in notes),
        },
        "sample": [n.canonical() for n in sorted(notes, key=lambda n: n.start)[:GET_NOTES_SAMPLE_SIZE]],
        "note": f"{len(notes)} notes is too many to list; this is a summary. Filter in run() for detail.",
    }
    return GetNotesResult.model_validate(result)


@_tool_boundary
def fl_doctor(ctx: Context) -> DoctorReport:
    """MCP tool. Return the typed session diagnostic report."""
    session = ctx.request_context.lifespan_context["session"]
    return session.doctor()


# A render is only listenable if the caller can retrieve it; wavBase64 is
# capped so a long project cannot balloon one tool reply past what a model
# request can carry. 12 MiB of WAV is ~90 s of stereo 44.1 kHz.
RENDER_WAV_BASE64_MAX_BYTES = 12 * 1024 * 1024
_LAST_RENDER_KEY = "last_render"


@_tool_boundary
def render(ctx: Context) -> RenderResult:
    """Play the loop once and record the live mix to a WAV for listening. Returns {"path",
    "wavBase64"} (base64 of the WAV, omitted with a note when the file
    exceeds the size cap). A render takes about the loop's length.
    project_saved is false only on a trial FL whose save before the capture
    did not land (project_save_detail says why); the WAV is still verified.
    Call analyze afterwards for measurements."""
    fl = ctx.request_context.lifespan_context["fl"]
    output = Path(tempfile.mkdtemp(prefix="flmcp-render-")) / "render.wav"
    try:
        receipt = fl.render_receipt(output)
    except ToolError as exc:
        return _tool_error_response(_tool_error_result(exc))
    path = receipt.path
    ctx.request_context.lifespan_context[_LAST_RENDER_KEY] = path
    result: dict[str, Any] = {
        "path": str(path),
        "project_saved": receipt.project_saved,
        "project_save_detail": receipt.project_save_detail,
    }
    data = path.read_bytes()
    if len(data) <= RENDER_WAV_BASE64_MAX_BYTES:
        result["wavBase64"] = base64.b64encode(data).decode("ascii")
    else:
        result["note"] = (
            f"WAV is {len(data)} bytes (> {RENDER_WAV_BASE64_MAX_BYTES}); read it from path instead."
        )
    return RenderResult.model_validate(result)


@_tool_boundary
def analyze(ctx: Context, wav: str | None = None) -> AudioAnalysis:
    """Measure a rendered WAV: bpm, key, integrated LUFS, true peak, and
    spectral balance. ``wav`` defaults to the most recent render (rendering
    fresh if none exists yet)."""
    fl = ctx.request_context.lifespan_context["fl"]
    try:
        path = Path(wav) if wav else ctx.request_context.lifespan_context.get(_LAST_RENDER_KEY)
        if path is None:
            path = fl.render(Path(tempfile.mkdtemp(prefix="flmcp-render-")) / "render.wav")
            ctx.request_context.lifespan_context[_LAST_RENDER_KEY] = path
        return AudioAnalysis.model_validate(fl.analyze(path))
    except ToolError as exc:
        return _tool_error_response(_tool_error_result(exc))
    except OSError as exc:
        return _tool_error_response(
            ToolErrorResult(
                error="invalid_argument",
                message=str(exc),
                fix="Pass the path returned by render, or call render first.",
                retryable=False,
            )
        )


@_tool_boundary
def get_scene(ctx: Context, detail: str = "summary", channel: int | None = None) -> GetSceneResult:
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
        return _tool_error_response(
            ToolErrorResult(
                error="invalid_argument",
                message="No project file configured.",
                fix="Run: python -m fl_mcp.make_agent_project",
                retryable=False,
            )
        )

    if detail == "notes":
        return GetSceneResult.from_notes(
            SceneNotesResult.model_validate({"notes": scene.notes_detail(project, channel)})
        )
    if detail != "summary":
        return _tool_error_response(
            ToolErrorResult(
                error="invalid_argument",
                message=f'detail must be "summary" or "notes", got {detail!r}',
                fix='Use detail="summary" or detail="notes".',
                retryable=False,
            )
        )

    proj = flp.read(project)
    names = {c.index: c.name for c in proj.channels}
    return GetSceneResult.from_summary(
        SceneSummary.model_validate(scene.summarize(project, names, tempo=proj.tempo))
    )


def tool_definitions() -> tuple[Any, ...]:
    """The engine's complete MCP tool surface, in stable presentation order."""
    return run, get_notes, get_scene, render, analyze, fl_doctor


def _tool_error_result(exc: ToolError) -> ToolErrorResult:
    return ToolErrorResult(
        error=exc.category,
        message=exc.message,
        fix=exc.fix,
        retryable=exc.retryable,
        candidates=exc.candidates,
    )


def _tool_error_response(error: ToolErrorResult) -> CallToolResult:
    """Deliver an anticipated engine failure through MCP's isError channel."""
    return CallToolResult(
        content=[TextContent(type="text", text=error.model_dump_json())],
        is_error=True,
    )


def create_server() -> MCPServer:
    """Create the versioned MCP server without opening its transport."""
    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        description=f"FL Studio MCP {build_version()}",
        lifespan=lifespan,
    )
    for tool in tool_definitions():
        server.add_tool(tool)
    return server


def main() -> None:
    """Entry point: logging.basicConfig(stream=sys.stderr, level from
    $FL_MCP_LOG), construct the MCP server with the lifespan, run."""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get(LOG_LEVEL_ENV, "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,  # stdout is the JSON-RPC wire; no prior handler may hold it
    )
    create_server().run()
