---
name: listening
description: Measuring a render, since playback cannot be heard.
---

You cannot hear playback, so you listen by rendering and measuring in code, computing whatever answers your question.

Some measurements and what they show:

- Integrated loudness (`pyloudnorm`): how loud the render sits overall.
- True peak, the sample peak after 4x oversampling: clipping that a plain peak misses between samples.
- Spectral balance, the share of energy in low, mid and high bands.
- Loudness per bar range: how the energy moves through the song.

A render plays in real time, so a few bars or one channel alone (`solo=`) costs less than the whole song; `fl.meters` takes seconds and says which channels sound.
To hear what one part adds, render the same bars with it muted (`mute=[part]`) and compare.
Audio the user gives you (in your workspace, such as `uploads/`) reads with `fl.read_wav` and measures the same way.
Numbers are cheap to read; a picture costs tokens, but a spectrogram shows a lot at once.

Reading a spectrogram: time runs left to right, pitch bottom to top, brightness is energy.
A kick shows as short vertical stripes low down, hats as specks high up, held notes as horizontal bands.
Silence where a part should be playing means it is muted, has no notes there, or never loaded.

```python
import numpy as np, pyloudnorm, scipy.signal, matplotlib.pyplot as plt

# a few bars to listen to; in a session, the song you wrote
fl.track("Kick").write(theory.drums("x...x...x...x..." * 4), mode="replace")
fl.track("Bass").write([Note(45, b * 4, 3.5, 0.8) for b in range(4)], mode="replace")
wav = fl.render("mix.wav", bars=4)
x, sr = fl.read_wav(wav)
loudness = pyloudnorm.Meter(sr).integrated_loudness(x)
true_peak = 20 * np.log10(np.abs(scipy.signal.resample_poly(x, 4, 1, axis=0)).max())
spectrum = np.abs(np.fft.rfft(x.mean(axis=1))) ** 2
hz = np.fft.rfftfreq(len(x), 1 / sr)
bands = {name: spectrum[(hz >= lo) & (hz < hi)].sum() / spectrum.sum()
         for name, lo, hi in (("low", 0, 150), ("mid", 150, 5000), ("high", 5000, sr / 2))}
print(f"{loudness:.1f} LUFS, true peak {true_peak:.1f} dBTP", {k: round(v, 2) for k, v in bands.items()})

# the bass alone against the kick, then a spectrogram with bar lines
spec = lambda w: np.abs(np.fft.rfft(fl.read_wav(w)[0].mean(axis=1)))
bass, kick = (spec(fl.render(f"{c}.wav", bars=2, solo=[c])) for c in ("Bass", "Kick"))
plt.figure(); plt.loglog(bass, label="bass"); plt.loglog(kick, label="kick"); plt.legend()
plt.figure(); plt.specgram(x.mean(axis=1), Fs=sr, NFFT=4096, cmap="magma")
for t in np.arange(0, wav.span.seconds, 240 / wav.span.tempo_bpm):
    plt.axvline(t, c="w", lw=0.5)
```
