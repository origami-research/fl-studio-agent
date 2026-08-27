# Representation architecture

How FL state, note groups, and edits are modeled, compressed, and diffed for the model.
Implemented on branch `spool-first-control`; 14 tests in `mcp-server/tests/test_representation.py`.

## The shape in one picture

```
                 pydantic models (schemas.py)          <- the ONE definition
                 full, lossless, validated
                        |
        +---------------+----------------+-------------------+
        |               |                |                   |
   hashing.py        views.py          diff.py          model_dump_json
   content id      compression        structured         (durable commands,
   (fingerprint)   for the model      edits (deltas)      spool, disk)
        |               |                |
   "same note          "only what       "what changed,
    everywhere"         differs from      not a replay
                        resting"          of actions"
```

One rule governs the split: **pydantic models are the single source of truth; every other form is a projection of them at a boundary.** The model authors typed objects (JSON tool calls or kernel Python both validate into the same classes); JSON appears only as the encoding at three edges (MCP in, spool out, disk), and the *view* the model reads is a compressed projection - never the raw dump.

## Layer 1 - the models (`schemas.py`)

Observations (`Channel`, `MixerTrack`, `Effect`, `Send`, `SessionState`) and edits (`NoteDelta`, `NoteMove`) join the existing `Note`, `NoteSetRef`, `ActionEntry`.

The load-bearing design choice: **every default is the resting / no-op value** - `volume_db=0.0` (unity), `pan=0.0` (center), `muted=False`, `plugin=None`, empty effect/send lists. This is what makes compression free downstream: a channel nobody touched has every field at default, so it projects to almost nothing.

## Layer 2 - content-addressing (`hashing.py`)

FL has no note identity, so a note *is* its value. `note_hash(note)` fingerprints a note's `canonical()` form; `noteset_hash(notes)` fingerprints a group order-independently (hash of sorted member hashes). Both are salted with `SCHEMA_VERSION`, so a schema bump changes every id by construction and stale caches invalidate safely.

This one module is the single basis for: the shadow score's keys, dedup (identical rewrites collide), the action log's `before/after` hashes, and diffing. A note has exactly one id everywhere.

## Layer 3 - compression (`views.py`)

`render(model)` = `model.model_dump(exclude_defaults=True, exclude_none=True)`. A resting channel becomes `{"index":1,"name":"808 Kick"}`; a 17-track mixer where 15 tracks are default collapses to a few hundred bytes, and the model's attention lands on what's actually set.

- Nested lists compact recursively (each `Channel` in `SessionState.channels` drops its own defaults).
- `note_view(notes)` returns the full compact list when small (`LAZY_THRESHOLD=50`) and a `NoteSetSummary` when large - `{"count":5000,"low":"C2","high":"C6","density":8.0,"id":...}` - so a huge pattern never floods context; the model filters, then pulls the slice it wants.

Discipline: `exclude_defaults` is for **views** (describing state; default-valued state is uninteresting). It is NOT used for **durable commands**, where "explicitly set to 0" must differ from "unset" - those dump in full via `model_dump_json`.

## Layer 4 - structured edits (`diff.py`)

Edits are not a replay of imperative action strings. Every change - the model's writes and the user's hand-edits alike - is a content-addressed `NoteDelta`.

`diff_notes(old, new)`:
1. exact-hash pass - notes in both are unchanged, ignored;
2. leftovers split into `removed` (old-only hashes) and `added` (new-only);
3. greedy nearest pairing turns close removed/added pairs into `moved`, with a `confidence` that falls off with distance - a dragged note stays the *same* note to the model; a low-confidence pair is flagged, not asserted.

Deltas are:
- **compact** - carry only the change (three moved notes, not the whole new state);
- **foldable** - `fold(bootstrap, deltas)` reconstructs any point; the append-only action log *is* this fold log, not a script;
- **reversible** - `invert(delta, base)` gives the undo delta;
- **order-robust** - hash-keyed, so independent edits commute (git-style merge, not command replay).

This is also how "the user changed something" is surfaced: `get_notes` returns a delta between the shadow score and the freshly-parsed project, so the model sees *what changed*, not a diary of the user's clicks. The user's and the model's edits are the same object type, diffable against each other.

## What the model receives, end to end

```
get_session_context  -> views.render_session(state)     # compact named JSON
get_notes (small)    -> views.note_view(notes)          # compact note list
get_notes (large)    -> NoteSetSummary                  # census; filter then pull
get_notes (post-edit)-> {"notes": ..., "source": "project",
                         "delta": NoteDelta, ...}        # what the user changed
write_notes(...)      -> validated into Note[] -> spool  # full dump, no exclusion
```

## Why this is the right architecture

- **One definition, many projections** - no drift between what's validated, stored, hashed, and shown.
- **Compression is a property of good defaults**, not a special codec - choosing resting values as defaults makes `exclude_defaults` do the work.
- **Identity by content** - the only honest model when the host has no note ids; it makes dedup, caching, and diffing fall out of one hash function.
- **Edits as deltas** - compact, reversible, foldable, and mergeable; the same shape for model writes and user edits, which is what makes drift cheap to detect and show.

Next integration step (wave-2 records store): key the shadow score by `noteset_hash`, emit `NoteDelta` on every write into `actions.jsonl`, and back `get_notes`' provenance with `diff_notes` against a fresh parse.
