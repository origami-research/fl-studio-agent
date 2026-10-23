#!/usr/bin/env bash
# Step 7 of docs/oss/launch-checklist.md: publish the VERIFIED mirror to the public repository and
# the engine to PyPI. This is the one-way step. It refuses to run unless a verification it runs
# itself says PASS and the owner passes --i-approve-publication. It never touches the private
# repository's refs.
#
# Usage:
#   scripts/oss/publish-public-mirror.sh --repo owner/name --tag vX.Y.Z --i-approve-publication [--mirror PATH] [--pypi-only]
#
# Order: verify, build and smoke-test the release from the mirror's main, check PyPI and the
# target repository, then push main and the tag, then upload to PyPI, then read both back.
# Everything that can fail cheaply fails before the first push. If the upload fails after the
# push, rerun with --pypi-only: it uploads only when the public main is exactly this mirror's main.
set -euo pipefail
REPO=""; TAG=""; APPROVED=0; PYPI_ONLY=0; MIRROR="${HOME}/Documents/Code/fl-studio-mcp-public.git"
DIST_NAME="fl-studio-agent"
while [ $# -gt 0 ]; do case "$1" in
  --repo) REPO="$2"; shift 2 ;; --tag) TAG="$2"; shift 2 ;; --mirror) MIRROR="$2"; shift 2 ;;
  --pypi-only) PYPI_ONLY=1; shift ;;
  --i-approve-publication) APPROVED=1; shift ;; *) echo "unknown argument: $1"; exit 2 ;; esac; done
[ -n "$REPO" ] && [ -n "$TAG" ] || { echo "usage: --repo owner/name --tag vX.Y.Z --i-approve-publication [--mirror PATH] [--pypi-only]"; exit 2; }
[ "$APPROVED" = 1 ] || { echo "refusing: publication is one-way; pass --i-approve-publication after the owner's explicit go"; exit 2; }
[ -d "$MIRROR" ] || { echo "refusing: no mirror at $MIRROR (run scripts/oss/build-public-mirror.sh first)"; exit 2; }
MIRROR="$(cd "$MIRROR" && pwd)"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
[ -e "$HERE/docs/oss/PUBLICATION-HOLD" ] && { echo "refusing: docs/oss/PUBLICATION-HOLD exists (owner hold, see the file); lift it in a reviewed commit first"; exit 2; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/fl-studio-agent-publish.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Verification is run HERE AND NOW against the mirror being published; a committed report from
# an earlier day proves nothing about this mirror (2026-09-06: a stale PASS report nearly let a
# mirror through whose fresh verification had just printed FAIL). The report checked is the one
# this run writes (2026-10-22: the old code read the newest committed docs/oss/verification-*.md).
echo "running verification against $MIRROR"
REPORT="$WORK/verification.md"
if ! OSS_VERIFICATION_REPORT="$REPORT" bash "$HERE/scripts/oss/verify-public-mirror.sh" --mirror "$MIRROR"; then echo "refusing: verification did not pass"; exit 2; fi
grep -q "^Status: PASS" "$REPORT" || { echo "refusing: this run's verification report is not PASS"; exit 2; }
echo "verification: PASS (run in this invocation)"
# The mirror must not contain any removal-set path in any reachable commit (belt and braces).
while IFS= read -r p; do case "$p" in ''|'#'*) continue ;; esac; if git -C "$MIRROR" log --all --format=%H -- "$p" | grep -q .; then echo "refusing: removal-set path still reachable in the mirror: $p"; exit 2; fi; done < "$HERE/docs/oss/history-removal-set.txt"
echo "removal set: clean"

