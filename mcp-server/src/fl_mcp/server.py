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

from fl_mcp import __version__, manual
from fl_mcp.contract import (
    ENV_KERNEL_ROOT,
    WORKSPACE_ONLY_FLAG,
    FLLicense,
    RunResult,
    ToolErrorResult,
    fl_license_from_env,
    server_description,
)
from fl_mcp.errors import ToolError
from fl_mcp.kernel.display import ShownImage
from fl_mcp.kernel.host import RUN_BUDGET_SECONDS, KernelHost, run_budget_from_env
from fl_mcp.kernel.launch import Workspace
from fl_mcp.platform import PlatformUnsupportedError, platform_for

from .session import create_session

log = logging.getLogger(__name__)

SERVER_NAME = "fl-studio"
# What clients that defer tools show before the model opens run's description: Claude
# Code lists only tool names and server instructions until the model searches for a tool,
# and "run" alone says nothing about music.
SERVER_INSTRUCTIONS = (
    "Make and edit music in a live FL Studio session: use the run tool (Python with the fl SDK); "
    "its description is the manual."
)
WORKSPACE_ONLY_INSTRUCTIONS = (
    "A Python workspace with no FL Studio session: use the run tool; its description says what it has."
)
LOG_LEVEL_ENV = "FL_MCP_LOG"


@contextlib.asynccontextmanager
async def lifespan(server: Any, *, with_fl: bool = True) -> Any:
    """Async context manager: build the session after FL launches so MIDI ports
    hot-plug into the running host, and the run kernel host bound to it; close
    both on shutdown. ``with_fl=False`` (--workspace-only) starts no session: the
    kernel runs plain Python and its fl calls are refused."""
    run_budget = run_budget_from_env(os.environ)  # a bad setting fails before FL starts
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
        kernel = KernelHost(session, workspace, launcher, os.environ, run_budget=run_budget)
        cleanup.callback(kernel.close)
        kernel.start()  # ahead of the first run: a cold machine is slowest to start one (#324)
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
    """Run Python in the kernel bound to this session (the description is set below)."""
    # The model reads manual.tool_description, generated from manual.KERNEL, sdk.py's
    # docstrings and fl_mcp/guides and filled with this host's values (create_server):
    # with an FL session, the session facts and the index of what that FL can use (a trial
    # FL's leaves out fl.save); without one (--workspace-only, where fl calls are refused),
    # the kernel and the guide index only. The kernel is a separate process (fl_mcp.kernel,
    # docs/38) and the OS, not Python filtering, confines it to its workspace.
    ran = ctx.request_context.lifespan_context["kernel"].run(code)
    return _with_images(ran, ran.shown)


def run_description(
    *, fl_license: FLLicense | None, confined: bool = True, run_budget: float = RUN_BUDGET_SECONDS
) -> str:
    """What the model reads about run on this host: with an FL session, the manual of what
    that FL can use (a trial FL cannot save); without one (--workspace-only, ``fl_license``
    None), the kernel and the guides, since every other fl call but reading a file is refused.
    ``confined`` and ``run_budget`` are the host's, so the text states what holds there."""
    return manual.tool_description(fl_license=fl_license, confined=confined, run_budget=run_budget)


# help(run) and the contract read the same text the arena's boxes serve.
run.__doc__ = run_description(fl_license=FLLicense.LICENSED)


def server_instructions(*, with_fl: bool) -> str:
    return SERVER_INSTRUCTIONS if with_fl else WORKSPACE_ONLY_INSTRUCTIONS


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
    confined = _confined()
    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        description=server_description(confined),
        instructions=server_instructions(with_fl=not workspace_only),
        lifespan=functools.partial(lifespan, with_fl=not workspace_only),
    )
    (only,) = tool_definitions()
    fl_license = None if workspace_only else fl_license_from_env(os.environ)
    description = run_description(
        fl_license=fl_license, confined=confined, run_budget=run_budget_from_env(os.environ)
    )
    server.add_tool(only, description=description)
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
