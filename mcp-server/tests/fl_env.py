"""Emulate FL Studio's embedded Python environment for piano roll scripts.

The fake `flpianoroll` module covers the *API*. This module covers the
*sandbox* — and the sandbox is where every real-world failure has come from.

Observed behavior of FL Studio 2025's embedded interpreter, each learned by
watching a real script fail:

1. `open()` succeeds only inside FL's own data tree. Elsewhere it raises
   `SystemError: <class '_io.FileIO'> returned NULL without setting an
   exception` — even though `os.path.exists()` returns True for the same path.
2. `os.remove()` is blocked outright:
   `SystemError: <built-in function remove> returned NULL without setting an
   exception`. `os.replace`/`os.rename`/`os.makedirs` are assumed blocked too.
3. Blocked operations raise `SystemError`, NOT `OSError`. Handlers written as
   `except OSError` do not catch them.
4. FL calls `apply()` with NO arguments when the script defines no
   `createDialog()`. A signature of `apply(form)` raises TypeError.
5. Top-level module code runs first, before `apply()`.

`run_pyscript` reproduces all of it, so a script that passes here has a real
chance of working in FL.
"""

from __future__ import annotations

import builtins
import os as _real_os
import types
from pathlib import Path

import fake_flpianoroll


class _Blocked(SystemError):
    """Matches how FL reports a refused syscall."""


def _refuse(name: str):
    def blocked(*args, **kwargs):
        raise _Blocked(f"<built-in function {name}> returned NULL without setting an exception")

    return blocked


class SandboxedOs(types.ModuleType):
    """`os` with the file-mutating calls FL refuses."""

    def __init__(self) -> None:
        super().__init__("os")
        for attr in dir(_real_os):
            if not attr.startswith("_"):
                setattr(self, attr, getattr(_real_os, attr))
        self.path = _real_os.path
        for blocked in ("remove", "unlink", "replace", "rename", "makedirs", "mkdir", "rmdir"):
            setattr(self, blocked, _refuse(blocked))


def sandboxed_open(allowed_roots: list[Path]):
    """`open` that refuses paths outside FL's data tree, the way FL's does."""
    real_open = builtins.open

    def guarded(file, mode="r", *args, **kwargs):
        resolved = Path(str(file)).resolve()
        if not any(
            resolved == root or root in resolved.parents for root in allowed_roots
        ):
            raise SystemError(
                "<class '_io.FileIO'> returned NULL without setting an exception"
            )
        if "encoding" in kwargs:
            # FL's restricted open is fussier than CPython's; scripts that pass
            # encoding= have failed in the wild, so treat it as unsupported.
            raise TypeError("'encoding' is an invalid keyword argument for this function")
        return real_open(resolved, mode, *args, **kwargs)

    return guarded


def run_pyscript(script_path: Path, allowed_roots: list[Path]) -> types.ModuleType:
    """Execute a .pyscript the way FL Studio does, under FL's restrictions.

    The restrictions are installed into `sys.modules` and `builtins` rather than
    the script's namespace, because the script's own `import os` would otherwise
    rebind straight back to the unrestricted module — which is exactly what FL
    does not let happen.

    Top-level code runs first; then `apply()` is called with no arguments, as FL
    does for scripts that define no `createDialog()`.
    """
    import sys

    module = types.ModuleType("fl_script")
    ns = module.__dict__
    ns["__file__"] = str(script_path)

    real_os = sys.modules["os"]
    real_open = builtins.open
    sys.modules["os"] = SandboxedOs()
    sys.modules["flpianoroll"] = fake_flpianoroll
    builtins.open = sandboxed_open(allowed_roots)
    try:
        source = script_path.read_text()
        exec(compile(source, str(script_path), "exec"), ns)

        apply_fn = ns.get("apply")
        if callable(apply_fn):
            apply_fn()  # FL passes nothing when there is no createDialog()
    finally:
        sys.modules["os"] = real_os
        builtins.open = real_open

    return module
