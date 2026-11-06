# Coverage map - what we cover, what we don't, and the efficient path

This is a LIVING document, maintained every time coverage changes. It is the answer to "what does this not cover?" and the map that keeps us from rediscovering gaps by accident.

North star: an agent that **produces** music in FL (not just arranges MIDI notes) with the human in the loop. Priority = distance from that north star. Efficiency = prefer unlocks that open many capabilities at once.

Capability acquisition is governed by the [capability acquisition model](33-capability-acquisition-model.md): classify each gap as `.flp` data or a live host call before choosing work.

## Legend
- Coverage: `live` (no reload) / `file` (flpkit, may need reload) / `read-only` / `NONE`
- Care: T1 (core, now) .. T5 (meta/enabler)
- Path: the most efficient way to close it

## The map

| Safety check | Coverage | Path to close / note |
|---|---|---|
| Bridge ABI handshake | load-time | Read-only `FHD_GetColorRGBValue` palette count, followed by `FPD_SetSampleRate`, `FPD_SetBlockSize`, and `Idle_Public` in three seconds. A mismatch writes `bridge_status=abi_mismatch:<detail>` and blocks host dispatch. |
| Channel index | global, every live call | Every channel the engine names live is FL's global index, as in the song file, whatever filter group the channel rack shows: the controller's composites and every `channels.*` and `plugins.*` call carry FL's `useGlobalIndex` flag (controller 0.2.4). Before 2026-11-06 a filter group made the live indices count only that group's channels, so a write could land on the wrong channel (Lane A). Live 2026-11-06 on fl-mcp-03 (trial FL 2026 26.1.6), the agent project opened with a filter group showing only Bass, Synth Bass and Piano: unflagged, FL counted 3 channels and its channel 0 was Bass; with the flag (positional or keyword) 23, channel 0 Drums 808. The engine listed all 23 by global index, `set_levels` on Piano landed on channel 7, notes written to Bass rendered only in a Bass solo (Piano and Drums 808 solos silent), and every mute came back. The flag changes nothing for a mixer effect slot (`plugins.getPluginName` on Master slot 9). |
| Audio engine | live heartbeat | `render_blocks` counts `Eff_Render`; `render_rate` measures blocks per heartbeat interval. A zero rate means FL's audio engine is not running, so inspect the configured output device. |

