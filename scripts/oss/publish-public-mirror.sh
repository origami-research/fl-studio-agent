#!/usr/bin/env bash
# Step 7 of docs/oss/launch-checklist.md: publish the VERIFIED mirror to the public repository and
# the engine to PyPI, for the first release or any later one. This is the one-way step. It
# refuses to run unless a verification it runs itself says PASS and the owner passes
# --i-approve-publication. It never touches the private repository's refs.
#
# Usage:
#   scripts/oss/publish-public-mirror.sh --repo owner/name --tag vX.Y.Z --i-approve-publication [--mirror PATH] [--resume]
#
# Updates are fast-forward only. The mirror is rebuilt from the full private history every time;
# filter-repo is deterministic, so with unchanged rules the rebuilt mirror reproduces every
# published commit and the new main descends from the published main. If it does not (a rule
# changed what history looks like), the script refuses: rewriting public history would break
# every clone and fork, and is a separate owner decision.
#
# Order: verify, build and smoke-test the release and the Claude Desktop bundle from the mirror's
# main, check PyPI and the target repository (fast-forward), then upload to PyPI and read it back,
# then push main, then push the tag. The tag starts .github/workflows/release.yml in the public
# repository (GitHub release with the bundle, then the MCP Registry entry, which needs the PyPI
# version); this script waits for that run and reads the release back. PyPI goes first so the
# public README's install buttons never point at a version PyPI does not have.
# Everything that can fail cheaply fails before the upload. If a run stops after the upload, rerun
# with --resume: it skips the steps already done (the PyPI version this mirror uploaded, a public
# main already equal to this mirror's main) and finishes the rest.
set -euo pipefail
REPO=""; TAG=""; APPROVED=0; RESUME=0; MIRROR="${HOME}/Documents/Code/fl-studio-mcp-public.git"
DIST_NAME="fl-studio-agent"
while [ $# -gt 0 ]; do case "$1" in
  --repo) REPO="$2"; shift 2 ;; --tag) TAG="$2"; shift 2 ;; --mirror) MIRROR="$2"; shift 2 ;;
  --resume) RESUME=1; shift ;;
  --i-approve-publication) APPROVED=1; shift ;; *) echo "unknown argument: $1"; exit 2 ;; esac; done
[ -n "$REPO" ] && [ -n "$TAG" ] || { echo "usage: --repo owner/name --tag vX.Y.Z --i-approve-publication [--mirror PATH] [--resume]"; exit 2; }
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
[ -s "$WORK/public/docs/releases/$TAG.md" ] || { echo "refusing: no release notes at docs/releases/$TAG.md"; exit 2; }
(cd "$WORK/public" && bash scripts/build-wheel.sh --release --out "$WORK/dist") || { echo "refusing: the release build or its installed-wheel smoke test failed"; exit 2; }
ARTIFACTS=("$WORK/dist/"*.whl "$WORK/dist/"*.tar.gz)
for artifact in "${ARTIFACTS[@]}"; do [ -f "$artifact" ] || { echo "refusing: missing release artifact $artifact"; exit 2; }; done
echo "release artifacts: $(cd "$WORK/dist" && ls *.whl *.tar.gz | tr '\n' ' ')"
# The release workflow builds the bundle the same way; building it here fails before any push.
(cd "$WORK/public" && bash scripts/build-mcpb.sh "$WORK/bundle" >/dev/null) || { echo "refusing: the Claude Desktop bundle does not validate and pack"; exit 2; }
echo "Claude Desktop bundle: validates and packs"

# PyPI: the version must be new (or, with --resume, already carry both files of this release)
# and a token must be present. The token is never printed.
pypi_json() { curl -s "https://pypi.org/pypi/$DIST_NAME/$VERSION/json"; }
pypi_status="$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$DIST_NAME/$VERSION/json")"
PYPI_DONE=0
if [ "$pypi_status" = 200 ] && [ "$RESUME" = 1 ]; then
  kinds="$(pypi_json | python3 -I -c 'import json,sys; print(" ".join(sorted({f["packagetype"] for f in json.load(sys.stdin)["urls"]})))')"
  [ "$kinds" = "bdist_wheel sdist" ] || { echo "refusing: PyPI has $DIST_NAME $VERSION with files [$kinds], not a wheel and an sdist; finish it by hand"; exit 2; }
  PYPI_DONE=1
  echo "PyPI: $DIST_NAME $VERSION already uploaded (resuming)"
