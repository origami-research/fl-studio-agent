# 37 - Open-source launch decision (owner, 2026-09-05)

The owner decided, in the orchestrator chat on 2026-09-05:

1. The Bridge plugin is OPEN, together with the engine, the controller script, and the knowledge docs.
2. The license is MIT (this repository's LICENSE now carries it; the public mirror inherits it).
3. The boundary is the existing open-core plan: open the engine and the knowledge, keep the product and the infrastructure private (the arena, hosted service, billing, branded app).

What this does not yet authorize, per [oss-history-rewrite-plan.md](oss-history-rewrite-plan.md):

- The public path allowlist and the removal set still need the owner's review of the `git filter-repo --analyze` output.
- The merge-freeze window for the rewrite still needs a date.
- Redistribution of any FL SDK-derived material is NOT granted by this decision; the proprietary SDK stays out of the public tree (it is fetched per checkout and gitignored).

Next mechanical step: a dry-run extraction that produces the analysis, the proposed allowlist, and a secret scan for the owner to approve, without publishing anything.
