#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
mirror="$HOME/Documents/Code/fl-studio-mcp-public.git"
while (($#)); do
  case "$1" in
    --mirror) mirror="$2"; shift 2 ;;
    --mirror=*) mirror="${1#--mirror=}"; shift ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) mirror="$1"; shift ;;
  esac
done
report_file=${OSS_VERIFICATION_REPORT:-}
removal_file="$repo_dir/docs/oss/history-removal-set.txt"
terms_file="$repo_dir/docs/oss/private/private-terms.txt"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/fl-studio-mcp-public-verify.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT

# Each check calls begin_check, so the counts in the report are derived, never maintained by
# hand (2026-10-22: the hand-kept total had drifted one above the real number of checks).
failures=()
skips=()
checks=0
failed_checks=0
current_failed=0
begin_check() {
  checks=$((checks + 1))
  current_failed=0
}
record_failure() {
  failures+=("$1")
  echo "FAIL: $1" >&2
  if ((current_failed == 0)); then
    failed_checks=$((failed_checks + 1))
    current_failed=1
  fi
}
record_skip() {
  skips+=("$1")
  echo "SKIP: $1" >&2
}

case "$mirror" in
  /*) ;;
  *) mirror="$PWD/$mirror" ;;
esac
mirror=$(cd "$mirror" 2>/dev/null && pwd) || { echo "mirror does not exist: $mirror" >&2; exit 2; }
[[ $(git -C "$mirror" rev-parse --is-bare-repository 2>/dev/null || true) == true ]] || {
  echo "mirror must be a bare repository: $mirror" >&2
  exit 2
}

commit_count=$(git -C "$mirror" rev-list --all --count)
tag_count=$(git -C "$mirror" for-each-ref --format='%(refname)' refs/tags | wc -l | tr -d ' ')
blob_count=$(git -C "$mirror" rev-list --all --objects | awk 'NF > 1 { print $1 }' | sort -u | wc -l | tr -d ' ')

objects_file="$work_dir/objects"
git -C "$mirror" rev-list --all --objects >"$objects_file"
paths_file="$work_dir/paths"
awk 'NF > 1 { sub(/^[^ ]+ /, ""); print }' "$objects_file" | sort -u >"$paths_file"

begin_check  # removal-set paths are absent from every reachable commit
removed_paths_failed=0
while IFS= read -r removed_path || [[ -n "$removed_path" ]]; do
  [[ -z "$removed_path" || "$removed_path" == \#* ]] && continue
  if [[ "$removed_path" == */ ]]; then
    if grep -Fqx "${removed_path%/}" "$paths_file" || grep -Fq "${removed_path}" "$paths_file"; then
      removed_paths_failed=1
    fi
  elif grep -Fqx "$removed_path" "$paths_file"; then
    removed_paths_failed=1
  fi
done <"$removal_file"
if ((removed_paths_failed)); then
  record_failure "a removed path remains"
fi

ensure_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then
    command -v gitleaks
    return
  fi
  # gitleaks is distributed by Homebrew. Install it only when absent so this
  # verifier remains reproducible on clean release machines.
  if command -v brew >/dev/null 2>&1; then
    brew install gitleaks >&2
    command -v gitleaks
    return
  fi
  echo "gitleaks is required. Install it with: brew install gitleaks" >&2
  return 1
}

begin_check  # gitleaks over full history
if gitleaks_bin=$(ensure_gitleaks); then
  if ! (cd "$mirror" && "$gitleaks_bin" git --redact --no-banner); then
    record_failure "gitleaks reported findings"
  fi
else
  record_failure "gitleaks was unavailable"
fi

