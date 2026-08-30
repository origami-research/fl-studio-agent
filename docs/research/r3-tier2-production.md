# R3 - The Tier-2 production layer: automation write, mixer, instruments, samples

Research plan, 2026-08-30.
Scope: the production capabilities docs/24 marks NONE at Tier 2 - automation WRITE, mixer routing and effect chains, instrument/preset loading - plus the T3 audio-clip item.
The read-side probe is done (native/probe/PROBE_VERDICT_2026-08-30.md); this plan is the write-side counterpart: which host calls or file structures MUTATE beyond notes, the decisive experiment for each, the API each produces, and an honest feasibility verdict per item.

This is a plan, not an implementation.
Every capability claim below carries its evidence citation; anything without one is marked as a hypothesis.

## 0. Method: two mechanisms, one live window, and what "decisive" means

Every Tier-2 item resolves onto one or both of two mechanisms:

- **File RE** (Surface 5): the docs/23 playbook - corpus ground truth, minimal-pair byte diff, full-stream consistency, template-don't-invent, and the acceptance ladder where FL's own re-save round-trip is the strongest rung.
  The file lane always honors an arbitrary target but costs an FL reopen (E6 refuted concurrent edits: FL's save overwrites them - docs/25-fl-capability-catalog.md Surface 5).
- **Live host call or scripting call** (Surfaces 1 and 3): no reload, but only where a call exists.
  The read-side probe already enumerated Surface 1 - the dispatcher is exactly 110 ids wide (PROBE_FINDINGS "110 ids wide"), so there is no undiscovered id space to hope for; the write-side question is what the *known* mutating ids actually do live.

Three standing facts bound the whole plan:

1. **Live plugin instantiation has no reachable API.** `add_effect`/`add_channel` are `ilmcp`-bound and the native `ilmcp` module registers only in FL's own Gopher session, not our controller interpreter (REFUTED live, docs/12 §4).
   On Surface 1, the only plugin-loading ids are modal UI (`FHD_ShowPlugSelector` 15, `FHD_ShowPicker` 57), and a modal freezes `OnIdle` and every plugin tick (catalog Surface 1).
   So anything that creates a plugin instance is file-lane or template-carried, full stop.
2. **Plugin state blobs are opaque to EDIT but not to CARRY.** Decoding a vendor preset's guts is REFUTED-forever (catalog Surface 5).
   But the playbook's template rule (docs/23 rule 9) does not require understanding bytes to copy them verbatim - which is exactly what "add a stock effect with default state" and "load this preset file" need.
   This asymmetry is the load-bearing insight for items 2 and 3.
3. **Mutating host calls exist and are already named.** FL's dispatcher has exactly three cases that write the undo singleton - 17 (`FHD_AddNotesToPR`, proven live), 42 (`FHD_LoadAudioClip`), 91 (undecoded) - plus 77 (`FHD_SetContextInfo`), which accepts `CI_TrackName/Color/Volume/Pan/MuteSolo` for mixer tracks, and 54 (`FHD_FloatAutomation`), one of only three cases reaching FL's pattern accessors, as a writer (PROBE_FINDINGS; catalog Surface 1).
   These five ids ARE the write-side probe's candidate list; nothing else in 0..109 mutates project state from a plugin.

**The one live window.** Mirroring the read probe's efficiency ("one live window, many answers"), all live questions below consolidate into a single supervised window with the same harness invariants: offline-buildable and ctest-gated before any insert, the static crash ledger (ids 94/99 skipped by construction), session-token-stamped commands so stale files can never auto-run, checkpoint-before-every-call with quarantine-on-crash, and evidence as timestamped jsonl (native/probe/RUNBOOK_PROBE.md).
Where the write-probe lives: `native/probe/` is scheduled for deletion (#80), so the write ops go into the Bridge's existing executor as temporary `wp_*` ops behind the same spool/UDP command path - the Bridge already has command dispatch, checkpoints, and the dispatch breadcrumb log, so this is tens of lines, not a second plugin.
Before that, promote the static safety ledger out of the probe: move `probe_safety.h` to `native/abi/fl_dispatch_ledger.h`, because the Bridge must never call 94/99 either (RUNBOOK_PROBE §5) and the ledger must survive the probe's deletion.
The `wp_*` ops are themselves throwaways: delete them in the PR that records their verdicts, same as the probe.

---

## 1. Automation WRITE (docs/24 care rank 1)

### What we have and what is opaque

flpkit already reads automation at corpus scale: event 234 on a type-5 channel, 17-byte header, point count u32 at offset 17, then 24-byte points - `x_delta` f64 in beats from the previous point, `value` f64 0..1, `tension` f32, 4-byte tension-linked tail (flpkit `_decode_automation`; 9,676 points corpus-verified point-identical to pyflp, docs/21 A15).
Two things are opaque:

- **The trailer**: 112 bytes after the points in 1,098 of 1,100 corpus blobs, 136 in 2 (flpkit layout notes).
  Reads pass through it untouched; a writer must know whether it references the points (count, last position) or is independent state.
- **The target link**: which knob the curve controls.
  FL names automation channels after their target ("Volume - Master"), so reads get the target for free, but the actual binding bytes are undecoded (catalog Surface 5 unverified list; docs/21 A15).

### Path: file RE first; a live lane exists but is second

File RE is primary because the reader is 90% done and the acceptance ladder is fully mechanized for this surface.
`FHD_FloatAutomation` (id 54) is the live-lane candidate - a decoded writer into FL's pattern accessors - but its payload shape is unknown and it can wait for the consolidated write-probe window (§5).

### Decisive experiments (docs/23 minimal pairs, all FL-authored saves)

| Pair | The one-change save | What the diff decides |
|---|---|---|
| MP-A1 | Drag ONE existing point's value | The point encoding round-trips as decoded, and whether the trailer changes when points do. If the trailer is untouched, a point-patching writer can carry it verbatim (template rule) and never decode it. |
| MP-A2 | Add ONE point to an existing clip | Whether count+24 bytes is the whole delta, or the trailer/header references point count or extent. |
| MP-A3 | Two saves, identical one-point clip, linked to two different targets (channel volume vs a plugin parameter) | Localizes the target-link bytes, wherever they live - the channel's own event block, a separate link event, or the trailer. Run one pair for an internal target (Master volume) and one for a plugin parameter, because the two may encode differently. |
| MP-A4 | Create a new automation clip on an empty project (smallest possible pair) | The complete minimal record set a new clip needs - the template for clip creation. |

pyflp's structs are the starting field-name map for the link bytes, treated as hypothesis per playbook rule 4 (it has been wrong three times).

### Acceptance (the ladder, docs/23 rule 8)

(a) FL opens the written file; (b) behavior changes exactly as predicted - render the project headless (`OsxFL -R`) before and after and confirm the automated parameter audibly/measurably moved, or read the moved value over the live wire; (c) FL's own Cmd+S re-save round-trips the written blob byte-identically.

### API produced

flpkit (format-level, with its own corpus tests):

```python
def write_automation(
    path: Path,
    channel: int,
    points: Sequence[AutomationPointLike],  # position (beats, absolute), value 0..1, tension
    *,
    mode: Literal["replace"] = "replace",
) -> int:  # points written, patch-and-verify like set_channel_levels
```

v1 scope, correct by construction: `write_automation` patches points inside an EXISTING FL-authored automation channel (kind 5, blob present), converting absolute positions back to `x_delta`, carrying header and trailer verbatim.
A channel that is not kind 5 or has no blob is an error, not an invitation to fabricate (playbook rule 9).
v2, gated on MP-A3/MP-A4 decoding the link: `create_automation_clip(path, target=...)` for new curves on new targets.

fl_mcp seam (`flp.py` re-export) and session method:

```python
def set_automation(self, channel: int, points: list[AutomationPoint]) -> AutomationReceipt:
    # file lane: store content-addressed write, reload contract, READBACK verify via flpkit (never echo)
```

`AutomationPoint`/`AutomationReceipt` join `schemas.py`; the session method lands wherever #80's session split puts the file-lane writers.

### Tests

- flpkit: MP-derived golden byte tests; re-encode every corpus blob and require byte identity (the writer proves the reader).
- This repo: corpus differential stays; `tests/e2e/` tier-2 gains the re-save round-trip gate for one written clip.

### What gets deleted

Landing automation write completes flpkit's coverage of everything pyflp was the oracle for; retire the pyflp differential dependency in the same arc (docs/25 §3 item 8 already schedules this).

### Confidence / risk

**High** for editing existing curves: the point format is corpus-proven and the trailer is carryable even if never decoded.
**Medium** for creating new clips/links: MP-A3 could reveal the link is tangled with FL's internal controller registry; the honest fallback is v1 plus template projects that pre-carry linked (empty) automation clips for the common targets.

---

## 2. Mixer routing + effect chains (care rank 2)

This item splits into two capabilities with opposite verdicts, and conflating them is how it stayed "the biggest production gap".

### 2a. Routing and mixer levels: probably ZERO new RE - verify, then wire it

Surface 3 documents mixer WRITES we have never exercised: `mixer.setRouteTo`, `setTrackVolume`, `setTrackPan`, `muteTrack`, `soloTrack`, `setTrackName` (docs/05-controller-script.md module table).
The controller script already whitelists the `mixer` module for passthrough, and the live-verified MCPTools public set includes `set_mixer_routing`, `route_channels_to_mixer`, `reset_mixer_routing`, and `get_mixer_routing` (fl-scripts/controller/device_FLMCP.py `MCP_TOOL_WHITELIST`; MCPTools import is LIVE, docs/12 §4).
And FL pushes `FPD_RoutingChanged` to plugins on routing edits - confirmed live in the probe window (PROBE_VERDICT Q3) - so a routing write has a free push-side confirmation signal, not just polling.

**Decisive experiment** (minutes inside the consolidated live window, no plugin work): over the existing wire, call `mixer.setRouteTo(src, dest, 1)` on a scratch project, then verify on three independent channels - `mixer.getRouteSendActive` readback, the Bridge/observer seeing `FPD_RoutingChanged`, and a Cmd+S file diff.
Same pattern for one `setTrackVolume` call.
The known Surface-3 caveat applies: mutating calls are refused for seconds after project load ("Operation unsafe at current time"), and the script already parks and retries on `OnIdle` (catalog Surface 3).

**API produced** (wire lane, live, no reload):

```python
def mixer_state(self) -> MixerState:                     # inserts, levels, routes (extends snapshot)
def set_mixer_route(self, source: int, dest: int, enabled: bool) -> MixerReceipt
def set_mixer_levels(self, track: int, *, volume: float | None, pan: float | None,
                     muted: bool | None = None, solo: bool | None = None) -> MixerReceipt
```

Receipts verify by readback (`getRouteSendActive` / `getTrackVolume`), never echo (#54).
Send LEVEL (as opposed to on/off) may not be settable via `setRouteTo`; if the stubs' signature confirms that gap, the file lane below covers it and the wire lane owns only enable/disable plus track levels - capability detection picks the lane, no flag (#54).

**Confidence: high.** Documented API + live-verified transport + a live push signal to confirm against.
Risk is only that `setRouteTo` is one of the stub functions that exists but no-ops on FL 2026 - which the one experiment settles.

### 2b. The mixer FILE section: RE it for offline mode and for send levels

flpkit currently stops at the boundary: `EVENT_MIXER_FLAGS` (236) marks where the mixer section begins and nothing past it is decoded (flpkit constants).
FL's ~164 bundled corpus projects have rich mixers, so ground truth is free.

**Sequence** (playbook order): pyflp's insert/slot field map as hypothesis -> full-stream consistency across the corpus -> minimal pairs:

| Pair | Change | Decides |
|---|---|---|
| MP-M1 | Toggle one send route on/off | The route-matrix encoding and its location. |
| MP-M2 | Drag one send knob | The send-level field - the thing 2a possibly cannot write. |
| MP-M3 | Rename one insert | The insert record's identity/name framing, which delimits records for the walker. |

**API produced**: `flpkit.read_mixer(path) -> tuple[FlpInsert, ...]` (`FlpInsert`: index, name, routes as `(dest, level)`, slots as `(index, plugin_name, state: bytes)` with state opaque), then `flpkit.write_mixer_route(path, source, dest, *, enabled, level=None)`.
Session: the same `set_mixer_route` signature routes file-lane automatically when FL is closed.

**Confidence: medium-high.** The mixer section is era-versioned like the playlist was; the signature-detection discipline (playbook rule 5) is exactly what handled the 32/60/80/88 playlist strides.

### 2c. Effect chains (add/remove an effect in a slot): file RE with the template rule

No live path exists (standing fact 1: `add_effect`/`remove_effect` are ilmcp-bound REFUTED; Surface-1 plugin loading is modal-only).
This is pure file work, and the template rule makes it tractable without decoding plugin state:

| Pair | Change | Decides |
|---|---|---|
| MP-E1 | Same project, plus ONE stock effect (e.g. Fruity Reverb 2) on insert 1 slot 1, default preset | The complete byte extent of one slot block: plugin identity events (201 InternalName et al.), the wrapper/state blob (carried opaque), and slot placement fields. |
| MP-E2 | The same effect on a different insert and slot | Isolates the position fields from the identity/state bytes - the only fields the writer patches. |
| MP-E3 | Same slot, same effect, one knob turned, saved twice | Whether native-effect state is a patchable param map or an opaque blob; either answer is fine (blob is carried verbatim), but a param map additionally enables offline default tweaks. |

**API produced**: `flpkit.add_insert_effect(path, insert: int, slot: int, plugin: str, *, donor: Path) -> None` - donor names an FL-authored project containing the plugin's slot block to template from (FL's bundled demos cover the stock effects, so the donor bank is the corpus, not repo-shipped blobs), plus `remove_insert_effect(path, insert, slot)`.
Session: `add_effect(insert, slot, plugin)` / `remove_effect(insert, slot)`, file-lane.
Composition note: once the effect EXISTS, its parameters are live-tweakable today via the wire's plugin-param surface (docs/24 "Plugin params - covered"), so add-by-file + tweak-by-wire is a complete production loop with one reload.

**Confidence: medium.** The slot block might reference global tables (plugin dictionary, window state) that a splice must also patch; MP-E1's diff answers that on day one, and the acceptance ladder's rung (c) - FL re-saving our spliced block byte-identically - is the proof it holds.

---

## 3. Add instruments / load presets (care rank 3)

### Honest feasibility, stated first

- **Live instantiation: not possible.** Every live surface is refuted or modal (standing fact 1). No probe can change this; the write-probe does not spend window time on it.
- **File instantiation of stock/native generators: plausible and testable.** A generator channel is an event-64 block plus plugin events - the same template-splice shape as 2c, one level up.
- **Arbitrary third-party VSTs: low confidence.** The wrapper blob embeds paths and host-side plugin-DB references; we know from the plugin-DB `.fst` work that FL resolves natives by name with decodable entries, but VST wrappers are a bigger surface. Not the first target.
- **Preset GUTS: refuted forever** (catalog Surface 5). But **preset LOADING does not need the guts** - a preset `.fst` is an event stream whose state event can be spliced verbatim into a channel's block if MP-I2 confirms the container framing. Carrying, not decoding.
- **Reachable today, zero RE: template-carried instruments.** A template project with N stock generators pre-loaded, which the agent renames, tweaks (live param writes), and sequences. This is the interim answer and stays the fallback.

### Decisive experiments

| Pair | Change | Decides |
|---|---|---|
| MP-I1 | Same project, plus ONE stock generator channel (e.g. 3x Osc), default state | The complete channel-creation record set: event 64 framing, required sibling events, mixer-route defaults, and what (if anything) outside the channel block must also change (channel count fields, group membership). |
| MP-I2 | Save the SAME plugin's state twice as browser presets, one knob apart; separately, save the project twice with that knob turned | Whether the `.fst` preset's state payload and the project's in-channel state payload share one container event - if yes, `load_preset` is a verbatim splice; if no, map the transform between them. |
| MP-I3 | MP-I1 repeated for a simple VST3 | How much host-side reference material the wrapper block drags in - the go/no-go on third-party support. |

### API produced

```python
flpkit.add_channel(path, plugin: str, *, name: str, donor: Path) -> int   # new channel iid
flpkit.apply_preset(path, channel: int, preset_fst: Path) -> None         # verbatim state splice
```

Session: `add_instrument(plugin, name)` and `load_preset(channel, preset)` on the file lane, with the template-project fallback documented as the path when a donor block is unavailable.

### Confidence / risk

**Medium** for stock generators and native-format presets; **low** for VSTs (MP-I3 may return a no, and that verdict gets recorded, not worked around).
Sequenced after items 1 and 2 because 2c's slot-splice experience de-risks the identical mechanism here.

---

## 4. Audio clips / samples (care rank 4, T3)

Channel-clip playlist records already parse (docs/24), so the gap is the sampler CHANNEL: a channel whose content is a sample path.

- **File lane (primary)**: MP-S1 - same project plus one audio channel referencing a known WAV - decodes the sample-path event (a text event, so UTF-16 on FL >= 11.5 per flpkit's text rules) and the sampler defaults.
  API: `flpkit.set_channel_sample(path, channel, wav: Path)` and `add_audio_channel(path, wav, *, name, donor)`; then existing `write_playlist` places the clip.
- **Live lane (write-probe, §5)**: `FHD_LoadAudioClip` (42) "drops an audio file into the playlist" and `FHD_LoadInChannel` (43) "loads a sample into selected channel(s)" - both DOC-only, never exercised, both string-pointer calls so they run guarded, never blind (catalog Surface 1).
  42 is one of the three undo-singleton writers, which is evidence it performs a real project mutation.
  If either works, it is the only LIVE object-creation call we have anywhere - worth the two window-minutes it costs.

**Confidence: high** for the file lane (small record, strong corpus); **unknown** for 42/43 until the window - the "selected channels" targeting of 43 carries the same selection-dependence that ChanNum verdicts taught us to distrust.

---

## 5. The consolidated write-probe window (one sitting, all live questions)

Reusing the read-probe's harness pattern exactly (RUNBOOK_PROBE): offline-buildable, ctest ABI gate before insert, static ledger, session tokens, checkpoint/quarantine, jsonl evidence, scratch project, FL frontmost.

| Op | Host call | Question | Guard |
|---|---|---|---|
| `wp_route_verify` | none (wire + observer only) | Does `mixer.setRouteTo` mutate live, and does `FPD_RoutingChanged` confirm it? | n/a - documented API |
| `wp_setcontext` | 77, `CI_TrackVolume`/`CI_TrackPan`/`CI_TrackMuteSolo` | Can a PLUGIN write mixer levels (Bridge-side redundancy for the wire)? | scalar values only, one track, readback via 76 |
| `wp_loadclip` | 42 | Live audio-clip drop into the playlist? | guarded string buffer, path to a known scratch WAV, undo checked after |
| `wp_loadinchannel` | 43 | Live sample load into the selected channel? | same; selection recorded before/after |
| `wp_floatauto` | 54 | Payload shape and effect of `FHD_FloatAutomation` - the live automation lane's existence proof | attempt only with a payload derived from RTTI/disassembly first (offline prep, #64 rule: no struct ships without a primary-source golden test); if the shape cannot be derived offline, SKIP live - never sweep a writer blind |
| (passive) | - | Which `FPD_*` fires on mixer/slot edits by the human - feeds the observer's event vocabulary | log-only |

Abort and triage rules carry over verbatim from RUNBOOK_PROBE §4.
Verdicts land in a `WPROBE_VERDICT_<date>.md`, propagate to docs/24 rows in the same PR (docs/24 maintenance rule), and the `wp_*` ops are deleted in the PR that records them.

---

## 6. Sequence, dependencies, deletions

**Order** (value x readiness, matching docs/25 §3):

1. **Automation write v1** (file, MP-A1/A2/A4) - no live window needed for v1; highest care rank; unblocks retiring pyflp.
2. **The consolidated live window** (§5) - piggybacks on the next supervised session the R2 ship lane schedules for bridge promotion; `wp_route_verify` alone likely closes routing.
3. **Mixer wire lane** (2a APIs) - lands immediately on a green `wp_route_verify`.
4. **Mixer file section + effect chains** (2b, 2c) - corpus work, fully offline.
5. **Automation v2** (link decode, MP-A3) + **instruments/presets** (3) - both are template-splice descendants of 2c.
6. **Samplers/audio clips** (4) - last, smallest.

**Dependencies on other research areas, by name:**

- **R2 ship lane (bridge promotion + observer)**: supplies the live window this plan piggybacks on, and the observer whose `FPD_RoutingChanged` subscription is 2a's third verification channel (issue #68 sequence step 4).
- **RTTI scraper (#63)**: the offline payload derivation for `wp_floatauto` is exactly the scraper's record-RTTI walk; if the scraper lands first, id 54's payload comes for free.
- **Code-health split (#80)**: the new session methods (automation, mixer, instruments) must land in the post-split module layout, not swell session.py past its 931 lines.
- **CI is dead** (docs/24 enablers): every landing here merges on the local gate (ruff + pytest + ctest) until the owner lever is pulled.

**What gets DELETED** (doctrine #54, net-negative accounting):

- `native/probe/` (already scheduled, #80) - and this plan moves its one durable artifact, the static safety ledger, to `native/abi/` first.
- The `wp_*` ops, in the PR that records their verdicts.
- The pyflp differential dependency, once automation write completes flpkit's coverage (docs/25 §3 item 8).
- If 2a verifies, the "Mixer routing - NONE" and "Automation WRITE - NONE" rows in docs/24 are rewritten in the same PRs that change them - the map is the deliverable that keeps this plan honest.

**Summary verdict table:**

| Capability | Mechanism | Decisive experiment | API | Confidence |
|---|---|---|---|---|
| Automation write (existing curves) | file RE | MP-A1/A2 | `flpkit.write_automation`, `session.set_automation` | high |
| Automation write (new clips/links) | file RE (+ live via id 54 later) | MP-A3/A4, `wp_floatauto` | `flpkit.create_automation_clip` | medium |
| Mixer routing + levels | LIVE, Surface 3 (documented) | `wp_route_verify` | `session.set_mixer_route`, `set_mixer_levels` | high |
| Mixer file section / send levels | file RE | MP-M1..M3 | `flpkit.read_mixer`, `write_mixer_route` | medium-high |
| Effect chains | file RE (template splice) | MP-E1..E3 | `flpkit.add_insert_effect`, `session.add_effect` | medium |
| Instruments (stock) | file RE (template splice) | MP-I1 | `flpkit.add_channel`, `session.add_instrument` | medium |
| Presets (load, not decode) | file RE (verbatim splice) | MP-I2 | `flpkit.apply_preset`, `session.load_preset` | medium |
| Instruments (third-party VST) | file RE | MP-I3 | same, if go | low |
| Audio clips / samplers | file RE + live probe of 42/43 | MP-S1, `wp_loadclip`/`wp_loadinchannel` | `flpkit.set_channel_sample`, `add_audio_channel` | high (file) / unknown (live) |
