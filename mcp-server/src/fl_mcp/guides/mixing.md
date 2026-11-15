---
name: mixing
description: Optional reference on setting levels, sends, effects and panning from code.
---

What the project's mixer holds, and how code changes it.
`fl.mixer` lists the tracks in use with their effects and sends; `fl.channels` says which track each channel plays into.

- Levels and panning: `fl.track(ch).set_levels(volume=..., pan=...)`.
- Sends: the project has a Reverb Send (Fruity Reeverb 2) fed by the melodic tracks and a Delay Send (Fruity Delay Bank) fed by Lead and Pluck; `fl.send(part_track, send_track, level=...)` sets how much a track feeds one.
- Effects: `fl.effect(track, slot)` reaches a loaded effect; its `params("freq")` finds controls by name, and `set_param` returns a `display` ("120 Hz", "-3.0 dB") that says what you actually set.
- The Bass track and the Drum Bus hold Fruity Parametric EQ 2 in slot 0; loading an effect preset onto a channel with `fl.load_preset` adds one elsewhere.
- The master ends in Maximus, a limiter: a mix fed into it too hot comes out squashed rather than louder, which shows as a true peak close to the loudness (`fl.guide("listening")`).
- The 808 track starts with a lower fader because its distortion makes it much louder than the other parts.
- Code cannot set a compressor's sidechain input; `fl.guide("sidechain")` draws ducking instead.

Rendering with `stems=[...]` gives each part its own WAV to measure.

```python
import pyloudnorm as pyln
# two bars to balance; in a session, the song you wrote
fl.track("Kick").write(theory.drums("x...x...x...x..." * 2, key=60), mode="replace")
fl.track("Synth Bass").write([Note(33, b + 0.5, 0.4, 0.8) for b in range(8)], mode="replace")
fl.track("Pad").write([Note(k, 0, 8, 0.6) for k in theory.chord("A3", "minor")], mode="replace")
track = {c.name: c.mixer_track for c in fl.channels}
send = {m.name: m.index for m in fl.mixer}
for ch, volume in (("Kick", 0.8), ("Synth Bass", 0.62), ("Clap", 0.6), ("Hat", 0.45), ("Pad", 0.42)):
    fl.track(ch).set_levels(volume=volume)
fl.track("Hat").set_levels(pan=0.2)
fl.track("Perc").set_levels(pan=-0.2)
print(fl.send(track["Pad"], send["Reverb Send"], level=0.7))
print([(p.name, p.display) for p in fl.effect(track["Bass"], 0).params("freq")[:3]])
parts = ["Kick", "Synth Bass", "Pad"]
stems = fl.render("mix.wav", bars=2, stems=parts)
lufs = {ch: pyln.Meter(fl.read_wav(s)[1]).integrated_loudness(fl.read_wav(s)[0]) for ch, s in zip(parts, stems)}
print({ch: round(float(v - lufs["Kick"]), 1) for ch, v in lufs.items()}, "LU against the kick")
```
