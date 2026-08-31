# Coverage map - what we cover, what we don't, and the efficient path

This is a LIVING document, maintained every time coverage changes. It is the answer to "what does this not cover?" and the map that keeps us from rediscovering gaps by accident.

North star: an agent that **produces** music in FL (not just arranges MIDI notes) with the human in the loop. Priority = distance from that north star. Efficiency = prefer unlocks that open many capabilities at once.

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
| Tempo / transport | live (wire) | T2 | covered |
| Plugin params (existing plugin) | live (wire) | T2 | covered (read/set, #50) |
| Channel levels (vol/pan/pitch) | file | T2 | covered (flpkit) |
| Playlist / arrangement - read | file | T2 | covered (flpkit 0.2) |
| Playlist clips - write | file, FL-verified | T2 | covered (flpkit 0.4); live version needs host call (unknown) |
| Automation clips - read | file | T2 | covered (flpkit 0.3) |
| **Automation - WRITE (existing clip)** | file+reload, save/reparse equality-verified | T2 | covered (flpkit 0.6): carries the FL-authored header/trailer and patches points; flpkit verifies its save/reparse and `Track.set_automation()` independently reparses for equality before reload. Creating a new target/link remains RE. |
| **Mixer routing (sends/inserts)** | NONE | T2 | opaque; RE file + probe for host calls. Biggest "production" gap |
| **Effect chains (add EQ/reverb/comp)** | NONE | T2 | needs plugin-instantiation host call (probe) or file RE |
| **Add/remove instruments** | NONE | T2 | plugin instantiation - no known API; probe + Image-Line plugin observation |
| **Load presets/patches** | NONE | T2 | opaque plugin blob; probe for a load-preset host call |
| Audio clips / samples | NONE | T3 | channel-clip playlist records decode; needs sampler-channel writes |
| Pattern lifecycle (create/del/rename) | partial (file) | T3 | flpkit can create patterns on write; rename/delete unbuilt |
| Channel lifecycle (add/del/reorder) | NONE | T3 | file RE or controller API |
| Piano-roll transforms (quantize/arp/strum) | NONE | T3 | we write raw notes; FL's tools via piano-roll scripting API (documented) |
| Markers / time & key sig / groove | NONE | T3 | file RE; some via controller API (documented) |
| Stem export / MIDI export | NONE | T3 | render is full-mix only (-R); stems need per-track routing or CLI flags (unknown) |
| Full-mix render | headless CLI | T3 | covered (E4: OsxFL -R) |
| Scene analysis (key/digest) | file | T2 | covered (get_scene) |
| Undo / redo control | undo via wire | T3 | redo + file-lane revert unbuilt (store content-addresses writes) |

## Enablers (T4) - not features, but they gate scale/safety
- CI is DEAD (org billing) - no automated gate; merges rely on local runs. OWNER lever.
- Windows - parked on hardware (E8).
- Bridge routes by capability detection (no flag): fresh heartbeat + proto match + ping = eligible, file lane otherwise. LIVE-UNVERIFIED post-integration; the RUNBOOK_BRIDGE_V3 window is the promotion gate.
- Robustness built offline, unverified live: bulkhead invariant set, circuit breaker, agent-vs-human guard (observer). See #54.
- Licensing/template project deferred (pending license).

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
