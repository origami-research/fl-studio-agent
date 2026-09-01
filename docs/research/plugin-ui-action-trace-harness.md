# Plugin UI action trace harness

## Status

This is a design and an inert discovery artifact, not a live probe and not a
plugin feature. Coverage stays `NONE` in [the coverage map](../24-coverage-map.md).

The artifact is `fl_mcp.passes.action_trace(profile)`. The existing `Plugins`
pass remains in `DEFAULT_PASSES`, so the harness is derived from the same
per-build dispatcher alignment and profile that the discovery pipeline owns.
`python -m fl_mcp.discover` prints it, but it emits no `wp_*` command and does
not install a hook, launch FL, drive the UI, or call a dispatcher id.

## Future orchestrator step

In a supervised, disposable-project FL window, the orchestrator may install a
native dispatcher-boundary observer and ask a human to perform exactly one of:

1. Add one instrument.
2. Add one effect to one selected mixer insert.
3. Load one preset into an existing plugin.

The hook records dispatcher entry and return only. It does not invoke, replay,
or infer a handler from an observed id. Each trace is retained with the
per-build profile and the exact human gesture. The independent acceptance
readback is the visible FL state plus a saved-project diff; the plugin file-RE
and mixer-writer owners decide how a later feature interprets that diff.

## Safety boundary

`native/abi/fl_dispatch_ledger.h` is the authority for calls. This harness
issues none. In particular ids 94, 99, and 106 are forbidden, as are all
unknown or unsafe ids. Observing an id is evidence, never permission to call
it. Any candidate still first needs static payload/completion decoding and then
a separately approved disposable-project experiment.

The artifact records provenance from `docs/33-capability-acquisition-model.md`,
the ABI ledger, and `native/probe/PROBE_VERDICT_2026-08-30.md`, and records the
readback expected for every gesture. Focused tests prove its command list is
empty and its fatal set includes the static ledger's live-confirmed hazards.
