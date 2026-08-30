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
| Notes - read | file | T1 | live read needs a host readback getter (probe Q1) |
| Notes - write, SELECTED target | **live** (bridge) | T1 | done 2026-08-29 (#65); FL-save-confirmed |
| Notes - write, ANY target | file+reload | T1 | select-then-write; needs select/activate host call (probe Q2). Retires the reload lane for notes |
| Observe user actions | NONE | T1 | undo-history read (probe Q3) + lazy checkpoint reads |
| Tempo / transport | live (wire) | T2 | covered |
| Plugin params (existing plugin) | live (wire) | T2 | covered (read/set, #50) |
| Channel levels (vol/pan/pitch) | file | T2 | covered (flpkit) |
| Playlist / arrangement - read | file | T2 | covered (flpkit 0.2) |
| Playlist clips - write | file, FL-verified | T2 | covered (flpkit 0.4); live version needs host call (unknown) |
| Automation clips - read | file | T2 | covered (flpkit 0.3) |
| **Automation - WRITE** | NONE | T2 | opaque trailer + target-link bytes; RE via minimal-pair diff (playbook) |
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
- Bridge is OFF by default - promote only after select-then-write + live verdicts.
- Robustness unbuilt: bulkhead (bridge crash = FL down), circuit breaker, agent-vs-human concurrency rules. See #54.
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
