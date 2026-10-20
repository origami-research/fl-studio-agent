"""Windows: a private (mode 0o700) directory is unreachable from the kernel.

Since Python 3.12.4 (CVE-2024-4030), ``os.mkdir(path, 0o700)`` on Windows gives
the new directory an explicit ACL for SYSTEM, Administrators, and the owner
only. Inside an AppContainer every access also needs the container's SID, so
the kernel could not use a directory it had just made: ``tempfile.mkdtemp``
failed in model code and pip failed on its build directories. Everything the
kernel can write is its own workspace, so "private" adds nothing there; the
directory inherits the workspace ACL instead, which grants the container.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

_PRIVATE = 0o700
_INHERIT = 0o777


def share_private_dirs(os_module: ModuleType) -> None:
    """Make ``os_module.mkdir(path, 0o700)`` create an inheriting directory."""
    real_mkdir = os_module.mkdir

    def mkdir(path: Any, mode: int = _INHERIT, *, dir_fd: int | None = None) -> None:
        real_mkdir(path, _INHERIT if mode == _PRIVATE else mode, dir_fd=dir_fd)

    os_module.mkdir = mkdir
