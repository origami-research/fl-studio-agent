# 08 - Roadmap, Spikes, and Open Questions

Ordering principle: **retire the riskiest unknown first.** The entire project stands on
one unverified behavior (cross-channel `FHD_AddNotesToPR` from an effect). If Spike 1
fails, we redesign before writing anything else; everything after it is known
engineering.

## Spike 1.5 - dispatcher probe (runs before or parallel with Spike 1)

Bridge-plugin mode that sweeps undocumented host-dispatcher message IDs 0-255 on a
scratch project, hunting for note/selection **getters**. Full spec in
[11-interface-spec.md](11-interface-spec.md) §6.1. Best case: live readback exists and
the shadow-score machinery shrinks to a cache. Worst case: a few days and certainty.

- [ ] Sweep 0-255, classify: clean error / silence / crash (contained, scratch project)
- [ ] Validate candidates: write known chord via `FHD_AddNotesToPR`, check for matching returns
- [ ] Sweep specifically around known selection-related IDs for selection queries
- [ ] Written verdict recorded in 11 §6.1

**Exit criteria:** written getter-exists / getter-does-not-exist verdict.

## Spike 1 - the load-bearing unknown (native plugin)

Minimal effect plugin, no spool, no protocol: hardcoded to write a C-major chord via
`FHD_AddNotesToPR` a few seconds after load (from Idle). Then a parameter sweep,
recording results into 03/04:

- [ ] Notes appear in the piano roll of the **selected** channel with `ChanNum=-1`?
- [ ] **Explicit `ChanNum=k`** targets channel k? (Decides cross-plane orchestration.)
- [ ] **Explicit `PatNum=k`** targets non-current patterns? Patterns never opened in
      the piano roll? What does `-1` mean with the piano roll closed?
- [ ] `NPF_EmptyFirst` clears whole pattern vs just the target channel?
- [ ] `Count=0 + NPF_EmptyFirst` = legal pure-clear?
- [ ] `Vol`/`Pan`/`Pitch` ranges & scaling (header defaults imply Vol 0..128-ish;
      verify empirically, incl. `Color` mapping to note color/MIDI channel)
- [ ] Write during playback: safe? audible glitch? notes land correctly?
- [ ] Write with piano roll window open on the target pattern: instant visual update?
- [ ] Undo: does one write = one undo step? (`general.saveUndo` interaction)
- [ ] 1,000-note write in one call: latency, stability. (Sets server chunk size.)
- [ ] Effect registered but placed in Generators folder / vice versa: behavior deltas
- [ ] macOS + Windows parity of all of the above
- [ ] Exact install paths per platform/FL version; unsigned dev-build loading on macOS

**Exit criteria:** chord lands on an arbitrary channel+pattern on both platforms, with
a written decision on targeting strategy (explicit vs select-then-write).

## Spike 2 - spool loop (still no MCP)

Bridge plugin + spool directory + `add_notes`/`ping` ops; driven by `echo` and shell
scripts.

- [ ] Idle-driven polling with editor closed (`FHD_WantIdle=2`) works; measure
      effective poll frequency
- [ ] Atomic rename discipline on macOS + Windows (no partial reads under load)
- [ ] Crash containment: malformed JSON, unknown op, oversized command → error
      responses, FL stays alive
- [ ] Idempotent replay of duplicate ids

## Spike 3 - control plane echo (controller script)

`device_FLMCP.py` + a 50-line host-side probe script (no MCP yet).

- [ ] Virtual port creation on macOS (rtmidi); loopMIDI flow on Windows
- [ ] `OnSysEx` fires for our frames; `device.midiOutSysex` (or actual send function)
      returns frames to the probe
- [ ] Embedded stdlib reality check: `json`, `base64` importable? (else: 7-in-8 packer)
- [ ] `hello` payload complete: FL version, PPQ (`general.getRecPPQ` naming), tempo
- [ ] Dispatch: `transport.start/stop`, `mixer.setTrackVolume`, `channels.selectOneChannel`,
      `plugins.getParamValue/setParamValue` round trips
- [ ] Tempo set via `processRECEvent(REC_Tempo, …)` - stable?
- [ ] Ctrl+S emulation for save (`sync_from_project` dependency)
- [ ] Throughput/latency: ops per second sustained without FL UI jank; frame-size cap
      empirically checked
