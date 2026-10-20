"""Thin MCP layer. The product surface is the SDK; tools exist to (1) run
Python against it, (2) read state cheaply, (3) diagnose setup.

LANE W implements this module (docs/21). No module globals: the session is
built in the lifespan and injected into tools via the framework's Context
(verified supported by the installed MCP version). ``main()`` configures
logging to STDERR - stdout is the JSON-RPC wire, one stray line corrupts it.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from fl_mcp import __version__
from fl_mcp.audio import AudioAnalysis
from fl_mcp.contract import (
    ENV_KERNEL_ROOT,
    GetNotesResult,
    GetSceneResult,
    RenderResult,
    RunResult,
    SceneNotesResult,
    SceneSummary,
    ToolErrorResult,
    build_version,
)
from fl_mcp.errors import ToolError
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import Workspace

from . import scene, sdk
from .schemas import DoctorReport, Target
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
    """Async context manager: build the session after FL launches so MIDI ports
    hot-plug into the running host, yield {"fl": sdk.connect(session)} and the
    run kernel host, close both on shutdown."""
    session = create_session()
    session.start_audio_stream_lifecycle()
    fl = sdk.connect(session)
    with contextlib.ExitStack() as cleanup:
        # The box names a per-job root and owns its lifetime; a dev run gets a
        # temp root removed at shutdown.
        root = os.environ.get(ENV_KERNEL_ROOT) or cleanup.enter_context(
            tempfile.TemporaryDirectory(prefix="flmcp-kernel-", ignore_cleanup_errors=True)
        )
        kernel = KernelHost(
            session, Workspace.create(Path(root)), session.platform.kernel_launcher(), os.environ
        )
        # "session" rides along because doctor() and provenance reads are session
        # surface, deliberately not exposed on the SDK object the model drives.
        try:
            yield {"fl": fl, "session": session, "kernel": kernel}
        finally:
            kernel.close()
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
    """Run a Python cell in a persistent kernel bound to the live FL Studio
    session, like a Jupyter notebook. Variables, imports, and functions persist
    across calls. It is full Python: import anything, use files, numpy,
    time.sleep, type/dir/getattr. `%pip install pkg` (or `!pip install pkg`)
    installs a package for later imports; `!cmd` runs a shell command. The
    working directory is your workspace, the one place you can write; its files
    last for the session. Pre-bound: fl (the SDK), theory, Note,
    AutomationPoint. A trailing expression is repr'd like a REPL.

    Returns {"outcome", "stdout", "result" | "traceback"}; stdout includes
    stderr. outcome is "ok", or "error" (read the traceback and fix the code),
    or "interrupted" when the cell ran past its 300 s budget (time inside fl
    calls does not count) - the kernel and its variables are intact, or "kernel_lost" when
    the kernel process died or was killed (os._exit, a crash, or ignoring the
    interrupt) - its variables are gone, files stay, the next run starts fresh.

    How the session behaves: every fl call applies to FL immediately and
    returns once FL has it, or raises with the reason. A read right after a
    write returns the written notes; there is nothing to wait for, save, or
    reopen. To hear the result, render.

    The SDK (no introspection needed):
    - fl.set_tempo(140.0) -> the tempo FL now has; fl.tempo reads it.
    - fl.channels -> [Channel(index, name), ...].
    - fl.track("Bass") / fl.track(2, pattern=1) -> Track (exact name, index, or
      unique substring; pattern defaults to 1).
    - Track.write([Note(...), ...], mode="merge"|"replace") -> WriteResult(wrote,
      track, pattern, mode, total): "merge" adds the notes, "replace" swaps the
      pattern's notes on that track; total is how many it now has.
      Track.notes() -> the notes it has now; Track.clear() empties it.
    - Note(key=60 or "C4"/"F#3", start=0.0, length=1.0, velocity=0.8, pan=0.5)
      with start/length in beats from pattern start.
    - Track.set_channel_levels(volume=0.9, pan=-0.15, pitch=0) - volume 0..1,
      pan -1..1, pitch whole semitones -48..48; returns FL's values.
    - Track.params(query="cutoff") / Track.set_param("cutoff", 0.55) - the
      instrument's parameters, values normalized 0..1.
    - fl.render("mix.wav") -> Path of the WAV. Only the file name is used: it
      lands in the session's renders folder, which you can read but not write.
      fl.analyze(path) -> bpm/key/LUFS/spectral balance; fl.read_wav(path) ->
      (samples: numpy.ndarray, sample_rate: int).
      A render plays the song in real time (FL's loop mode decides: the
      playlist in song mode, only the selected pattern in pattern mode), so
      render once when the pass is done; the harness listens back after you
      say you are done. To check notes and tempo, use get_notes/get_scene
      (instant) instead.
    - fl.snapshot() -> tempo + channels.
    - During a match FL is never reopened: fl.save() is not needed (and a
      trial FL cannot save), and Track.set_automation, fl.add_effect and
      fl.file_edit() are unavailable because they rewrite the saved song.
    - theory.chord("C", "minor") / theory.scale("E", "minor") -> MIDI ints;
      theory.drums("x...x...x...x...", key="C1", step_beats=0.25) -> a
      ready-to-write [Note] step grid ('x' = hit, '.' = rest).
    """
    # Engineering notes (not part of the model-facing description above): the
    # kernel is a separate process (fl_mcp.kernel, docs/38) and the OS, not
    # Python filtering, confines it to its workspace. A test keeps the
    # docstring's "300 s" in step with kernel.host.RUN_CELL_TIMEOUT_SECONDS.
    return ctx.request_context.lifespan_context["kernel"].run(code)


@_tool_boundary
def get_notes(pattern: int, channel: int | str, ctx: Context) -> GetNotesResult:
    """The notes on one channel's pattern as FL has them now, including every
    write made this session. A list when small, a summary when large.

    ``channel`` is an index; for a channel by name use run with
    fl.track("Lead").notes().
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

    result: dict[str, Any] = {"target": target.model_dump(), "note_count": len(notes)}
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
    """Setup diagnostics for whoever runs this engine. Not needed to check your
    work: edits apply immediately and reads return what FL has."""
    session = ctx.request_context.lifespan_context["session"]
    return session.doctor()


