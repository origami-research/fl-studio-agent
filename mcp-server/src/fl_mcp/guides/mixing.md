---
name: mixing
description: How levels, panning, sends and effects are set from code.
---

How the mixer works from code.
`fl.mixer` lists the mixer tracks in use with their effects and sends, and `fl.channels` says which track each channel plays into.
The project's notes, at the top of `print(fl.scene())`, may say more about how its mixer is laid out.

- Levels and panning: `fl.track(ch).set_levels(volume=..., pan=...)` sets a channel's; FL returns the values it took.
- Sends: `fl.send(source_track, target_track, level=...)` sets how much one mixer track feeds another.
- Effects: `fl.effect(track, slot)` reaches a loaded effect; its `params("freq")` finds controls by name, and `set_param` returns a `display` ("120 Hz", "-3.0 dB") that says what you actually set.
- An effect preset loaded onto a channel with `fl.load_preset` adds that effect to the channel's mixer track.
- A limiter on the master holds peaks down, so a mix fed into it harder gets denser rather than louder; that shows as a true peak close to the loudness (`fl.guide("listening")`).

Rendering with `stems=[...]` gives each part its own WAV to measure.

```python
import pyloudnorm as pyln
# two bars to measure, on the engine's starting project; in a session, the song you wrote
parts = ["Kick", "Synth Bass", "Pad"]
fl.track("Kick").write(theory.drums("x...x...x...x..." * 2), mode="replace")
fl.track("Synth Bass").write([Note(33, b, 0.5) for b in range(8)], mode="replace")
fl.track("Pad").write([Note(k, 0, 8) for k in theory.chord("A3", "minor")], mode="replace")
print(fl.track("Pad").set_levels(volume=0.5, pan=-0.2))
track = {c.name: c.mixer_track for c in fl.channels}
reverb = next(m for m in fl.mixer if m.name == "Reverb Send")  # a send track in that project
print(fl.send(track["Pad"], reverb.index, level=0.7))
fx = next(e for m in fl.mixer for e in m.effects)  # the first effect loaded anywhere
print(fx.plugin, [(p.name, p.display) for p in fx.params()[:3]])
stems = fl.render("mix.wav", bars=2, stems=parts)
lufs = {ch: pyln.Meter(fl.read_wav(s)[1]).integrated_loudness(fl.read_wav(s)[0]) for ch, s in zip(parts, stems)}
print({ch: round(float(v - lufs["Kick"]), 1) for ch, v in lufs.items()}, "LU against the kick")
```
