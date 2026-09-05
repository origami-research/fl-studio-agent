# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

## flpkit PyPI prerequisite

`flpkit` `0.8.0` is not published on PyPI. Publish a compatible `flpkit`
release before publishing this engine, then make it the engine's normal
released dependency and regenerate `mcp-server/uv.lock`. Until then, internal
checkouts use the pinned Git source only through the `private` extra.

1. Approve [public-allowlist.txt](public-allowlist.txt), the MIT license, and
   [history-removal-set.txt](history-removal-set.txt). Confirm that every
   included FL-derived file is redistributable.
2. Resolve the release blockers in [extraction-audit.md](extraction-audit.md):
   finish PyPI metadata and make the wheel build from the sdist. Complete the
   `flpkit` prerequisite above before publishing the engine.
3. Announce and enforce a merge and tag freeze. Preserve an access-controlled
   private backup and notify every clone owner.
4. Create a fresh mirror from the private remote. Run the approved allowlist
   extraction and removal command only in that mirror.
5. Verify every rewritten reachable commit, tag, and blob lacks each removal
   path and private-content term. Run `gitleaks git --redact --no-banner` in
   the rewritten mirror. Rotate any credential finding before publication.
6. Make a clean checkout from the rewritten mirror. Run the documented tests,
   artifact review, license review, `scripts/check-release.sh`, and a
   second-reviewer allowlist comparison. The release check builds both
   artifacts, rebuilds a wheel from the sdist alone, installs that wheel in a
   fresh venv, and runs the contract and import smoke tests. Confirm no private
   refs, reflogs, bundles, pull-request patches, or release archives are
   published.
7. Create the new public repository only after verification passes, then push
   the reviewed refs, create the approved tag, and publish the built package to
   PyPI. Keep the original repository private.

## Approvals

- 2026-09-05: the owner approved the public allowlist, the MIT license, and the history removal set (orchestrator chat, "all approved"). Step 1 is complete. The rewrite still runs only on a fresh mirror, after the freeze window the owner names, and publication needs the target repository name.