elif [ "$pypi_status" != 404 ]; then
  echo "refusing: $DIST_NAME $VERSION already exists on PyPI or PyPI is unreachable (HTTP $pypi_status); --resume continues a run that uploaded it"; exit 2
fi
if [ "$PYPI_DONE" = 0 ]; then
  if [ -z "${UV_PUBLISH_TOKEN:-}" ] && [ -r "$HOME/.config/pypi/token" ]; then
    UV_PUBLISH_TOKEN="$(tr -d '[:space:]' < "$HOME/.config/pypi/token")"; export UV_PUBLISH_TOKEN
  fi
  [ -n "${UV_PUBLISH_TOKEN:-}" ] || { echo "refusing: no PyPI token (UV_PUBLISH_TOKEN or ~/.config/pypi/token)"; exit 2; }
  echo "PyPI: $DIST_NAME $VERSION is free; token present"
fi

# The target repository: it must exist (the owner creates it), the tag must be new, and its main,
# if any, must be an ancestor of the mirror's main (equal to it only when resuming).
gh repo view "$REPO" --json name >/dev/null 2>&1 || { echo "refusing: $REPO does not exist or is not visible to this gh login"; exit 2; }
REMOTE="https://github.com/$REPO.git"
[ -z "$(git ls-remote "$REMOTE" "refs/tags/$TAG")" ] || { echo "refusing: $REPO already has tag $TAG"; exit 2; }
public_sha="$(git ls-remote "$REMOTE" refs/heads/main | cut -f1)"
git -C "$MIRROR" remote remove public 2>/dev/null || true
git -C "$MIRROR" remote add public "$REMOTE"
MAIN_DONE=0
if [ "$public_sha" = "$MAIN_SHA" ]; then
  [ "$RESUME" = 1 ] || { echo "refusing: $REPO main is already $MAIN_SHA; nothing new to publish (--resume finishes an interrupted run)"; exit 2; }
  MAIN_DONE=1
  echo "public main already equals the mirror's main (resuming)"
elif [ -z "$public_sha" ]; then
  [ "$(gh api "repos/$REPO/tags" --jq 'length')" = 0 ] || { echo "refusing: $REPO has tags but no main"; exit 2; }
  echo "first publication into the empty repository $REPO"
else
  git -C "$MIRROR" fetch --quiet public "+refs/heads/main:refs/remotes/public/main"
  if ! git -C "$MIRROR" merge-base --is-ancestor "$public_sha" "$MAIN_SHA"; then
    echo "refusing: the rebuilt mirror's main ($MAIN_SHA) does not descend from the published main ($public_sha)."
    echo "  The rewrite rules changed what published history looks like (an allowlist entry removed or narrowed,"
    echo "  a replacement or redaction rule changed). Restore the rule, or get the owner's decision on a history rewrite."
    exit 2
  fi
  echo "fast-forward: $(git -C "$MIRROR" rev-list --count "$public_sha..$MAIN_SHA") new commit(s) on top of the published main $public_sha"
fi

if [ "$PYPI_DONE" = 0 ]; then
  uv publish "${ARTIFACTS[@]}" || { echo "PyPI upload failed; nothing was pushed to GitHub. Fix the cause and rerun (with --resume if PyPI now shows $VERSION)"; exit 1; }
fi
# Read PyPI back before anything public points at it.
for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$DIST_NAME/$VERSION/json")" = 200 ] && break
  sleep 10
