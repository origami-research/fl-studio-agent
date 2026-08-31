"""MOVED to fl_mcp.fl.paths (the OS/FL quarantine, docs/29 §3).

Import shim only; the integration manager rewires store.py/server callers
to fl_mcp.fl.paths and deletes this file.
"""

from fl_mcp.fl.paths import (  # noqa: F401
    app_data_root,
    params_dir,
    project_dir,
    projects_dir,
    spool_dir,
)
