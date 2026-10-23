#!/usr/bin/env bash
# Validate and pack the Claude Desktop bundle (mcpb/) into OUT_DIR/fl-studio-agent.mcpb.
# The asset name has no version so README links can use releases/latest/download/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${1:?usage: scripts/build-mcpb.sh OUT_DIR}"
MCPB="@anthropic-ai/mcpb@2.1.2"
python3 "$ROOT/scripts/install_links.py" --check
mkdir -p "$OUT_DIR"
npx --yes "$MCPB" validate "$ROOT/mcpb/manifest.json"
npx --yes "$MCPB" pack "$ROOT/mcpb" "$OUT_DIR/fl-studio-agent.mcpb"
