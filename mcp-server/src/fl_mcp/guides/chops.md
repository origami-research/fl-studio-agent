---
name: chops
description: Chop, stretch, reverse or re-pitch a sample in Python and play it.
---

A sampler plays whatever WAV you give it, so any sample edit is a few lines of numpy or librosa and a `fl.load_sample` of the file you wrote.

How it plays, and why it matters:

- A note plays the sample from its start, at its own pitch on C4 (MIDI 60), resampled on other keys.
- Some channels stop the sample when the note ends, so write each note as long as the sample.
- Cut on the beat grid: one step is `60 / fl.tempo` seconds per beat, so chops line up with the song.
- `librosa.effects.time_stretch` changes length without pitch and `librosa.effects.pitch_shift` pitch without length; a reversed slice (`x[::-1]`) makes a riser or a swell.

Render a bar of the result and listen before building on it (see `fl.guide("listening")`).

```python
import numpy as np, soundfile

vocal = fl.sounds("vocal chop", kind="sample", limit=1)[0]
x, sr = fl.read_wav(vocal)
step = int(sr * 30 / fl.tempo)  # an eighth note
chops = [x[i * step:(i + 1) * step] for i in range(max(1, len(x) // step))]
bar = np.concatenate([chops[i % len(chops)] for i in (0, 0, 3, 1, 2, 2, 5, 1)])
soundfile.write("chop.wav", bar, sr)  # bar[::-1] reverses it
loaded = fl.load_sample("Sampler 1", "chop.wav")  # FL renames the channel "chop"
beats = len(bar) / sr * fl.tempo / 60
track = fl.track(loaded.channel)
track.write([Note(60, b, beats) for b in range(0, 16, 4)], mode="replace")
print(loaded.name, track.notes()[:2])
```