checkout="$work_dir/checkout"
git clone "$mirror" "$checkout"
begin_check  # private terms in public HEAD
private_terms_failed=0
while IFS= read -r term || [[ -n "$term" ]]; do
  [[ -z "$term" || "$term" == \#* ]] && continue
  # Terms describe public HEAD, while the removal set covers rewritten history.
  # The removal set necessarily names the paths it removes.
  if git -C "$checkout" grep -I -w -F -- "$term" HEAD -- . \
      ':(exclude)docs/oss/history-removal-set.txt' >/dev/null; then
    private_terms_failed=1
  fi
done <"$terms_file"
if ((private_terms_failed)); then
  record_failure "a private-content term remains in public HEAD"
fi

begin_check  # Python suite
if ! (cd "$checkout" && uv sync && uv run --directory mcp-server pytest -q); then
  record_failure "Python sync and test suite failed"
fi
begin_check  # ruff
if ! (cd "$checkout" && uvx ruff check .); then
  record_failure "ruff failed"
fi
# The public tree must build the Bridge with nothing but the SDK every developer fetches
# (2026-10-22: native/abi was missing from the allowlist and no check noticed).
begin_check  # native build and ctest
if [[ ! -d "$checkout/native/sdk" ]] && ! bash "$checkout/native/scripts/fetch_sdk.sh" >/dev/null 2>&1; then
  record_skip "native build and ctest: the FL Studio SDK could not be fetched (native/scripts/fetch_sdk.sh); configure with -DFL_SDK_DIR=/path/to/licensed-sdk"
elif ! (cmake -S "$checkout/native" -B "$work_dir/native-build" && cmake --build "$work_dir/native-build" -j 8 && ctest --test-dir "$work_dir/native-build" --output-on-failure); then
  record_failure "native build or ctest failed"
else
  echo "PASS: native build and ctest"
fi
begin_check  # the SDK is not tracked
if git -C "$checkout" ls-files --error-unmatch native/sdk >/dev/null 2>&1; then
  record_failure "the fetched FL Studio SDK is tracked in the public tree"
fi

# Relative Markdown links in public HEAD must resolve inside the public tree.
begin_check  # dead relative links
if ! dead_links=$(python3 -I "$repo_dir/scripts/oss/public_docs.py" dead-links "$checkout"); then
  record_failure "dead relative links in public HEAD: $(echo "$dead_links" | head -20 | paste -sd';' -)"
fi

begin_check  # release artifact
if (cd "$checkout" && bash scripts/build-wheel.sh --release --out "$work_dir/release" >/dev/null 2>&1); then
  echo "PASS: release artifact check (scripts/build-wheel.sh --release)"
else
  record_failure "release artifact check (scripts/build-wheel.sh --release)"
fi

# Commit messages are public too: no private term may survive in any message.
begin_check  # commit messages
message_hits=$(git -C "$mirror" log --all --format='%B' | grep -ciE '\barena\b|producer[ -]arena' || true)
if [[ "$message_hits" != 0 ]]; then
  record_failure "commit messages name the private product ($message_hits line(s) match arena)"
fi

# History is public too: old versions of a file must not name the private product either.
# docs/oss/private/private-terms.txt covers public HEAD only; this covers every reachable commit (2026-10-22:
# earlier README, SETUP and docs versions named it although HEAD was clean).
# xargs batches the revisions; each batch must come before the pathspec separator.
# git grep exits 1 for "no match"; anything above 1 is a scan error and fails the check.
begin_check  # product name in any file version
content_hits_file="$work_dir/content-hits"
if ! git -C "$mirror" rev-list --all | xargs sh -c 'git -C "$0" grep -I -l -i -E "producer[ -]?arena" "$@" -- . \
    ":(exclude)scripts/oss/verify-public-mirror.sh"; [ "$?" -le 1 ]' \
    "$mirror" >"$content_hits_file"; then
  record_failure "could not scan history for the private product name"
fi
content_hits=$(cut -d: -f2- "$content_hits_file" | sort -u)
if [[ -n "$content_hits" ]]; then
  record_failure "file content in history names the private product: $(echo "$content_hits" | paste -sd, -)"
fi

# Every historical Markdown version obeys docs/oss/private/history-redactions.txt.
begin_check  # history redactions
set +e
redaction_hits=$(python3 -I "$repo_dir/scripts/oss/public_docs.py" scan-history "$mirror" "$repo_dir/docs/oss/private/history-redactions.txt")
redaction_status=$?
set -e
if ((redaction_status == 1)); then
  record_failure "redacted lines remain in history: $(echo "$redaction_hits" | head -10 | paste -sd';' -)"
elif ((redaction_status != 0)); then
  record_failure "could not scan history for redacted lines"
fi

# Every author and committer is a GitHub no-reply identity (docs/oss/private/mailmap.txt).
begin_check  # no-reply identities
identity_hits=$(git -C "$mirror" log --all --format='%ae%n%ce' | sort -u | grep -v -E '^([0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com|noreply@github\.com)$' || true)
if [[ -n "$identity_hits" ]]; then
  record_failure "commit identities are not GitHub no-reply addresses: $(echo "$identity_hits" | paste -sd, -)"
fi

status=PASS
if ((${#failures[@]})); then
  status=FAIL
fi
report=$(cat <<EOF
# Public mirror verification - $(date -u +%Y-%m-%d)

Status: $status

- Reachable commits: $commit_count
- Tags: $tag_count
- Unique reachable blobs: $blob_count
- Checks passed: $((checks - failed_checks - ${#skips[@]}))
- Checks skipped: ${#skips[@]}
- Checks not passed: $failed_checks
EOF
)
printf '%s\n' "$report"
if [[ -n "$report_file" ]]; then
  printf '%s\n' "$report" >"$report_file"
fi

((${#failures[@]} == 0))
