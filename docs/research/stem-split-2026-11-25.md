# Stem split: Spleeter 4stems in the server (2026-11-25)

`fl.separate(audio)` splits an audio file into vocals, drums, bass and other, one WAV each.
Before it, a model that wanted stems installed demucs with pip inside the song: about 1 GB of downloads and about 40 minutes on the CPU.

## The model and its licence

The model is Deezer's Spleeter 4stems, the `4stems.tar.gz` asset of https://github.com/deezer/spleeter/releases/tag/v1.4.0.
Its SHA-256 is `3adb4a50ad4eb18c7c4d65fcf4cf2367a07d48408a5eb7d03cd20067429dfaa8`, the value in Spleeter's own `checksum.json`, and the downloaded file matched it.
The archive holds the TensorFlow checkpoint only (`checkpoint`, `model.data-00000-of-00001`, `model.index`, `model.meta`), with no licence file.
The repository's LICENSE is MIT, "Copyright (c) 2019-present, Deezer SA.".
The JOSS paper (doi:10.21105/joss.02154) says the "source code and pre-trained models" are "distributed under a MIT license", so the weights are covered too.
NOTICE.md carries the licence text, as MIT asks of copies.
Demucs v4 (htdemucs) separates better, but its weights are for research only, so it was not used.

## The export

`scripts/export_stem_model.py` loads the checkpoint into Spleeter's own U-Net code (spleeter 2.4.2, TensorFlow 2.12, Python 3.11), freezes the four U-Nets into one graph and converts it with tf2onnx (opset 17).
The input is the mix's magnitude spectrogram, (1, 512, 1536, 2): Spleeter's 4stems-16kHz setting, which reads the same checkpoint up to 16.5 kHz instead of 11 kHz.
The file, `spleeter-4stems.onnx`, is 157,214,482 bytes, float32, SHA-256 `cc2078ed3a669b183c56f7e70fa6ba7ef9d9fee120a8539b78c2b50fed184cdd`.
On a random mix-like input, the ONNX outputs differ from TensorFlow's by at most 1.1e-5 of the largest estimate.

## The pipeline around it

`stem_model.py` reproduces Spleeter's STFT, ratio masks and inverse STFT in numpy.
Above 16.5 kHz each stem takes the average of its mask below ("average", one of Spleeter's two settings), so the masks sum to one everywhere and the stems sum back to the mix.
Spleeter cuts a long file into 11.9 s blocks with hard edges; `stem_model.py` uses 11.8 s windows that overlap by a quarter and crossfade, so memory stays at one window's.
After each window it checks the run's time budget and stops, writing nothing, when the rest would not finish.

## Measurements

All on one 2-vCPU Xeon 8375C machine with 8 GB, onnxruntime on the CPU, the engine's own `stem_model.write` writing 16-bit WAVs.
The test mixes were made for this test and hold no recorded music: a formant-synthesised voice singing 4 s and resting 4 s at 120 BPM, a synthesised drum kit, a saw bass and plucked chords, each kept as its true stem.

| Clip | Model load | Split | Whole process | Peak memory |
|---|---|---|---|---|
| 10 s loop | 0.7 s | 1.5 s | 2.7 s | 0.9 GB |
| 3 min track | 0.4 s | 30.0 s | 35.5 s | 1.1 GB |

That is about 10 s per minute of audio.
Spleeter's own separation (TensorFlow) took 7.5 s and 35.2 s on the same clips, and its process peaked at 5.7 GB.
On disk the model is 157 MB; onnxruntime was already installed for `fl.sounds`.

Updated 2026-11-26, on a 2-vCPU, 8 GB Windows box with FL running beside it, on similar synthesised mixes: a 3 min track took 54 s (about 18 s per minute) and a 10 s loop 2.5 s, but the first call took 39 s, most of it loading the model from a disk just restored from a snapshot.
The server process peaked at 1.1 GB.

## Quality, on the test mixes only

| Check | 10 s loop | 3 min track |
|---|---|---|
| Stems summed against the mix | 68.7 dB, largest error 9.2e-5 | 68.5 dB, largest error 9.2e-5 |
| Against Spleeter's own output, same setting | 62 to 79 dB | 6 to 20 dB |
| Vocals while the voice rests, below the sung part | 59 dB | 56 dB |

The remaining difference between the summed stems and the mix is the rounding of each stem to 16 bits.
On the loop, which fits one window, the stems match Spleeter's to within float rounding.
On the track they differ where Spleeter's hard block edges and these crossfades differ, and each stem's distance from its true stem is the same as Spleeter's or better:

| Stem | This, loop | Spleeter, loop | This, track | Spleeter, track |
|---|---|---|---|---|
| Vocals | 3.9 dB | 3.9 dB | 1.9 dB | 1.9 dB |
| Drums | 1.9 dB | 1.9 dB | 1.9 dB | 1.9 dB |
| Bass | 11.1 dB | 11.1 dB | 10.5 dB | 10.5 dB |
| Other | 2.1 dB | 2.1 dB | 3.3 dB | 2.6 dB |

These are signal-to-distortion ratios against the true stems.
They are low for vocals, drums and other because a synthesised voice and kit are far from the music Spleeter was trained on; they say the pipeline matches Spleeter, not how well it splits real songs.
Spleeter's default for 4stems, which drops everything above 11 kHz, gave the drums 1.1 dB on the loop and 1.0 dB on the track: the hi-hats live up there.

## Not checked

- How well it splits real music; no recorded music was downloaded for this test.
