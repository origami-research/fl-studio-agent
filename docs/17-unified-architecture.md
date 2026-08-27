# Unified architecture: best of both lineages

The project split into two working lineages that solved different halves of the problem.
This is the spec that merges them correctly - not one overwriting the other, but each contributing the layer it got right.

- **`main`** (working, shipped): the elegant permission-free **note-write** path - rewrite the `.flp` and reopen FL - plus a backend cascade, sandbox-aware tests, and an autonomous harness.
- **`spool-first-control`** (this session): the **live control plane** (server-hosted MIDI), the **representation layer** (content-addressing, compression, structured deltas), the **records/history** model, the notebook **kernel**, and the corrected native-plugin findings.

The synthesis: **main's backends are the *how notes/edits land* layer; our work is the *live control, state, and memory* layer above it.** They stack.

## The one-picture architecture

```
              model (JSON tools  +  kernel Python)
                        |
   representation layer:  schemas · views(compress) · hashing · diff(deltas)
   memory layer:          shadow score · action log (foldable delta history)
                        |
   ┌─────────────────────────── the router ───────────────────────────┐
   |  every op picks the best available backend, live-first:          |
   |                                                                   |
   |   NON-NOTE ops (transport/mixer/params/channels/step seq)         |
   |     live  ── server-hosted MIDI control plane (control_midi)      |  ~6ms, in place
   |     fallback ── flp_writer (rewrite .flp) for the file-expressible ones
   |                                                                   |
   |   NOTE ops (write/clear/read)                                     |
   |     live  ── pyscript-in-FL (needs one-time grants)               |  in place
   |     default ── flp_writer: rewrite .flp + reopen FL (no perms)    |  main's win
   |     manual ── queue; user runs the script                         |  fallback
   |     future ── driven native plugin (loads base-free; poke to apply)|  fast-path
   └───────────────────────────────────────────────────────────────────┘
                        |
                   FL Studio 2026
   read-back:  parse the saved/written .flp (PyFLP) -> feeds shadow score + provenance
```

Two invariants make it coherent:

