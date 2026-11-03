# Mixer routing and presets through the controller API, live (2026-11-02)

**Scope.** Before wiring `fl.route`, `fl.send`, `fl.presets` and `fl.set_preset`, each FL call they use was run live.
The run used FL Studio 26.1.6.5639 (trial) on a Windows development machine, controller API version 45.
Only documented MIDI controller scripting calls were used, sent through the FLMCP controller's generic passthrough.

## Result

| Need | Call | Live answer |
| --- | --- | --- |
| Route a channel to a mixer track | `channels.setTargetFxTrack(ch, insert)` | `channels.getTargetFxTrack(ch)` read back `insert`. |
| Turn a send on or off | `mixer.setRouteTo(src, dst, on)`, then `mixer.afterRoutingChanged()` | `mixer.getRouteSendActive(src, dst)` flipped between 0 and 1. |
| Set a send's level | `mixer.setRouteToLevel(src, dst, 0.5)` | `mixer.getRouteToLevel(src, dst)` read back 0.5; a new send's level is 0.8. |
| Count a plugin's presets | `plugins.getPresetCount(ch, -1)` | 66 on FLEX. |
| List preset names | `plugins.getName(ch, -1, 6, i)` (6 is `FPN_Preset`) | Each preset's name. |
| Change preset | `plugins.nextPreset(ch, -1)`, `plugins.prevPreset(ch, -1)` | The plugin changed preset, and FL renamed the channel to the preset ("Pure Feather"). |

## What shaped the design

- FL has no getter for the current preset.
  `plugins.getName(ch, -1, 6, 0)` still returned index 0's name after a step, and the first three parameter values on FLEX did not change.
  The channel's name is the only visible readback, so `set_preset` steps from the preset the name shows and confirms by the name it ends on.
- Sampler channels (the template's 808 drums) are not plugins.
  Every `plugins.*` call on one answers "Plugin not valid", which the engine reports as "not a plugin; use load_sample".
- In the template, only the Master insert's slot 9 (the FL MCP Bridge) holds an effect plugin.
- After a cold boot, FL took about 3 to 5 minutes before controller requests were answered; the controller's hello arrived first.
- FL's own assistant tool module was not used: FL restricts it to its own assistant session.

## End to end through `run` (later the same day)

Each SDK method ran once in a `run` cell on the same machine, through the engine's MCP server and its kernel.
`fl.route`, `fl.presets` (66 FLEX presets) and `fl.set_preset` (to "Bite Me", then back to "80s Theatre") returned FL's readback with one action sentence each.
`fl.send(6, 7, level=0.5)` returned the send at 0.5, but `fl.send(6, 7, on=False)` raised after turning the send off: FL answers `mixer.getRouteToLevel` with "Index out of range" for a send that is off.
A send that is off now reads back with `level=None`, and a level with `on=False` is refused; the rerun returned `on=False level=None`.
`fl.sounds` found FL's real `Data/Patches` layout ([loading note](load-sample-preset-live-2026-11-02.md)).

## Not verified live

- Whether `plugins.nextPreset` wraps from the last preset to the first; `set_preset` never relies on it.
- How long a step takes on a plugin with thousands of presets: `fl.presets` reads one name per round trip (66 names took about 20 seconds with two preset changes).
- FL for macOS, for every call here.