# A render is only listenable if the caller can retrieve it; wavBase64 is
# capped so a long project cannot balloon one tool reply past what a model
# request can carry. 12 MiB of WAV is ~90 s of stereo 44.1 kHz.
RENDER_WAV_BASE64_MAX_BYTES = 12 * 1024 * 1024
_LAST_RENDER_KEY = "last_render"


@_tool_boundary
def render(ctx: Context) -> RenderResult:
    """Play the song once from its start and record the mix to a WAV: how to hear
    your edits. FL's loop mode decides what plays: the playlist in song mode, the
    selected pattern in pattern mode. Returns {"path", "wavBase64", "span"}:
    wavBase64 is the WAV in base64 (omitted with a note past the size cap); span
    says what played (source "song" or "pattern"), its live beats and tempo, and
    the seconds recorded. A render takes about that long, ten minutes at most.
    Call analyze afterwards for measurements."""
    session = ctx.request_context.lifespan_context["session"]
    try:
        rendered = session.render_wav(_fresh_render_path(ctx))
    except ToolError as exc:
        return _tool_error_response(_tool_error_result(exc))
    path = rendered.path
    ctx.request_context.lifespan_context[_LAST_RENDER_KEY] = path
    result: dict[str, Any] = {"path": str(path), "span": rendered.span}
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
            path = fl.render(_fresh_render_path(ctx))
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
    """Read what is in FL now - tempo, key, and what each channel has, including
    every write made this session and the current live tempo.

    Call this before writing, so additions fit what is already there. It returns
    an analysis rather than a note dump: a 56-note project summarises to about
    20 tokens instead of ~960, and the summary answers the questions that
    actually get asked.

    Args:
        detail: "summary" (default) for tempo, inferred key and a per-channel
            digest - note count, span in beats, register, pitch classes, and for
            bass-register parts the root motion; channels with no notes are
            listed as such. Use "notes" only when you need individual notes,
            such as harmonising an existing line.
        channel: With detail="notes", limit to one channel index.

    Returns:
        For "summary", a `summary` string is included that is already formatted
        for reading. Key inference carries a confidence and can be wrong on
        modal or chromatic material - treat "low" as "no reliable key".
    """
    if detail not in ("summary", "notes"):
        return _tool_error_response(
            ToolErrorResult(
                error="invalid_argument",
                message=f'detail must be "summary" or "notes", got {detail!r}',
                fix='Use detail="summary" or detail="notes".',
                retryable=False,
            )
        )
    session = ctx.request_context.lifespan_context["session"]
    project = session.project
    if project is None or not project.is_file():
        log.warning("get_scene needs the agent project file; none is configured")
        return _tool_error_response(
            ToolErrorResult(
                error="invalid_argument",
                message="The song's layout cannot be read in this session.",
                fix="Use get_notes or run with fl.track(...).notes() to read notes.",
                retryable=False,
            )
        )
    rows = scene.rows_from_notes(session.all_notes())
    if detail == "notes":
        return GetSceneResult.from_notes(
            SceneNotesResult.model_validate({"notes": scene.notes_detail(rows, channel)})
        )
    names = {c.index: c.name for c in session.channel_names()}
    return GetSceneResult.from_summary(
        SceneSummary.model_validate(scene.summarize(project, rows, names, tempo=session.tempo()))
    )


def _fresh_render_path(ctx: Context) -> Path:
    """A new WAV path in the renders folder, so run can read the result too."""
    renders = ctx.request_context.lifespan_context["kernel"].workspace.renders
    return Path(tempfile.mkdtemp(prefix="render-", dir=renders)) / "render.wav"


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
