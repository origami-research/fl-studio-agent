"""Thin MCP layer: one tool, ``run``. The product surface is the SDK (``fl``) and
plain Python in an OS-confined kernel; reading the song, checking it sounds,
rendering and listening, files and shell are all code in ``run`` (owner's
decision 2026-11-03, which replaced get_scene, check, render, analyze and the
workspace tools). Setup diagnostics are the operator's (``fl-mcp-doctor``), not a
model tool. ``--workspace-only`` serves ``run`` with no FL session.

LANE W implements this module (docs/21). No module globals: the session is
built in the lifespan and injected into tools via the framework's Context
(verified supported by the installed MCP version). ``main()`` configures
logging to STDERR - stdout is the JSON-RPC wire, one stray line corrupts it.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pydantic_core
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel

from fl_mcp import __version__
from fl_mcp.contract import (
    ENV_KERNEL_ROOT,
    WORKSPACE_ONLY_FLAG,
    RunResult,
    ToolErrorResult,
    server_description,
)
from fl_mcp.errors import ToolError
from fl_mcp.kernel.display import ShownImage
from fl_mcp.kernel.host import KernelHost
from fl_mcp.kernel.launch import Workspace
from fl_mcp.platform import PlatformUnsupportedError, platform_for

from .session import create_session

log = logging.getLogger(__name__)

SERVER_NAME = "fl-studio"
LOG_LEVEL_ENV = "FL_MCP_LOG"


@contextlib.asynccontextmanager
async def lifespan(server: Any, *, with_fl: bool = True) -> Any:
    """Async context manager: build the session after FL launches so MIDI ports
    hot-plug into the running host, and the run kernel host bound to it; close
    both on shutdown. ``with_fl=False`` (--workspace-only) starts no session: the
    kernel runs plain Python and its fl calls are refused."""
    # Each close is its own callback, run in reverse order, so one that fails
    # (a revoke icacls refuses) cannot skip the rest.
    with contextlib.ExitStack() as cleanup:
        workspace = Workspace.create(_kernel_root(cleanup))
        if with_fl:
            session = create_session()
            session.start_audio_stream_lifecycle()
            cleanup.callback(session.close)
            launcher = session.platform.kernel_launcher()
        else:
            session = None
            launcher = platform_for(sys.platform).kernel_launcher()
        kernel = KernelHost(session, workspace, launcher, os.environ)
        cleanup.callback(kernel.close)
        yield {"kernel": kernel}


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
    """Run Python in a persistent kernel bound to the live FL Studio session, like
    a notebook cell; this is the only tool, so everything is code. Variables and
    imports persist. The working directory is your workspace, the one place you
    can write (files last for the session); open(), subprocess and `!cmd` work,
    and `%pip install pkg` adds any package (the internet is reachable).
    Pre-bound: fl, Note, theory, display. A trailing expression is repr'd.

    Returns {"outcome", "stdout", "result" | "traceback", "actions", "images"}:
    actions is what FL did, one sentence per change, even if the cell failed
    partway. outcome is "ok", "error", "interrupted" (past the 300 s budget;
    time inside fl calls does not count; variables kept) or "kernel_lost"
    (process died; variables gone, files kept). Images show like a notebook's:
    end with a matplotlib figure or PIL image, call display(...), or leave
    figures open; at most 4 per run, about width x height / 750 tokens each.

    fl applies every call to FL at once and returns what FL now has, or raises.
    Channels are a name, index or unique part of a name.
    Read:  fl.tempo; fl.channels (index, name, about: what the project says the
           channel is for, volume, pan, muted, mixer_track); fl.mixer (tracks in
           use: volume, pan, muted, solo, effects, sends); fl.playlist (clips);
           fl.patterns; fl.track(ch, pattern=1).notes(); fl.scene() (a digest:
           tempo, key, what each channel plays).
    Write: fl.set_tempo(bpm); t = fl.track(ch); t.write([Note(60, 0.0, 1.0,
           0.8)], mode="merge"|"replace") (key or "C4", start and length in
           beats, velocity); t.clear(); t.set_levels(volume=0..1, pan=-1..1,
           pitch=semitones); t.params("cutoff") / t.set_param("cutoff", 0.55);
           fl.route(ch, mixer_track); fl.send(src, dst, level=0.8, on=True).
    Sounds: fl.sounds("808 kick", kind="sample"|"preset") -> stock sounds (path
           relative to fl.stock_folder, readable); fl.load_sample(ch, sound);
           fl.load_preset(ch, sound); fl.presets(ch); fl.set_preset(ch, name).
    Hear:  fl.render("mix.wav", bars=, start_bar=1, solo=[ch], mute=[ch],
           stems="channels"|"mixer"|[ch]) -> the WAV's Path with .span (start_bar,
           beats, tempo_bpm, seconds); it plays in real time. Stems are one play
           each, a list: only channels with notes in those bars (.skipped are the
           rest), or exactly the channels listed.
           fl.meters(bars=2, start_bar=1) -> which channels sounded, from FL's
           mixer meters (.summary). fl.read_wav(path) -> (samples, rate), for a
           render or a stock sample.
    Project: fl.save() (a trial FL cannot); fl.undo() (after a solo render,
           write the track back instead: FL's undo holds the mute changes).
    theory.chord("C", "minor"), theory.scale("E", "minor") -> MIDI ints;
    theory.drums("x...x...", key="C1", step_beats=0.25) -> [Note].

    Installed: numpy, scipy, soundfile, librosa, matplotlib, pyloudnorm, mido,
    music21, Pillow. Listening is yours to build. A spectrogram, bar lines:
      import matplotlib.pyplot as plt, numpy as np
      wav = fl.render("mix.wav"); x, sr = fl.read_wav(wav)
      plt.specgram(x.mean(axis=1), Fs=sr, NFFT=4096, cmap="magma")
      for t in np.arange(0, wav.span.seconds, 240 / wav.span.tempo_bpm): plt.axvline(t, c="w", lw=.5)
    The bass alone against the kick:
      spec = lambda w: abs(np.fft.rfft(fl.read_wav(w)[0].mean(axis=1)))
      bass, kick = (spec(fl.render(f"{c}.wav", bars=4, solo=[c])) for c in ("Bass", "Kick"))
      plt.figure(); plt.loglog(bass, label="bass"); plt.loglog(kick, label="kick"); plt.legend()
    """
    # Engineering notes (not part of the model-facing description above): the
    # kernel is a separate process (fl_mcp.kernel, docs/38) and the OS, not
    # Python filtering, confines it to its workspace. A test keeps the
    # docstring's "300 s" in step with kernel.host.RUN_CELL_TIMEOUT_SECONDS.
    ran = ctx.request_context.lifespan_context["kernel"].run(code)
    return _with_images(ran, ran.shown)


def tool_definitions() -> tuple[Any, ...]:
    """The engine's tool surface: run, and nothing else."""
    return (run,)


def _with_images(result: BaseModel, images: list[ShownImage]) -> Any:
    """The result as the model reads it, with each image after it as MCP image
    content: a picture the client shows the model, never base64 in the text."""
    if not images:
        return result
    return CallToolResult(
        content=[
            TextContent(type="text", text=pydantic_core.to_json(result, indent=2).decode()),
            *(
                ImageContent(type="image", data=base64.b64encode(image.data).decode(), mime_type=image.mime_type)
                for image in images
            ),
        ],
        structured_content=result.model_dump(mode="json"),
    )


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


def create_server(*, workspace_only: bool = False) -> MCPServer:
    """Create the versioned MCP server without opening its transport: the one
    tool, ``run``; ``workspace_only`` starts no FL session behind it."""
    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        description=server_description(_confined()),
        lifespan=functools.partial(lifespan, with_fl=not workspace_only),
    )
    for tool in tool_definitions():
        server.add_tool(tool)
    return server


def _confined() -> bool:
    """Whether this host's launcher confines model code; an unsupported OS runs none."""
    try:
        confined = platform_for(sys.platform).kernel_launcher().confined
    except PlatformUnsupportedError:
        return False
    if not confined:
        log.warning("run is NOT confined on this host: model code has your access")
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
        help="serve run with no FL session: plain Python in the confined kernel, fl calls refused",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get(LOG_LEVEL_ENV, "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,  # stdout is the JSON-RPC wire; no prior handler may hold it
    )
    create_server(workspace_only=args.workspace_only).run()
