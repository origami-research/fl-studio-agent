"""One cell of model code: Jupyter-style magics, REPL result, captured output, shown images.

The kernel and the dev session both execute cells through ``run_cell`` so the
REPL rule (a trailing expression is repr'd, or shown when it is an image) has
one implementation.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import importlib
import io
import os
import re
import shlex
import subprocess
import sys
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any

from fl_mcp import theory
from fl_mcp.contract import RunResult
from fl_mcp.kernel import display as displays
from fl_mcp.schemas import AutomationPoint, Note

CELL_FILENAME = "<fl>"
# Frames from files under the engine package are plumbing, not the model's code.
# Pure path comparison (no filesystem): co_filename and __file__ share a source.
_ENGINE_DIR = Path(__file__).parents[1]
# Per text field of a result. Past this a model cannot use the text, and the
# whole result must stay well under the server's wire-message bound.
OUTPUT_LIMIT_CHARS = 1_000_000
# A line that is a magic: `!cmd` runs a shell command, `%pip args` runs pip.
# Neither can start a valid Python statement, so the rewrite never changes code.
_MAGIC = re.compile(r"^(?P<indent>[ \t]*)(?:!(?P<shell>.*)|%pip(?P<pip>(?:[ \t].*)?))$")


def new_namespace(fl: object) -> dict[str, Any]:
    """The cell globals: full builtins plus the pre-bound music surface."""
    return {
        "__name__": "__main__",
        "__builtins__": builtins,
        "fl": fl,
        "theory": theory,
        "Note": Note,
        "AutomationPoint": AutomationPoint,
        "display": display,
        "__fl_shell__": shell,
        "__fl_pip__": pip,
    }


def expand_magics(code: str) -> str:
    """Rewrite magic lines into calls, keeping line numbers for tracebacks."""
    lines = []
    for line in code.splitlines():
        match = _MAGIC.match(line)
        if match is None:
            lines.append(line)
        elif match["shell"] is not None:
            lines.append(f"{match['indent']}__fl_shell__({match['shell'].strip()!r})")
        else:
            lines.append(f"{match['indent']}__fl_pip__({match['pip'].strip()!r})")
    return "\n".join(lines)


def display(*objects: object) -> None:
    """Show images in this cell's result: PIL images, matplotlib figures, PNG/JPEG bytes or files."""
    if displays.current is None:
        raise RuntimeError("display() shows images while a cell runs")
    displays.current.show(*objects)


def shell(command: str) -> None:
    """``!command``: its output joins the cell's output; a failure raises."""
    _run_command(command, shell=True)


def pip(args: str) -> None:
    """``%pip args``: the kernel's own pip. Installs land in PIP_TARGET (the workspace)."""
    _run_command([sys.executable, "-m", "pip", *shlex.split(args, posix=os.name != "nt")], shell=False)
    importlib.invalidate_caches()


def _run_command(command: str | list[str], *, shell: bool) -> None:
    # The kernel's matplotlib backend lives in the engine; another interpreter draws headless.
    env = {**os.environ, "MPLBACKEND": "Agg"}
    completed = subprocess.run(
        command, shell=shell, capture_output=True, text=True, errors="replace", check=False, env=env
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    completed.check_returncode()


def run_cell(code: str, namespace: dict[str, Any]) -> RunResult:
    """Execute one cell in ``namespace``; any exception, SystemExit included, is the result.

    ``actions`` is left empty: the server fills it from the fl calls it served.
    """
    try:
        tree = ast.parse(expand_magics(code), CELL_FILENAME, "exec")
    except SyntaxError as exc:
        return RunResult(
            outcome="error", stdout="", traceback="".join(traceback.format_exception_only(exc)), actions=[]
        )

    # REPL semantics: a trailing expression is evaluated and repr'd, so
    # `fl.tempo` answers like Python instead of printing nothing; an image is
    # shown instead, like a notebook's output.
    last_expr: ast.Expression | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = ast.Expression(tree.body.pop().value)

    output = io.StringIO()
    shown = displays.current = displays.Display()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                exec(compile(tree, CELL_FILENAME, "exec"), namespace)
                result = eval(compile(last_expr, CELL_FILENAME, "eval"), namespace) if last_expr else None
                if result is not None and shown.show_result(result):
                    result = None
            except BaseException:
                with contextlib.suppress(Exception):  # figures drawn before an error still show
                    shown.flush_figures()
                raise
            shown.flush_figures()
    except BaseException as exc:  # noqa: BLE001 - the cell boundary: model code's exception (incl. SystemExit, KeyboardInterrupt) IS the result
        return RunResult(
            outcome="error",
            stdout=_bounded(output.getvalue()),
            traceback=_bounded(_model_traceback(exc)),
            actions=[],
        ).with_images(shown.images, shown.dropped)
    finally:
        displays.current = None
    return RunResult(
        outcome="ok",
        stdout=_bounded(output.getvalue()),
        result=None if result is None else _bounded(repr(result)),
        actions=[],
    ).with_images(shown.images, shown.dropped)


def _model_traceback(exc: BaseException) -> str:
    """The traceback as the model reads it: every exception in the chain
    Python prints, each without engine frames."""
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        link.__traceback__ = _model_frames(link.__traceback__)
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    return "".join(traceback.format_exception(exc))


def _model_frames(tb: TracebackType | None) -> TracebackType | None:
    """The traceback without engine frames: the exec plumbing above the cell
    and the SDK and kernel wire below an fl call. The model's code ends at its
    fl call and the error follows, like any library call."""
    kept: list[TracebackType] = []
    while tb is not None:
        if not Path(tb.tb_frame.f_code.co_filename).is_relative_to(_ENGINE_DIR):
            kept.append(tb)
        tb = tb.tb_next
    rebuilt = None
    for frame in reversed(kept):
        rebuilt = TracebackType(rebuilt, frame.tb_frame, frame.tb_lasti, frame.tb_lineno)
    return rebuilt


def _bounded(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT_CHARS:
        return text
    dropped = len(text) - OUTPUT_LIMIT_CHARS
    return text[:OUTPUT_LIMIT_CHARS] + f"\n[... {dropped} more characters not shown]"
