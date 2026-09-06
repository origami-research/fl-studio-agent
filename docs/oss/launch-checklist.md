# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

## flpkit on PyPI

flpkit 0.8.1 was published on 2026-09-06; the engine depends on the released package (`flpkit==0.8.1`) with no git source. The release smoke check can run unskipped.

## Approvals

- 2026-09-05: the owner approved the public allowlist, the MIT license, and the history removal set (orchestrator chat, "all approved"). Step 1 is complete. The rewrite still runs only on a fresh mirror, after the freeze window the owner names, and publication needs the target repository name.

## Step 7, mechanised

`scripts/oss/publish-public-mirror.sh --repo <owner/name> --tag v0.1.0 --i-approve-publication` creates the fresh public repository, pushes the verified mirror's `main` and the tag, and refuses when: the latest verification report is not PASS, any removal-set path is still reachable in the mirror, the target repository already exists, or the approval flag is missing. It never touches the private repository's refs. Publishing the engine to PyPI is done afterwards from the public repository (its own trusted-publishing workflow), never from the private one.
