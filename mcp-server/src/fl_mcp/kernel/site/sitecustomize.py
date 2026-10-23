"""Imported by every Python the kernel and the workspace tools start (pip,
``!python``, ``python`` in shell) via PYTHONPATH.

The kernel itself applies the same fixes directly; see fl_mcp.kernel.private_dirs
and kernel.child.
"""

import os
import sys

if sys.platform == "win32":
    from fl_mcp.kernel.private_dirs import share_private_dirs

    share_private_dirs(os)

# What pip installed into the workspace imports here too, after the engine's own
# packages, so an install never replaces the SDK's numpy or pydantic.
_target = os.environ.get("PIP_TARGET")
if _target and _target not in sys.path:
    sys.path.append(_target)
