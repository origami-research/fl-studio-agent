# Plugin/channel-state FLP RE: placement proven, mutation blocked

Status: **read-only placement evidence; no codec writer**.  This investigation
ran only against installed FL-authored artifacts.  It did not start FL, send a
dispatcher command, or create a synthetic `.flp`/`.fst` record.

## Artifact inventory

These files remain in the FL 2026.1.5 build 5384 installation and are tested
in place by `mcp-server/tests/test_plugin_state_corpus.py`; they are not copied
into this repository.

| artifact | SHA-256 | observed tuple |
| --- | --- | --- |
| `Data/System/Render tests/FL Keys.flp` | `9997e7551992483c529eac5ed89b2f0913b6e5ffc1da004debb21fff98d6d4c4` | rack `FL Keys` + wrapper 52 B + state 76 B |
| `Data/System/Render tests/Fruity Parametric EQ 2.flp` | `58bed4358c96d44b0daef5e94db0a96f81b4da643a1a219026dca297c953f316` | rack `3x Osc` + wrapper 52 B + state 92 B; mixer `Fruity Parametric EQ 2` + wrapper 52 B + state 305 B |
| `Plugins/Fruity/Generators/FL Studio Mobile/Installed/Synth Presets2/3xOsc/Bells/Bell.fst` | `03cf4006b786cf9ed67fbf550d8c28706f90391f3e3224176c9b330872cbc40fa` | `ChannelState` (`FLhd` format `0x20`), `3x Osc` + wrapper 52 B + state 84 B, display name `Bell` |

## What the bytes prove

The `flpkit.codec.Stream` walker reaches all three events without a size
override.  In the two project artifacts, every observed plugin instance is an
adjacent sequence:

```
201  PluginID.InternalName     variable text
212  wrapper                   52 bytes
213  opaque plugin state       optional, variable bytes
```

The first `236` (`InsertID.Flags`) is the boundary already used by flpkit's
channel attribution: the tuple before it is the rack generator; the tuple
after it is the mixer effect.  The Bell preset is not just a raw `213` blob:
it is an FL `ChannelState` FST containing the same sequence plus channel
events.  Therefore a future preset path must carry the whole FST/channel-state
record, retaining every unproved byte.

`213` remains opaque.  The three artifacts establish placement and extent,
not the inner parameter schema, an insert-slot allocation rule, or a safe
splice contract.  The pyflp field names were used only to orient this scan;
the assertions above read FL's bytes directly.

## Why no writer landed

These are independently authored examples, **not minimal pairs**.  Comparing
`FL Keys.flp` with either the empty template or the EQ project would mix
unbounded project changes and would invent an add/load rule.  No pair records
one user-visible action, so flpkit cannot safely add `PluginFormat`,
`add_channel`, `add_insert_effect`, or `apply_preset`.

`mcp-server/tests/e2e/test_plugin_state_minimal_pair.py` is the strict,
executable blocker.  Before any codec mutation, collect three FL-authored,
same-baseline pairs:

1. Empty project -> add `FL Keys` channel -> save.
2. `3x Osc` channel -> load `Bell.fst` -> save.
3. `3x Osc` project -> add `Fruity Parametric EQ 2` to one specified insert -> save.

For each, diff the full event stream, preserve all unproved bytes, parse the
candidate with flpkit, then use FL open/readback/re-save byte survival before
promoting a writer.
