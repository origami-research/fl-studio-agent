"""One-command live FL SDK session for development and verification."""

from __future__ import annotations

import argparse
import ast
import code
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import traceback
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from fl_mcp import sdk
from fl_mcp.contract import ENV_AGENT_PROJECT, ENV_SPOOL_ROOT
from fl_mcp.core.carriers.udp import PORT_FILE, BridgeEndpoint, discover
from fl_mcp.fl.process import fl_kill_pid, launch_fl
from fl_mcp.template import template_path

ENV_PROJECT = ENV_AGENT_PROJECT
ENV_SPOOL = ENV_SPOOL_ROOT
MARKER_FILE = ".fl-mcp-dev-session.json"
READY_TIMEOUT_SECONDS = 120.0
# The broker caps one control request at 60 s; wait for hello in bounded slices.
HELLO_SLICE_SECONDS = 30.0


def main(argv: Sequence[str] | None = None) -> int:
    """Launch, attach, and expose a live SDK session. Returns a shell status."""
    args = _parser().parse_args(argv)
    project, copied_project = _project(Path(args.project) if args.project else None)
    spool, temporary_spool = _spool(Path(args.spool) if args.spool else None)
    token = uuid.uuid4().hex
    marker = spool / MARKER_FILE
    endpoint: BridgeEndpoint | None = None
    _prepare(spool, marker, token)
    try:
        # FL first, ports second. Measured 2026-09-04 on the dev Mac: when the
        # broker's virtual ports already exist at FL launch the wire stays silent
        # even after recycles, while ports that appear after FL is up are bound by
        # hot-plug and hello answers at once (this is also the arena's order:
        # FL boots, then fl-mcp connects). So the broker is started by the first
        # session request below, never before launch_fl.
        launch_fl(project, spool_root=spool)
        endpoint = _wait_for_bridge(spool)
        _wait_for_hello(spool)
        with _session_environment(project, spool):
            fl = sdk.connect()
            if args.exec_code is not None:
                _execute(args.exec_code, {"fl": fl})
            elif not args.no_repl:
                code.interact("FL MCP dev session. Live SDK is bound as `fl`.", local={"fl": fl})
        return 0
    finally:
        _teardown(marker, token, endpoint.pid if endpoint is not None else None)
        if copied_project is not None:
            shutil.rmtree(copied_project.parent, ignore_errors=True)
        if temporary_spool:
            shutil.rmtree(spool, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, help="FL project to open")
    parser.add_argument("--spool", type=Path, help="session spool directory")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--exec", dest="exec_code", help="Python to run with the live SDK bound as fl")
    action.add_argument(
        "--no-repl", action="store_true", help="verify bridge and control readiness, then exit"
    )
    return parser


def _project(project: Path | None) -> tuple[Path, Path | None]:
    if project is not None:
        return project.expanduser().resolve(), None
    template = template_path()
    if not template.is_file():
        raise FileNotFoundError(f"bridge project template is missing: {template}")
    directory = Path(tempfile.mkdtemp(prefix="fl-mcp-dev-project-"))
    copied = directory / template.name
    shutil.copy2(template, copied)
    return copied, copied


def _spool(spool: Path | None) -> tuple[Path, bool]:
    if spool is None:
        return Path(tempfile.mkdtemp(prefix="fl-mcp-dev-spool-")), True
    root = spool.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root, False


def _prepare(spool: Path, marker: Path, token: str) -> None:
    (spool / PORT_FILE).unlink(missing_ok=True)
    marker.write_text(json.dumps({"token": token}) + "\n", encoding="utf-8")


def _wait_for_bridge(spool: Path) -> BridgeEndpoint:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if endpoint := discover([spool]):
            return endpoint
        time.sleep(0.2)
    raise TimeoutError(f"FL did not publish {PORT_FILE} under {spool} within {READY_TIMEOUT_SECONDS:g}s")


def _wait_for_hello(spool: Path) -> None:
    """Wait for the controller through the SAME path fl-mcp uses.

    A raw BrokerControl.await_hello never recycles a silent wire, so this tool
    could report "controller attached, no response" forever while fl-mcp itself
    connected fine (the arena's real-engine test). ControlLink.hello() recycles
    the ports for a hot-plug bind exactly like the server does.
    """
    import time

    from fl_mcp.session.facade import create_session

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    session = create_session()
    try:
        while time.monotonic() < deadline:
            if session.link.hello() is not None:
                return
            time.sleep(1.0)
    finally:
        if session.control is not None:
            session.control.close()
    raise TimeoutError(
        f"no controller hello within {READY_TIMEOUT_SECONDS:g}s (FL up, wire silent after recycles)"
    )


@contextlib.contextmanager
def _session_environment(project: Path, spool: Path) -> Iterator[None]:
    old_project, old_spool = os.environ.get(ENV_PROJECT), os.environ.get(ENV_SPOOL)
    os.environ[ENV_PROJECT], os.environ[ENV_SPOOL] = str(project), str(spool)
    try:
        yield
    finally:
        _restore_environment(ENV_PROJECT, old_project)
        _restore_environment(ENV_SPOOL, old_spool)


def _restore_environment(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _execute(source: str, namespace: dict[str, Any]) -> None:
    tree = ast.parse(source, mode="exec")
    trailing = (
        ast.Expression(tree.body.pop().value) if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    )
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(tree, "<fl-mcp-dev-session>", "exec"), namespace)
            result = eval(compile(trailing, "<fl-mcp-dev-session>", "eval"), namespace) if trailing else None
    except BaseException as exc:
        raise RuntimeError("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))) from exc
    output = stdout.getvalue()
    if output:
        print(output, end="")
    if trailing is not None:
        print(repr(result))


def _teardown(marker: Path, token: str, pid: int | None) -> None:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if payload.get("token") != token:
        return
    if pid is not None:
        fl_kill_pid(pid)
    marker.unlink(missing_ok=True)
