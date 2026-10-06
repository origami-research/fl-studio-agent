#!/usr/bin/env bash
# Build the fl-studio-agent wheel from the committed tree and prove it is self-contained.
#
# Usage: scripts/build-wheel.sh [--release] [--out DIR]
#
# Default (box artifact): the version is stamped <pyproject version>+g<short commit>,
# so every commit is a distinct pip version: `pip show` names the exact build and pip
# never answers "already satisfied" across different code. Beside the wheel it writes
# <wheel>.requirements.txt, the hash-locked runtime closure exported from uv.lock.
# It prints the name and sha256 pins that scripts/windows/provision.ps1 verifies.
# --release builds the unstamped version (PyPI rejects local versions); same checks.
#
# Nothing is uploaded.
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
release=0
out_dir="$repo_dir/dist"
while (($#)); do
  case "$1" in
    --release) release=1; shift ;;
    --out) out_dir="$2"; shift 2 ;;
    *) echo "usage: $0 [--release] [--out DIR]" >&2; exit 2 ;;
  esac
done
# The Windows box venv runs this Python: python-rtmidi 1.5.8 ships no cp313
# Windows wheel, and the box has no compiler to build one.
box_python=3.12

if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
  echo "refusing to build: tracked files have uncommitted changes (the build names a commit)" >&2
  exit 1
fi
sha=$(git -C "$repo_dir" rev-parse --short=7 HEAD)
base=$(sed -n 's/^version = "\([^"+]*\)"$/\1/p' "$repo_dir/mcp-server/pyproject.toml")
if [ -z "$base" ]; then
  echo "no version line in mcp-server/pyproject.toml" >&2
  exit 1
fi
version="$base+g$sha"
if [ "$release" -eq 1 ]; then
  version="$base"
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/fl-mcp-wheel.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT
src="$work_dir/src"
mkdir -p "$src" "$work_dir/dist"
git -C "$repo_dir" archive HEAD | tar -x -C "$src"
python3 - "$src/mcp-server/pyproject.toml" "$version" <<'PY'
import pathlib, re, sys
path, version = pathlib.Path(sys.argv[1]), sys.argv[2]
text, n = re.subn(r'(?m)^version = "[^"]*"$', f'version = "{version}"', path.read_text(), count=1)
if n != 1:
    sys.exit(f"{path}: expected exactly one version line to stamp")
path.write_text(text)
PY

# uv builds the sdist, then the wheel from that sdist: the route that carries ../LICENSE.
uv build --quiet --directory "$src/mcp-server" --out-dir "$work_dir/dist"
wheel=$(find "$work_dir/dist" -maxdepth 1 -name '*.whl' -print -quit)
name=$(basename "$wheel")
case "$name" in
  "fl_studio_agent-$version-py3-none-any.whl") ;;
  *) echo "built $name, expected version $version" >&2; exit 1 ;;
esac
requirements="$work_dir/dist/$name.requirements.txt"
uv export --directory "$src/mcp-server" --frozen --no-dev --no-emit-project --no-header --format requirements-txt \
  --quiet --output-file "$requirements"

# Every runtime file must be inside the wheel: the box has no repository checkout.
python3 - "$wheel" "$version" <<'PY'
import sys, zipfile
wheel, version = sys.argv[1], sys.argv[2]
names = set(zipfile.ZipFile(wheel).namelist())
required = [
    "fl_mcp/fl_scripts/device_FLMCP.py",
    "fl_mcp/fl_scripts/flmcp_codec.py",
    "fl_mcp/fl_scripts/MCP Write Notes.pyscript",
    "fl_mcp/assets/session-template.flp",
    f"fl_studio_agent-{version}.dist-info/licenses/LICENSE",
]
missing = [n for n in required if n not in names]
if not any(n.startswith("fl_mcp/profiles/") and n.endswith(".json") for n in names):
    missing.append("fl_mcp/profiles/*.json")
if missing:
    sys.exit(f"wheel is missing runtime files: {missing}")
PY

# The Windows closure must resolve to binary wheels only for the box Python.
uv pip compile "$requirements" --python-platform x86_64-pc-windows-msvc --python-version "$box_python" \
  --only-binary :all: --no-header --quiet --output-file "$work_dir/windows-resolution.txt"

# Install exactly what the box installs (locked closure, then the wheel with no
# resolution), outside the repository, and exercise the installed package.
uv venv --quiet --seed --python "$box_python" "$work_dir/venv"
venv_python="$work_dir/venv/bin/python"
"$venv_python" -m pip install --quiet --require-hashes --only-binary :all: -r "$requirements"
"$venv_python" -m pip install --quiet --no-deps --no-index "$wheel"
"$venv_python" -m pip check
(cd "$work_dir" && "$work_dir/venv/bin/fl-mcp-contract" >"$work_dir/contract.json")
test -s "$work_dir/contract.json"
(cd "$work_dir" && "$venv_python" - "$version" <<'PY'
import sys
from importlib.metadata import entry_points, version
from importlib.resources import files

import fl_mcp
from fl_mcp.contract import build_version
from fl_mcp.controller_version import CONTROLLER_PROTOCOL_VERSION
from fl_mcp.core.carriers import wire
from fl_mcp.template import template_path

expected = sys.argv[1]
assert version("fl-studio-agent") == fl_mcp.__version__ == build_version() == expected, fl_mcp.__version__
assert "site-packages" in fl_mcp.__file__, f"imported from {fl_mcp.__file__}, not the installed wheel"
assert CONTROLLER_PROTOCOL_VERSION
assert wire.encode_message
assert files("fl_mcp.fl_scripts").joinpath("MCP Write Notes.pyscript").is_file()
assert template_path().is_file()
scripts = {ep.name for ep in entry_points(group="console_scripts")}
assert {"fl-mcp", "fl-mcp-doctor", "fl-mcp-install"} <= scripts, scripts
PY
)

mkdir -p "$out_dir"
cp "$wheel" "$requirements" "$out_dir/"
wheel_sha=$(shasum -a 256 "$out_dir/$name" | cut -d' ' -f1)
requirements_sha=$(shasum -a 256 "$out_dir/$name.requirements.txt" | cut -d' ' -f1)
echo "built $out_dir/$name (installed-wheel smoke test passed)"
echo "WHEEL_NAME=\"$name\""
echo "WHEEL_SHA256=\"$wheel_sha\""
echo "REQUIREMENTS_NAME=\"$name.requirements.txt\""
echo "REQUIREMENTS_SHA256=\"$requirements_sha\""
