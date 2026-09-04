# Plugin-add minimal pair (FL-authored, one action)

Captured 2026-09-01 by a single human GUI gesture (NOT computer-use in the product):
add exactly one effect - **Fruity Parametric EQ 2** - to the **Master** insert, then save.

- `baseline.flp` - before (no added effect).
- `after-add-parametric-eq2-on-master.flp` - after (Master + Fruity Parametric EQ 2).

Reliable byte-level facts (a hand-rolled event parser desynced - use flpkit's decoder):
- size delta: **+519 bytes**; first difference at offset 18 (FLhd size field); a change at offset ~201.

This is the deterministic reference the file-RE add-effect writer is built from. The shipped
capability is programmatic flpkit (docs/33) - the pair is one-time evidence, never a runtime path.
