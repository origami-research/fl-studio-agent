# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

## flpkit on PyPI

flpkit 0.8.1 was published on 2026-09-06; the engine depends on the released package (`flpkit==0.8.1`) with no git source. The release smoke check can run unskipped.

## Approvals

- 2026-09-05: the owner approved the public allowlist, the MIT license, and the history removal set (orchestrator chat, "all approved"). Step 1 is complete. The rewrite still runs only on a fresh mirror, after the freeze window the owner names, and publication needs the target repository name.

## Step 7, mechanised

`scripts/oss/publish-public-mirror.sh --repo <owner/name> --tag v0.1.0 --i-approve-publication` creates the public repository (or accepts the owner-created one only while it has zero branches), pushes the verified mirror's `main` and the tag, and refuses when: the verification it runs itself is not PASS, any removal-set path is still reachable in the mirror, the target repository has branches, `docs/oss/PUBLICATION-HOLD` exists, or the approval flag is missing. It never touches the private repository's refs. Publishing the engine to PyPI is done afterwards from the public repository (its own trusted-publishing workflow), never from the private one.

## Package name

The PyPI name `fl-studio-mcp` is owned by a third party (rosasynthesiz, 1.0.0, May 2026). The engine's distribution is `fl-studio-agent`, matching the public repository name; the import package stays `fl_mcp` and the `fl-mcp*` commands keep their names.

## Hold

2026-09-07: the owner put publication on hold until the platform is up and running.
2026-10-22: the owner lifted the hold ("publish please"); `docs/oss/PUBLICATION-HOLD` is deleted.
The publish script still refuses while that file exists, so a future hold is one reviewed commit that re-adds it.

## Open before the push (2026-10-22)

- `docs/38-python-kernel.md` (added after the hold) names the private product's machine image and infrastructure path, so it joined the history removal set; public docs that link to it carry dead links until a scrubbed copy is published.
- Older versions of README, SETUP and several docs name the private product although public HEAD is clean; the verifier now checks every reachable file version and fails on them until the owner chooses to scrub or remove those versions.
