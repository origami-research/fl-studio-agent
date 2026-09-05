# Public-history rewrite plan

This is a reviewable launch procedure, not an instruction to rewrite the
private repository now. The owner must explicitly approve the public boundary,
license, and force-push window before any command in this document is run.

## Preconditions

1. Approve the public allowlist and the final license in writing.
2. Freeze merges and tags for the release window, notify every clone owner, and
   preserve a private, access-controlled backup.
3. Build a fresh mirror clone for the operation. Never run `git filter-repo` in
   the canonical private clone.
4. Obtain written confirmation that any included FL SDK-derived material and
   binary fixture may be redistributed.

## Review the removal set

The minimum removal set is `docs/32-repo-split.md`, all editor-review artifacts,
and the removed strategy reports under `docs/research/`. Expand it with every
path found by the final private-content review. Prefer an allowlisted public
extraction if that review identifies ambiguous material.

```bash
git clone --mirror <private-remote> fl-studio-mcp-public.git
cd fl-studio-mcp-public.git
git filter-repo --analyze
```

Review `filter-repo/analysis/` with the owner, then record the approved path
list in the release ticket. Do not infer an allowlist from this example.

## Rewrite only after approval

Use a reviewed path file to avoid a shell glob changing the scope:

```text
docs/32-repo-split.md
<review-artifact-directory>/
docs/research/improvement-plan-2026-09-01.md
docs/research/insights-2026-09-01.md
```

Translate that approved list into explicit `--path` and `--path-glob` arguments
in the release ticket, then run `git filter-repo --invert-paths` in the mirror.
The exact command is intentionally left for that approval because the public
boundary, not this draft, determines its complete removal set.

## Verify before publication

1. Search every reachable rewritten commit, tag, and blob for each removed path
   and for private-content terms from the approved review.
2. Run `gitleaks git --redact --no-banner` in the rewritten mirror and resolve
   every finding. Rotate any exposed credential before publishing, even if the
   rewrite removed it.
3. Create a clean checkout from the rewritten mirror and run the documented
   tests plus the public artifact and license review.
4. Have a second reviewer compare the rewritten tree against the approved
   allowlist. Confirm that no private refs, reflogs, bundles, pull-request
   patches, or release archives will be published.
5. Only then create a new public remote, push the reviewed refs, and retain the
   original repository as private.
