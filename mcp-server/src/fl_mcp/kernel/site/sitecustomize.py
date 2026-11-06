"""Imported by every Python the kernel starts (pip, ``!python``, a subprocess
running ``python``) via PYTHONPATH.

The kernel itself applies the same fixes directly; see fl_mcp.kernel.private_dirs,
fl_mcp.kernel.null_device and kernel.child.
"""

import os
import sys

if sys.platform == "win32":
    import subprocess

    from fl_mcp.kernel.null_device import share_null_device
    from fl_mcp.kernel.private_dirs import share_private_dirs

    share_private_dirs(os)
    share_null_device(os, subprocess, os.environ["TMP"])

# What pip installed into the workspace imports here too, after the engine's own
# packages, so an install never replaces the SDK's numpy or pydantic.
_target = os.environ.get("PIP_TARGET")
if _target and _target not in sys.path:
    sys.path.append(_target)
