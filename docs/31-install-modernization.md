# Install modernization: one-command, one-click, Windows, browser

Goal: take install from "clone + 4 manual steps + build a native plugin" to "one command, or one click from a web page", and add Windows. Grounded in how the ecosystem actually ships this today.

## What the ecosystem does (the patterns to copy)

1. **`.mcpb` Desktop Extensions (formerly `.dxt`)** - Anthropic's one-click MCP install for Claude Desktop: bundle the server + deps into ONE file, the user clicks "Install" and configures via a UI. Built with `npm i -g @anthropic-ai/dxt` -> `dxt init` -> `dxt pack`. This is THE "install from browser" answer (a landing page with an Install/Add-to-Claude link to the `.mcpb`). Ref: anthropic.com/engineering/desktop-extensions, github.com/modelcontextprotocol/mcpb.
2. **`uvx` / `uvx.sh`** - single-command, cross-platform (mac/Linux/Windows) Python-tool install with NO Python needed: `curl -LsSf https://astral.sh/uv/install.sh | sh` bootstraps uv, then `uvx fl-studio-mcp`. uvx.sh even generates the one-liner for a PyPI package. Ref: docs.astral.sh/uv, uvx.sh.
3. **`curl | sh` bootstrap** - the house style for tools with native pieces (uv, ollama): one script that detects the OS, downloads the right prebuilt binary, and wires config.
4. **Tauri companion app** - a small cross-platform (Rust) desktop app for the GUI setup flow + status, far lighter than Electron - the modern choice when a tool needs a native install + a settings UI.

## The hard constraint (be honest)
This is not a pure-cloud SaaS: it requires **FL Studio installed + licensed** and an **unsigned native plugin loaded into FL**. So no install can eliminate "you must own FL". The friction we CAN remove is: Python setup, the plugin build, the FL wiring, and the MCP registration. Prebuilt plugin binaries per-OS are the key enabler.

## Plan (tiers, each shippable)

### T1 - one command (biggest friction cut, do first)
- Publish `fl-studio-mcp` to PyPI. Then `uvx fl-studio-mcp install` runs the whole setup: agent project + FL wiring + `claude mcp add`, and downloads the **prebuilt Bridge binary** for the user's OS (release artifacts) instead of a local cmake build.
- Ship prebuilt Bridge binaries per release (macOS arm64/x64 now; Windows later) via CI/GitHub Releases, so no compiler needed.
- A `curl -LsSf https://<host>/install.sh | sh` that bootstraps uv then runs the above - the README one-liner.

### T2 - one click from the browser (`.mcpb`)
- Package the server as an `.mcpb` (bundle server + Python deps). A landing page with "Add to Claude Desktop" links the `.mcpb`; Claude Desktop installs + configures in one click.
- The native plugin + FL wiring is the piece a `.mcpb` can't do alone - the extension's first-run prompts the user to run the bundled FL-setup step (or hands off to the companion app, T3). Be explicit about this in the manifest's setup UI.

### T3 - the companion app (Tauri) + Windows
- A small Tauri app: detect FL + version, install the plugin + controller script + agent project with a progress UI, register the MCP server, and show a live health panel (a GUI `fl_doctor`). This is the "proper application" and the natural home for the OS-specific bits.
- **Windows support** rides here. The `fl/` quarantine (issue #80/G5) already isolated the OS-coupled code into `fl_mcp/fl/{paths,process}.py`, so Windows = implement the Windows side of those + a Windows Bridge build (FL SDK `.dll`) + Windows focus/process handling (no AppleScript/pkill). Substantial but bounded; the file-based features are already cross-platform Python.

## Ordering / effort
- T1: medium, highest leverage - publish to PyPI + prebuilt-binary CI + an install subcommand. (Blocked partly by CI being dead - fix that first.)
- T2: small once T1's packaging exists - `dxt pack` + a landing page.
- T3: large - a real app + the Windows port; the payoff is non-technical users + Windows reach.

## Dependencies
- **Fix CI** (owner lever) is a prerequisite for prebuilt-binary releases and for shipping anything hosted.
- Plugin code-signing/notarization (macOS) and the unsigned-load entitlement are real steps for a distributable binary vs. the current self-built dylib.
