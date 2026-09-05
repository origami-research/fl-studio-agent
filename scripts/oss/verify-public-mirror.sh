#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
mirror=${1:-"$HOME/Documents/Code/fl-studio-mcp-public.git"}
report_file=${OSS_VERIFICATION_REPORT:-}
removal_file="$repo_dir/docs/oss/history-removal-set.txt"
terms_file="$repo_dir/docs/oss/private-terms.txt"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/fl-studio-mcp-public-verify.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT

failures=()
record_failure() {
  failures+=("$1")
  echo "FAIL: $1" >&2
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
# The term list is the verifier's control input. It is intentionally retained
# for repeatable releases, so exclude only that file's blob from its own scan.
awk 'NF > 1 && $2 != "docs/oss/private-terms.txt" { print $1 }' "$objects_file" | sort -u >"$content_objects"
commit_objects=$(git -C "$mirror" rev-list --all | sort -u)
tag_objects=$(git -C "$mirror" for-each-ref --format='%(objecttype) %(objectname)' refs/tags | awk '$1 == "tag" { print $2 }')
printf '%s\n%s\n' "$commit_objects" "$tag_objects" | sed '/^$/d' | sort -u >>"$content_objects"
sort -u "$content_objects" -o "$content_objects"

private_terms_failed=0
while IFS= read -r term || [[ -n "$term" ]]; do
  [[ -z "$term" || "$term" == \#* ]] && continue
  if git -C "$mirror" cat-file --batch-check='%(objectname) %(objecttype)' <"$content_objects" |
      awk '$2 == "blob" || $2 == "commit" || $2 == "tag" { print $1 }' |
      git -C "$mirror" cat-file --batch | LC_ALL=C grep -F -a -- "$term" >/dev/null; then
    private_terms_failed=1
  fi
done <"$terms_file"
if ((private_terms_failed)); then
  record_failure "a private-content term remains"
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

if gitleaks_bin=$(ensure_gitleaks); then
  if ! (cd "$mirror" && "$gitleaks_bin" git --redact --no-banner); then
    record_failure "gitleaks reported findings"
  fi
else
  record_failure "gitleaks was unavailable"
fi

checkout="$work_dir/checkout"
git clone "$mirror" "$checkout"
if ! (cd "$checkout" && uv sync --all-extras && uv run --directory mcp-server pytest -q); then
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
  record_failure "native build could not run because the private FL SDK is absent"
fi
if ! (cd "$checkout" && scripts/check-release.sh); then
  record_failure "release check failed"
fi

check_count=7
status=PASS
if ((${#failures[@]})); then
  status=FAIL
fi
report=$(cat <<EOF
# Public mirror verification - 2026-09-05

Status: $status

- Reachable commits: $commit_count
- Tags: $tag_count
- Unique reachable blobs: $blob_count
- Checks passed: $((check_count - ${#failures[@]}))
- Checks not passed: ${#failures[@]}
EOF
)
printf '%s\n' "$report"
if [[ -n "$report_file" ]]; then
  printf '%s\n' "$report" >"$report_file"
fi

((${#failures[@]} == 0))
