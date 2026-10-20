"""Imported by every Python the kernel starts (pip, ``!python``) via PYTHONPATH.

The kernel itself applies the same fix directly; see fl_mcp.kernel.private_dirs.
"""

import os
import sys

if sys.platform == "win32":
    from fl_mcp.kernel.private_dirs import share_private_dirs

    share_private_dirs(os)
