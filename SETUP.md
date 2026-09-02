# Setup

From a fresh clone to "ask an agent for notes, they appear in FL Studio".

**Requirements:** macOS, FL Studio 2026, Python 3.11+, and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Windows is not supported yet (tracked in docs/31). The file-based features (compose/arrange/analyze `.flp` files) need no FL running; the live features (write into a running FL, live synth params, observe) need FL open with the Bridge plugin.

---

## 1. Install the server

```bash
cd mcp-server
uv venv
uv pip install -e . -e ..        # the package + the repo-root (harness + flmcp_codec)
```

## 2. Create the agent's project + wire FL

```bash
uv run python -m fl_mcp.make_agent_project   # ~/Documents/Image-Line/FL Studio/Projects/FLMCP Agent.flp
uv run fl-mcp-install                         # note script + controller script + MIDI assignment
```

The server derives its expected controller protocol version from the exact controller source that
`fl-mcp-install` deploys. If `fl_doctor` reports a stale controller, run `fl-mcp-install` and
restart FL Studio; the warning is advisory so compatible minor mismatches can still operate.

The agent project is the **only** project the agent modifies; your own work is never touched, and a backup is written before every change.

## 3. (Live features) install the Bridge plugin

```bash
native/scripts/fetch_sdk.sh
cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release
cmake --build native/build --target bridge
native/scripts/install_bridge.sh
```

Then, once, in FL: Mixer (F9) → an insert slot → add effect → **FL MCP Bridge**, and Cmd+S so it auto-loads next time. Without this you still get the file-based features (with a brief FL restart per edit).

## 4. Register with your agent

```bash
claude mcp add fl-studio -- "$(pwd)/.venv/bin/python" -m fl_mcp
```

Absolute path required. Restart the session. Then ask: "make a 4-bar chord loop."

---

## What you can ask for
Compose (notes to any pattern), shape the sound (live synth params), read/understand the project (key, arrangement, automation), set tempo, play/stop, and render to WAV. Full list: `docs/24-coverage-map.md`.

## Honest notes
- It drives **your** FL on **your** machine; live operations take over screen focus and restart FL around file writes.
- It runs local code that opens FL - a dev setup, not a hosted product (see docs/31 for the cloud/product path).
- Easier install (one-command + one-click `.mcpb` + a companion app + Windows) is planned in **docs/31-install-modernization.md**.
