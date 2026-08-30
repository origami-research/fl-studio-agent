# RUNBOOK - Bridge v3 live verification (bulkhead + heartbeat additions + event forwarding + wp_* discovery)

Orchestrator-run, supervised-window procedure, in the RUNBOOK_BRIDGE_V2 pattern.
Repo agents never launch, kill, or drive FL; this document is the contract for the human/orchestrator who does.
Scope: what PR "L4 native" added - the completed bulkhead invariant set, the heartbeat `proto=`/`pat=`/`chan=` lines, `bridge_events.jsonl` forwarding, and the `wp_*` discovery ops that replaced `native/probe/`.

## 0. Offline gate (run before any live step)

```
native/scripts/fetch_sdk.sh          # once per checkout
cmake -S native -B native/build
cmake --build native/build -j
ctest --test-dir native/build --output-on-failure
```

All five tests must pass (`bridge_core_test`, `bridge_events_test`, `bridge_gui_test`, `bridge_udp_test`, `bridge_abi_check`).
This IS the bulkhead promotion gate in executable form; the checklist below maps each invariant to its enforcement.

## 1. The bulkhead invariant set (R2 s2.2) - where each lives

| # | Invariant | Enforced by | Proven by |
|---|---|---|---|
| 1 | Static fatal-id ledger; only live-proven ids issued in normal operation | `native/abi/fl_dispatch_ledger.h` (ids 94/99 + hazard table); the executor issues only `FHD_AddNotesToPR`, `FHD_SetDirty`; wp ops consult `IsUnsafe`/`IsDocumented` before EVERY dispatcher call | `bridge_core_test` sections 15-16 (94/99 never reach the mock host over a full sweep range) |
| 2 | Claim-before-execute | `ExecuteCommand` records the id in `consumed.json` before any host call; wp ops ride the same executor | `bridge_core_test` sections 8, 16 |
| 3 | Concrete targets only | `BuildNotesParams` refuses pattern < 1 / channel < 0 | section 2b |
| 4 | Bounded tick work, no blocking syscalls on host threads, socket thread never calls the host | pump caps (`kMaxUdpCommandsPerPump`, `kMaxEventsPerPump`); event enqueue is a lock-free ring stamp; wp_timing iters capped at 2000 | `bridge_events_test` section 6 (race harness); `bridge_udp_test` |
| 5 | Exception containment at the tick boundary | `Pump()`/`Tick()` try/catch; the un-catchable FPC longjmp is exactly what invariants 1-3 exist to never trigger | crash-free live window |
| 6 | Struct layout enforced at build time | static_assert wall in `bridge_core.cpp` (hand-written fallback) or L2's `native/generated/fl_profile_generated.h` when present, plus `check_vtable.sh` (`bridge_abi_check`) | the build itself |

Live confirmation of the set as a whole: one supervised window in which sections 2-4 below all pass with FL never crashing, hanging, or showing a modal.

## 2. Heartbeat additions (docs/27 s6 - what L1's detect() reads)

Install and load the bridge (`native/scripts/install_bridge.sh`, insert "FL MCP Bridge" on any mixer insert or open a project that carries it).
Read `<spool>/bridge_heartbeat.txt` where `<spool>` = `~/Documents/Image-Line/FL Studio/Settings/Piano roll scripts/FLMCP`.

Expected new keys (after the existing tick/applied/... block):

```
proto=2
pat=-1
chan=-1
event_seq=0
event_dropped=0
```

Semantics to verify live:

1. Fresh load: `pat=-1` (unknown) and `chan=-1`.
2. Send a live note write targeting pattern 2 (any channel) through the server or `bridge_spool_cli`-style spool command; after the ack, `pat=2` - the bridge learned it from its own write's SetCurrentPattern side effect.
3. `chan=` stays `-1` FOREVER.
   This is correct, not a bug: FL exposes no selected-channel getter (VERDICT Q2; CI_ ids are mixer-track only), so the router must prove channel matches via the wire (`channels.selectedChannel()`), never via the heartbeat.
4. Open a different project (File > Open): the next heartbeat shows `pat=-1` again (FPD_ProjLoaded reset).

## 3. Event forwarding (docs/27 s5-6 - what L3's drain_pushes() reads)

Tail `<spool>/bridge_events.jsonl` (also mirrored in the parent `Piano roll scripts` root).
Line schema (pinned with L3; one JSON object per line, raw FPD ids, no coalescing):

```
{"seq": 1, "ts_ns": ..., "unix_ms": ..., "id": 12, "name": "FPD_SetPlaying", "index": 0, "value": 1, "gui": 1, "dropped": 0}
```

