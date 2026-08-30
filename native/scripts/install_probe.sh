#!/usr/bin/env bash
# Install the FL MCP Probe diagnostic plugin into FL Studio's native effects
# folder. This is a THROWAWAY diagnostic (see native/probe/RUNBOOK_PROBE.md),
# not a production component - install it on a scratch machine/project, run the
# passes, record the verdict, then uninstall.
#
# Same mechanics as install_bridge.sh: FL loads unsigned native plugins from
# inside its own bundle (group-writable, no sudo; disable-library-validation
# entitlement lets an unsigned dylib load). The Probe is an EFFECT and has no
# UI; loading it on any mixer insert starts its extended tick.
#
# Usage: install_probe.sh [--uninstall] [--folder Effects|Generators]
# FL_APP overrides auto-detection.
set -euo pipefail

ACTION="install"
FOLDER="Effects"
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) ACTION="uninstall"; shift ;;
        --folder)    FOLDER="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done
case "$FOLDER" in
    Effects|Generators) ;;
    *) echo "unknown --folder '$FOLDER' (want: Effects | Generators)" >&2; exit 1 ;;
esac

NAME="FL MCP Probe"
DYLIB="${NAME}_x64.dylib"
NATIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$NATIVE_DIR/build/$DYLIB"

if [ -z "${FL_APP:-}" ]; then
    FL_APP="$(ls -d "/Applications/FL Studio"*.app 2>/dev/null | sort -V | tail -1)"
fi
if [ -z "${FL_APP:-}" ] || [ ! -d "$FL_APP" ]; then
    echo "No FL Studio.app found under /Applications. Set FL_APP=/path/to/FL Studio.app." >&2
    exit 1
fi

PLUGDIR="$FL_APP/Contents/Resources/FL/Plugins/Fruity/$FOLDER"
DEST="$PLUGDIR/$NAME"

if [ "$ACTION" = "uninstall" ]; then
    rm -rf "$DEST"
    echo "Removed $DEST"
    exit 0
fi

if [ ! -d "$PLUGDIR" ]; then
    echo "FL Studio $FOLDER folder not found: $PLUGDIR" >&2
    exit 1
fi
if [ ! -f "$BUILD" ]; then
    echo "Not built yet. Run:" >&2
    echo "  native/scripts/fetch_sdk.sh" >&2
    echo "  cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release" >&2
    echo "  cmake --build native/build --target probe" >&2
    exit 1
fi

mkdir -p "$DEST"
cp "$BUILD" "$DEST/$DYLIB"
printf 'ps_Vendorname=FL MCP\n' > "$DEST/Plugin.nfo"
xattr -d com.apple.quarantine "$DEST/$DYLIB" 2>/dev/null || true

echo "Installed: $DEST/$DYLIB"
echo "  type:   FPF_Type_Effect (base-free, 64-slot ABI)"
echo "Next: restart FL, open a SCRATCH project, Mixer (F9) > insert > add effect > '$NAME'."
echo "Then follow native/probe/RUNBOOK_PROBE.md."
