# Surge XT live in FL, a spike (2026-11-13)

**Scope.** Whether the engine can play Surge XT (GPL-3, the audio it renders is the user's) live: install it on a box, load it into a channel, and choose one of its factory patches.
Runs used FL Studio 26.1.6.5639 (trial) for Windows on the dev box fl-mcp-04 (prod image), controller API 45, the engine's own session code for every FL call, and renders measured in numpy.
Probe scripts, snapshots and renders are kept outside the repository, on the operator's Mac.

**Verdict.** Not built into the engine yet: installing and loading Surge is feasible and robust, but choosing a patch works only through Surge's own preferences, with no readback of which patch loaded.
The next step is below.

## Results

| Question | What was tried | Live answer |
| --- | --- | --- |
| Install | Surge XT 1.3.4 (latest stable), `surge-xt-win64-1.3.4-setup.exe`, sha256 `6E221E05F29254508142B9E0ED76A85F22FA1B512501BEBDE571951E7EEFECCA` (unsigned, so pin the hash), as admin: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /COMPONENTS="VST3,Patches,Wavetables"` | Exit 0 in 44 s, no prompts. The VST3 lands in `Common Files\VST3\Surge Synth Team\`; 637 factory patches in `C:\ProgramData\Surge XT\patches_factory\<Category>\<Name>.fxp`, 2,371 third-party ones beside them. |
| Licence | The Surge repository and its installer | GPL-3; the patches live in that repository with no separate licence, so they may be redistributed with a link to the matching source. Vital's presets may not, so Vital was left out. |
| FL's plugin scan | Restart FL after the install; then one scan in FL's Plugin Manager | A restart finds nothing new. The scan (under 60 s, no dialogs) wrote a 398-byte `Surge XT.fst` and `.nfo` under `Plugin database\Installed\Generators\VST3\`, a `.Plugins.ini` section and a `VerifiedIDs.nfo` line; no registry keys. The `.fst` is a stateless reference: the VST3's GUID, name, path and vendor. |
| Load into a channel | `load_in_channel` (`FHD_LoadInChannel`) with that `.fst`, and with a copy of it after every database entry was deleted and FL restarted | Both put Surge XT on the channel: `plugins.getPluginName(ch, -1)` = `"Surge XT"`, the channel named after the `.fst` file's stem (so `loading.py`'s confirmation works unchanged), the same mixer track. 3.1 s for the first instance, 1.3 s after. No scan is needed. The `.vst3` binary or a Surge `.fxp` sent the same way only rename the channel. |
| FL's presets | `plugins.getPresetCount`, `plugins.getName(ch, -1, 6, i)` (`fl.presets`) | 1 preset, named `""`: Surge 1.3.4 is built without exposing its patches as programs, so `fl.presets`/`fl.set_preset` cannot pick one. |
| A wrapper preset holding a patch | "Save preset as", "Save channel state as", a project save | A trial FL greys out the first and answers the others with "Buy FL Studio", leaving an empty file. A hand-made state record (the `.fst` plus the `.fxp` payload) was ignored unframed and crashed FL's wrapper with a modal dialog length-framed: hand-built state must never be sent. |
| Surge's initial patch | Write `initialPatchName` and `initialPatchCategory` to `Documents\Surge XT\SurgeXTUserDefaults.xml`, load a fresh Surge instance, delete the file | Surge opened "Attacky, Category: Basses". Renders of the same chord, solo: Init Saw RMS 0.109 (centroid 3.2 kHz), Basses/Attacky 0.252 (1.3 kHz), Keys/DX EP 0.052 (2.8 kHz); Attacky again and with no scan matched. |
| Cost | Four idle instances | About 0.02 cores and 34 MB each; a trial FL hosts Surge with no trial mark. |

## Why it is not built yet

- **No readback.** FL cannot say which patch Surge loaded, and a wrong name or category silently gives Init Saw, so a load could report a patch that is not playing; only a render or Surge's window shows it.
- **It writes a user's file.** The initial patch is a user preference; the engine would have to rewrite it for every load and put the user's own file back afterwards, and a crash in between would leave it changed.
- **A channel already playing Surge** keeps its instance and patch when the `.fst` loads again, so a patch change needs another instrument loaded first.
- **Saving and reopening** a project with Surge could not be checked on a trial FL.

## Next step

Readback first: after a load, read Surge's parameters (`plugins.getParamValue`, which reaches any VST3 parameter) and compare them with the patch's own values from its `.fxp`, so a load is confirmed or refused; with that, the initial-patch route is safe to build behind `fl.load_preset("Surge XT/<Category>/<Name>")` and `fl.sounds` listing the factory patches.
Alternatively, one wrapper preset saved by a licensed FL with a Surge patch shows FL's own state framing, which would let the engine load a patch as FL's state without touching Surge's preferences.
The arena image needs only the installer above once the engine supports it.
