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

for file in docs/oss/public-allowlist.txt docs/oss/history-removal-set.txt; do
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

pushd "$work_dir/source.git" >/dev/null
filter_repo --force --paths-from-file "$repo_dir/docs/oss/history-removal-set.txt" --invert-paths
filter_repo --force --paths-from-file "$repo_dir/docs/oss/public-allowlist.txt"
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