| Capability | Coverage | Care | Path to close / note |
|---|---|---|---|
| Notes - read | session record + file | T1 | **PROBE VERDICT: no readback getter exists** (re-checked 2026-10-18) - reads overlay this session's confirmed writes on the project FL loaded ([docs/27 §1a](27-shared-surfaces.md#1a-note-state)) |
| Notes - write, SELECTED target | **live** (bridge) | T1 | done 2026-08-29 (#65); FL-save-confirmed |
| Notes - write, ANY PATTERN | **live** (bridge) | T1 | **PROBE VERDICT: PatNum honored** - bridge writes any pattern live (switches current pattern as a side effect). Retires reload for pattern-targeted notes |
| Notes - write, ANY CHANNEL | **live** (bridge + wire select) | T1 | **LIVE:** before each mismatched write, select exactly the named channel over the control wire and verify `channels.selectedChannel()` before sending the Bridge command. Selection remains changed. FL's piano roll is closed before every Bridge note edit: writes with it open froze FL during the next playback on a 4,000-note song (#290, 3 of 3 box runs; 0 of 4 with it closed). FL exposes no live note getter, so the Bridge acknowledgement is the confirmation and the session records the result; a licensed FL with a readable project also saves and the server re-reads it. |
| Observe user actions - transport/selection/routing | **push-capable** | T1 | **PROBE VERDICT: FL pushes SetPlaying/SongPosChanged/SetFocus/ContextInfoChanged/RoutingChanged** live |
| Observe user actions - score edits | poll-and-diff | T1 | no edit push; poll cost ~0ms vs 8.7ms tick (affordable) |
| Tempo / transport | live (machine-wide wire; broker in session mode) | T2 | covered; multi-session control uses BrokerControl with pid-addressed broadcast ([docs/34](34-per-session-control.md)) |
| Control broker | live (broker) | T4 | live-verified (#158); one machine-wide broker (pid-addressed broadcast over the single MIDI wire) owns controller requests and prevents one FL session from consuming another's control traffic. |
| Plugin params (existing plugin) | live (wire) | T2 | normalized read/set is live-verified (#50). The documented API exposes only name/value; formatted display, kind, ranges and option labels require a captured per-plugin model. Sytrus full-model capture is pending the exact live run in `docs/research/plugin-param-model-sytrus.md`. |
| Channel levels (vol/pan/pitch) | file | T2 | covered (flpkit) |
| Playlist / arrangement - read | file | T2 | covered (flpkit 0.2) |
| Playlist clips - write | file, FL-verified | T2 | covered (flpkit 0.4); live version needs host call (unknown) |
| Automation clips - read | file | T2 | covered (flpkit 0.3) |
| File-edit batching | file+reload (development only) | T4 | `with fl.file_edit():` batches file-lane changes and reloads FL once after confirmed reparse. In session mode it raises retryable `file_lane_requires_reload` before any file write: a session-mode FL process is never killed by the engine. |
| **Automation - WRITE (existing clip)** | file+reload (development only), save/reparse equality-verified | T2 | covered (flpkit 0.6): carries the FL-authored header/trailer and patches points; flpkit verifies its save/reparse and `Track.set_automation()` independently reparses for equality before reload. Session mode refuses it before mutation because no live automation writer exists. **New clip + target link: NONE** - a 2026-08-31 launch-only attempt produced byte-identical files, so it is not evidence; the strict E2E gate requires an FL-authored one-action pair before a creator exists (docs/research/automation-create-flp-re-2026-08-31.md). |
| **Mixer routing (sends/inserts)** | live (wire) | T2 | `fl.route` / `fl.send` (2026-11-02): `channels.setTargetFxTrack`, `mixer.setRouteTo` plus `mixer.afterRoutingChanged`, and `mixer.setRouteToLevel`, each read back from FL. The calls were live-verified on FL 2026 for Windows, controller API 45 ([note](research/routing-presets-live-2026-11-02.md)); the SDK path ran live end to end through `run` the same day, which found and fixed a send turned off reading no level (it now reads back `level=None`). A file-side routing writer remains NONE: corpus map routing bitmap=235, mixer params=225 candidate ids 192/193/64..191, no FL-authored minimal pair (docs/research/mixer-routing-flp-re-2026-08-31.md). |
| **Effect chains (add EQ/reverb/comp)** | live (Bridge), stock effect presets | T2 | Live (2026-11-02): `fl.load_preset(channel, effect_preset)` loads a stock `Plugin presets/Effects/<plugin>/...` preset through the Bridge's `FHD_LoadInChannel`, and FL adds that effect to the next free slot of the mixer track the channel plays through; the result is that track's slots read before and after ([note](research/load-sample-preset-live-2026-11-02.md)). Effects with no stock preset, VSTs, and placing an effect in a chosen slot remain NONE live. `fl.add_effect`, the file-side path (one FL-authored Fruity Parametric EQ 2 record into an empty first Master slot, development mode only), was removed on 2026-11-06 so that loading an effect preset is the one way to add an effect. The codec keeps `flpkit.add_effect(path, insert, plugin_name, slot=)` (the template builder splices the Bridge with it); it reads the whole opaque record from its typed reference DB and reparses it after writing; unrecorded native plugins and all VST defaults refuse rather than synthesize state. |
| **Add/remove instruments** | replace: live (Bridge); add/remove: NONE | T2 | Replacing a channel's instrument is live (2026-11-02): `fl.load_preset(channel, instrument_preset)` puts the stock preset's plugin on the channel, a sampler included, keeps its mixer track, and is read back through `plugins.getPluginName` ([note](research/load-sample-preset-live-2026-11-02.md)). Adding or removing a channel: the same static candidate map; the corpus establishes the same opaque tuple before 236 for a rack generator, but no add-instrument pair or writer exists; I1 and V1 are required evidence ([checklist](research/plugin-add-minimal-pair-capture-checklist.md)). |
| **Load presets/patches** | live (wire and Bridge) | T2 | A plugin's own presets: `fl.presets` / `fl.set_preset` (2026-11-02) list them with `plugins.getPresetCount` and `plugins.getName(ch, -1, 6, i)` and step with `plugins.nextPreset` / `prevPreset`; FL has no current-preset getter, so the channel's new name is the readback. Stock preset files: `fl.load_preset` (2026-11-02) loads one from `fl.sounds` through the Bridge's `FHD_LoadInChannel` after selecting exactly that channel; an instrument preset replaces the channel's plugin and an effect preset adds an effect, each confirmed by readback ([note](research/load-sample-preset-live-2026-11-02.md)). VST presets refuse. A file splice still requires P1 ([checklist](research/plugin-add-minimal-pair-capture-checklist.md)). |
| Stock sound search | local files | T2 | `fl.sounds(query)` (2026-11-02) lists this install's stock presets (`Plugin presets/.../*.fst`) and samples (`Packs/...`) under FL's `Data/Patches`, read by the engine because the `run` kernel cannot read FL's install. The location and layout were checked on FL 2026 for Windows (`C:\Program Files\Image-Line\FL Studio 2026\Data\Patches`); FL for macOS is unchecked. FLEX's own presets ship as `.flexpack` packs and are not listed. |
| Audio clips / samples | sampler samples: live (Bridge); playlist audio clips: NONE | T3 | `fl.load_sample(channel, sample)` (2026-11-02) loads a stock sample into a sampler channel through the Bridge's `FHD_LoadInChannel`; FL renames the channel after the sample, which is the readback ([note](research/load-sample-preset-live-2026-11-02.md)). Samples outside FL's stock folder are not offered. Playlist audio clips: channel-clip playlist records decode; `FHD_LoadAudioClip` is untried. |
| Pattern lifecycle (create/del/rename) | partial (file) | T3 | flpkit can create patterns on write; rename/delete unbuilt |
| Channel lifecycle (add/del/reorder) | NONE | T3 | file RE or controller API |
| Piano-roll transforms (quantize/arp/strum) | NONE | T3 | we write raw notes; FL's tools via piano-roll scripting API (documented) |
| Markers / time & key sig / groove | NONE | T3 | file RE; some via controller API (documented) |
| Stems, solo and bar-range renders | live Bridge capture | T3 | 2026-11-03: `fl.render(bars=, start_bar=, solo=, mute=, stems=)`. Solo and mute set the channel rack's mutes in one controller op each way (`ctl.set_mutes`, controller 0.2.3: `channels.muteChannel` per channel, each read back with `isChannelMuted`), play only after FL answers a ping, and set back what they changed; a restore FL does not confirm is sent again once FL answers, then reported channel by channel and set back before the next solo or mute render (#309: one mute and one read per channel, 44 requests on 23 channels, and FL stopped answering mid-render). Live 2026-11-06 on fl-mcp-03 (trial FL 2026, 23-channel agent project): the calls of the take that froze, replayed three times on fresh FL, then four more `solo=["808"]` renders each, 15 solo renders with no freeze and every mute back; a range starts at `transport.setSongPos` in absolute ticks; stems are one play per channel or per mixer track's channels, only for channels with notes in the bars (since a later 2026-11-03 change; a list of channels overrides). Live-verified 2026-11-03 on a trial FL 2026 Windows box with the 23-channel agent project (pattern mode): bar 2 alone matched bar 2 of the full render (envelope correlation 0.96, offset under 12 samples); a Bass solo held the low band (85 dB below 150 Hz against 40 dB at 0.4-2 kHz) and a Piano solo the mids; every mute came back; 23 one-bar channel stems took about 4 minutes. FL records the mute changes in its undo history, so `fl.undo()` refuses one (see sdk-reference). MIDI export: NONE. |
| Live project save | live (wire) | T2 | live-verified (#164, #167): `FPT_Save` persists live state, and the saved `.flp` is reparsed to confirm the resulting project state. |
| Full-mix render | live Bridge capture | T3 | live-verified 2026-09-04: 16 written notes yielded 347711 stereo frames at 44100 Hz (7.88 s, -18.4 LUFS, -2.3 dBFS) and `render_wav` took about 9 s, versus about 64 s for CLI export. `fl.render(path)` reads the live arrangement's length from FL (playlist in song mode, selected pattern in pattern mode, at the live tempo), arms the Master Bridge, plays once, and records exact frames; nothing is saved. It needs FL audio output, the same box requirement as RDP audio. Missing Bridge, capture stalls, drops, silence, or a mismatched WAV fail loudly. `fl.export_offline(path)` is separately requested, boots a second FL, and takes about a minute. |
| Audibility check (per channel) | live wire meters | T3 | `fl.meters()` (the `check` tool until 2026-11-03, when `run` became the only tool; added 2026-10-18): plays a few bars from a bar, polls `mixer.getTrackPeaks` (API v1) on each channel's mixer track and the master, then stops FL. Unit-covered against a scripted controller. LIVE-UNVERIFIED: real meter readings and their fall-off between polls, `transport.setSongPos` in absolute ticks. Channels sharing a mixer track are measured together; a channel routed straight to the master cannot be measured alone (`sounded=None`); routing between mixer tracks is not traced. |
| Listening to a render | local WAV, image | T2 | 2026-11-03: primitives only, in `run` (the only tool); numpy, scipy, soundfile, librosa, matplotlib, pyloudnorm, mido and music21 are installed. `fl.read_wav(path)` gives the samples, and `run` shows any image a cell displays (matplotlib, PIL), so the model measures and plots what it wants; a render's span (FL's tempo and beats) places bars, and solo and stem renders give one instrument alone. The canned `analyze` tool, `fl.analyze` and `fl.spectrogram` were removed. No audio input: Claude cannot take audio. |
| Mixer read (tracks, effects, sends) | live wire | T3 | 2026-11-03: `fl.mixer`, one composite controller read (`state.mixer`, controller 0.2.2) of the tracks in use: the master, tracks a channel plays into, tracks with an effect or a new name, and tracks those send to; sends are scanned only from tracks in use. LIVE-UNVERIFIED until the PR's box run. |
| Playlist and patterns read | file / live wire | T2 | 2026-11-03: `fl.playlist` is the clips of the song FL opened (the SDK cannot change the playlist; a trial FL never saves); `fl.patterns` reads number, name and length live. |
| Scene analysis (key/digest) | file | T2 | covered (get_scene) |
| Undo / redo control | undo via wire | T3 | redo + file-lane revert unbuilt (store content-addresses writes) |

## Enablers (T4) - not features, but they gate scale/safety

- CI runs, but fails at `uv --system` on an externally managed interpreter; merges rely on local runs. OWNER lever.
- Windows offline port landed (#113); live Windows DLL/load/ping/note-write validation remains.
- Bridge routes by capability detection (no flag): fresh heartbeat + proto match + ping = eligible. Since 2026-10-27 a note write with no live Bridge refuses; the file lane serves only targets the live Bridge cannot verify. LIVE-UNVERIFIED post-integration; the RUNBOOK_BRIDGE_V3 window is the promotion gate.
- Bridge session isolation: `FLMCP_SPOOL_ROOT` selects one shared server/Bridge mailbox; default roots remain the measured FL fallback. FL is always started by spawning its executable with this session environment, never `open -a`, because LaunchServices makes FL single-instance. Unit-covered; two-live-FL confirmation remains an orchestrator run.
- Robustness built offline, unverified live: bulkhead invariant set, circuit breaker, agent-vs-human guard (observer). See #54.
- Licensing/template project deferred (pending license).

## Non-blocking render verdict (2026-08-31)

There is no verified in-process, non-blocking render call to wire as
`render_slice` or `render_async`.

The static FLEngine dispatcher decode for FL 2026.1.5 build 5384 gives a
complete 0..109 command table.  Its only named project-render
entry is `FHD_RenderProject` (id 60).  The SDK describes that entry as opening
the rendering dialog, and the native fatal-id ledger classifies it `modal_ui`.
It cannot be an unattended, asynchronous host render. `ui.exportWav` is not a
controller API at all, so it cannot be a renderer or an artifact receipt.

The remaining undocumented handled ids are not a runtime search space: ids 94
and 99 are live-confirmed crash/hang hazards and 106 also wedges the bridge
tick.  Calling another unknown id to look for a renderer would violate the
ledger and can take down the user's FL session.  No host call was added.

**Safe mitigation now.** Do not put `fl.render()` in the ears loop. It plays the
live arrangement once in real time while the Bridge records it, validates the
WAV, and remains a user-visible, single-flight operation at a bounded cadence.
The macOS `-R` command and adjacent WAV output were live-verified in #169.

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
