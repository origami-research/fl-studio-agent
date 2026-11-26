---
name: chops
description: Editing a sample in Python and playing it on a channel.
---

A sampler plays whatever WAV you give it, so any sample edit is a few lines of numpy or librosa and a `fl.load_sample` of the file you wrote.
`fl.separate` splits a recording, such as one the user attached, into vocals, drums, bass and the rest, one WAV each, to chop or load like any other.

`help(fl.load_sample)` says how a sampler plays the file.

- One beat is `60 / fl.tempo` seconds, so cuts on that grid line up with the song.
- `librosa.effects.time_stretch` changes length without pitch and `librosa.effects.pitch_shift` pitch without length; `x[::-1]` reverses a slice.

A short render lets you hear the result (see `fl.guide("listening")`).

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
