# 11 — FL Studio will not load third-party native plugins (macOS)

**This invalidates the Bridge plugin design in [04](04-native-plugin.md).** The
architecture assumed a native effect could be installed alongside Image-Line's
own, get an Idle callback, and apply note edits with no user action. It cannot —
FL never loads the binary at all.

## What was tried

Everything the format appears to require, matched against Image-Line's own
effects byte for byte:

| Step | Result |
|---|---|
| Build a universal arm64+x86_64 dylib exporting exactly `CreatePlugInstance` and `SetExternalAppHandle` | matches `Fruity Big Clock_x64.dylib` exactly (verified with `nm -gU`, `lipo`) |
| Install as `Plugins/Fruity/Effects/<Name>/<Name>_x64.dylib` + `Plugin.nfo` | same layout as every stock effect |
| Confirm the dylib is loadable | `dlopen` + `dlsym` succeed outside FL |
| Confirm FL permits unsigned third-party code | it ships `com.apple.security.cs.disable-library-validation` |
| Generate a project referencing it via `PluginID.InternalName` | project opens (RSS 290 MB, identical to the stock template it was derived from) |
| Register it in the plugin database as `Installed/Effects/Fruity/<Name>.nfo` | replicated field-for-field from `Emphasizer.nfo` |

## The decisive measurement

The plugin was instrumented with a load trace at three stages — a
`__attribute__((constructor))` that fires at `dlopen`, an entry stamp in
`CreatePlugInstance`, and one at the end of the constructor — each writing to
`/tmp`, which no sandbox restricts.

**No trace file ever appeared.** Not even the `dlopen` one, across a 90-second
launch with the project open.

That distinguishes the failure precisely. FL is not failing to *instantiate* the
plugin, and it is not failing to *write* its heartbeat. It never loads the
binary. `Plugins/Fruity/Effects` is effectively reserved for Image-Line's own
set; being on disk in the right shape with the right registry entry is not
enough.

Without this instrumentation the symptom was an unexplained heartbeat timeout,
which is indistinguishable from a dozen other causes. Adding the trace was worth
more than any further guessing.

## What this does not tell us

- Whether Windows behaves the same. The one public report of this working
  ([BeatSage](https://beatsage.ai/blog/fl-studio-midi-breakthrough)) is
  Windows-only, and their plugin may have been registered by a mechanism not
  visible here.
- Whether some additional signing, vendor registration, or internal allowlist
  would make FL accept it. Nothing in the SDK documentation mentions one.

`FHD_AddNotesToPR` itself remains untested — the question Spike 1 was built to
answer is still open, because no third-party plugin can get far enough to call
it on this platform.

## Consequences

- **The note plane cannot be a native plugin on macOS.** [04](04-native-plugin.md)
  is blocked, not merely unbuilt.
- **The piano roll `.pyscript` remains the only working way to write notes**, and
  it can only be started from FL's Scripting menu.
- So the autonomous trigger has to come from outside FL. The remaining option is
  macOS UI scripting — sending the shortcut bound to the script
  (`harness/trigger.py`). That needs a one-time Accessibility/Automation grant
  and briefly steals window focus, which is exactly why the native plugin was
  preferred. A working path beats a blocked one.

## If someone wants to revisit this

Worth trying, roughly in order of cost:

1. Repeat the load-trace experiment on Windows — cheap, and settles the platform
   question.
2. Ask Image-Line directly whether third-party native plugins are supported.
   The SDK is published; the loading rules are not.
3. Wrap the FL SDK plugin as a VST and check whether `TFruityPlugHost` is still
   reachable. Almost certainly not — a VST gets the VST host interface — but it
   would be quick to disprove.

The load trace stays in `native/bridge/src/bridge.cpp`; re-running it is the
first thing any retry should do.
