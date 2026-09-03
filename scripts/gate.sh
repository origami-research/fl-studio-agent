#!/usr/bin/env bash
# Gate a remote PR head or branch from a disposable, clean checkout.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/gate.sh [PR_NUMBER|BRANCH|HEAD]

Gates a fresh checkout of origin's PR head, branch, or default HEAD. The
checkout is removed when the gate finishes. With no argument, HEAD means the
remote default branch.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

source_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)"
origin_url="$(git -C "$source_root" remote get-url origin)"
target="${1:-HEAD}"
checkout_root="$(mktemp -d "${TMPDIR:-/tmp}/fl-mcp-gate.XXXXXXXX")"
checkout="$checkout_root/repo"
pytest_log="$checkout_root/pytest.log"
head_sha="unknown"
failed=0
pytest_passed="unknown"
pytest_skipped="unknown"

# shellcheck disable=SC2329 # Invoked by the EXIT trap below.
cleanup() {
    rm -rf "$checkout_root"
}

summary() {
    local result="PASS"
    if [ "$failed" -ne 0 ]; then
        result="FAIL"
    fi
    printf '\n%s: head=%s pytest=%s passed, %s skipped\n' "$result" "$head_sha" "$pytest_passed" "$pytest_skipped"
}

trap cleanup EXIT
trap 'failed=1; summary; exit 1' ERR
trap 'failed=1; summary; exit 1' INT TERM

run_phase() {
    local name="$1"
    shift
    printf '\n==> %s\n' "$name"
    if "$@"; then
        printf '    PASS: %s\n' "$name"
    else
        printf '    FAIL: %s\n' "$name" >&2
        failed=1
    fi
}

printf 'Fresh checkout: %s\n' "$target"
git clone --quiet --no-checkout "$origin_url" "$checkout"

if [[ "$target" =~ ^[0-9]+$ ]]; then
    git -C "$checkout" fetch --quiet --no-tags origin "refs/pull/$target/head:refs/remotes/origin/pr/$target"
    gate_ref="refs/remotes/origin/pr/$target"
elif [ "$target" = "HEAD" ]; then
    gate_ref="origin/HEAD"
else
    git -C "$checkout" fetch --quiet --no-tags origin "refs/heads/$target:refs/remotes/origin/$target"
    gate_ref="refs/remotes/origin/$target"
fi

git -C "$checkout" checkout --quiet --detach "$gate_ref"
head_sha="$(git -C "$checkout" rev-parse HEAD)"
printf 'Gating head: %s\n' "$head_sha"

cd "$checkout/mcp-server"
run_phase "uv sync" uv sync
run_phase "editable root install (flmcp_codec)" uv pip install -e ..

cd "$checkout"
run_phase "native SDK fetch" ./native/scripts/fetch_sdk.sh
if [ "$failed" -eq 0 ]; then
    run_phase "native configure" cmake -S native -B native/build
    run_phase "native build" cmake --build native/build
    run_phase "native tests" ctest --test-dir native/build
fi

cd "$checkout/mcp-server"
printf '\n==> pytest\n'
if uv run pytest -q 2>&1 | tee "$pytest_log"; then
    printf '    PASS: pytest\n'
else
    printf '    FAIL: pytest\n' >&2
    failed=1
fi

pytest_summary="$(tail -n 20 "$pytest_log" | grep -Eo '[0-9]+ passed(, [0-9]+ skipped)?' | tail -n 1 || true)"
if [[ "$pytest_summary" =~ ([0-9]+)[[:space:]]passed ]]; then
    pytest_passed="${BASH_REMATCH[1]}"
fi
if [[ "$pytest_summary" =~ ,[[:space:]]([0-9]+)[[:space:]]skipped ]]; then
    pytest_skipped="${BASH_REMATCH[1]}"
else
    pytest_skipped="0"
fi

run_phase "ruff" uvx ruff check ..
summary
exit "$failed"
