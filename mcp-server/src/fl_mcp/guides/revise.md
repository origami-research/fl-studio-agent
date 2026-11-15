---
name: revise
description: Revise in a loop against a reference track or a loudness target.
---

Agents fail mostly on long chains of edits made without checking, and revising against a reference raises the pass rate.
So revise in a loop, one change at a time:

1. Render, and measure it the way `fl.guide("listening")` does: loudness, true peak, spectral balance, energy per section.
2. Measure the target the same way.
   The target is a reference file the brief or the user gives (in your workspace, such as `uploads/`).
   With no reference, use streaming norms: about -14 LUFS integrated, true peak below -1 dBTP.
3. Fix the single biggest gap between the two, with one musical change.
4. Render again and measure; keep the change only if the gap shrank.

Write each pass down (change, expected, measured, verdict) in your notes file, so a follow-up or a lost kernel does not repeat a fix that did not work.
Stop when the remaining gaps are smaller than what the brief cares about; numbers support your ear, they do not replace the brief.

```python
import numpy as np, pyloudnorm, soundfile

def measure(x, sr):
    x = x if x.ndim == 2 else x[:, None]
    spectrum = np.abs(np.fft.rfft(x.mean(axis=1))) ** 2
    hz = np.fft.rfftfreq(len(x), 1 / sr)
    return {"lufs": pyloudnorm.Meter(sr).integrated_loudness(x),
            "low": spectrum[hz < 150].sum() / spectrum.sum(),
            "high": spectrum[hz > 5000].sum() / spectrum.sum()}

# a few bars to listen to; in a session, the song you wrote
fl.track("Kick").write(theory.drums("x...x...x...x..." * 4, key=60), mode="replace")
fl.track("Bass").write([Note(r, b * 4, 3.5, 0.8) for b, r in enumerate([45, 41, 48, 43])], mode="replace")  # A2 F2 C3 G2
# a reference from the user would be read with soundfile.read("uploads/reference.wav")
target = {"lufs": -14.0, "low": 0.35, "high": 0.08}
now = measure(*fl.read_wav(fl.render("pass1.wav", bars=4)))
gaps = {k: now[k] - target[k] for k in target}
biggest = max(gaps, key=lambda k: abs(gaps[k]) / (10 if k == "lufs" else 0.1))
print({k: round(v, 2) for k, v in now.items()}, "fix first:", biggest, round(gaps[biggest], 2))
```
