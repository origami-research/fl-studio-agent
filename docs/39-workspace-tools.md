# Workspace tools (removed)

**Status: removed** (2026-11-03).

The engine served `shell`, `read_file`, `write_file`, `edit_file`, `glob` and `grep` beside the FL tools from 2026-10-22, behind `FLMCP_WORKSPACE_TOOLS=1` or alone under `fl-mcp --workspace-only`.
The owner replaced them, with `get_scene`, `check` and `render`, by one tool: `run`.
Files are `open()`, shell is `subprocess` or `!cmd`, and notes are files in the workspace, all in the same OS sandbox the tools used ([38](38-python-kernel.md)).

What remains:

- `fl-mcp --workspace-only` serves `run` with no FL session behind it, for computer-use runs: plain Python works and `fl` calls are refused.
- `FLMCP_WORKSPACE_TOOLS` is no longer read.
- The `fl_mcp/workspace` package, its confined op process and the workspace tools' action sentences are deleted.

The design and box acceptance of the removed tools are in the git history of this file.
