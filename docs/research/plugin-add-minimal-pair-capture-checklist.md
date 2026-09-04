# Plugin-add generalization capture checklist

## Status

This is a capture protocol, not evidence that a writer, splice, or live plugin
API works.  The checked-in pair proves only one FL-authored stock-effect action:
Fruity Parametric EQ 2 added to Master.  It is insufficient to generalize over
plugin kind, identity, or preset state.

Capture every pair in a disposable project with FL Studio itself.  No script,
hex editor, or file writer may create either member of a pair.

## Invariant for every pair

1. Start a new disposable project and make all setup choices before saving
   `baseline.flp`: target channel/insert/slot, routing, and the source plugin
   where applicable.  The target must be empty when the requested action is an
   add.
2. Save `baseline.flp` from FL.  Record the FL version/build, OS, architecture,
   project template, target location, and exact plugin/preset identity.  For a
   VST, also record format (VST2/VST3), vendor, version, and its FL plugin-database
   entry or binary hash.
3. Perform exactly the one action named below.  Use the picker only as needed
   to select that action's named plugin/preset; do not rename, move, route,
   tweak, or make any other project change before the after-save.
4. Save from FL as `after.flp`.  Keep both files and SHA-256 hashes under
   `mcp-server/tests/evidence/plugin-generalization/<pair>/` with this metadata.
5. Read back `after.flp` in FL: reopen it and confirm the visible identity and
   target listed for that pair.  Save that reopened project as `resaved.flp`;
   retain it and record whether the relevant identity/state survives.  Do not
   demand byte equality unless a comparison proves it--FL may rewrite unrelated
   serialization.
6. Decode and diff the *complete* FLP event streams for baseline, after, and
   resave with flpkit.  Inventory every changed, inserted, and deleted event.
   Unknown bytes remain opaque evidence; they are not permission to patch them.
   The resave comparison must show that the target plugin identity and carried
   state survive FL's parse/save cycle.

An installed passive dispatcher observer may retain its entry/return trace with
the FL build and gesture, but no trace is required to make a file-format pair
and an observed dispatcher id is never callable evidence.

## The four captures

| Pair | Baseline state | One FL action | Required FL readback |
|---|---|---|---|
| **E2 - second native effect** | Empty/default project; Master insert selected; Master slot 1 empty | Add **Fruity Reeverb 2** at Master slot 1, leaving its default state untouched | Master slot 1 visibly names Fruity Reeverb 2 after reopen; its slot remains Master/1 after resave |
| **I1 - native instrument** | Empty/default project; no extra channel added after baseline save | Add **FL Keys** as one channel, leaving its default state untouched | Channel rack visibly contains one FL Keys channel after reopen; its channel identity/state survives resave |
| **V1 - third-party VST** | Empty/default project; no extra channel added after baseline save | Add one preselected, installed third-party VST once, leaving its default state untouched | Channel rack visibly contains that recorded VST identity after reopen; FL can load it and its carried state survives resave |
| **P1 - preset load** | A single existing **3x Osc** channel in default state; record the exact source channel identity before baseline save | Load the shipped **Bell.fst** preset through FL's preset UI once | The same channel still identifies as 3x Osc and the captured FL UI shows the loaded Bell state after reopen; that state survives resave |

`E2` intentionally changes plugin identity while holding the existing effect
case's class (stock native effect) constant.  `I1` changes from an insert-slot
tuple to a generator-channel tuple.  `V1` tests the host-reference risk without
claiming third-party support: an unavailable VST is a recorded blocked capture,
not a substitute native plugin.  `P1` tests a channel-state change, not a raw
`213` payload; the existing corpus shows that an FST carries the complete
channel/plugin tuple.

## Promotion bar

Do not add or widen `add_insert_effect`, `add_channel`, or `apply_preset` from
these files alone.  Promotion needs the four retained pairs, their complete
event inventories, successful FL reopen/resave survival, and a writer that
preserves every unexplained field verbatim.  A VST writer additionally needs
V1's recorded plugin identity to survive its own output; a failure is the
third-party no-go verdict, not a fallback to a guessed wrapper format.

Related evidence: the existing E1 files in
`mcp-server/tests/evidence/plugin-effect/`, the executable blocker
`mcp-server/tests/e2e/test_plugin_state_minimal_pair.py`, and the corpus
findings in [plugin-channel-state-flp-re-2026-08-31.md](plugin-channel-state-flp-re-2026-08-31.md).