# The release is built from exactly what is published: a clone of the mirror's main.
MAIN_SHA="$(git -C "$MIRROR" rev-parse refs/heads/main)"
git clone --quiet "$MIRROR" "$WORK/public"
VERSION="$(sed -n 's/^version = "\([^"+]*\)"$/\1/p' "$WORK/public/mcp-server/pyproject.toml")"
[ "$TAG" = "v$VERSION" ] || { echo "refusing: tag $TAG does not match the package version $VERSION in mcp-server/pyproject.toml"; exit 2; }
(cd "$WORK/public" && bash scripts/build-wheel.sh --release --out "$WORK/dist") || { echo "refusing: the release build or its installed-wheel smoke test failed"; exit 2; }
ARTIFACTS=("$WORK/dist/"*.whl "$WORK/dist/"*.tar.gz)
for artifact in "${ARTIFACTS[@]}"; do [ -f "$artifact" ] || { echo "refusing: missing release artifact $artifact"; exit 2; }; done
echo "release artifacts: $(cd "$WORK/dist" && ls *.whl *.tar.gz | tr '\n' ' ')"

# PyPI: the version must be new and a token must be present. The token is never printed.
pypi_status="$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$DIST_NAME/$VERSION/json")"
[ "$pypi_status" = 404 ] || { echo "refusing: $DIST_NAME $VERSION already exists on PyPI or PyPI is unreachable (HTTP $pypi_status)"; exit 2; }
if [ -z "${UV_PUBLISH_TOKEN:-}" ] && [ -r "$HOME/.config/pypi/token" ]; then
  UV_PUBLISH_TOKEN="$(tr -d '[:space:]' < "$HOME/.config/pypi/token")"; export UV_PUBLISH_TOKEN
fi
[ -n "${UV_PUBLISH_TOKEN:-}" ] || { echo "refusing: no PyPI token (UV_PUBLISH_TOKEN or ~/.config/pypi/token)"; exit 2; }
echo "PyPI: $DIST_NAME $VERSION is free; token present"

if [ "$PYPI_ONLY" = 1 ]; then
  public_sha="$(git ls-remote "https://github.com/$REPO.git" refs/heads/main | cut -f1)"
  [ "$public_sha" = "$MAIN_SHA" ] || { echo "refusing: --pypi-only needs https://github.com/$REPO main ($public_sha) to equal this mirror's main ($MAIN_SHA)"; exit 2; }
  echo "public main already equals the mirror's main; uploading to PyPI only"
else
  # An existing repository is accepted only when it is EMPTY (2026-10-22: a blanket "already
  # exists" refusal, left in by #226, made the owner-created empty repository unpublishable).
  if gh repo view "$REPO" --json name >/dev/null 2>&1; then
    branches=$(gh api "repos/$REPO/branches" --jq 'length')
    tags=$(gh api "repos/$REPO/tags" --jq 'length')
    [ "$branches" = 0 ] && [ "$tags" = 0 ] || { echo "refusing: $REPO already has $branches branch(es) and $tags tag(s); publication only targets an empty repository"; exit 2; }
    echo "publishing into the existing empty repository $REPO"
  else
    echo "creating public repository $REPO"
    gh repo create "$REPO" --public --description "Drive FL Studio from an agent: MCP engine, native Bridge plugin, controller script and the reverse-engineering knowledge behind them (MIT)" >/dev/null
  fi
  git -C "$MIRROR" remote remove public 2>/dev/null || true
  git -C "$MIRROR" remote add public "https://github.com/$REPO.git"
  git -C "$MIRROR" push public "refs/heads/main:refs/heads/main"
  git -C "$MIRROR" tag -f "$TAG" "$MAIN_SHA" >/dev/null
  git -C "$MIRROR" push public "refs/tags/$TAG"
  echo "pushed main ($MAIN_SHA) and $TAG to https://github.com/$REPO"
fi

uv publish "${ARTIFACTS[@]}" || { echo "PyPI upload failed after the GitHub push; fix the cause, then rerun this command with --pypi-only"; exit 1; }

# Read both back before reporting success.
remote_main="$(git ls-remote "https://github.com/$REPO.git" refs/heads/main | cut -f1)"
remote_tag="$(git ls-remote "https://github.com/$REPO.git" "refs/tags/$TAG" | cut -f1)"
default_branch="$(gh api "repos/$REPO" --jq .default_branch)"
[ "$remote_main" = "$MAIN_SHA" ] && [ "$remote_tag" = "$MAIN_SHA" ] && [ "$default_branch" = main ] || {
  echo "read-back mismatch: main=$remote_main tag=$remote_tag default_branch=$default_branch expected $MAIN_SHA on main"; exit 1; }
echo "GitHub read-back: default branch main, main and $TAG at $MAIN_SHA"
for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$DIST_NAME/$VERSION/json")" = 200 ] && break
  sleep 10
done
uv venv --quiet --python 3.12 "$WORK/clean"
(cd "$WORK" && uv pip install --quiet --no-cache --python "$WORK/clean/bin/python" "$DIST_NAME==$VERSION") || { echo "read-back failed: pip install $DIST_NAME==$VERSION"; exit 1; }
(cd "$WORK" && "$WORK/clean/bin/python" -I -c "import fl_mcp, sys; assert fl_mcp.__version__ == sys.argv[1], fl_mcp.__version__" "$VERSION") || { echo "read-back failed: installed version"; exit 1; }
(cd "$WORK" && "$WORK/clean/bin/fl-mcp-contract" > "$WORK/contract.json" && test -s "$WORK/contract.json") || { echo "read-back failed: fl-mcp-contract"; exit 1; }
for command in fl-mcp fl-mcp-doctor fl-mcp-install; do [ -x "$WORK/clean/bin/$command" ] || { echo "read-back failed: $command not installed"; exit 1; }; done
echo "PyPI read-back: pip install $DIST_NAME==$VERSION in a clean venv works; fl-mcp-contract runs"
echo "published: https://github.com/$REPO and https://pypi.org/project/$DIST_NAME/$VERSION/"