- [ ] Normalize every function name used in 05's table against the API Stubs

## Milestones

**M1 - vertical slice.** MCP server with exactly four tools: `fl_doctor`,
`write_notes`, `play`, `stop`. A model writes a beat you can watch land. This is the
demo that proves the product.

**M2 - control-plane breadth.** Full tool catalog from 06 except render/sync. Shadow
score with persistence. Composition helper tools.

**M3 - template + sound design.** Template `.flp` + manifest + name resolution +
`list/set_plugin_param` with per-plugin param-name caching. `load_sample`.

**M4 - ground truth + render.** `sync_from_project` (save → PyFLP → shadow rebuild),
`generate_manifest`, `render_project` via `FL.exe /R` on saved copies.

**M5 - packaging.** Installers (plugin binaries, script, template, port setup),
macOS signing/notarization, first-run walkthrough, `fl_doctor` polish. Public release.

**Later / explicitly deferred:** MIDI-direct plugin transport (replace spool if
latency ever matters), Edison/audio tooling, playlist arrangement tools, multi-project
sessions, the private-product offline pipeline (PyFLP compose + CLI render at scale).

## Open-questions ledger

Every [ASSUMED]/[REPORTED] item in these docs, in one place. Each gets resolved by a
spike and its doc updated - this table is the checklist.

| # | Question | Doc | Spike |
|---|---|---|---|
| 1 | Explicit `ChanNum` works from effect-registered plugin | 04 | 1 |
| 2 | Explicit `PatNum` / closed-piano-roll semantics | 04 | 1 |
| 3 | `NPF_EmptyFirst` scope (pattern vs channel) | 03/04 | 1 |
| 4 | `Count=0` clear legality | 03 | 1 |
| 5 | `Vol/Pan/Pitch/Color` runtime ranges | 03 | 1 |
| 6 | Write-during-playback safety | 02/04 | 1 |
| 7 | Native plugin install paths per platform/version | 04 | 1 |
| 8 | SDK header redistribution license | 01/04 | pre-M5 legal check |
| 9 | Idle frequency with editor closed | 02/04 | 2 |
| 10 | Embedded Python stdlib (`json`, `base64`) | 03/05 | 3 |
| 11 | `getRecPPQ`/`getCurrentTempo`/SysEx-send exact names | 05 | 3 |
| 12 | `processRECEvent` tempo-set stability | 05 | 3 |
| 13 | Ctrl+S emulation for save | 05/06 | 3 |
| 14 | SysEx frame-size cap across drivers | 03 | 3 |
| 15 | FL version floor (piano roll scripting isn't needed - what's our true min?) | 00 | 1–3 |

## Testing philosophy

- Spikes produce **recorded findings** (updates to these docs) as their primary
  artifact; code is secondary.
- From M1 on: protocol-level integration tests run against a **fake FL** (a test
  harness implementing both protocol endpoints), so CI needs no FL license. A small
  manual smoke checklist covers real-FL behavior per release.
- The Bridge plugin gets a standalone host shim for unit-testing command execution
  logic (dispatcher mocked) - the only untestable-in-CI line is the dispatcher call
  itself.

## Open questions added 2026-08-24 (from spec 11 workshop)

| # | Question | Doc | Spike |
|---|---|---|---|
| 16 | One `FHD_AddNotesToPR` call = one undo step? | 11 §5.1 | 1 |
| 17 | Undo-stack introspection can attribute whose step is on top? | 11 §5.1 | 3 |
| 18 | `OnDirtyChannel`/`OnRefresh` coverage of pattern/note edits | 11 freshness | 3 |
| 19 | FL autosave file locations + format stability for background parse | 11 freshness | 3 |
| 20 | `score.selection` availability in pyscript for note-level selection | 11 §9 | 3 |
| 21 | Filesystem access from embedded interpreter (spool control plane) | 11 addendum, issue #8 | 3 |

Sequencing note: docs/11 §10 is the authoritative stage order and extends the
milestones below with the pydantic core (M1), records/action log (M2), notebook
kernel + bootstrap (M3), diff-matching references (M4).
