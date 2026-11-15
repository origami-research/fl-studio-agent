---
name: listening
description: Measure a render for loudness, true peak, spectrum and kick against bass.
---

You cannot hear playback, so you listen by rendering and measuring in code.
The engine ships no canned analysis on purpose: compute what answers your question.

What to measure, and why:

- Loudness, integrated over the render with `pyloudnorm`, says whether the track sits where listeners expect; compare it with the target in `fl.guide("revise")`.
- True peak, the sample peak after 4x oversampling, catches the clipping a plain peak misses between samples.
- Spectral balance, the share of energy in low, mid and high bands, shows a muddy or thin mix before you look at single instruments.
- Energy per section, loudness per bar range, shows whether a drop actually lands harder than the verse.

Render only what answers the question: a render plays in real time, so a few bars or one soloed channel is cheaper than the whole song.
Render a channel alone (`solo=`) to compare two parts, such as the bass against the kick: if both peak in the same low band they mask each other, so move one in pitch or duck one.
A spectrogram with a line at every bar shows where sections change and where a part drops out.
Look at the numbers first; a picture costs tokens and is easier to misread.

Reading a spectrogram: time runs left to right, pitch bottom to top, brightness is energy.
A kick shows as short vertical stripes low down, one per hit.
Hats show as specks high up.
Pads and other held notes show as horizontal bands.
Silence where a part should be playing means something is wrong: the part is muted, has no notes there, or never loaded.
Sections should look different at their bar lines: a breakdown thins at the bottom and a drop fills it back in.
The same picture repeating for the whole song is a loop, which `fl.guide("arrangement")` fixes; little energy above 5 kHz reads as dull, a bright smear with no gaps as harsh.

Finding a buried part: first check whether it sounded at all (`fl.meters` takes seconds; on screen, watch its mixer meter).
If it did, render the same bars twice, once with the part muted (`mute=[part]`), and compare loudness and bands: when the mix barely changes, the part is buried.
Raise it, cut the part covering it in that band, or move it to a register nobody else uses.

```python
import numpy as np, pyloudnorm, scipy.signal, matplotlib.pyplot as plt

# a few bars to listen to; in a session, the song you wrote
fl.track("Kick").write(theory.drums("x...x...x...x..." * 4, key=60), mode="replace")
fl.track("Bass").write([Note(r, b * 4, 3.5, 0.8) for b, r in enumerate([45, 41, 48, 43])], mode="replace")  # A2 F2 C3 G2
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
