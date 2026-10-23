#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
default_destination="$HOME/Documents/Code/fl-studio-mcp-public.git"
destination=${1:-"$default_destination"}
remote=${OSS_PRIVATE_REMOTE:-}

usage() {
  echo "usage: $0 [destination-bare-mirror]" >&2
  echo "Set OSS_PRIVATE_REMOTE to override this checkout's origin URL." >&2
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "$remote" ]]; then
  remote=$(git -C "$repo_dir" remote get-url origin)
fi

case "$destination" in
  /*) ;;
  *) destination="$PWD/$destination" ;;
esac
destination=$(cd "$(dirname "$destination")" && pwd)/$(basename "$destination")
source_git_dir=$(git -C "$repo_dir" rev-parse --git-dir)
source_git_dir=$(cd "$repo_dir/$source_git_dir" 2>/dev/null && pwd || cd "$source_git_dir" && pwd)

if [[ "$destination" == "$repo_dir" || "$destination" == "$source_git_dir" || "$destination" == "$repo_dir"/* ]]; then
  echo "refusing to write the canonical checkout or anything inside it: $destination" >&2
  exit 2
fi
if [[ $(basename "$destination") != *.git ]]; then
  echo "destination must be a bare mirror path ending in .git: $destination" >&2
  exit 2
fi
if [[ -e "$destination" && ! -d "$destination" ]]; then
  echo "destination exists and is not a directory: $destination" >&2
  exit 2
fi
if [[ -d "$destination" && $(git -C "$destination" rev-parse --is-bare-repository 2>/dev/null || true) != true ]]; then
  echo "destination exists but is not a bare repository: $destination" >&2
  exit 2
fi

for file in docs/oss/public-allowlist.txt docs/oss/history-removal-set.txt docs/oss/private/message-replacements.txt \
    docs/oss/private/content-replacements.txt docs/oss/private/history-redactions.txt docs/oss/private/mailmap.txt; do
  [[ -s "$repo_dir/$file" ]] || { echo "missing $file" >&2; exit 2; }
done

filter_repo() {
  if command -v git-filter-repo >/dev/null 2>&1; then
    git-filter-repo "$@"
  else
    uvx --from git-filter-repo git-filter-repo "$@"
  fi
}

parent_dir=$(dirname "$destination")
mkdir -p "$parent_dir"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/fl-studio-mcp-public-mirror.XXXXXX")
staging="$parent_dir/.$(basename "$destination").staging.$$"
backup="$parent_dir/.$(basename "$destination").previous.$$"
trap 'rm -rf "$work_dir" "$staging" "$backup"' EXIT

git clone --mirror "$remote" "$work_dir/source.git"
before_commits=$(git -C "$work_dir/source.git" rev-list --all --count)

redactions="$repo_dir/docs/oss/private/history-redactions.txt"
callback="$work_dir/redact-callback.py"
cat >"$callback" <<'CALLBACK'
import os
import sys
sys.path.insert(0, os.environ["FLMCP_OSS_SCRIPTS"])
from public_docs import redact_file_info
return redact_file_info(filename, mode, blob_id, value)
CALLBACK

pushd "$work_dir/source.git" >/dev/null
filter_repo --force --paths-from-file "$repo_dir/docs/oss/history-removal-set.txt" --invert-paths
filter_repo --force --paths-from-file "$repo_dir/docs/oss/public-allowlist.txt"
# Redaction rewrites history only: public HEAD must already be clean by hand, so what the
# owner reviewed on main is exactly what the mirror publishes.
set +e
head_findings=$(python3 -I "$repo_dir/scripts/oss/public_docs.py" check-ref "$work_dir/source.git" main "$redactions")
head_status=$?
set -e
if ((head_status == 1)); then
  echo "refusing: public HEAD still carries lines docs/oss/private/history-redactions.txt removes; clean them on main:" >&2
  printf '%s\n' "$head_findings" >&2
  exit 2
elif ((head_status != 0)); then
  echo "refusing: could not scan public HEAD for redacted lines" >&2
  exit 2
fi
filter_repo --force --replace-text "$repo_dir/docs/oss/private/content-replacements.txt"
FLMCP_OSS_SCRIPTS="$repo_dir/scripts/oss" FLMCP_REDACTIONS="$redactions" filter_repo --force --file-info-callback "$callback"
filter_repo --force --replace-message "$repo_dir/docs/oss/private/message-replacements.txt"
filter_repo --force --mailmap "$repo_dir/docs/oss/private/mailmap.txt"
popd >/dev/null

after_commits=$(git -C "$work_dir/source.git" rev-list --all --count)
git -C "$work_dir/source.git" fsck --no-reflogs --unreachable >/dev/null
git clone --mirror "$work_dir/source.git" "$staging"

if [[ -e "$destination" ]]; then
  mv "$destination" "$backup"
fi
mv "$staging" "$destination"
rm -rf "$backup"

echo "public mirror: $destination"
echo "commits before: $before_commits"
echo "commits after: $after_commits"
