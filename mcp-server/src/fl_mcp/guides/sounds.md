---
name: sounds
description: Finding sounds by how they sound, hearing them and loading them.
---

`fl.sounds` searches FL's stock samples and instrument presets and the CC0 packs by a plain description of how something sounds, such as "punchy dry kick" or "warm pad with a slow attack", best match first.
To find sounds like one you already have, pass it as `like=` (a library sound, or a WAV you rendered or chopped).

A sound's name says little about how it sounds, so listening to a few candidates can help:

- Read a sample with `fl.read_wav`.
- Play a preset with `fl.audition`, which renders it alone and puts the song back.
- Each Sound also carries measurements (`decay`, `brightness`, `low`, `high`, `pitch`) to compare without listening.

Load a choice with `fl.load_sample` for a sample or `fl.load_preset` for an instrument preset.
A Sound's `pitch` is its MIDI note, so on a sampler `60 + wanted - pitch` plays the note you want (`help(fl.load_sample)` says why); to change the sample itself, see `fl.guide("chops")`.

```python
import numpy as np

def brightness(x, sr):
    spectrum = np.abs(np.fft.rfft(x.mean(axis=1)))
    return float((np.fft.rfftfreq(len(x), 1 / sr) * spectrum).sum() / spectrum.sum())

pads = fl.sounds("warm pad with a slow attack", kind="preset", limit=3)
heard = {p.path: brightness(*fl.read_wav(fl.audition(p))) for p in pads}
warmest = min(pads, key=lambda p: heard[p.path])
print(fl.load_preset("Pad", warmest).name)

kicks = fl.sounds("punchy dry kick", kind="sample", limit=3)
shortest = min(kicks, key=lambda k: k.decay if k.decay is not None else float("inf"))  # None: not measured
x, sr = fl.read_wav(shortest)  # a sample needs no audition
print(fl.load_sample("Kick", shortest).name, f"{len(x) / sr:.2f} s")
```
