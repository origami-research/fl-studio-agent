#!/usr/bin/env bash
# Install the production FL MCP Bridge plugin into FL Studio's native effects
# folder.
#
# FL only loads native ("Fruity") plugins from inside its own app bundle — there
# is no per-user location like there is for VSTs. That folder is group writable,
# so no sudo is needed. FL ships with the entitlement
# com.apple.security.cs.disable-library-validation (verified with
# `codesign -d --entitlements` against the installed app), so it loads an
# unsigned third-party dylib.
#
# The Bridge is an EFFECT: TNotesParams.ChanNum retargets to the selected
# channel only for effects (fp_plugclass.h), which is what lets the note plane
# address an arbitrary channel. It has no editor and no UI; once inserted on any
# mixer insert it silently consumes the FLMCP note spool on its extended tick.
#
# Usage:
#   install_bridge.sh [--uninstall] [--folder Effects|Generators]
#
# FL_APP overrides auto-detection; otherwise the newest
# "/Applications/FL Studio*.app" is used.

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

NAME="FL MCP Bridge"
DYLIB="${NAME}_x64.dylib"
NATIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$NATIVE_DIR/build/$DYLIB"

if [ -z "${FL_APP:-}" ]; then
    FL_APP="$(ls -d "/Applications/FL Studio"*.app 2>/dev/null | sort -V | tail -1)"
fi
if [ -z "${FL_APP:-}" ] || [ ! -d "$FL_APP" ]; then
    echo "No FL Studio.app found under /Applications." >&2
    echo "Set FL_APP=/path/to/FL Studio.app if installed elsewhere." >&2
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
    echo "  cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release" >&2
    echo "  cmake --build native/build --target bridge" >&2
    exit 1
fi

mkdir -p "$DEST"
cp "$BUILD" "$DEST/$DYLIB"
# Capital V matches the field name in stock plugins' Plugin.nfo (e.g. Fruity
# Balance: "ps_Vendorname=Image-Line").
printf 'ps_Vendorname=FL MCP\n' > "$DEST/Plugin.nfo"
xattr -d com.apple.quarantine "$DEST/$DYLIB" 2>/dev/null || true

echo "Installed: $DEST/$DYLIB"
echo "  type:   FPF_Type_Effect (base-free, 64-slot ABI)"
echo "  folder: $FOLDER"
echo
echo "Next:"
echo "  1. Restart FL Studio."
echo "  2. Mixer (F9) > select an insert > add effect > search '$NAME'."
echo "     If it does not appear: Options > Manage plugins > Find installed plugins."
echo "  3. Ask the MCP server to write notes; the Bridge applies them on its"
echo "     tick with no further trigger. Watch the spool:"
echo "     ~/Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP/"
echo "     (bridge_status.txt, bridge_heartbeat.txt, bridge_dispatch.txt,"
echo "      responses/<id>.json; touch bridge_debug.enable for the state dump)"
