#!/usr/bin/env bash
# Install the Bridge built in native/build into FL Studio, with the rest of the
# FL-side setup. The install logic is `fl-mcp-install --bridge`
# (mcp-server/src/fl_mcp/fl/install.py), shared with Windows; this wrapper only
# names the macOS build output. Close FL Studio first.
set -euo pipefail
NATIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILT="$NATIVE_DIR/build/FL MCP Bridge_x64.dylib"
if [ ! -f "$BUILT" ]; then
    echo "Not built yet. Run:" >&2
    echo "  cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release" >&2
    echo "  cmake --build native/build --target bridge" >&2
    exit 1
fi
exec uv run --project "$NATIVE_DIR/../mcp-server" fl-mcp-install --bridge "$BUILT" "$@"
