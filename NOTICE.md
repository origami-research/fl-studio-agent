# Notice

fl-studio-agent is MIT licensed ([LICENSE](LICENSE)).
This file records third-party content the engine downloads or refers to, its licence, and where it comes from.

## FL Studio's stock content

The agent project and `fl.sounds` refer to samples and presets that FL Studio installs in its own `Data/Patches` folder, by path.
None of that content is copied into this repository or downloaded by the engine; it stays in the user's FL Studio install under Image-Line's licence.
The sound index fl-mcp-install downloads holds, per stock sample and instrument preset, its path, a CLAP embedding (512 numbers) and six measurements, computed from that content on a machine with FL; it holds no audio, and neither the sounds nor any part of them can be rebuilt from it ([note](docs/research/sound-search-2026-11-13.md)).

## The sound model

`fl-mcp-install` downloads the CLAP model `fl.sounds` ranks with, exported to ONNX by `scripts/export_sound_model.py` (the pinned files and their SHA-256 are in `mcp-server/src/fl_mcp/assets/sound-search.json`).

- Checkpoint: LAION's `music_audioset_epoch_15_esc_90.14.pt` from https://huggingface.co/lukewys/laion_clap at revision `b3708341862f581175dba5c356a4ebf74a9b6651`, trained by LAION on music, AudioSet and LAION-Audio-630K.
- Licence: CC0 1.0 Universal (the model repository's licence); LAION's code, https://github.com/LAION-AI/CLAP, is CC0 1.0 too.
- Tokenizer: RoBERTa's `tokenizer.json` from https://huggingface.co/FacebookAI/roberta-base at revision `e2da8e2f811d1448a5b465c236feacd80ffbac7b`, MIT licence (Copyright (c) Facebook, Inc. and its affiliates).
- Paper: Wu, Chen, Zhang, Hui, Berg-Kirkpatrick, Dubnov, "Large-scale Contrastive Language-Audio Pretraining with Feature Fusion and Keyword-to-Caption Augmentation", ICASSP 2023.
- Changes: the text and audio encoders with their projections were exported separately, with unit-length outputs, and their weights quantized to int8 (the audio encoder's log-mel front end kept in float).

## CC0 sample packs

`fl-mcp-install` downloads a curated subset of two sample libraries by Versilian Studios.
Each file comes unmodified from a pinned upstream commit and is checked against its SHA-256 in `mcp-server/src/fl_mcp/assets/sound-packs.json`.
Files are renamed and filed into folders so that a producer can find them by name; their audio content is unchanged.
No Freesound content is included.

### VCSL (Versilian Community Sample Library)

- Upstream: https://github.com/sgossner/VCSL
- Pinned commit: `c1ea7bcc3c7309650ab0da9d15c9cd1fbc4a4c7e`
- Licence: CC0 1.0 Universal (public domain dedication).
- Licence text: https://github.com/sgossner/VCSL/blob/c1ea7bcc3c7309650ab0da9d15c9cd1fbc4a4c7e/LICENSE
- Root notes were read from the upstream SFZ mappings on the `sfz` branch (commit `dfcf4a4918771eee884b96ad4493de82ef84daf6`).

### VSCO 2 CE (Versilian Studios Chamber Orchestra 2: Community Edition)

- Upstream: https://github.com/sgossner/VSCO-2-CE
- Pinned commit: `440300901dfe9275fd84e0b7763af1f8443ae62e`
- Licence: CC0 1.0 Universal (public domain dedication).
- Licence text: https://github.com/sgossner/VSCO-2-CE/blob/440300901dfe9275fd84e0b7763af1f8443ae62e/LICENSE
- Root notes were read from the upstream SFZ mappings on the `SFZ` branch (commit `6dd651d55dde97fd4028699be9d4481f26917891`).
- The upstream readme asks, without making it a licence condition, that users credit Versilian Studios / Sam Gossner (and Ivy Audio / Simon Dalzell where applicable), link to the VSCO: CE homepage, and not sell the samples on their own.

Samples recorded by Sam Gossner and Simon Dalzell, sample cutting by Elan Hickler (Soundemote), via Versilian Studios.
