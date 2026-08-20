#!/usr/bin/env bash
# Install the Spike 1 plugin into FL Studio's native effects folder.
#
# FL only loads native ("Fruity") plugins from inside its own app bundle — there
# is no per-user location the way there is for VSTs. That folder is group
# writable, so no sudo is needed, but it does mean writing into a signed .app.
# That is fine here because FL ships with the entitlement
# com.apple.security.cs.disable-library-validation, so it will load an unsigned
# third-party dylib.
#
# Usage: native/scripts/install_spike1.sh [--uninstall]

set -euo pipefail

NAME="FL MCP Spike1"
DYLIB="${NAME}_x64.dylib"
NATIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$NATIVE_DIR/build/$DYLIB"

FL_APP="${FL_APP:-/Applications/FL Studio 2025.app}"
EFFECTS="$FL_APP/Contents/Resources/FL/Plugins/Fruity/Effects"
DEST="$EFFECTS/$NAME"

if [ ! -d "$EFFECTS" ]; then
    echo "FL Studio effects folder not found: $EFFECTS" >&2
    echo "Set FL_APP=/path/to/FL Studio.app if installed elsewhere." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$DEST"
    echo "Removed $DEST"
    exit 0
fi

if [ ! -f "$BUILD" ]; then
    echo "Not built yet. Run:" >&2
    echo "  cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release" >&2
    echo "  cmake --build native/build" >&2
    exit 1
fi

mkdir -p "$DEST"
cp "$BUILD" "$DEST/$DYLIB"
printf 'ps_vendorname=FL MCP\n' > "$DEST/Plugin.nfo"

# Local dev builds are unsigned; macOS quarantines nothing we produce locally,
# but strip the attribute defensively in case the dylib was ever downloaded.
xattr -d com.apple.quarantine "$DEST/$DYLIB" 2>/dev/null || true

echo "Installed: $DEST/$DYLIB"
echo
echo "Next:"
echo "  1. Restart FL Studio."
echo "  2. Mixer (F9) > select an insert > add effect > search '$NAME'."
echo "     If it does not appear, run a plugin rescan:"
echo "     Options > Manage plugins > Find installed plugins."
echo "  3. Run native/spike1/run_experiment.sh to drive it."
