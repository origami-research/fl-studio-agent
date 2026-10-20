"""Thin MCP layer. The product surface is the SDK; tools exist to (1) run
Python against it, (2) read the song cheaply, (3) prove it sounds: check in
seconds, render to hear it. Setup diagnostics are the operator's
(``fl-mcp-doctor``), not a model tool. The surface is docs/06 "Tool surface".

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

from fl_mcp import __version__
from fl_mcp.actions import ActionLog
from fl_mcp.contract import (
    ENV_KERNEL_ROOT,
    AnalyzeResult,
    CheckToolResult,
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
from .schemas import RenderReceipt
from .session import create_session

log = logging.getLogger(__name__)

SERVER_NAME = "fl-studio"
LOG_LEVEL_ENV = "FL_MCP_LOG"


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
        # "session" rides along for the reads and checks the tools make directly.
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

    Returns {"outcome", "stdout", "result" | "traceback", "actions"}; stdout
    includes stderr, and actions lists what FL did, one sentence per change,
    even when the cell failed partway. outcome is "ok", or "error" (read the
    traceback and fix the code), or "interrupted" when the cell ran past its
    300 s budget (time inside fl calls does not count) - the kernel and its
    variables are intact, or "kernel_lost" when the kernel process died or was
    killed (os._exit, a crash, or ignoring the interrupt) - its variables are
    gone, files stay, the next run starts fresh.

    How to work: write, check at milestones, render at the end. Every fl call
    applies to FL immediately and returns once FL has it, or raises with the
    reason; its result is what FL now has. There is nothing to wait for, save,
    reopen or re-read. To prove parts are audible, check (a few seconds); to
    hear the song, render once when the pass is done.

    The SDK (no introspection needed):
    - fl.set_tempo(140.0) -> the tempo FL now has; fl.tempo reads it.
    - fl.channels -> [Channel(index, name, ...), ...].
    - fl.track("Bass") / fl.track(2, pattern=1) -> Track (exact name, index, or
      unique substring; pattern defaults to 1).
    - Track.write([Note(...), ...], mode="merge"|"replace") -> WriteResult(wrote,
      track, pattern, mode, total): "merge" adds the notes, "replace" swaps the
      pattern's notes on that track; total is how many it now has.
      Track.notes() -> the notes it has now; Track.clear() empties it.
    - Note(60, 0.0, 1.0, 0.8) = Note(key=60, start=0.0, length=1.0,
      velocity=0.8, pan=0.5): by position or name, in that order; key is a
      MIDI int or a name like "C4"/"F#3"; start/length in beats from pattern start.
    - Track.set_channel_levels(volume=0.9, pan=-0.15, pitch=0) - volume 0..1,
      pan -1..1, pitch whole semitones -48..48; returns FL's values.
    - Track.params(query="cutoff") / Track.set_param("cutoff", 0.55) - the
      instrument's parameters, values normalized 0..1.
    - fl.check(bars=2, start_bar=1) -> CheckResult: plays those bars and reads
      FL's mixer meters; per channel whether it sounded, its peak dBFS, and why
      it was silent (muted, no notes in those bars, ...); .summary is one line.
    - fl.render("mix.wav") -> Path of the WAV. Only the file name is used: it
      lands in the session's renders folder, which you can read but not write.
      A render plays the whole song in real time (FL's loop mode decides: the
      playlist in song mode, only the selected pattern in pattern mode).
      fl.analyze(path) -> bpm/key/LUFS/spectral balance; fl.read_wav(path) ->
      (samples: numpy.ndarray, sample_rate: int).
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
def check(ctx: Context, bars: int = 2, start_bar: int = 1) -> CheckToolResult:
    """Prove the parts are audible in a few seconds: play ``bars`` bars (1 to 8,
    default 2) from ``start_bar`` (default 1) and read FL's own mixer meters.
    FL stops afterwards and nothing in the song changes.

    Returns per channel: name, sounded (true/false), peak_dbfs, and the reason
    when it was silent and FL shows why (muted, its mixer track muted, volume 0,
    no notes in those bars); sounded is null only for a channel that plays
    straight into the master, which cannot be measured on its own. Also the
    master's peak and a one-line summary. Check after a batch of writes; render
    once at the end to hear the song."""
    session = ctx.request_context.lifespan_context["session"]
    checked = session.check(start_bar, bars)
    actions = ActionLog()
    actions.called("check", {"start_bar": start_bar, "bars": bars}, checked)
    return CheckToolResult(**checked.model_dump(), actions=actions.sentences())


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
    the seconds recorded. A render takes about that long, ten minutes at most,
    so render once when the pass is done (check proves audibility in seconds).
    Call analyze afterwards for measurements."""
    actions = ActionLog()
    try:
        rendered = _render(ctx, actions)
    except ToolError as exc:
        return _tool_error_response(_tool_error_result(exc))
    path = rendered.path
    result: dict[str, Any] = {"path": str(path), "span": rendered.span, "actions": actions.sentences()}
    data = path.read_bytes()
    if len(data) <= RENDER_WAV_BASE64_MAX_BYTES:
        result["wavBase64"] = base64.b64encode(data).decode("ascii")
    else:
        result["note"] = (
            f"WAV is {len(data)} bytes (> {RENDER_WAV_BASE64_MAX_BYTES}); read it from path instead."
        )
    return RenderResult.model_validate(result)


@_tool_boundary
def analyze(ctx: Context, wav: str | None = None) -> AnalyzeResult:
    """Measure a rendered WAV: bpm, key, integrated LUFS, true peak, and
    spectral balance. ``wav`` defaults to the most recent render (rendering
    fresh if none exists yet)."""
    fl = ctx.request_context.lifespan_context["fl"]
    actions = ActionLog()
    try:
        path = Path(wav) if wav else ctx.request_context.lifespan_context.get(_LAST_RENDER_KEY)
        if path is None:
            path = _render(ctx, actions).path
        measured = fl.analyze(path)
        actions.measured(measured, path.name if wav else "the render")
        return AnalyzeResult(**measured.model_dump(), actions=actions.sentences())
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
def get_scene(
    ctx: Context, detail: str = "summary", channel: int | None = None, pattern: int | None = None
) -> GetSceneResult:
    """Read what is in FL now - tempo, key, and what each channel has, including
    every write made this session and the current live tempo. This is the one
    read: its answer is current, so there is no need to read again after a write.

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
        pattern: With detail="notes", limit to one pattern number.

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
                fix='Read a track\'s notes with run: fl.track("Bass").notes().',
                retryable=False,
            )
        )
    rows = scene.rows_from_notes(session.all_notes())
    if detail == "notes":
        return GetSceneResult.from_notes(
            SceneNotesResult.model_validate({"notes": scene.notes_detail(rows, channel, pattern)})
        )
    names = {c.index: c.name for c in session.channel_names()}
    return GetSceneResult.from_summary(
        SceneSummary.model_validate(scene.summarize(project, rows, names, tempo=session.tempo()))
    )


def _render(ctx: Context, actions: ActionLog) -> RenderReceipt:
    """Render to a new WAV in the renders folder, so run can read the result too;
    analyze measures it next unless told otherwise."""
    output = ctx.request_context.lifespan_context["kernel"].workspace.fresh_render()
    rendered = ctx.request_context.lifespan_context["session"].render_wav(output)
    actions.called("render_wav", {"output": output}, rendered)
    ctx.request_context.lifespan_context[_LAST_RENDER_KEY] = rendered.path
    return rendered


def tool_definitions() -> tuple[Any, ...]:
    """The engine's complete MCP tool surface, in stable presentation order."""
    return run, get_scene, check, render, analyze


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
