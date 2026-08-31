"""MOVED to fl_mcp.fl (the OS/FL quarantine, docs/29 §3).

Import shim only, so session/ and other lanes stay green until the
integration manager rewires their imports to fl_mcp.fl.paths / .process;
delete this file with that rewire.
"""

from fl_mcp.fl.paths import (  # noqa: F401
    SPOOL_SUBDIRS,
    fl_app,
    fl_settings_dirs,
    piano_roll_scripts_dir,
    primary_settings_dir,
)
from fl_mcp.fl.process import fl_process_running  # noqa: F401
