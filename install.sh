#!/usr/bin/env bash
# One-command installer for FL Studio MCP (macOS first; Windows tracked, not
# yet implemented — see docs/08-roadmap.md).
#
# A thin wrapper around fl_mcp.install.install_all() + fl_mcp's fl_doctor —
# all the real logic lives in Python (mcp-server/src/fl_mcp/install.py,
# session.py's FLSession.doctor()) so this script and the `fl_doctor` MCP
# tool never drift apart. No native plugin build step here: the native
# Bridge plugin (native/) isn't ready (docs/12/15) and isn't part of this
# installer. No template project step either: that's deferred until a
# licensed FL Studio install is available (product decision, this merge) —
# reported below as its own PENDING line, never PASS/WARN/FAIL. No PyPI
# packaging step: distribution is a separate, not-yet-scoped fix item
# (docs/15 fix item 7) — this always installs from the repo checkout.
#
# Steps, each independent and safe to re-run:
#   1. Controller + piano-roll scripts + MIDI port assignment (reg.xml)
#   2. Register the MCP server with Claude Code
#   3. fl_doctor — plain-language status of every plane
#
# A step's failure doesn't abort the others; the summary at the end lists
# PASS/WARN/FAIL per step. Nothing here needs sudo.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$REPO_ROOT/mcp-server"
MCP_VENV_PY="$MCP_DIR/.venv/bin/python"

# Every relative path below is relative to the repo root, regardless of where
# this script was invoked from.
cd "$REPO_ROOT"

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
declare -a SUMMARY

step() {
    echo
    echo "── $1 ──"
}

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    SUMMARY+=("PASS  $*")
    echo "  [PASS] $*"
}

warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    SUMMARY+=("WARN  $*")
    echo "  [WARN] $*"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    SUMMARY+=("FAIL  $*")
    echo "  [FAIL] $*"
}

pending() {
    SUMMARY+=("PENDING  $*")
    echo "  [PENDING] $*"
}

if [ "$(uname -s)" != "Darwin" ]; then
    echo "install.sh currently supports macOS only."
    echo "Windows: virtual-port setup differs (docs/03) and isn't wired up here yet —"
    echo "tracked in docs/08-roadmap.md. Manual setup: see mcp-server/README.md."
    exit 1
fi

echo "FL Studio MCP — installer"
echo "Repo: $REPO_ROOT"

# ---------------------------------------------------------------------------
# Step 1 — controller + piano-roll scripts + MIDI port assignment
# ---------------------------------------------------------------------------
step "1/3  Controller + piano-roll scripts + MIDI assignment"

if ! command -v uv >/dev/null 2>&1; then
    fail "uv not found — install it first (https://docs.astral.sh/uv/), then re-run this script."
else
    echo "  Syncing mcp-server's Python environment (uv sync)..."
    if (cd "$MCP_DIR" && uv sync) >/tmp/flmcp_uv_sync.log 2>&1; then
        INSTALL_OUTPUT="$("$MCP_VENV_PY" -m fl_mcp.install 2>&1)"
        INSTALL_RC=$?
        echo "$INSTALL_OUTPUT" | sed 's/^/  /'
        if [ "$INSTALL_RC" -eq 0 ]; then
            pass "Controller script, piano-roll script, and MIDI assignment installed"
        else
            fail "fl_mcp.install reported a failure — see output above. Common cause: FL Studio has" \
                 "never been run on this machine (its Settings folder doesn't exist yet). Run FL" \
                 "Studio once, then retry."
        fi
    else
        fail "uv sync failed — see /tmp/flmcp_uv_sync.log"
    fi
fi

# ---------------------------------------------------------------------------
# Step 2 — register with Claude Code
# ---------------------------------------------------------------------------
step "2/3  Register the MCP server with Claude Code"

MCP_REGISTER_CMD=("$MCP_VENV_PY" -m fl_mcp)

if command -v claude >/dev/null 2>&1; then
    if claude mcp get fl-studio >/dev/null 2>&1; then
        pass "Already registered with Claude Code as 'fl-studio'"
    else
        if claude mcp add fl-studio -- "${MCP_REGISTER_CMD[@]}" >/tmp/flmcp_claude_add.log 2>&1; then
            pass "Registered with Claude Code as 'fl-studio'"
        else
            fail "claude mcp add failed — see /tmp/flmcp_claude_add.log"
        fi
    fi
else
    warn "claude CLI not found — register manually. Paste into your MCP client's config:"
    cat <<JSON
        {
          "mcpServers": {
            "fl-studio": {
              "command": "$MCP_VENV_PY",
              "args": ["-m", "fl_mcp"]
            }
          }
        }
JSON
fi

pending "Template project (flmcp-template.flp) — deferred until a licensed FL Studio install is" \
        "available on this machine. Not a failure; nothing to do here yet (docs/07)."

# ---------------------------------------------------------------------------
# Step 3 — fl_doctor
# ---------------------------------------------------------------------------
step "3/3  fl_doctor"

if [ -x "$MCP_VENV_PY" ]; then
    if "$MCP_VENV_PY" -c "
from fl_mcp.session import FLSession

report = FLSession().doctor()


def line(plane, name):
    if plane.get('connected'):
        print(f'  [PASS] {name}: connected')
    elif plane.get('status') == 'pending':
        print(f'  [PENDING] {name}: {plane.get(\"detail\")}')
    else:
        detail = plane.get('fix') or plane.get('message') or plane.get('detail') or 'not connected'
        print(f'  [INFO] {name}: {detail}')


line(report.get('control_plane', {}), 'control plane (control_midi / controller script)')
line(report.get('note_plane', {}), 'note plane (v0 piano-roll script)')
line(report.get('template_project', {}), 'template project')
print(f\"  Overall: {report.get('status')}\")
" 2>/tmp/flmcp_doctor.log; then
        pass "fl_doctor ran (see plane status above — the control plane shows 'not connected' until" \
             "you start the fl-studio MCP server, which opens the ports; see docs/14 on ordering)"
    else
        fail "fl_doctor did not run — see /tmp/flmcp_doctor.log"
    fi
else
    warn "Skipped — mcp-server's virtualenv isn't set up (see step 1)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "── Summary ──"
for line in "${SUMMARY[@]}"; do
    echo "  $line"
done
echo
echo "$PASS_COUNT passed, $WARN_COUNT warned, $FAIL_COUNT failed (plus any PENDING items above)."

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
