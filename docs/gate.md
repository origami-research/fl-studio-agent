# Merge gate

Run the local merge gate from any checkout of this repository:

```bash
scripts/gate.sh 170       # GitHub PR number
scripts/gate.sh main      # remote branch
scripts/gate.sh           # origin's default HEAD
```

The script clones `origin` into a disposable directory, resolves and prints the
exact head SHA, then runs the gate there. A PR number is fetched from
`refs/pull/<number>/head`, so the result cannot accidentally use a local or
stale tracking branch.

The gate runs `uv sync`, installs the repository root editable into the
`mcp-server` environment (the e2e tests import `harness`), fetches the proprietary
SDK, builds and tests native code, then runs `uv run pytest -q` and
`uvx ruff check`. Native work comes before pytest so the Python/C++ differential
test is eligible to run. The SDK fetch, build, tests, Python tests, and Ruff are
all required: any failure exits non-zero. Its final `PASS` or `FAIL` line records
the gated SHA plus pytest passed and skipped counts; compare that SHA with the
PR immediately before merge.

The fetched SDK and build products live only in the disposable checkout and are
removed after the command completes.
