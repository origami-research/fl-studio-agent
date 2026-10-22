#!/usr/bin/env bash
# Step 7 of docs/oss/launch-checklist.md: publish the VERIFIED mirror to a new public repository.
# This is the one-way step. It refuses to run unless a verification it runs itself says PASS
# and the owner passes --i-approve-publication. It never touches the private repository's refs.
#
# Usage:
#   scripts/oss/publish-public-mirror.sh --repo owner/name --tag v0.1.0 --i-approve-publication [--mirror PATH]
set -euo pipefail
REPO=""; TAG=""; APPROVED=0; MIRROR="${HOME}/Documents/Code/fl-studio-mcp-public.git"
while [ $# -gt 0 ]; do case "$1" in
  --repo) REPO="$2"; shift 2 ;; --tag) TAG="$2"; shift 2 ;; --mirror) MIRROR="$2"; shift 2 ;;
  --i-approve-publication) APPROVED=1; shift ;; *) echo "unknown argument: $1"; exit 2 ;; esac; done
[ -n "$REPO" ] && [ -n "$TAG" ] || { echo "usage: --repo owner/name --tag vX.Y.Z --i-approve-publication [--mirror PATH]"; exit 2; }
[ "$APPROVED" = 1 ] || { echo "refusing: publication is one-way; pass --i-approve-publication after the owner's explicit go"; exit 2; }
[ -d "$MIRROR" ] || { echo "refusing: no mirror at $MIRROR (run scripts/oss/build-public-mirror.sh first)"; exit 2; }
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
[ -e "$HERE/docs/oss/PUBLICATION-HOLD" ] && { echo "refusing: docs/oss/PUBLICATION-HOLD exists (owner hold, see the file); lift it in a reviewed commit first"; exit 2; }
# Verification is run HERE AND NOW against the mirror being published; a committed report from
# an earlier day proves nothing about this mirror (2026-09-06: a stale PASS report nearly let a
# mirror through whose fresh verification had just printed FAIL).
# The report it checks is the one this run writes, never a committed file (2026-10-22: the
# old code read the newest docs/oss/verification-*.md, the stale-report seam again).
echo "running verification against $MIRROR"
REPORT="$(mktemp "${TMPDIR:-/tmp}/fl-studio-agent-verification.XXXXXX")"
trap 'rm -f "$REPORT"' EXIT
if ! OSS_VERIFICATION_REPORT="$REPORT" bash "$HERE/scripts/oss/verify-public-mirror.sh" --mirror "$MIRROR"; then echo "refusing: verification did not pass"; exit 2; fi
grep -q "^Status: PASS" "$REPORT" || { echo "refusing: this run's verification report is not PASS"; exit 2; }
echo "verification: PASS (run in this invocation)"
# The mirror must not contain any removal-set path in any reachable commit (belt and braces).
while IFS= read -r p; do case "$p" in ''|'#'*) continue ;; esac; if git -C "$MIRROR" log --all --format=%H -- "$p" | grep -q .; then echo "refusing: removal-set path still reachable in the mirror: $p"; exit 2; fi; done < "$HERE/docs/oss/history-removal-set.txt"
echo "removal set: clean"
# An existing repository is accepted only when it is EMPTY (2026-10-22: a blanket "already exists"
# refusal above this check, left in by #226, made the owner-created empty repository unpublishable).
if gh repo view "$REPO" --json name >/dev/null 2>&1; then
  # The owner may have created the repository by hand (org policy denies creation to this login).
  # It must be EMPTY: publishing into a repository with history would rewrite someone's refs.
  branches=$(gh api "repos/$REPO/branches" --jq 'length')
  [ "$branches" = 0 ] || { echo "refusing: $REPO already has $branches branch(es); publication only targets an empty repository"; exit 2; }
  echo "publishing into the existing empty repository $REPO"
else
  echo "creating public repository $REPO"
  gh repo create "$REPO" --public --description "Drive FL Studio from an agent: MCP engine, native Bridge plugin, controller script and the reverse-engineering knowledge behind them (MIT)" >/dev/null
fi
git -C "$MIRROR" remote remove public 2>/dev/null || true
git -C "$MIRROR" remote add public "https://github.com/$REPO.git"
git -C "$MIRROR" push public "refs/heads/main:refs/heads/main"
git -C "$MIRROR" tag -f "$TAG" main >/dev/null && git -C "$MIRROR" push public "refs/tags/$TAG"
echo "pushed main and $TAG to https://github.com/$REPO"
echo "next: build and publish fl-studio-agent to PyPI from a checkout of the PUBLIC repository, never from this private one."
