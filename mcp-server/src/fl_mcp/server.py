"""Thin MCP layer. The product surface is the SDK; tools exist to (1) run
Python against it, (2) read the song cheaply, (3) prove it sounds: check in
seconds, render to hear it. Setup diagnostics are the operator's
(``fl-mcp-doctor``), not a model tool. The surface is docs/06 "Tool surface".
Behind FLMCP_WORKSPACE_TOOLS=1, or alone under --workspace-only, it also
serves the workspace tools: shell and file tools confined like run (docs/39).

LANE W implements this module (docs/21). No module globals: the session is
built in the lifespan and injected into tools via the framework's Context
(verified supported by the installed MCP version). ``main()`` configures
logging to STDERR - stdout is the JSON-RPC wire, one stray line corrupts it.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent

from fl_mcp import __version__, actions, errors
from fl_mcp.actions import ActionLog
from fl_mcp.contract import (
    ENV_KERNEL_ROOT,
    WORKSPACE_ONLY_FLAG,
    AnalyzeResult,
    CheckToolResult,
    EditFileResult,
    GetSceneResult,
    GlobResult,
    GrepMode,
    GrepResult,
    ReadFileResult,
    RenderResult,
    RunResult,
    SceneNotesResult,
    SceneSummary,
    ShellResult,
    ToolErrorResult,
    WriteFileResult,
    server_description,
    workspace_tools_from_env,
)
from fl_mcp.errors import ToolError
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import Workspace
from fl_mcp.platform import PlatformUnsupportedError, platform_for
from fl_mcp.workspace import ops
from fl_mcp.workspace.runner import FILE_OP_TIMEOUT_SECONDS, SHELL_GRACE_SECONDS, OpTimeout, WorkspaceRunner

from . import scene, sdk
from .schemas import RenderReceipt
from .session import create_session

log = logging.getLogger(__name__)

SERVER_NAME = "fl-studio"
LOG_LEVEL_ENV = "FL_MCP_LOG"


@contextlib.asynccontextmanager
async def lifespan(server: Any, *, workspace_tools: bool = False) -> Any:
    """Async context manager: build the session after FL launches so MIDI ports
    hot-plug into the running host, yield {"fl": sdk.connect(session)} and the
    run kernel host (plus the workspace tools' runner when they are on), close
    them all on shutdown."""
    session = create_session()
    session.start_audio_stream_lifecycle()
    fl = sdk.connect(session)
    # Each close is its own callback, run in reverse order, so one that fails
    # (a revoke icacls refuses) cannot skip the rest.
    with contextlib.ExitStack() as cleanup:
        workspace = Workspace.create(_kernel_root(cleanup))
        cleanup.callback(session.close)
        # One launcher for both, so the workspace is granted once (kernel.windows).
        launcher = session.platform.kernel_launcher()
        kernel = KernelHost(session, workspace, launcher, os.environ)
        cleanup.callback(kernel.close)
        # "session" rides along for the reads and checks the tools make directly.
        context: dict[str, Any] = {"fl": fl, "session": session, "kernel": kernel}
        if workspace_tools:
            context["workspace"] = WorkspaceRunner(workspace, launcher, os.environ)
            cleanup.callback(context["workspace"].close)
        yield context


@contextlib.asynccontextmanager
async def workspace_lifespan(server: Any) -> Any:
    """--workspace-only: no FL session, only the workspace tools' runner (docs/39)."""
    with contextlib.ExitStack() as cleanup:
        workspace = Workspace.create(_kernel_root(cleanup))
        runner = WorkspaceRunner(workspace, platform_for(sys.platform).kernel_launcher(), os.environ)
        try:
            yield {"workspace": runner}
        finally:
            runner.close()


def _kernel_root(cleanup: contextlib.ExitStack) -> Path:
    """The box names a per-job root and owns its lifetime; a dev run gets a
    temp root removed at shutdown."""
    return Path(
        os.environ.get(ENV_KERNEL_ROOT)
        or cleanup.enter_context(
            tempfile.TemporaryDirectory(prefix="flmcp-kernel-", ignore_cleanup_errors=True)
        )
    )


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


_LAST_RENDER_KEY = "last_render"


@_tool_boundary
def render(ctx: Context) -> RenderResult:
    """Play the song once from its start and record the mix to a WAV: how to hear
    your edits. FL's loop mode decides what plays: the playlist in song mode, the
    selected pattern in pattern mode. Returns {"path", "bytes", "span"}: path is
    the WAV file (run code reads it with fl.read_wav or fl.analyze), bytes its
    size; span says what played (source "song" or "pattern"), its live beats and
    tempo, and the seconds recorded. The audio stays in the file, never in this
    result. A render takes about that long, ten minutes at most, so render once
    when the pass is done (check proves audibility in seconds). Call analyze
    afterwards for measurements."""
    # The WAV never goes into the result as text: a 23 s render is ~5.8M tokens of
    # base64, which overflows the model's context on the next request in any real
    # client. A person or client that wants the audio opens the file at path.
    actions = ActionLog()
    try:
        rendered = _render(ctx, actions)
    except ToolError as exc:
        return _tool_error_response(_tool_error_result(exc))
    return RenderResult(
        path=str(rendered.path),
        bytes=rendered.path.stat().st_size,
        span=rendered.span,
        actions=actions.sentences(),
    )


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


# -- the workspace tools (docs/39) ----------------------------------------------------
# Served only when FLMCP_WORKSPACE_TOOLS=1 or under --workspace-only. Each call
# runs in its own confined process (fl_mcp.workspace); docstring tests keep the
# limits they state in step with fl_mcp.workspace.ops.


@_tool_boundary
def shell(command: str, ctx: Context, timeout_s: int = ops.SHELL_TIMEOUT_DEFAULT) -> ShellResult:
    """Run a shell command in your workspace folder and return its output.
    Commands run in cmd.exe on Windows (the FL Studio host) and /bin/sh on
    macOS/Linux. python and pip are on PATH: pip installs into the workspace,
    where python (and run, when you have it) can import the package.

    cmd.exe runs one line: chain steps with &&, or write a script file and run
    it. Each call is a fresh process started in the workspace: cd, environment
    variables and background processes do not carry over, and anything still
    running when the command returns is stopped. On Windows and macOS the
    operating system confines the command: it can read and write only the
    workspace, read the renders folder, and reach the public internet, not
    this machine's own services.

    Args:
        command: the command line, up to 16 KB.
        timeout_s: seconds before the command is stopped, 1 to 600 (default 120).

    Returns {exit_code, timed_out, stdout, stderr, truncated, full_output,
    duration_s, note, actions}. A non-zero exit_code is a normal result, not an
    error. Output past 16,000 characters keeps its start and end: truncated is
    true and full_output names a file in the workspace with all of it (read it
    with read_file or search it with grep)."""
    if len(command.encode("utf-8")) > ops.SHELL_COMMAND_BYTES or not command.strip():
        raise errors.invalid_argument("shell", "command must be 1 byte to 16 KB of text")
    if not 1 <= timeout_s <= ops.SHELL_TIMEOUT_MAX:
        raise errors.invalid_argument(
            "shell", f"timeout_s must be 1 to {ops.SHELL_TIMEOUT_MAX}, got {timeout_s}"
        )
    runner = _workspace(ctx)
    number = runner.next_shell_number()
    log.info("workspace shell %d: %s", number, command[:500])
    try:
        found = runner.call(
            "shell",
            {"command": command, "timeout_s": timeout_s, "log_number": number},
            timeout=timeout_s + SHELL_GRACE_SECONDS,
        )
    except OpTimeout:
        found = {
            "exit_code": None,
            "timed_out": True,
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "full_output": None,
            "duration_s": float(timeout_s + SHELL_GRACE_SECONDS),
            "note": "The command ran past its limit and was stopped; its output was lost.",
        }
    sentence = actions.ran_command(command, found.get("exit_code"), found.get("duration_s", 0.0), timeout_s)
    return ShellResult.model_validate({**found, "actions": [sentence]})


@_tool_boundary
def read_file(path: str, ctx: Context, offset: int = 1, limit: int = ops.READ_LINES) -> ReadFileResult:
    """Read a text file with line numbers. path is relative to your workspace
    (like notes/plan.md) or absolute; offset is the first line to show (from 1,
    default 1) and limit how many lines (1 to 2000, default 2000). Lines longer
    than 2000 characters are cut, and one result holds at most 16,000
    characters of content: note then says which offset reads on. A binary file
    returns its size and kind (midi, audio, image, fl_song, archive, binary)
    with a hint instead of content.

    Returns {path, kind, bytes, content, start_line, end_line, total_lines,
    truncated, hint, note, actions}."""
    if offset < 1:
        raise errors.invalid_argument("read_file", f"offset is a line number from 1, got {offset}")
    if not 1 <= limit <= ops.READ_LINES:
        raise errors.invalid_argument("read_file", f"limit must be 1 to {ops.READ_LINES}, got {limit}")
    found = _file_op(ctx, "read_file", {"path": path, "offset": offset, "limit": limit})
    return ReadFileResult.model_validate({**found, "actions": []})


@_tool_boundary
def write_file(path: str, content: str, ctx: Context) -> WriteFileResult:
    """Write a text file in your workspace, replacing it if it exists; missing
    parent folders are created. path is relative to the workspace (like
    notes/plan.md) or absolute. content is up to 1 MB of text, written exactly
    as given in UTF-8. The renders folder is read-only.

    Returns {path, bytes, created, actions}."""
    if len(content.encode("utf-8")) > ops.WRITE_BYTES:
        raise errors.invalid_argument("write_file", "content is over 1 MB; write it in parts or with shell")
    found = _file_op(ctx, "write_file", {"path": path, "content": content})
    return WriteFileResult.model_validate(
        {**found, "actions": [actions.wrote_file(found.get("path", path), found.get("bytes", 0))]}
    )


@_tool_boundary
def edit_file(
    path: str, old_string: str, new_string: str, ctx: Context, replace_all: bool = False
) -> EditFileResult:
    """Replace exact text in a file. old_string must appear exactly once
    (include surrounding lines to make it unique), or set replace_all to replace
    every occurrence. Matching is exact, including spaces, indentation and line
    endings. Each string is up to 256 KB. When old_string is missing or not
    unique, the file is left unchanged and the error says which.

    Returns {path, replacements, actions}."""
    for name, value in (("old_string", old_string), ("new_string", new_string)):
        if len(value.encode("utf-8")) > ops.EDIT_STRING_BYTES:
            raise errors.invalid_argument(
                "edit_file", f"{name} is over 256 KB; use write_file for large changes"
            )
    found = _file_op(
        ctx,
        "edit_file",
        {"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all},
    )
    return EditFileResult.model_validate(
        {**found, "actions": [actions.edited_file(found.get("path", path), found.get("replacements", 0))]}
    )


@_tool_boundary
def glob(pattern: str, ctx: Context, path: str | None = None) -> GlobResult:
    """Find files by name, newest first. pattern uses * (within one folder),
    ** (any number of folders) and ?, for example "**/*.mid" or "notes/*.md";
    path is the folder to search (default: your workspace). At most 500 paths.
    The workspace's own .packages, .cache, .tmp and .appdata folders are skipped
    unless path points inside them; linked folders are not followed.

    Returns {paths, truncated, note, actions}."""
    return GlobResult.model_validate(
        {**_file_op(ctx, "glob", {"pattern": pattern, "path": path}), "actions": []}
    )


@_tool_boundary
def grep(
    pattern: str,
    ctx: Context,
    path: str | None = None,
    glob: str | None = None,
    case_insensitive: bool = False,
    output_mode: GrepMode = "files",
) -> GrepResult:
    """Search file contents line by line with a Python regular expression.
    path is a file or folder (default: your workspace); glob keeps only files
    whose name matches (for example "*.py", or "notes/**/*.md" for a path).
    output_mode "files" (default) lists matching files newest first, "lines"
    gives "path:line: text", "count" gives "path: N" matching lines per file.
    At most 200 results. Binary files and files over 20 MB are skipped, and so
    are the workspace's .packages, .cache, .tmp and .appdata folders unless path
    points inside them.

    Returns {mode, matches, truncated, note, actions}."""
    args = {
        "pattern": pattern,
        "path": path,
        "glob": glob,
        "case_insensitive": case_insensitive,
        "output_mode": output_mode,
    }
    return GrepResult.model_validate({**_file_op(ctx, "grep", args), "actions": []})


def _workspace(ctx: Context) -> WorkspaceRunner:
    return ctx.request_context.lifespan_context["workspace"]


_NAMED_ARGS = ("path", "pattern", "glob")


def _file_op(ctx: Context, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """A file operation in its confined process; one that runs too long is stopped and refused."""
    for name in _NAMED_ARGS:
        if len(args.get(name) or "") > ops.NAME_CHARS:
            raise errors.invalid_argument(op, f"{name} is over {ops.NAME_CHARS} characters")
    log.info("workspace %s %s", op, args.get("path") or args.get("pattern") or "")
    try:
        return _workspace(ctx).call(op, args, timeout=FILE_OP_TIMEOUT_SECONDS)
    except OpTimeout as exc:
        raise ToolError(
            "invalid_argument",
            f"{op} ran past {FILE_OP_TIMEOUT_SECONDS:g} s and was stopped.",
            "Narrow the path, the pattern or the glob, or do it with shell.",
        ) from exc


def _render(ctx: Context, actions: ActionLog) -> RenderReceipt:
    """Render to a new WAV in the renders folder, so run can read the result too;
    analyze measures it next unless told otherwise."""
    output = ctx.request_context.lifespan_context["kernel"].workspace.fresh_render()
    rendered = ctx.request_context.lifespan_context["session"].render_wav(output)
    actions.called("render_wav", {"output": output}, rendered)
    ctx.request_context.lifespan_context[_LAST_RENDER_KEY] = rendered.path
    return rendered


def tool_definitions() -> tuple[Any, ...]:
    """The engine's FL tool surface, in stable presentation order."""
    return run, get_scene, check, render, analyze


def workspace_tool_definitions() -> tuple[Any, ...]:
    """The workspace tools (docs/39), in stable presentation order."""
    return shell, read_file, write_file, edit_file, glob, grep


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


def create_server(*, workspace_only: bool = False, workspace_tools: bool = False) -> MCPServer:
    """Create the versioned MCP server without opening its transport.

    The default is the FL tools alone. ``workspace_tools`` adds the workspace
    tools beside them; ``workspace_only`` serves the workspace tools alone,
    with no FL session.
    """
    if workspace_only:
        chosen_lifespan: Any = workspace_lifespan
        tools = workspace_tool_definitions()
    elif workspace_tools:
        chosen_lifespan = functools.partial(lifespan, workspace_tools=True)
        tools = tool_definitions() + workspace_tool_definitions()
    else:
        chosen_lifespan = lifespan
        tools = tool_definitions()
    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        description=server_description(_confined()),
        lifespan=chosen_lifespan,
    )
    for tool in tools:
        server.add_tool(tool)
    return server


def _confined() -> bool:
    """Whether this host's launcher confines model code; an unsupported OS runs none."""
    try:
        confined = platform_for(sys.platform).kernel_launcher().confined
    except PlatformUnsupportedError:
        return False
    if not confined:
        log.warning("run and the workspace tools are NOT confined on this host: model code has your access")
    return confined


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point: logging.basicConfig(stream=sys.stderr, level from
    $FL_MCP_LOG), construct the MCP server with the lifespan, run.

    MCP clients start it with no arguments; ``--help`` and ``--version`` exist
    so a person checking an install gets an answer instead of a server waiting
    on stdin."""
    parser = argparse.ArgumentParser(
        prog="fl-mcp",
        description="The FL Studio MCP server, over stdio. Your MCP client starts it; "
        "set up FL once with fl-mcp-install and check it with fl-mcp-doctor.",
    )
    parser.add_argument("--version", action="version", version=f"fl-studio-agent {__version__}")
    parser.add_argument(
        WORKSPACE_ONLY_FLAG,
        action="store_true",
        help="serve only the workspace tools (shell and file tools), with no FL session (docs/39)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get(LOG_LEVEL_ENV, "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,  # stdout is the JSON-RPC wire; no prior handler may hold it
    )
    create_server(
        workspace_only=args.workspace_only, workspace_tools=workspace_tools_from_env(os.environ)
    ).run()
