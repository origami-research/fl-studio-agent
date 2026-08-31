"""MOVED to fl_mcp.fl.install (the OS/FL quarantine, docs/29 §3).

Alias shim: ``fl_mcp.install`` IS the moved module, so the ``fl-mcp-install``
console script and existing monkeypatches keep working until the integration
manager repoints them and deletes this file.
"""

import sys

from fl_mcp.fl import install as _install

sys.modules[__name__] = _install
