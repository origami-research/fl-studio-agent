# Mixer routing FLP RE: corpus map, writer blocked

Status: **no writer**. The map below is enough to target a controlled minimal-pair experiment, but not enough to safely write a project. This worker has no capturable desktop (`screencapture` returns `could not create image from display`), so it cannot make and inspect the required FL-authored one-change saves. Do not turn these observations into a writer until the pair and live acceptance ladder pass.

## What the FL 2026 corpus establishes

The complete installed `FL/Data/**/*.flp` corpus walks cleanly with flpkit's current event skeleton. The mixer section consists of repeated per-insert events followed by one project-wide parameter event:

| Meaning | Event id | Payload | Evidence / interpretation |
| --- | ---: | --- | --- |
| Insert routing bitmap | 235 (`0xEB`) | variable bytes, one byte per possible destination | Every nonzero byte marks an active destination. `Empty.flp` has 16 vectors with only index 0 set; `Empty with 4 sends.flp` has source inserts 0-15 with indices 0, 17, 18, 19, 20 set. |
| Insert flags | 236 (`0xEC`) | variable, normally 12 bytes | Begins the mixer section for flpkit channel attribution. Opaque for routing/level work. |
| Mixer parameters | 225 (`0xE1`) | one variable event containing 12-byte records | Present once per project. Every `System/Empty.flp` record is exactly 12 bytes. |

The observed parameter-record layout is:

```
0..3   opaque (zero in the stock Empty template)
4      parameter id
5      opaque (normally 31 for track-owned values)
6..7   little-endian channel_data
8..11  little-endian signed i32 value
```

`channel_data` packs slot in bits 0-5 and a mixer-storage index in bits 6-12. The storage index is offset by 64: the first visible insert in `System/Empty.flp` has `channel_data=0x7000`, so `(channel_data >> 6) & 0x7f` is 64. This is a storage convention, not yet a public insert-index contract.

Candidate IDs, corroborated by the installed PyFLP source but **not promoted to writer facts**, are:

| Parameter id | Candidate | Corpus value in `Empty.flp` |
| ---: | --- | ---: |
| 192 (`0xC0`) | insert post-fader volume | 12,800 |
| 193 (`0xC1`) | insert pan | 0 |
| 64..191 | route/send level | absent when all routes have their default level |

The routing bitmap (235) says *whether* a source routes to a destination; a 225 record in the 64..191 range is expected to say *at what level*. The ordering/linkage between an individual 235 byte, the parameter ID, and an FL public insert number remains unproven.

## Required minimal pairs (next live window)

Use a copied `System/Empty.flp`, save a baseline, then make exactly one change and save a second copy. Do not change selection, names, or plugins between saves.

1. Set visible insert 1 volume from unity to 0.73; diff baseline / changed. This must isolate one id-192 record's `msg` i32 and establishes scale and storage-index mapping.
2. Set the same insert pan to a non-round asymmetric value; diff. This proves id 193 and its scale/sign.
3. Disable its default route to Master; diff. This identifies the exact byte in its 235 routing vector and confirms destination 0 is Master.
4. Enable one route from insert 1 to a non-send insert and set its level to a non-default value; diff. This ties bitmap index, route-level id, and `msg` scale together.

One narrow writer may proceed after its own pair and live acceptance ladder pass; the other three pairs remain prerequisites for their respective fields. For the first candidate, that means the volume pair must prove the public insert-1 to storage-index mapping, id 192, and its value scale. Its `flpkit/formats/mixer.py` implementation must locate that selected 225 record, carry its opaque bytes, patch only the proved i32 field, then verify via an independent `flpkit.read()`. The FL MCP seam must independently reparse again before reload/receipt, as automation does. FL must then open and re-save the patched file without changing the target field.

## Reproducible experiment

`experiments/e9_mixer_minimal_pair.py` is intentionally an annotated, non-writer live-test scaffold. It writes two copies only after each control API call has read back successfully, restores the original control value in a `finally`, and exits nonzero when a pair cannot be captured. Its output files are the evidence to attach before implementing a codec format.

`mcp-server/tests/e2e/test_mixer_minimal_pair.py` is the deliberately xfailed
promotion gate. It owns the required `evidence/mixer-volume/baseline.flp` and
`changed.flp` artifacts, and refuses byte-identical saves. Remove the xfail
only when that pair is checked in, decoded, and pinned as reproducible
evidence; an XPASS means the codec and live-FL acceptance test must replace
this placeholder in the same change. The provenance suite also rejects a
public `MixerFormat` or `write_mixer_routing` seam while `mixer.route` remains
planned, so a codec cannot silently imply coverage. The artifact gate uses
flpkit's event walker to require exactly one changed 225 record, id 192, with
the first eight identity bytes preserved and only its mapped i32 value changed;
it rejects any other byte difference in the pair.
