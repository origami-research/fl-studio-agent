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

## Not verified live

- The SDK methods end to end: they are unit-covered against a scripted controller that answers as FL did above.
- Whether `plugins.nextPreset` wraps from the last preset to the first; `set_preset` never relies on it.
- How long a step takes on a plugin with thousands of presets: `fl.presets` reads one name per round trip.
- The location and layout of FL's stock `Data/Patches` folder that `fl.sounds` searches, on either operating system.
