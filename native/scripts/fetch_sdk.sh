#!/usr/bin/env bash
# Fetch the FL Studio plugin SDK into native/sdk/ (gitignored).
#
# The SDK is proprietary: its headers carry "FL Studio generator/effect plugins
# SDK (99-21)" with an Image-Line copyright and no redistribution grant, and no
# LICENSE file ships with it. So it cannot live in this repo - every developer
# fetches their own copy.
#
# Source of record is https://www.image-line.com/developers (login required).
# By default this pulls the copy vendored in an unofficial CMake template, which
# is convenient but NOT authoritative; prefer the official download.

set -euo pipefail

SDK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/sdk"
MIRROR="https://github.com/Barsay/FL-Studio-SDK-CMake-Template.git"

if [ -f "$SDK_DIR/fp_plugclass.h" ]; then
    echo "SDK already present: $SDK_DIR"
    exit 0
fi

echo "Fetching FL Studio SDK into $SDK_DIR"
echo "NOTE: proprietary Image-Line code. Do not commit it; native/sdk is gitignored."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --depth 1 --quiet "$MIRROR" "$TMP/template"
mkdir -p "$SDK_DIR"
cp "$TMP/template/SDK/"*.h "$TMP/template/SDK/"*.cpp "$SDK_DIR/"

echo "Done. Files:"
ls -1 "$SDK_DIR"
