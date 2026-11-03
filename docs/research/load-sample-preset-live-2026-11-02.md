# Loading samples and presets into channels, live (2026-11-02)

**Scope.** `fl.load_sample` and `fl.load_preset` load a stock file into a channel through the Bridge's `load_in_channel` command, which makes FL's documented host call `FHD_LoadInChannel` (plugin SDK id 43).
Each behaviour below was observed on FL Studio 26.1.6.5639 (trial) for Windows, controller API 45, on the template project, then built into the engine's readback.
FL's id-43 handler was also read statically in both engines (native/bridge/BRIDGE_FINDINGS.md, "FHD_LoadInChannel").

## What FL does

| Load | Before | After (FL's readback) |
| --- | --- | --- |
| `808 Cowbell.wav` into sampler channel 1 | "808 Clap", sampler | "808 Cowbell", sampler; the channel settings show `808 Cowbell.wav`, and FL's hint bar says "Channel sample was changed" |
| `808 Kick.wav` into a sampler while another channel is "808 Kick" | "808 CH", sampler | "808 Kick #3" (FL numbers a name that is taken) |
| A WavPack sample (`Attack Clap 01.wv`) into a sampler | sampler | renamed "Attack Clap 01", sampler |
| `808 Kick.wav` into a 3x Osc channel | "Default", 3x Osc | "808 Kick #2", still 3x Osc: only the name changed |
| Sytrus preset `3o3ish 2.fst` into the FLEX channel | "FLEX Bass", FLEX | "3o3ish 2", Sytrus, same mixer track (Insert 5) |
| 3x Osc preset `Default.fst` into sampler channel 3 | "808 Snare", sampler | "Default", 3x Osc, same mixer track (Insert 4) |
| A second Sytrus preset into the Sytrus channel | "3o3ish 2" | "3o3ish 3", Sytrus |
| Fruity Reeverb 2 preset into sampler channel 2 (mixer track 3) | slots empty | the channel is unchanged; mixer track 3 slot 0 holds Fruity Reeverb 2 |
| Then Reeverb 2, Delay 3 and Limiter presets into the same channel | slot 0 used | slots 1, 2 and 3 fill in order; nothing is replaced |
| An EQ preset into channel 1 while mixer track 7 is selected | track 7 selected | the EQ lands on track 2, the channel's own; FL reports it as "Fruity parametric EQ 2" |

So a preset for a different plugin swaps the channel's instrument, keeps its mixer track, and names the channel after the preset.
That is the way to change a channel's instrument, including turning a sampler into a plugin channel.
An effect preset adds an effect, in the next free slot of the mixer track the channel plays through, so effects can be added live.
FL opens the new plugin's window (not a dialog); a trial FL titles some plugins "TRIAL VERSION".
Loading a plugin took 2.2 to 3.2 seconds before the Bridge acknowledged; a sample took about a quarter second.

## The call's arguments

- **The file name** is read as NUL-terminated UTF-8 on both platforms.
  The first build sent UTF-16 on Windows, where Delphi's `PChar` is UTF-16; FL acknowledged and loaded nothing, because its handler reads the first byte, measures a byte string, and decodes it with code page 65001.
  A build sending UTF-8 loaded on the first try.
- **Index, the call's "mode"**, does nothing: FL's handler never reads it on either platform, and 0, 1, 2 and -1 loaded alike live.
  The Bridge passes 0.
- **The channels:** FL loads into every selected channel.
  The engine selects exactly the target with the controller's `select_channels` and reads FL's selection back: `channels.selectedChannel(1, 0)` and `channels.selectedChannel(1, 1)` must both be the target.
  With channels 1 and 3 selected, `selectedChannel(1, 1)` answered 3; with only channel 2 selected, it answered 2.

## How the engine confirms a load

- A sample: the channel is still a sampler (`channels.getChannelType` is 0) and is named after the file, or that name with ` #N`.
  A plugin channel is refused before anything changes.
- An instrument preset: `plugins.getPluginName(channel, -1)` is the preset folder's plugin, compared without case, and the channel is named after the preset (the plugin alone cannot confirm a load on a channel that already plays it).
- An effect preset: the channel's mixer track (`channels.getTargetFxTrack`) gains exactly one effect, of the preset's plugin, between its slots read before and after (`mixer.isTrackPluginValid`, `plugins.getPluginName(track, slot)`).
  A full mixer track is refused first.
- VST presets are refused: which plugin and role they load as was not checked.

## Stock sounds

`fl.sounds` found FL's real `Data/Patches` layout on Windows: `Plugin presets/<Generators|Effects|VST>/<plugin>/...` and `Packs/...`.
`808 Cowbell` and `808 Kick` resolve to `Packs/Drums/Percussion/808 Cowbell.wav` and `Packs/Drums/Kicks/808 Kick.wav`.
The install holds 7,771 presets, 3,035 WAV, 1,954 WavPack, 278 OGG, 227 FLAC and 2 MP3 files; FLEX's own presets ship as `.flexpack` packs, which `fl.sounds` does not list.

## Not verified live

- FL for macOS: its handler was read statically (UTF-8, Index unused, every selected channel), but no load ran there.
- A sample into plugins that take samples themselves (Slicex, FPC, DirectWave): refused, since the channel is not a sampler.
- What FL does when a mixer track's ten slots are full: the engine refuses first.
- VST presets and `Channel presets/`.
