# Choosing sounds by how they sound (2026-11-13)

**Scope.** `fl.sounds` ranks every stock sample, every stock instrument preset and every CC0 pack sample by how it sounds, from a description or a reference sound, so the model can listen to a few candidates and choose; the curated kits and `fl.kit` are removed.
This note records the model chosen and its licence, the index and how it was built, the measured cost, the live results, and the open question about Image-Line's terms.

## The model

| Checkpoint | Licence of the weights | Why |
| --- | --- | --- |
| LAION's `music_audioset_epoch_15_esc_90.14.pt` (`lukewys/laion_clap`, revision `b3708341862f581175dba5c356a4ebf74a9b6651`, SHA-256 `fae3e9c0...6dedd`) | CC0 1.0 (the model repository's licence; LAION's code is CC0 1.0 too); the tokenizer is RoBERTa's (MIT) | Trained on music plus AudioSet plus LAION-Audio-630K: LAION reports it best of its released checkpoints on ESC-50 (sound effects, 90.14 % zero-shot) and on GTZAN (music, 71 %), against 89.98 % and 69 % for the music-and-speech one; our earlier research found the LAION music checkpoints track music judgments best. |

Both licences allow shipping the converted weights with attribution (NOTICE.md).
The training data (AudioSet, LAION-Audio-630K, music) is not shipped and is not redistributed by the weights.

The Hugging Face conversion of this checkpoint, `laion/larger_clap_music` (Apache-2.0), was the first choice and is broken: on transformers 4.38 and 4.57 every text embeds the same (pairwise cosine 0.999 for "punchy dry house kick", "warm analog pad slow attack", "phonk cowbell" and "a dog barking") and so does every sound (mean pairwise 0.99), so its first index ranked noise (the same five sounds for every query).
LAION's own code on the original checkpoint gives distinct embeddings (pairwise -0.18 to 0.07), as does `laion/clap-htsat-unfused`.
So the engine's model is exported from the original checkpoint with LAION's code (`scripts/export_sound_model.py`).

The two encoders run in the server with onnxruntime on the CPU, never in the run kernel.
The audio encoder carries its own log-mel front end, so it takes the 48 kHz waveform; the engine prepares that as LAION's pipeline does (mono, 16-bit, first 10 s or repeated to 10 s).
The tokenizer (RoBERTa's byte-level BPE) is the engine's own numpy-free Python, so the only new dependency is onnxruntime.

| Check (Mac, M-series) | Result |
| --- | --- |
| int8 text encoder against the float checkpoint (laion_clap) | cosine 0.966 to 0.997 on eight queries; top 5 over the 181 CC0 pack sounds the same as the float model's in 4.5 of 5 places on average over 12 queries |
| int8 audio encoder against the float checkpoint | cosine 0.9998 to 0.9999; quantizing the log-mel front end too gave 0.43 on a decaying 55 Hz sine, so it stays float |
| the engine's tokenizer against `tokenizers` on 3,003 random strings | identical ids |

## Install size and query cost

| | Size |
| --- | --- |
| Text encoder (`clap-text-int8.onnx`) | 126.5 MB |
| Audio encoder (`clap-audio-int8.onnx`) | 80.6 MB |
| Tokenizer (`clap-tokenizer.json`) | 1.4 MB |
| Sound index for FL 26.1.6.5639 | 10.1 MB |
| onnxruntime wheel | 14 MB (Windows x64), 20 MB (macOS arm64) |

A text query took 64 to 88 ms on a 4-vCPU Windows machine (the 10,546-sound index), the first one 1.6 s more to load the model; on an M-series Mac a text embedding takes 3 ms and a sound 56 ms.
Splitting every path into words for the name bonus took most of a query until it was cached (117 to 197 ms before).

onnxruntime ships no Intel macOS wheel since 1.24 (and no macOS 13 wheel); there the dependency is skipped and `fl.sounds` matches by name only, saying so with `score` None.

## The index

Built with `scripts/build_sound_index.py` on fl-mcp-04 (trial FL 26.1.6.5639 for Windows, the packaged agent project), detached and resumable through the listening runner, 2026-11-13 to 11-14.

| | Count |
| --- | --- |
| Samples: FL's `Packs` and the CC0 packs | 5,227 of 5,227 |
| Instrument presets (`Plugin presets/Generators`) | 5,319 of 5,512 |
| Presets that make no sound from notes | 184: MIDI Out 41, Fruity Envelope Controller 22, Fruity Keyboard Controller 7, Dashboard 7, Fruity Granulizer 8, Drumaxx 3, and Wasp 47 and Wasp XT 40 (silent from the default phrase; not checked further) |
| Presets FL would not load | 9: ReWired 5, Patcher 2, Sakura 1, SimSynth 1 |
| Index file | 10,546 sounds, 10.1 MB, SHA-256 `5467f612...2064131` |

A preview took about 10 s (load, phrase write, a 4.5 s live render, notes back), so the presets took about 16 hours of box time; embedding and measuring took about 0.4 s a sound.
The build found two engine bugs, fixed here: a preset FL refused left the Audition channel renamed, so every later audition failed; and SimSynth's 73 presets were refused because FL names the plugin "SimSynth Live".
Effect presets, Toxic Biohazard, GMS, Morphine and FLEX presets (their own formats) are not indexed.

## Live results

The owner's four queries, top 5 each, are on the review page with the two takes where the model chose its own sounds; the owner judges them by ear before this merges.
By eye: "punchy dry house kick" returns five stock kicks; "distorted 808" an 808 kick and long kicks; "phonk cowbell" a cowbell first, then cowbell-heavy FPC kits and rides; "warm analog pad slow attack" a pad sample first, then long SFX and string beds, the weakest of the four.

## Shipping the index

The index holds paths and numbers, none of FL's audio.
Whether it may ship publicly is an open owner decision; until then the release `sound-search-1` is a draft, and public installs match sounds by name (`fl-mcp-doctor` says so).