With the agent idle, perform in FL and confirm the matching lines appear within ~1 s (FL frontmost - the tick only runs steadily while frontmost; backgrounded FL queues events in the ring and flushes on refocus):

| Your action | Expected id/name |
|---|---|
| Press play, then stop | 12 `FPD_SetPlaying` value 1 then 0 |
| Drag the song position | 13 `FPD_SongPosChanged` |
| Click into / out of a plugin window | 22 `FPD_SetFocus` |
| Select a different mixer track | 41 `FPD_ContextInfoChanged` (a burst is normal) |
| Change a mixer route | 25 `FPD_RoutingChanged` |
| Open a project | 27 `FPD_ProjLoaded` |

Invariants to spot-check:

- `seq` increases by exactly 1 per line (gap-free); drops appear ONLY as an increase in `dropped`.
- `seq` restarting at 1 means the plugin reloaded; the file is truncated at load only if it exceeded 4 MB.
- Id 14 (`FPD_SetTimeSig`) carries a POINTER in `value`; nothing may dereference it server-side.
- A bridge note write causes a context/focus burst tagged in time right after the ack - that is the agent's own echo (L3 tags origin).

## 4. wp_* discovery ops (the folded probe; fl_mcp.discover's native side)

The four probe passes are now ordinary v2 spool/UDP commands through the ONE executor - claimed once in the ledger, answered in `responses/<id>.json`, ack shape `{"id", "ok", "result": {...}, "backend": "bridge"}` (no `op` echo: `SpoolResponse.op` is a closed literal and wp ops ride the `result` field instead).

| Op | Args | Result |
|---|---|---|
| `wp_getters` | none | every curated safe getter called with its real convention (guarded buffer for 36/37/38); per-getter `ret`, `ret_class`, `buf_hex` |
| `wp_timing` | `{"iters": 200}` (cap 2000) | per-getter min/median/max/mean ns + `poll_bundle.median_sum_ns` |
| `wp_target` | `{"ppq": 96, "steps": [{"pattern": 2, "channel": 0}, ...]}` | one note per step, unique key per step (84+step); verdict comes from Cmd+S + flpkit readback, exactly like the probe's target pass |
| `wp_sweep` | `{"start": 88, "end": 109}` | skips documented (0-87) + ledger fatals (94/99) + quarantined ids by construction; per-id `ret`/`ret_class`/`ret_hex` |

Example (file carrier; UDP works identically through `bridge_wire`):

```
SPOOL=~/Documents/Image-Line/FL\ Studio/Settings/Piano\ roll\ scripts/FLMCP
echo '{"proto":2,"id":9001,"op":"wp_getters"}' > "$SPOOL/commands/009001.json"
echo '{"proto":2,"ids":[9001]}' > "$SPOOL/commands/queue.json"
# wait ~1s with FL frontmost, then:
cat "$SPOOL/responses/009001.json"
```

Crash-safety ritual (replaces the probe's session-token handshake):

- Before each dispatcher call a wp op fsyncs `<spool>/wp_inflight.txt` naming the id in flight; `bridge_dispatch.txt` keeps the same breadcrumb for note writes.
- If FL dies mid-op: relaunch FL.
  The crashed COMMAND never replays (its id was claimed before execution).
  Re-issue the same pass with a NEW id: the op reads the stale in-flight checkpoint, appends that id to `<spool>/wp_quarantine.txt`, reports it as `"recovered_crasher"`, and skips it - a crash strictly advances discovery.
- `wp_quarantine.txt` is server/operator-writable: `echo "103 manual" >> wp_quarantine.txt` pre-quarantines an id for the next run.

Live acceptance for this PR: `wp_getters` returns the known-good FL 2026 values (nonzero mixing/playback time hex, `FHD_GetTimeMul` ret 0 as the negative control), and `wp_sweep 88-109` completes with no crash, no hang, and `skipped` lines for 94/99.

## 5. What no longer exists (do not look for it)

- `native/probe/` source, `run_probe.sh`, `analyze_probe.py`, `make_fst.py`, `stamp_probe_project.py`, `install_probe.sh`, the probe session-token ritual, `probe_*.txt` command files.
  `PROBE_VERDICT_2026-08-30.md`, `PROBE_FINDINGS.md` and `evidence-2026-08-30/` remain as the durable record.
- The probe's `.fst` minting flow: the Bridge installs via `install_bridge.sh` as before; nothing needs a plugin rescan.