1. **Backends are interchangeable behind one router.** The model never picks a backend; it calls `write_notes` / `set_tempo` / `set_track_volume`, and the router tries live, falls back to project-write, then manual - reporting which ran (main's proven pattern, generalized from notes to every op).
2. **The layers above the router are backend-agnostic.** Compression, hashing, deltas, shadow score, and the kernel don't care whether a change landed via MIDI, a file rewrite, or a pyscript - they describe *state and change*, not mechanism.

## What each lineage contributes (and what's dropped)

| Concern | Winner | Why |
|---|---|---|
| Note write (default) | **main** `flp_writer` (rewrite `.flp` + reopen) | Permission-free, no plugin, works today. The whole reason our note-plane saga was hard - main sidestepped it. |
| Note write (live, in place) | **main** `live.py` pyscript path | Already the "live" backend; our interim pyscript is the same idea - converge on main's. |
| Live non-note control | **ours** `control_midi` | Main only edits files; our server-hosted MIDI gives ~6ms in-place transport/mixer/param/channel control with no reopen. Genuinely additive. |
| State observation | **ours** `schemas` + `views` | Compressed, named JSON via `exclude_defaults`; main returns ad-hoc dicts. |
| Content identity + dedup | **ours** `hashing` | One order-independent, schema-salted id for every note group. |
| History / undo memory | **ours** records + `diff` deltas | Foldable, reversible `NoteDelta` log, not action-string replay. |
| Composition power tool | **ours** `kernel` | Real Python over live note objects; optional, above the router. |
| Read notes back | **main** parse-the-`.flp` | Both lineages parse the saved project; this feeds our shadow-score provenance (`source: project`). |
| Backend selection + reporting | **main** cascade | `write_notes` reporting which backend ran; generalize to all ops. |
| Sandbox-correct tests | **main** `fl_env.py` + **ours** `fake_fl.py` | Keep both - main emulates FL's *file sandbox*, ours fakes the *control protocol*. |
| Autonomous FL harness | **main** `harness/` | FL lifecycle, doctor, verify - keep. |
| Native plugin | **corrected** | See below - our finding supersedes main's "impossible", but main's default makes the plugin optional. |

**Dropped from ours:** the interim `apply_notes.py` keystroke trigger (main's live/project-write backends replace it); the driven-plugin as a *primary* path (it becomes an optional future fast-path backend).
**Dropped from main:** the `docs/11 native-plugin-blocked` conclusion (it's wrong - corrected below).

## The router (the load-bearing new piece)

A single `Backend` protocol and an ordered registry per capability:

```python
class Backend(Protocol):
    def can_apply(self, op: Op) -> bool: ...      # am I set up + able to do this op?
    def apply(self, op: Op) -> Applied: ...       # do it; raise BackendUnavailable to fall through

# ordered, live-first; the router tries each until one applies, reports which ran
NOTE_BACKENDS    = [LivePyscript(), ProjectWrite(flp_writer), Manual()]   # + DrivenPlugin() when proven
CONTROL_BACKENDS = [MidiControlPlane(control_midi), ProjectWrite(flp_writer)]
```

- `Op` is a validated `schemas` command (a `Target` + typed args), so the router is backend-agnostic and every backend receives the same normalized input.
- `Applied` carries `{backend, before_hash, after_hash, delta}` so the memory layer records a structured `NoteDelta` regardless of which backend ran.
- Reporting `which backend ran` (main's UX) becomes a field on every tool result.

This is the reconciliation's core: main's "try backends in order, report which" pattern, promoted from a hardcoded cascade in `write_notes` to a first-class router that also carries the non-note MIDI control plane and feeds the representation/memory layers.

## Live-first, file-fallback - the coherence rule

The tension between "live MIDI, in place" (ours) and "rewrite the file, reopen" (main) resolves by tier, not by choosing one:

- **Live tier** (edits in place, needs one-time setup): MIDI control plane for non-notes; pyscript for notes. Preferred when available.
- **Project-write tier** (no permissions, reopens FL): `flp_writer` for everything file-expressible. The robust default and universal fallback - this is what makes the product work with zero setup.
- **Manual tier**: queue + user runs the script. Last resort.

`fl_doctor` reports which tiers are live, and the router degrades gracefully down the tiers, always reporting the backend used.

## The native plugin, corrected

Main's `docs/11` concluded FL won't load third-party plugins on macOS, inferred from a constructor that never fired. That inference was wrong: **the plugin never loaded because it used the SDK base class / a short vtable.** This session proved a **base-free, 64-slot-padded** plugin loads cleanly and stably in FL 2026 (`status=loaded`, verified live). But we also proved a windowless plugin gets **no idle callback** to self-poll (editor pumps idle; editor crashes on load). So:

- Correct the record: FL *does* load base-free native plugins.
- The plugin can't self-poll, so it can only work **server-driven** (poke a plugin parameter -> it applies notes in the param callback).
- Because main's `project-write` already gives a permission-free note path, the plugin is an **optional future fast-path** (instant, in-place), not a dependency. Keep the base-free ABI + driven design documented; do not gate the product on it.

## Testing (keep both harnesses)

- `fl_env.py` (main): emulates FL's Python *file sandbox* - `open()` only under FL's tree, no `encoding=`, `os.remove` blocked, `SystemError` on refusal. Plus `mutation_check.py` proving the emulator catches historical bugs.
- `fake_fl.py` (ours): fakes the *control protocol* + Bridge, so the router, control plane, records, and kernel run in CI without FL.
- Together they cover both the file-write backend and the live-control backend without a real FL.

## Migration path (two branches -> one trunk)

`main` stays the trunk (it ships). Land our additive layers on top, in dependency order, each a focused PR:

1. **Representation layer** (`hashing`, `views`, `diff`, observation/delta models) - pure, additive, zero conflict. Contribute first.
2. **The router** - refactor main's `write_notes` cascade into the `Backend` protocol + registry; wire main's backends into it unchanged. Behavior-preserving.
3. **Records + action log + deltas** - the memory layer above the router, fed by `Applied.delta`.
4. **MIDI control plane** as a `CONTROL_BACKENDS` entry - adds live non-note control alongside main's file-based `set_tempo`/`set_channel_mix`.
5. **Kernel** - optional power tool, last, above everything.
6. **Docs** - correct `docs/11`, fold this spec in as the north star.

Each step is additive and independently testable against both harnesses; nothing overwrites main's working note path.

## Why this is the right final architecture

- **It keeps what works** (main's permission-free note write) and **adds what was missing** (live control, honest state, foldable history, composition) without betting the product on the unverified pieces.
- **Backends are swappable, layers are agnostic** - so the driven plugin, an IAC bus, or a future FL API can drop in as new backends without touching the model-facing surface or the memory model.
- **One normalized command, one delta history, one compressed view** - regardless of which of three (or four) mechanisms actually moved the notes. That's the invariant that makes "best of both" coherent instead of two stapled-together systems.
