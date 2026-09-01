# Coverage map - what we cover, what we don't, and the efficient path

This is a LIVING document, maintained every time coverage changes. It is the answer to "what does this not cover?" and the map that keeps us from rediscovering gaps by accident.

North star: an agent that **produces** music in FL (not just arranges MIDI notes) with the human in the loop. Priority = distance from that north star. Efficiency = prefer unlocks that open many capabilities at once.

Capability acquisition is governed by the [capability acquisition model](33-capability-acquisition-model.md): classify each gap as `.flp` data or a live host call before choosing work.

## Legend
- Coverage: `live` (no reload) / `file` (flpkit, may need reload) / `read-only` / `NONE`
- Care: T1 (core, now) .. T5 (meta/enabler)
- Path: the most efficient way to close it

## The map

| Capability | Coverage | Care | Path to close / note |
|---|---|---|---|
| Notes - read | file | T1 | **PROBE VERDICT: no readback getter exists** - reads stay file-based (Cmd+S + flpkit) |
| Notes - write, SELECTED target | **live** (bridge) | T1 | done 2026-08-29 (#65); FL-save-confirmed |
| Notes - write, ANY PATTERN | **live** (bridge) | T1 | **PROBE VERDICT: PatNum honored** - bridge writes any pattern live (switches current pattern as a side effect). Retires reload for pattern-targeted notes |
| Notes - write, ANY CHANNEL | file+reload | T1 | **PROBE VERDICT: ChanNum ignored, no channel object** - channel targeting stays file-lane |
| Observe user actions - transport/selection/routing | **push-capable** | T1 | **PROBE VERDICT: FL pushes SetPlaying/SongPosChanged/SetFocus/ContextInfoChanged/RoutingChanged** live |
| Observe user actions - score edits | poll-and-diff | T1 | no edit push; poll cost ~0ms vs 8.7ms tick (affordable) |
| Tempo / transport | live (wire; ctl lane per session) | T2 | covered; multi-session control rides the pid-addressed ctl lane ([docs/34](34-per-session-control.md)) |
| Plugin params (existing plugin) | live (wire) | T2 | normalized read/set is live-verified (#50). The documented API exposes only name/value; formatted display, kind, ranges and option labels require a captured per-plugin model. Sytrus full-model capture is pending the exact live run in `docs/research/plugin-param-model-sytrus.md`. |
| Channel levels (vol/pan/pitch) | file | T2 | covered (flpkit) |
| Playlist / arrangement - read | file | T2 | covered (flpkit 0.2) |
| Playlist clips - write | file, FL-verified | T2 | covered (flpkit 0.4); live version needs host call (unknown) |
| Automation clips - read | file | T2 | covered (flpkit 0.3) |
| **Automation - WRITE (existing clip)** | file+reload, save/reparse equality-verified | T2 | covered (flpkit 0.6): carries the FL-authored header/trailer and patches points; flpkit verifies its save/reparse and `Track.set_automation()` independently reparses for equality before reload. **New clip + target link: NONE** — a 2026-08-31 launch-only attempt produced byte-identical files, so it is not evidence; the strict E2E gate requires an FL-authored one-action pair before a creator exists (docs/research/automation-create-flp-re-2026-08-31.md). |
| **Mixer routing (sends/inserts)** | NONE | T2 | Corpus map: routing bitmap=235; mixer params=225 candidate ids 192/193/64..191. No FL-authored minimal pair or writer yet. E9's strict xfailed, artifact-owned gate requires the checked-in baseline/changed volume pair; provenance rejects any public `MixerFormat`/writer seam until then (docs/research/mixer-routing-flp-re-2026-08-31.md). |
| **Effect chains (add EQ/reverb/comp)** | file, one native reference | T2 | The checked-in FL-authored pair supports only adding native Fruity Parametric EQ 2 to an empty Master slot 0. `flpkit.add_plugin(path, target, plugin_name, kind)` reads the whole opaque record from its typed reference DB and reparses it after writing; unrecorded native plugins and all VST defaults refuse rather than synthesize state. Other inserts, slots, effects, and VSTs remain NONE. |
| **Add/remove instruments** | NONE | T2 | same static candidate map. The corpus establishes the same opaque tuple before 236 for a rack generator, but no add-instrument pair or writer exists; I1 and V1 are required evidence ([checklist](research/plugin-add-minimal-pair-capture-checklist.md)). |
| **Load presets/patches** | NONE | T2 | no matched FHD command. A shipped `ChannelState` FST carries the complete opaque channel/plugin tuple; P1 is still required before any splice ([checklist](research/plugin-add-minimal-pair-capture-checklist.md)). |
| Audio clips / samples | NONE | T3 | channel-clip playlist records decode; needs sampler-channel writes |
| Pattern lifecycle (create/del/rename) | partial (file) | T3 | flpkit can create patterns on write; rename/delete unbuilt |
| Channel lifecycle (add/del/reorder) | NONE | T3 | file RE or controller API |
| Piano-roll transforms (quantize/arp/strum) | NONE | T3 | we write raw notes; FL's tools via piano-roll scripting API (documented) |
| Markers / time & key sig / groove | NONE | T3 | file RE; some via controller API (documented) |
| Stem export / MIDI export | NONE | T3 | render is full-mix only (-R); stems need per-track routing or CLI flags (unknown) |
| Full-mix render | verified offline (`OsxFL -R`); live control unverified | T3 | `fl.render(path)` calls `ui.exportWav(path)` and checks the WAV exists, but lacks a live-FL verdict; it blocks FL's UI. `OsxFL -R` on a saved copy is verified for non-blocking analysis. |
| Render analysis (BPM/key/loudness/peak/spectrum) | local WAV | T2 | `fl.analyze(path)` is deterministic Python measurement, no model or FL session required. |
| Scene analysis (key/digest) | file | T2 | covered (get_scene) |
| Undo / redo control | undo via wire | T3 | redo + file-lane revert unbuilt (store content-addresses writes) |

## Enablers (T4) - not features, but they gate scale/safety

- CI runs, but fails at `uv --system` on an externally managed interpreter; merges rely on local runs. OWNER lever.
- Bridge routes by capability detection (no flag): fresh heartbeat + proto match + ping = eligible, file lane otherwise. LIVE-UNVERIFIED post-integration; the RUNBOOK_BRIDGE_V3 window is the promotion gate.
- Bridge session isolation: `FLMCP_SPOOL_ROOT` selects one shared server/Bridge mailbox; default roots remain the measured FL fallback. Unit-covered; two-live-FL confirmation remains an orchestrator run.
- Robustness built offline, unverified live: bulkhead invariant set, circuit breaker, agent-vs-human guard (observer). See #54.
- Licensing/template project deferred (pending license).

## Non-blocking render verdict (2026-08-31)

There is no verified in-process, non-blocking render call to wire as
`render_slice` or `render_async`.

The static FLEngine dispatcher decode for FL 2026.1.5 build 5384 gives a
entry is `FHD_RenderProject` (id 60).  The SDK describes that entry as opening
the rendering dialog, and the native fatal-id ledger classifies it `modal_ui`.
It cannot be an unattended, asynchronous host render.  The controller's
`ui.exportWav` is likewise a live menu export and blocks the GUI until it
finishes; its artifact-existence readback does not change that fact.

The remaining undocumented handled ids are not a runtime search space: ids 94
and 99 are live-confirmed crash/hang hazards and 106 also wedges the bridge
tick.  Calling another unknown id to look for a renderer would violate the
ledger and can take down the user's FL session.  No host call was added.

**Safe mitigation now.** Do not put `fl.render()` in the ears loop.  For a
non-blocking analysis snapshot, save a project copy and render that copy in a
separate `OsxFL -R` process; validate the resulting WAV before analysis.  When
the current live project must be rendered, make it a user-visible, single-flight
operation at a bounded cadence, never an automatic feedback tick.

**Next evidence step.** On a matching FLEngine dylib, statically disassemble
the jump-table targets for the undocumented, non-quarantined ids and identify a
candidate's payload, completion signal, and thread behavior *before* any live
call.  Only then may a disposable-project experiment prove that it returns
without blocking and produces a valid WAV; that readback is the gate for a
future async seam.

## The five surfaces FL exposes (enumeration method per surface)
1. Plugin host calls (FHD_*) - SDK header + FL binary RTTI + probe sweep + observing Image-Line plugins.
2. Plugin callbacks (MIDI/transport/param/save pushed to us) - SDK header + live slot logging.
3. Controller/MIDI scripting API - **Image-Line documents this**; transcribe (free breadth).
4. Piano-roll scripting API - **documented**; transcribe.
5. File format - RE playbook (docs/23) + flpkit.

## Efficient order (maximal unlock per effort)
1. Finish the probe -> unlocks T1 (readback, select-then-write, undo-history) AND reveals which T2 items are even possible via host calls. One live window, many answers.
2. Transcribe the documented controller + piano-roll APIs into this map -> free T3 breadth.
3. RTTI scraper (#63) -> makes surface 1 mechanical; auto-detects future FL changes.
4. Then attack T2 by care order: automation-write, mixer, instruments - each via probe-found host call or file RE.

## Maintenance rule
Update this table in the same PR that changes coverage. A capability that moves NONE->live, or a probe that answers a Q, edits this file. This is the standing "keep track of what we don't cover" artifact.
