# Sytrus parameter model — live capture required

This file is deliberately not a synthetic parameter map. A complete Sytrus
map must enumerate all 597 live indices and values, then attach independently
verified display semantics. The prior E2 run established the 597 count and
normalized read/set/readback, but did not retain the complete dump or expose
format/type/option data.

## What the documented FL API exposes

The control lane can read `plugins.getPluginName`, `getParamCount`,
`getParamName`, and `getParamValue`, and set with `setParamValue`. It does not
expose formatted display text, units, min/max, a step size, a boolean/enum
flag, or enum option labels. Therefore no programmatic call can honestly turn
an arbitrary Sytrus normalized value into `440 Hz`, `-6 dB`, or `Bandpass`.

`PluginParamModel` is the typed home for verified per-plugin knowledge. Without
one, the API returns `kind="continuous"` and `display` as the normalized live
value; this is intentionally a truthful fallback, not a claim about the
plugin's UI semantics.

## Exact orchestrator capture

1. Open a disposable FL project with **Sytrus as generator on channel 2**, and
   connect the FLMCP controller/control lane. Do not run this against a user
   project.
2. From the repository root, run:

   ```sh
   PYTHONPATH=mcp-server/src python experiments/capture_plugin_param_model.py \
     --channel 2 --out /tmp/sytrus-param-surface.json
   ```

3. Attach `/tmp/sytrus-param-surface.json` to this PR/task. It is an
   read-only full dump: index, FL name, and live normalized value for every
   parameter, plus the plugin identity.
4. To finish this research artifact, provide a similarly programmatic source
   for each parameter's displayed text, unit/range/step and options (or a
   checked-in Sytrus metadata source with its provenance). The documented
   `plugins` API cannot supply those fields, so UI scraping or guessed grids is
   not acceptable evidence.

Once that evidence exists, commit the complete table here and instantiate the
corresponding `PluginParamModel`; then use the live control lane to set one
parameter from each kind and confirm its readback.
