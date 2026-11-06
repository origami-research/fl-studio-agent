"""Windows: the kernel's AppContainer cannot open the null device, so Python gets a stand-in.

NUL (``\\Device\\Null``) grants Everyone, SYSTEM, Administrators and RESTRICTED, but no
AppContainer SID and not ALL APPLICATION PACKAGES (its DACL on the dev box, 2026-11-06),
so every open from inside the container is denied, whatever the access asked for. No
capability covers it, only an administrator may change a device's DACL (and not across a
reboot), and Windows refuses a per-process device map that would point NUL elsewhere
(STATUS_INVALID_PARAMETER, same run). So every Python in the kernel's tree (the kernel,
pip, ``!python``, a script it starts) gets a stand-in inside its workspace:

- ``os.devnull`` names a file in the workspace's temp folder, so ``open(os.devnull, "w")``
  works; what is written stays there until the next truncating open.
- ``subprocess.DEVNULL`` is a new empty file for each process started, deleted when the
  last handle to it closes: the child's stdin reads end of file at once, and its output
  is written to that file and deleted with it.

A program that opens NUL itself is still refused, for example cmd's ``> NUL``.
"""

from __future__ import annotations

import uuid
from types import ModuleType
from typing import Any


def share_null_device(os_module: ModuleType, subprocess_module: ModuleType, folder: str) -> None:
    """Point ``os.devnull`` and ``subprocess.DEVNULL`` at files in ``folder``."""
    os_module.devnull = os_module.path.join(folder, "devnull")

    def _get_devnull(self: Any) -> int:
        if not hasattr(self, "_devnull"):
            # O_TEMPORARY deletes the file once every handle is closed (the child holds its
            # own); O_NOINHERIT keeps it out of other children: subprocess passes it on itself.
            flags = (
                os_module.O_RDWR | os_module.O_CREAT | os_module.O_EXCL | os_module.O_BINARY
                | os_module.O_TEMPORARY | os_module.O_NOINHERIT
            )  # fmt: skip
            os_module.makedirs(folder, exist_ok=True)  # model code may have removed it
            self._devnull = os_module.open(os_module.path.join(folder, f"devnull-{uuid.uuid4().hex}"), flags)
        return self._devnull

    subprocess_module.Popen._get_devnull = _get_devnull
