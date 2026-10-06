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
terms_file="$repo_dir/docs/oss/private-terms.txt"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/fl-studio-mcp-public-verify.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT

failures=()
skips=()
record_failure() {
  failures+=("$1")
  echo "FAIL: $1" >&2
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

content_objects="$work_dir/content-objects"
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

if gitleaks_bin=$(ensure_gitleaks); then
  if ! (cd "$mirror" && "$gitleaks_bin" git --redact --no-banner); then
    record_failure "gitleaks reported findings"
  fi
else
  record_failure "gitleaks was unavailable"
fi

checkout="$work_dir/checkout"
git clone "$mirror" "$checkout"
private_terms_failed=0
while IFS= read -r term || [[ -n "$term" ]]; do
  [[ -z "$term" || "$term" == \#* ]] && continue
  # Terms describe public HEAD, while the removal set covers rewritten history.
  # These two control files necessarily contain their own policy literals.
  if git -C "$checkout" grep -I -w -F -- "$term" HEAD -- . \
      ':(exclude)docs/oss/private-terms.txt' \
      ':(exclude)docs/oss/history-removal-set.txt' >/dev/null; then
    private_terms_failed=1
  fi
done <"$terms_file"
if ((private_terms_failed)); then
  record_failure "a private-content term remains in public HEAD"
fi

if ! (cd "$checkout" && uv sync && uv run --directory mcp-server pytest -q); then
  record_failure "Python sync and test suite failed"
fi
if ! (cd "$checkout" && uvx ruff check .); then
  record_failure "ruff failed"
fi
if [[ -d "$checkout/native/sdk" ]]; then
  if ! (cmake -S "$checkout/native" -B "$work_dir/native-build" && cmake --build "$work_dir/native-build" && ctest --test-dir "$work_dir/native-build" --output-on-failure); then
    record_failure "native build or ctest failed"
  fi
else
  record_skip "native build and ctest need the licensed FL Studio SDK. Configure with: cmake -S native -B native/build -DFL_SDK_DIR=/path/to/licensed-sdk"
fi

if (cd "$checkout" && bash scripts/build-wheel.sh --release --out "$work_dir/release" >/dev/null 2>&1); then
  echo "PASS: release artifact check (scripts/build-wheel.sh --release)"
else
  record_failure "release artifact check (scripts/build-wheel.sh --release)"
fi

# Commit messages are public too: no private term may survive in any message.
message_hits=$(git -C "$mirror" log --all --format='%B' | grep -ciE '\barena\b|producer[ -]arena' || true)
if [[ "$message_hits" != 0 ]]; then
  record_failure "commit messages name the private product ($message_hits line(s) match arena)"
fi

check_count=9
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
- Checks passed: $((check_count - ${#failures[@]} - ${#skips[@]}))
- Checks skipped: ${#skips[@]}
- Checks not passed: ${#failures[@]}
EOF
)
printf '%s\n' "$report"
if [[ -n "$report_file" ]]; then
  printf '%s\n' "$report" >"$report_file"
fi

((${#failures[@]} == 0))
