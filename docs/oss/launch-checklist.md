# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

1. Approve [public-allowlist.txt](public-allowlist.txt), the MIT license, and
   [history-removal-set.txt](history-removal-set.txt). Confirm that every
   included FL-derived file is redistributable.
2. Resolve the release blockers in [extraction-audit.md](extraction-audit.md):
   replace the draft license, finish PyPI metadata, use released `flpkit`, and
   make the wheel build from the sdist.
3. Announce and enforce a merge and tag freeze. Preserve an access-controlled
   private backup and notify every clone owner.
4. Create a fresh mirror from the private remote. Run the approved allowlist
   extraction and removal command only in that mirror.
5. Verify every rewritten reachable commit, tag, and blob lacks each removal
   path and private-content term. Run `gitleaks git --redact --no-banner` in
   the rewritten mirror. Rotate any credential finding before publication.
6. Make a clean checkout from the rewritten mirror. Run the documented tests,
   artifact review, license review, `uv build`, and a second-reviewer allowlist
   comparison. Confirm no private refs, reflogs, bundles, pull-request patches,
   or release archives are published.
7. Create the new public repository only after verification passes, then push
   the reviewed refs, create the approved tag, and publish the built package to
   PyPI. Keep the original repository private.
