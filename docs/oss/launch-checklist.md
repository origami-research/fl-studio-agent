# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

## flpkit on PyPI

flpkit 0.8.1 was published on 2026-09-06; the engine depends on the released package (`flpkit==0.8.1`) with no git source. The release smoke check can run unskipped.

## Approvals

- 2026-09-05: the owner approved the public allowlist, the MIT license, and the history removal set (orchestrator chat, "all approved"). Step 1 is complete. The rewrite still runs only on a fresh mirror, after the freeze window the owner names, and publication needs the target repository name.

## Step 7, mechanised

One command publishes both the repository and the package:

```bash
scripts/oss/publish-public-mirror.sh --repo origami-research/fl-studio-agent --tag v0.1.0 --mirror <verified-mirror.git> --i-approve-publication
```

It runs the verifier on the mirror in the same invocation, builds and smoke-tests the release wheel and sdist from the mirror's `main`, confirms the PyPI version is free and a token is present (`UV_PUBLISH_TOKEN` or `~/.config/pypi/token`, never printed), and checks that the target repository is empty.
Only then does it push `main` and the tag, upload to PyPI, and read both back (default branch, refs, a clean-venv `pip install` and `fl-mcp-contract`).
It refuses when the verification is not PASS, a removal-set path is reachable, the tag does not match the package version, the version already exists on PyPI, the target repository has branches or tags, `docs/oss/PUBLICATION-HOLD` exists, or the approval flag is missing.
If the upload fails after the push, rerun with `--pypi-only`: it uploads only when the public `main` equals the mirror's `main`.
It never touches the private repository's refs.

## How the mirror is built

`scripts/oss/build-public-mirror.sh <dest.git>` rewrites a fresh mirror clone of the private repository:

1. Paths: the history removal set goes first, then only the public allowlist stays.
2. Public HEAD must already satisfy the history redactions; the build refuses otherwise, so the published `main` is exactly what was reviewed.
3. File contents: the private product's name and links into its repository are rewritten in every file version.
4. History redactions: every historical Markdown version loses the lines the redaction rules name (licensing analysis, hosting and cloud-infrastructure notes, FL Studio binary offsets and addresses).
5. Commit messages: the private product's name is rewritten.
6. Identities: every author and committer becomes a GitHub no-reply address.

The rules for steps 3, 4 and 6 live in `docs/oss/private/`, which is itself in the removal set.

## Package name

The PyPI name `fl-studio-mcp` is owned by a third party (rosasynthesiz, 1.0.0, May 2026). The engine's distribution is `fl-studio-agent`, matching the public repository name; the import package stays `fl_mcp` and the `fl-mcp*` commands keep their names.

## Hold

2026-09-07: the owner put publication on hold until the platform is up and running.
2026-10-22: the owner lifted the hold ("publish please"); `docs/oss/PUBLICATION-HOLD` is deleted.
The publish script still refuses while that file exists, so a future hold is one reviewed commit that re-adds it.

## Owner decisions of 2026-10-22

- The private product is not named in any public file version, and two research documents about it and about OSS readiness leave history.
- Licensing analysis and hosting notes moved to private-only `docs/private/`; history versions lose those lines.
- Documents of FL Studio binary internals stay private (see the removal set); the code that needs the offsets stays public.
- `.gitignore`, `native/abi/`, `native/generated/` and `native/scripts/` are public, so the Bridge builds from the public tree with the fetched SDK.
- Commit identities are GitHub no-reply addresses.
- Publication waits for the co-author's MIT consent.

## Published 2026-10-23

- The co-author (ky11001) consented to MIT; the owner relayed it and gave the publish go.
- `scripts/oss/publish-public-mirror.sh` ran from merged main 14887fb: verification PASS in the same invocation (13 checks, 0 skipped), then it pushed `main` (735c35d) and tag `v0.1.0` to https://github.com/origami-research/fl-studio-agent and uploaded `fl-studio-agent` 0.1.0 to https://pypi.org/project/fl-studio-agent/0.1.0/.
- Read-back: the public default branch is `main` with `main` and `v0.1.0` at 735c35d; a clean-venv `pip install fl-studio-agent==0.1.0` installs the `fl-mcp*` commands and `fl-mcp-contract` runs.
- The script publishes only into an empty repository, so the next release needs a path for updating the published repository (follow-up).
