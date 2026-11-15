---
name: sounds
description: Choose sounds by ear with search by description, audition, then load.
---

A sound chosen by its name is a guess; the listening test hears the difference.
So choose every sound by how it sounds:

1. Describe the sound you want in plain words, as a producer would: "punchy dry house kick", "warm analog pad slow attack".
   `fl.sounds` ranks FL's stock samples and instrument presets and the CC0 packs by sound, best first.
   To match something you already have, pass it as `like=` (a library sound, or a WAV you rendered or chopped).
2. Listen to the best few, not just the first: read a sample with `fl.read_wav`, and play a preset with `fl.audition`, which renders it alone and puts the song back.
3. Compare what you heard with what the part needs, using the measurements each Sound carries (`decay`, `brightness`, `low`, `high`, `pitch`) and your own from the audio.
   A kick for a busy mix wants a short decay; a pad under a vocal wants little energy above 5 kHz.
4. Load the winner: `fl.load_sample` for a sample, `fl.load_preset` for an instrument preset.

Pick sounds that suit each other and the genre before writing many notes on them; changing a sound late changes the whole mix.

Kick and bass are one decision, not two.
The kick owns the attack and the bass or 808 owns the held low notes, so match their lengths: a long, boomy kick wants a short or off-beat bass, and a long 808 wants a short, punchy kick (compare their `decay`).
Play pitched samples in key: a sample's `pitch` is its MIDI note and a sampler plays it at that pitch on MIDI 60, so write `60 + wanted - pitch`; to change the sample itself, see `fl.guide("chops")`.
Check the pair with the kick-against-bass render in `fl.guide("listening")`.

Give each register one owner at a time.
Two pads, or a pad and a Rhodes in the same octave, blur into mud: keep one, voice the other an octave away, or let them take turns by section.
A Sound's `low`, `high` and `brightness` show where it lives; two sounds that read alike will compete for the same space.
Layer only sounds that add something different, such as a bright pluck an octave above a warm pad, and check a layer by rendering with and without it.

```python
import numpy as np

def brightness(x, sr):
    spectrum = np.abs(np.fft.rfft(x.mean(axis=1)))
    return float((np.fft.rfftfreq(len(x), 1 / sr) * spectrum).sum() / spectrum.sum())

pads = fl.sounds("warm analog pad slow attack", kind="preset", limit=3)
heard = {p.path: brightness(*fl.read_wav(fl.audition(p))) for p in pads}
warmest = min(pads, key=lambda p: heard[p.path])
print(fl.load_preset("Pad", warmest).name)

kicks = fl.sounds("punchy dry house kick", kind="sample", limit=3)
shortest = min(kicks, key=lambda k: k.decay if k.decay is not None else float("inf"))  # None: not measured
x, sr = fl.read_wav(shortest)  # a sample needs no audition
print(fl.load_sample("Kick", shortest).name, f"{len(x) / sr:.2f} s")
```
