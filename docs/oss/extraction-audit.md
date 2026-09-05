# OSS extraction dry run

Status: proposal only. No public repository, push, tag, PyPI upload, force-push,
or FL Studio launch occurred.

## Decision and boundary

The owner decision dated 2026-09-05 is: open the Bridge under MIT and publish
the engine plus knowledge; keep the product and infrastructure private. The
open-core proposal at `2b54ab9:docs/32-repo-split.md` supplies the same boundary:
`mcp-server`, the Bridge, controller scripts, contracts, and polished docs are
public. Raw experiments, harnesses, tools, probes, spikes, and the old control
experiment remain private by default.

`docs/37-oss-launch-decision.md` is absent from the reachable repository
history inspected here. The owner decision above was supplied with this work;
the historical open-core proposal is the recorded repository source used for
the boundary judgment.

The proposed public paths are in [public-allowlist.txt](public-allowlist.txt).
Everything not listed is excluded by construction. The removal set is in
[history-removal-set.txt](history-removal-set.txt), and is required even if the
eventual extraction uses the allowlist.

Ambiguous paths:

| Path | Proposal |
| --- | --- |
| `docs/research/` other than the two removal paths | Public only where it is polished, redistributable reverse-engineering knowledge; the reviewer must remove raw traces or internal notes before approval. |
| `native/probe/` and `native/spike1/` | Private: they are raw investigation scaffolding, not the supported Bridge. |
| `native/control/` | Private: it is a superseded control experiment, and its `fpsdk` subtree is confirmed proprietary SDK material. |
| `experiments/`, `harness/`, `tools/` | Private under the open-core proposal's default-private gray area. |
| `scripts/gate.sh` | Public: it is the documented, reproducible merge gate for the engine. |

## Isolated history analysis

The successful audit used a fresh bare mirror at
`/tmp/fl-studio-mcp-oss-mirror.RKNsuZ/fl-studio-mcp.git`. It analyzed 580
reachable revisions, 4,478 revision-object entries, and 1,101 unique historical
paths. `git filter-repo --analyze` produced its reports only in that mirror.

Exact successful commands:

```bash
remote_url=$(git remote get-url origin)
audit_tmp=$(mktemp -d /tmp/fl-studio-mcp-oss-mirror.XXXXXX)
mirror="$audit_tmp/fl-studio-mcp.git"
git clone --mirror "$remote_url" "$mirror"
cd "$mirror"
uvx --from git-filter-repo git-filter-repo --analyze
git rev-list --all --count
git rev-list --all --objects
git rev-list --all --objects | cut -d' ' -f2- | sed '/^$/d' | sort -u
```

`git-filter-repo` was not installed, so `uvx --from git-filter-repo` installed
and ran it in an isolated tool environment. No `git filter-repo` rewrite was
run.

Largest individual blobs were `mcp-server/uv.lock` (202,002 bytes),
`native/spike1/SPIKE1_FINDINGS.md` (106,139),
`mcp-server/tests/evidence/plugin-effect/after-add-parametric-eq2-on-master.flp`
(90,088), and `docs/21-v2-rewrite-spec.md` (57,559). The complete path and
size reports remain in the temporary mirror's `filter-repo/analysis/` directory.
These are ordinary repository-size findings, not removals by themselves.

## Credential and SDK review

No `gitleaks` or `trufflehog` executable was available. The audit therefore
used this documented historical regex sweep, without printing matched values:

```bash
secret_re='(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|-----BEGIN( [A-Z]+)? PRIVATE KEY-----|[Pp]assword[[:space:]]*[:=][[:space:]]*[^[:space:]]{8,}|[Tt]oken[[:space:]]*[:=][[:space:]]*[^[:space:]]{12,}|[Ss]ecret[[:space:]]*[:=][[:space:]]*[^[:space:]]{12,})'
git rev-list --all | while read -r revision; do
  git grep -IlE "$secret_re" "$revision" 2>/dev/null || true
done | sed 's/^[^:]*://' | sort -u
```

There were zero matches for AWS access keys, GitHub tokens, Slack tokens, or
private-key PEM headers. Four code paths matched the broad assignment fallback:
`mcp-server/src/fl_mcp/bridge_wire.py`,
`mcp-server/src/fl_mcp/core/carriers/udp.py`,
`mcp-server/src/fl_mcp/dev_session.py`, and `native/probe/src/probe_core.cpp`.
They implement local per-session protocol tokens rather than stored credentials.
Result: 0 secrets found, with those four paths retained for the final scanner
review.

The path-name sweep found `.github/workflows/secret-scan.yml` and no historical
`.env`, key, credential, or token file. It found six historical editor-review
artifacts, all covered by the removal glob.

**The proprietary FL SDK did enter history.** `native/control/fpsdk/src/cxx/`
contains the SDK's core definitions, plugin API header, and plugin API implementation; their headers say
"FL Studio generator/effect plugins SDK". The SDK filename set was obtained
from `native/scripts/fetch_sdk.sh`'s source and checked across every reachable
revision. `native/control/fpsdk/` is therefore in the removal set.

## PyPI readiness

`mcp-server/pyproject.toml` currently declares `fl-studio-mcp` version `0.1.0`
and six console entry points. It lacks project `readme`, SPDX `license`,
classifiers, and project URLs. Its `flpkit` dependency is a Git URL rather than
a released versioned package. The repository `LICENSE` is explicitly a private
draft, not MIT.

Before publication: replace `LICENSE` with the unmodified MIT text; add the
metadata above; change `flpkit` to a published compatible version requirement;
and make the controller inclusion work from the sdist, either by packaging the
controller under `mcp-server` or explicitly including its source in the sdist.

`uv build` was run in `mcp-server`. It created an sdist, then failed building
the wheel because Hatch could not find the force-included sibling path
`../fl-scripts/controller/device_FLMCP.py` in the unpacked sdist. No publishable
wheel was produced.

## Notes

A preliminary `git clone --mirror origin` command failed because `origin` was
treated as a filesystem path. Its following analysis command wrote reports to
this worktree's Git metadata, not to history or tracked files. No history was
rewritten. The results above are solely from the corrected, isolated mirror.
