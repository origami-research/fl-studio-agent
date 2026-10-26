# OSS launch checklist

Use this only after the owner gives written approval. The dry run did not make
any public change.

## flpkit on PyPI

flpkit 0.8.1 was published on 2026-09-06; the engine now depends on the released package `flpkit==0.9.0` (published 2026-10-26) with no git source. The release smoke check can run unskipped.

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
The tag starts `.github/workflows/release.yml` in the public repository: it builds the Bridge for macOS (universal) and Windows x64 through `.github/workflows/native.yml`, creates the GitHub release with `fl-studio-agent.mcpb`, both Bridge libraries, `SHA256SUMS` and `BRIDGE-NOTICE.txt` (`fl-mcp-install` downloads the Bridge from it), and publishes `server.json` to the MCP Registry through GitHub OIDC (the `io.github.origami-research/*` namespace is granted to that account's workflows, so no token is stored).
The script waits for that run, downloads the release, and checks every asset against `SHA256SUMS`.

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

## Owner decision of 2026-10-22: ship the Bridge prebuilt

- The owner: "just ship the bridge, do your best but it's ok, don't make it private and DIY".
- The owner accepts the uncertainty: the FL plugin SDK carries no redistribution grant and Image-Line states no terms.
  The Bridge compiles against the SDK headers for structure layouts and links no SDK code (`native/CMakeLists.txt`).
- Each release therefore carries the macOS (universal) and Windows x64 Bridge, `SHA256SUMS`, and a best-effort attribution notice (`native/bridge/NOTICE.txt`); `fl-mcp-install` installs the matching one.
- No signing identity exists, so the libraries are unsigned; SETUP.md says what users see.

## Published 2026-10-23

- The co-author (ky11001) consented to MIT; the owner relayed it and gave the publish go.
- `scripts/oss/publish-public-mirror.sh` ran from merged main 14887fb: verification PASS in the same invocation (13 checks, 0 skipped), then it pushed `main` (735c35d) and tag `v0.1.0` to https://github.com/origami-research/fl-studio-agent and uploaded `fl-studio-agent` 0.1.0 to https://pypi.org/project/fl-studio-agent/0.1.0/.
- Read-back: the public default branch is `main` with `main` and `v0.1.0` at 735c35d; a clean-venv `pip install fl-studio-agent==0.1.0` installs the `fl-mcp*` commands and `fl-mcp-contract` runs.
- Then the script published only into an empty repository; since the one-step-install change it also updates a published repository by fast-forward (Step 7 above).

## Released 0.1.1 on 2026-10-22

- First release through the update path (#265, #266).
- The mirror was rebuilt from merged main ef6d30e and reproduced the published `735c35d`.
- `scripts/oss/publish-public-mirror.sh` verified it in the same run: Status PASS, including the full suite, the native build with ctest, and the release artifact.
- It then uploaded `fl-studio-agent` 0.1.1 to PyPI and read it back, fast-forwarded the public `main` from 735c35d to f8c62a4 (4 commits), and pushed tag `v0.1.1`.
- The tag's release workflow created https://github.com/origami-research/fl-studio-agent/releases/tag/v0.1.1 with `fl-studio-agent.mcpb`.
  It also published `io.github.origami-research/fl-studio-agent` 0.1.1 to the MCP Registry through GitHub OIDC; the registry lists it as active.
- User-view read-back:
  - From a clean uv cache, `uvx --python 3.12 fl-studio-agent --version` prints 0.1.1, and the same command answers MCP `initialize` with the five tools.
  - `claude mcp add --scope local fl-studio -- uvx --python 3.12 fl-studio-agent` shows Connected in `claude mcp list`.
  - The published Cursor link decodes to `{"command":"uvx","args":["--python","3.12","fl-studio-agent"]}`, and the VS Code link redirects to `vscode:mcp/install` with the same entry.
  - The released bundle, run as `uv run --directory <bundle> src/server.py`, serves 0.1.1.
  - The README renders on GitHub with the five badges and the five client sections.
- Not verified: installing the bundle in the Claude Desktop app (no Claude Desktop on the release machine), and the Windows install path on a real Windows FL.

## Released 0.1.2 on 2026-10-22: the prebuilt Bridge

- The release shipped from merged main 4ef3e52 (#268, #270). The rebuilt mirror's main descended from the published f8c62a4.
- The publish script's verification passed in the same run. It uploaded 0.1.2 to PyPI, then stopped at the clean-venv read-back: the JSON API already showed 0.1.2, but uv's index did not yet.
  `--resume` finished the release: PyPI read-back, `main` fast-forwarded to 3556e69, tag `v0.1.2`, and the release workflow passed.
  The script now retries the install until the index catches up.
- https://github.com/origami-research/fl-studio-agent/releases/tag/v0.1.2 carries `fl-mcp-bridge-macos-universal.dylib` (Mach-O universal: x86_64, arm64), `fl-mcp-bridge-windows-x64.dll` (PE32+ x86-64 DLL), `fl-studio-agent.mcpb`, `SHA256SUMS` and `BRIDGE-NOTICE.txt`; every asset matches `SHA256SUMS`.
- The MCP Registry lists 0.1.2 as active and latest.
- User-view read-back: from a clean uv cache, `uvx --python 3.12 --from fl-studio-agent==0.1.2 fl-mcp-install --help` works.
  Against a mock FL (temporary HOME, `FL_APP` pointing at a mock app bundle), `fl-mcp-install` downloaded the macOS Bridge from v0.1.2. The installed library's SHA-256 (c4d4f50f...) equals the release's `SHA256SUMS`.
  A rerun reported `already current`, and `fl-mcp-doctor` reported `bridge.installed: true` with source `fl-studio-agent v0.1.2 release` and the same SHA-256.
  During a network outage the rerun reported a WARN with the build-yourself path, not a traceback.
- Not verified: loading the prebuilt Bridge in a real FL on macOS or Windows from this release, and the Windows download and administrator-terminal path.