done
resume_hint="rerun with --resume once fixed"
uv venv --quiet --python 3.12 "$WORK/clean"
(cd "$WORK" && uv pip install --quiet --no-cache --python "$WORK/clean/bin/python" "$DIST_NAME==$VERSION") || { echo "read-back failed: pip install $DIST_NAME==$VERSION; $resume_hint"; exit 1; }
(cd "$WORK" && "$WORK/clean/bin/python" -I -c "import fl_mcp, sys; assert fl_mcp.__version__ == sys.argv[1], fl_mcp.__version__" "$VERSION") || { echo "read-back failed: installed version; $resume_hint"; exit 1; }
(cd "$WORK" && "$WORK/clean/bin/fl-mcp-contract" > "$WORK/contract.json" && test -s "$WORK/contract.json") || { echo "read-back failed: fl-mcp-contract; $resume_hint"; exit 1; }
for command in fl-studio-agent fl-mcp fl-mcp-doctor fl-mcp-install; do [ -x "$WORK/clean/bin/$command" ] || { echo "read-back failed: $command not installed; $resume_hint"; exit 1; }; done
echo "PyPI read-back: pip install $DIST_NAME==$VERSION in a clean venv works; fl-mcp-contract runs"

if [ "$MAIN_DONE" = 0 ]; then
  # No force: the remote also refuses anything but a fast-forward.
  git -C "$MIRROR" push public "refs/heads/main:refs/heads/main" || { echo "pushing main failed; $resume_hint"; exit 1; }
fi
git -C "$MIRROR" tag -f "$TAG" "$MAIN_SHA" >/dev/null
git -C "$MIRROR" push public "refs/tags/$TAG" || { echo "pushing $TAG failed; $resume_hint"; exit 1; }
remote_main="$(git ls-remote "$REMOTE" refs/heads/main | cut -f1)"
remote_tag="$(git ls-remote "$REMOTE" "refs/tags/$TAG" | cut -f1)"
default_branch="$(gh api "repos/$REPO" --jq .default_branch)"
[ "$remote_main" = "$MAIN_SHA" ] && [ "$remote_tag" = "$MAIN_SHA" ] && [ "$default_branch" = main ] || {
  echo "read-back mismatch: main=$remote_main tag=$remote_tag default_branch=$default_branch expected $MAIN_SHA on main"; exit 1; }
echo "GitHub read-back: default branch main, main and $TAG at $MAIN_SHA"

# The tag push started the release workflow; wait for it and read the release back. From here on a
# failure is in the public repository's workflow, so the hints are gh commands, not --resume.
run_id=""
for _ in $(seq 1 30); do
  run_id="$(gh run list -R "$REPO" --workflow release.yml --commit "$MAIN_SHA" --event push --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true)"
  [ -n "$run_id" ] && break
  sleep 10
done
[ -n "$run_id" ] || { echo "the release workflow did not start for $TAG; start it with: gh workflow run release.yml -R $REPO -f tag=$TAG"; exit 1; }
gh run watch "$run_id" -R "$REPO" --exit-status >/dev/null || {
  echo "the release workflow failed: gh run view $run_id -R $REPO --log-failed; rerun it with: gh run rerun $run_id -R $REPO --failed"; exit 1; }
# Read the release back the way fl-mcp-install uses it: every asset present and matching SHA256SUMS.
mkdir "$WORK/release"
gh release download "$TAG" -R "$REPO" --dir "$WORK/release" || { echo "read-back failed: could not download release $TAG"; exit 1; }
for asset in fl-studio-agent.mcpb fl-mcp-bridge-macos-universal.dylib fl-mcp-bridge-windows-x64.dll SHA256SUMS BRIDGE-NOTICE.txt; do
  [ -s "$WORK/release/$asset" ] || { echo "read-back failed: release $TAG has no $asset"; exit 1; }
done
(cd "$WORK/release" && shasum -a 256 -c SHA256SUMS >/dev/null) || { echo "read-back failed: release $TAG assets do not match its SHA256SUMS"; exit 1; }
echo "GitHub release $TAG carries the bundle and both Bridge libraries, matching SHA256SUMS; registry publish job passed"
echo "published: https://github.com/$REPO/releases/tag/$TAG and https://pypi.org/project/$DIST_NAME/$VERSION/"
