# RUNBOOK - one-command discovery (the orchestrator's live run)

`python -m fl_mcp.discover` sequences the whole discovery pipeline (docs/29
Surface 4, r4 A3) into one resumable command:

```
static scrape  ->  passes (map_align, plugins, getters, timing, target, sweep)  ->  emit profile + diff
```

The passes are DATA (`fl_mcp/passes/`), run by ONE generic driver
(`fl_mcp/core/discovery.py::discover`). The native Bridge (`wp_*` ops) stays the
executor; this driver is pure orchestration and **never launches or drives FL**.
FL is a GUI app, the sweep is expected to take it down, and the relaunch-on-crash
loop is a sightmesh job around this command - the steps below.

## What runs without FL (every invocation)

`python -m fl_mcp.discover [--out profile.json]`

1. **Static scrape** (`scrape_rtti`): struct layouts, the VMT first-method
   offset, and the dispatcher bound, read straight from FLEngine's own RTTI.
2. **map_align**: aligns the scraped jump table (`dispatcher.handled_max_id` +
   `inert_in_range`) to the SDK command map and prints
   `N matched, M moved, K new`, names every MOVED (documented id gone inert - a
   handler disappeared, the smoke alarm), and prints the sweep input (the
   genuinely-new ids).
3. **plugins**: emits separate candidate maps for add-instrument, add-effect,
   and load-preset. The current SDK has no documented programmatic command for
   any of them, so their `matched`/`moved` sets are empty. Its non-fatal `new`
   ids are an offline-disassembly queue only; this pass issues no host calls.
4. **Emit**: with `--out`, writes the merged profile and prints the diff vs the
   loaded baseline - "what changed on this build" is the deliverable a human reads.

This half is the answer to "did the ABI move?" and needs no live FL. Run it on
every FL update as a merge-gate drift alarm.

## The live run (orchestrator only)

The live passes ride a running Bridge over the G2 Transport. The driver assumes
FL + Bridge are already up; the orchestrator owns FL's lifecycle:

1. **Provision** (once per machine): build + install the Bridge
   (`native/scripts/install_bridge.sh`), mint the plugin DB entry, and stamp a
   scratch project that has the Bridge on an insert. The checked-in scratch
   project is the goal so even this disappears (docs/24).
2. **Launch** (ports-before-launch rule): close FL if running, `open -a "FL Studio 2026" "<scratch>.flp"`,
   then `osascript -e 'tell application "FL Studio 2026" to activate'` - the
   frontmost requirement (`profile.tick.frontmost_required`) is satisfied by the
   job, not a human.
3. **Run**: `python -m fl_mcp.discover --live --out <profile.json> --resume`.
   - plugins -> getters -> timing -> target -> sweep, folded into the profile in order.
   - `target` PLANTS notes but does not judge; the verdict
     (`capabilities.pattern_targeting_live` / `channel_targeting_live`) comes
     from **Cmd+S + flpkit readback**, the non-negotiable live-acceptance gate
     (#63): `osascript -e 'tell application "System Events" to keystroke "s" using command down'`,
     then read the scratch `.flp` back and check each planted key landed under
     the (PatNum, ChanNum) it was sent to.
4. **Crash-resume supervisor** (the sightmesh job's loop): the sweep is EXPECTED
   to take FL down on a new build's unknown ids.
   - Watch FL's pid. On death, `open -a` the scratch project again and re-run the
     SAME `--live --resume` command.
   - `--resume` replays finished passes from `discovery_ledger.json` (next to
     `--out`); the Bridge's own `wp_quarantine.txt` skips the id the prior run
     crashed on. So a crash STRICTLY advances discovery - it never loops on the
     same id, never re-does finished work.
   - `sweep.fold` promotes each Bridge-reported fatal (`skipped: crashes_host` /
     `hangs_host`, or a dynamically `quarantined` id) into
     `profile.dispatcher.unsafe`. A new build's crashers thus flow into the
     profile as DATA - no C++ edit. (This is how id 106, issue #92, lands in the
     profile: the native ledger skips it, the sweep reports it, the fold records
     it.)
5. **Emit**: the final `--out` profile + the diff is the run's deliverable.

## Modal dialogs (the honest degraded mode)

FL's welcome/save dialogs freeze the OnIdle tick. The scratch project opens clean
and SIGKILL-then-open avoids the save prompt; any other modal is a fail-fast with
`screencapture -x` for the human. Never call a dispatcher id under a modal.

## Safety invariants (unchanged, the Bridge's)

- The static fatal-id ledger (`native/abi/fl_dispatch_ledger.h`) is consulted
  before every dispatcher call; documented ids (0-87) and the known fatals
  (94/99/106) are skipped by construction over ANY range or explicit id list.
- `wp_sweep` accepts an explicit `ids` list (map_align's unknowns) or a
  contiguous range; either way the same skip predicate gates every id, so a
  documented or ledger-fatal id can never be smuggled through.
- The plugin candidate report is not sweep input. Before any disposable-project
  experiment, an operator must statically decode a candidate's payload and
  completion signal. IDs 94, 99, and 106 are fatal and never candidates.
- Claim-before-execute means a command that crashes FL never replays; the durable
  in-flight checkpoint quarantines the id a prior run died on.
