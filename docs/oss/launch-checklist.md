# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

## flpkit on PyPI

flpkit 0.8.1 was published on 2026-09-06; the engine depends on the released package (`flpkit==0.8.1`) with no git source. The release smoke check can run unskipped.

## Approvals

- 2026-09-05: the owner approved the public allowlist, the MIT license, and the history removal set (orchestrator chat, "all approved"). Step 1 is complete. The rewrite still runs only on a fresh mirror, after the freeze window the owner names, and publication needs the target repository name.

## Step 7, mechanised: every release

One command publishes a release, the first or any later one:

```bash
scripts/oss/build-public-mirror.sh <mirror.git>
scripts/oss/publish-public-mirror.sh --repo origami-research/fl-studio-agent --tag vX.Y.Z --mirror <mirror.git> --i-approve-publication
```

Before a release, bump `version` in `mcp-server/pyproject.toml`, run `python3 scripts/install_links.py` (it regenerates `server.json` and the bundle manifest with the new version) and `uv run fl-mcp-contract > ../contracts/fl-mcp.json` from `mcp-server/` (the contract carries the version), relock with `uv lock`, and add the release notes at `docs/releases/vX.Y.Z.md`, all merged on main.

The publish script runs the verifier on the mirror in the same invocation, builds and smoke-tests the release wheel and sdist and packs the Claude Desktop bundle from the mirror's `main`, confirms the PyPI version is free and a token is present (`UV_PUBLISH_TOKEN` or `~/.config/pypi/token`, never printed), and checks the target repository (the tag is new and its `main` is an ancestor).
Then it uploads to PyPI and reads it back (a clean-venv `pip install`, the commands, `fl-mcp-contract`), pushes `main`, and only then pushes the tag.
PyPI goes first so the public README's install buttons never point at a version PyPI does not have.
The tag starts `.github/workflows/release.yml` in the public repository: it creates the GitHub release with `fl-studio-agent.mcpb` and publishes `server.json` to the MCP Registry through GitHub OIDC (the `io.github.origami-research/*` namespace is granted to that account's workflows, so no token is stored).
The script waits for that run and reads the release asset back.

Updates are fast-forward only.
The mirror is rebuilt from the full private history every time, and `git filter-repo` is deterministic: with unchanged rules the rebuild reproduces every published commit (checked 2026-10-22: rebuilding from main de37cb9 reproduced the published 735c35d as an ancestor of the new main).
The rewrite also maps commit hashes quoted in messages ("This reverts commit ...") to their rewritten hashes, so it needs every quoted commit present: build from the default source, a fresh `git clone --mirror` of the private GitHub repository, which keeps the pull-request refs those commits can live on.
A source without them (a plain local clone) produces different hashes from the first such message on.
So the script refuses when the rebuilt `main` does not descend from the published `main`, which means a rule changed published history, for example a removed or narrowed allowlist entry or a changed replacement or redaction rule.
Only add allowlist entries for paths that never existed in history; rewriting public history is a separate owner decision.

It also refuses when the verification is not PASS, a removal-set path is reachable, the tag does not match the package version, the release notes are missing, the version already exists on PyPI (without `--resume`), the tag already exists, `docs/oss/PUBLICATION-HOLD` exists, or the approval flag is missing.
If a run stops after the upload, rerun with `--resume`: it accepts a PyPI version that already has its wheel and sdist and a public `main` already equal to the mirror's `main`, and finishes the remaining steps.
If the release workflow fails, the script prints the `gh run rerun` command; `gh workflow run release.yml -f tag=vX.Y.Z` reruns a release for an existing tag.
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
- Then the script published only into an empty repository; since the one-step-install change it also updates a published repository by fast-forward (Step 7 above).
