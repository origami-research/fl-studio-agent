"""fl.sounds ranks by sound: the CLAP encoders (sound_model), the sound index (sound_index),
the ranking (sounds.search), its install (fl/sound_search) and the SDK's ``like``.

The real model is 200 MB and is not in the test environment, so the encoders are faked
here; what the model computes was checked against the reference implementation when it
was exported (docs/research/sound-search-2026-11-13.md). These tests pin everything
around it: the model's input, the tokenizer's algorithm, the index file, the order.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
from importlib.resources import files
from pathlib import Path

import numpy as np
import pytest
from session_fakes import make_session

from fl_mcp import errors, sdk, sound_index, sound_model
from fl_mcp import sounds as sounds_mod
from fl_mcp.fl import sound_search
from fl_mcp.kernel import wire
from fl_mcp.schemas import Sound

KICK = "Packs/Drums/Kicks/808 Kick.wav"
SNARE = "Packs/Drums/Snares/909 Snare.wav"
PAD = "Plugin presets/Generators/Sytrus/Pads/Warm Pad.fst"
HALL = "Plugin presets/Effects/Fruity Reeverb 2/Big Hall.fst"
NEW = "Packs/Drums/Kicks/New Kick.wav"  # on disk, not in the index (newer than it)
CLAP = "CC0/VCSL/Hand Percussion/Clap 1.wav"


def unit(i: int) -> np.ndarray:
    v = np.zeros(512, np.float32)
    v[i] = 1.0
    return v


class FakeEncoder:
    """Stands in for the ONNX encoders: a query's words pick its embedding."""

    def __init__(self) -> None:
        self.audio_calls: list[tuple[int, int]] = []

    def text(self, query: str) -> np.ndarray:
        return unit(0) * 0.6 + unit(1) * 0.8 if "kick" in query else unit(2)

    def audio(self, samples: np.ndarray, sample_rate: int) -> np.ndarray:
        self.audio_calls.append((len(samples), sample_rate))
        return unit(2)


MEASURES = {"seconds": 0.5, "pitch": None, "decay": 0.3, "brightness": 900.0, "low": 0.6, "high": 0.01}


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A small library on disk and an index of its samples and its instrument preset."""
    stock, packs, search = tmp_path / "Patches", tmp_path / "sounds", tmp_path / "sound-search"
    for root, relative in [(stock, p) for p in (KICK, SNARE, PAD, HALL, NEW)] + [(packs, CLAP)]:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"")
    search.mkdir()
    rows = [
        (KICK, unit(1), MEASURES),  # the kick: closest to "kick"
        (SNARE, unit(0), MEASURES | {"pitch": 62.0}),
        (PAD, unit(2), MEASURES | {"seconds": 6.0}),
        (CLAP, (unit(1) + unit(2)) / np.sqrt(2), MEASURES),
    ]
    sound_index.write(search / sound_index.INDEX_FILE, rows, {"fl_build": "test"})
    encoder = FakeEncoder()
    monkeypatch.setattr(sound_model, "encoder", lambda folder: encoder)
    monkeypatch.setattr(sounds_mod, "fl_patches_dir", lambda: stock)
    monkeypatch.setattr(sounds_mod, "sound_library_dir", lambda: packs)
    monkeypatch.setattr(sounds_mod, "sound_search_dir", lambda: search)
    return sounds_mod.library(), encoder


# -- ranking ----------------------------------------------------------------------------


def test_a_query_ranks_every_indexed_sound_by_how_it_sounds(lib):
    library, _ = lib

    found = sounds_mod.search(library, "punchy kick", limit=4)

    # "kick" -> 0.6 * e0 + 0.8 * e1: the kick (e1), the snare (e0), the clap (e1 and e2), the pad (e2);
    # the kick's path also holds one of the two words: half the name weight on top.
    assert [s.path for s in found] == [KICK, SNARE, CLAP, PAD]
    assert [s.score for s in found] == [0.85, 0.6, round(0.8 / np.sqrt(2), 3), 0.0]
    assert found[0].decay == 0.3 and found[0].pitch is None and found[1].pitch == 62.0


def test_a_sound_the_index_does_not_hold_matches_by_name_first(lib):
    """An effect preset or a file newer than the index cannot be ranked by sound; its name
    holding every word still finds it, ahead of the ranked ones, with no score."""
    library, _ = lib

    found = sounds_mod.search(library, "new kick", limit=3)
    assert found[0].path == NEW and found[0].score is None and found[0].seconds is None
    assert [s.path for s in found[1:]] == [KICK, SNARE]
    assert sounds_mod.search(library, "big hall", kind="preset", limit=1)[0].path == HALL


def test_a_name_that_holds_the_words_lifts_a_sound_by_whole_words_only():
    """CLAP does not know "808"; FL's folders do. A word counts where it begins a word of the
    path, so "pad" names "Pads/Warm Pad" and not "Drumpad"."""
    assert sounds_mod._named(["808", "kick"], KICK) == 1.0
    assert sounds_mod._named(["pad"], PAD) == 1.0
    assert sounds_mod._named(["pad"], "Plugin presets/Generators/Drumpad/Kicks/Hard.fst") == 0.0
    assert sounds_mod._named(["phonk", "cowbell"], "Packs/Drums/Percussion/808 Cowbell.wav") == 0.5


def test_kind_and_plugin_keep_one_kind_of_sound(lib):
    library, _ = lib

    assert [s.path for s in sounds_mod.search(library, "kick", kind="preset")] == [PAD]  # the effect has no sound
    assert [s.path for s in sounds_mod.search(library, "kick", plugin="sytrus")] == [PAD]
    assert all(s.kind == "sample" for s in sounds_mod.search(library, "kick", kind="sample"))


def test_like_a_library_sound_uses_its_row_and_leaves_it_out(lib):
    library, encoder = lib

    like = sounds_mod.reference_embedding(library, PAD)
    found = sounds_mod.search(library, like=like, exclude=PAD, limit=2)

    assert [s.path for s in found] == [CLAP, KICK]  # the clap shares the pad's e2
    assert encoder.audio_calls == []  # an indexed sound is not embedded again


def test_like_an_audio_file_embeds_it_here(lib):
    library, encoder = lib

    like = sounds_mod.reference_embedding(library, audio=(np.zeros((480, 2)), 48000))

    assert sounds_mod.search(library, like=like, limit=1)[0].path == PAD
    assert encoder.audio_calls == [(480, 48000)]


def test_without_an_index_a_query_matches_by_name_and_like_is_refused(lib):
    library, _ = lib
    (library.search / sound_index.INDEX_FILE).unlink()

    found = sounds_mod.search(library, "kick")
    assert [s.path for s in found] == [KICK, NEW] and all(s.score is None for s in found)
    with pytest.raises(ValueError, match="run fl-mcp-install"):
        sounds_mod.reference_embedding(library, KICK)
    with pytest.raises(ValueError, match="sound index is not installed"):
        sounds_mod.search(library, like=unit(0))


def test_without_the_model_a_query_matches_by_name(lib, monkeypatch):
    """No onnxruntime (Intel macOS) or no model files: name matches, said by score None."""
    library, _ = lib

    class Missing:
        def text(self, query):
            raise sound_model.ModelMissing("no model")

    monkeypatch.setattr(sound_model, "encoder", lambda folder: Missing())
    found = sounds_mod.search(library, "kick")
    assert [s.path for s in found] == [KICK, NEW] and found[0].score is None
    assert found[0].decay == 0.3  # the index still describes it


def test_the_session_searches_like_a_file_the_kernel_sent(lib):
    """like_audio is a file's bytes from the kernel, decoded and embedded in the server."""
    _, encoder = lib
    session, _ = make_session()
    wav = _wav_bytes(np.zeros(4800, np.int16), 48000)

    found = session.sounds(like_audio=wav, limit=1)
    assert found[0].path == PAD and encoder.audio_calls == [(4800, 48000)]
    with pytest.raises(errors.ToolError, match="not an audio file") as raised:
        session.sounds(like_audio=b"nope")
    assert raised.value.category == "invalid_argument"
    with pytest.raises(errors.ToolError, match="has no sound in the index"):
        session.sounds(like_path=HALL)


def _wav_bytes(frames: np.ndarray, rate: int) -> bytes:
    import io
    import wave

    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames.tobytes())
    return out.getvalue()


# -- the SDK and the kernel wire --------------------------------------------------------


def test_like_sends_a_library_path_or_the_bytes_of_a_file_you_wrote(tmp_path):
    mine = tmp_path / "audition.wav"
    mine.write_bytes(b"RIFF")
    sound = Sound(kind="sample", name="808 Kick", path=KICK)

    assert sdk.FL._reference(sound) == (KICK, None)
    assert sdk.FL._reference(KICK) == (KICK, None)
    assert sdk.FL._reference(mine) == (None, b"RIFF")
    assert sdk.FL._reference(str(mine)) == (None, b"RIFF")
    with pytest.raises(errors.ToolError):
        sdk.FL._reference(3)


def test_the_kernel_wire_carries_a_reference_and_measured_results():
    call = wire.encode_call("sounds", ("kick",), {"like_audio": b"\x00\x01", "limit": 5})
    assert wire.decode_call(call) | {"query": "kick", "like_audio": b"\x00\x01", "limit": 5} == wire.decode_call(call)
    found = [Sound(kind="sample", name="808 Kick", path=KICK, score=0.41, pitch=36.2, decay=1.2)]
    assert wire.decode_result("sounds", wire.encode_result("sounds", found)) == found


# -- the model's input and the tokenizer ------------------------------------------------


def test_the_audio_encoders_input_is_ten_seconds_at_48k_in_16_bits():
    """As the checkpoint was trained: mono 48 kHz, 16-bit, a short sound repeated to fill 10 s."""
    short = np.random.default_rng(0).standard_normal((44100, 2)) * 0.1

    x = sound_model.waveform(short, 44100)
    assert x.shape == (sound_model.CLIP_SAMPLES,) and x.dtype == np.float32
    once = sound_model.waveform(short[:, 0], 48000)
    assert np.array_equal(once, sound_model.waveform(np.tile(short[:, 0], 10), 48000))  # repeated either way
    assert np.allclose(once * 32767, np.round(once * 32767), atol=1e-3)  # on the 16-bit grid
    assert sound_model.waveform(np.full(48000 * 30, 2.0), 48000).max() == 1.0  # clipped, first 10 s
    with pytest.raises(ValueError):
        sound_model.waveform(np.zeros(0), 48000)


def test_the_tokenizer_is_byte_level_bpe_with_roberta_framing():
    """RoBERTa's algorithm on a toy vocabulary: words keep their leading space ("Ġ"),
    merges apply by rank, and the ids are framed by <s> and </s>."""
    vocab = {"<s>": 0, "</s>": 2, "k": 4, "i": 5, "c": 6, "Ġ": 7, "ki": 8, "ck": 9, "kick": 10, "Ġkick": 11, "8": 12, "0": 13}
    spec = {"model": {"vocab": vocab, "merges": ["k i", "c k", "ki ck", "Ġ kick"]}}
    tokenizer = sound_model.Tokenizer(spec)

    assert tokenizer.ids("kick kick") == [0, 10, 11, 2]
    assert tokenizer.ids("808") == [0, 12, 13, 12, 2]
    assert len(tokenizer.ids("kick " * 200)) == sound_model.MAX_TOKENS
    assert tokenizer.ids("kick " * 200)[-1] == 2


# -- the index file and its measurements -------------------------------------------------


def test_the_index_round_trips_and_says_what_it_does_not_know(tmp_path):
    path = tmp_path / sound_index.INDEX_FILE
    sound_index.write(path, [(KICK, unit(3) * 2, MEASURES)], {"fl_build": "26.1"})

    index = sound_index.load(path)
    assert index.paths == (KICK,) and index.meta["fl_build"] == "26.1"
    assert np.allclose(index.embeddings[0], unit(3))  # stored unit length
    assert index.measures_of(0) == pytest.approx(MEASURES)
    assert sound_index.load(tmp_path / "missing.npz") is None


def test_an_index_of_another_version_is_refused(tmp_path):
    path = tmp_path / sound_index.INDEX_FILE
    sound_index.write(path, [(KICK, unit(3), MEASURES)], {"version": 99})
    with pytest.raises(ValueError, match="version 99"):
        sound_index.load(path)


def test_the_measurements_read_what_a_person_hears():
    rate = 22050
    t = np.arange(rate) / rate
    tone = 0.5 * np.sin(2 * np.pi * 440 * t)
    noise = np.random.default_rng(0).standard_normal(rate) * 0.2
    hit = np.sin(2 * np.pi * 100 * t) * np.exp(-t / 0.1)  # one period per 10 ms level frame

    a440 = sound_index.measure(tone, rate)
    assert a440["pitch"] == pytest.approx(69, abs=0.2) and a440["brightness"] == pytest.approx(440, rel=0.05)
    assert a440["seconds"] == 1.0 and a440["high"] == 0
    assert sound_index.measure(noise, rate)["pitch"] is None
    assert sound_index.measure(noise, rate)["high"] > 0.4
    boom = sound_index.measure(hit, rate)
    assert boom["low"] > 0.9 and boom["decay"] == pytest.approx(0.345, abs=0.03)  # 30 dB = 3.45 time constants
    assert sound_index.measure(tone, rate, pitch=False)["pitch"] is None


# -- fl-mcp-install's download ------------------------------------------------------------


GOOD = b"onnx bytes"


@pytest.fixture
def spec(monkeypatch):
    value = {
        "version": "t",
        "fl_build": "26.1",
        "files": [{"name": "clap-text-int8.onnx", "url": "https://example.test/a", "sha256": hashlib.sha256(GOOD).hexdigest(), "size": len(GOOD)}],
    }
    monkeypatch.setattr(sound_search, "manifest", lambda: value)
    return value


def test_each_model_and_index_file_is_downloaded_and_checked(tmp_path, spec):
    urls: list[str] = []
    (tmp_path / "sound-index-old.npz").write_bytes(b"an older version's index")

    step = sound_search.install(tmp_path, fetch=lambda url: urls.append(url) or GOOD)

    assert step.status == "OK" and urls == ["https://example.test/a"]
    assert (tmp_path / "clap-text-int8.onnx").read_bytes() == GOOD
    assert not (tmp_path / "sound-index-old.npz").exists()
    again = sound_search.install(tmp_path, fetch=lambda url: pytest.fail("a current file was fetched again"))
    assert again.detail.endswith("already current")


def test_a_model_file_that_is_not_the_pinned_one_never_stays(tmp_path, spec):
    step = sound_search.install(tmp_path, fetch=lambda url: b"tampered")

    assert step.status == "WARN" and "matches by name only" in step.detail
    assert not (tmp_path / "clap-text-int8.onnx").exists()


def test_the_shipped_manifest_pins_every_file_the_search_reads():
    shipped = json.loads(files("fl_mcp").joinpath("assets", "sound-search.json").read_text(encoding="utf-8"))
    names = [entry["name"] for entry in shipped["files"]]

    assert sorted(names) == sorted([*sound_model.MODEL_FILES, sound_index.INDEX_FILE])
    for entry in shipped["files"]:
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0
        assert entry["url"].startswith("https://github.com/origami-research/fl-studio-agent/releases/download/")
        assert entry["size"] > 0
    assert Path(shipped["files"][0]["url"]).name  # a file, not a folder


def _not_found(url: str) -> bytes:
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def test_a_model_and_index_not_published_yet_leave_the_install_finished(tmp_path, spec):
    """The release is a draft until the owner publishes it (GitHub answers 404): a public
    install is finished, says fl.sounds matches by name, and does not ask the user to fix
    anything. A network failure is still a WARN that asks for a rerun."""
    step = sound_search.install(tmp_path, fetch=_not_found)

    assert step.status == "OK" and "matches by name only" in step.detail and "not published" in step.detail
    assert not (tmp_path / "clap-text-int8.onnx").exists()

    def offline(url: str) -> bytes:
        raise urllib.error.URLError("no network")

    assert sound_search.install(tmp_path, fetch=offline).status == "WARN"


def test_fl_mcp_install_exits_zero_when_sound_search_is_not_published(tmp_path, monkeypatch, capsys, spec):
    from fl_mcp.fl import install as install_mod
    from fl_mcp.fl.steps import InstallStep

    ok = InstallStep(status="OK", detail="done")
    for name in ("install_agent_project", "install_note_script", "install_controller", "configure_midi", "check_midi_ports"):
        monkeypatch.setattr(install_mod, name, lambda ok=ok: ok)
    monkeypatch.setattr(install_mod.sound_packs, "install", lambda: ok)
    real = sound_search.install
    monkeypatch.setattr(sound_search, "install", lambda: real(tmp_path, fetch=_not_found))

    install_mod.main(["--no-bridge"])

    out = capsys.readouterr().out
    assert "[OK] sound_search: fl.sounds matches by name only" in out and "Not finished" not in out


def test_the_doctor_line_says_how_fl_sounds_ranks_here(lib, monkeypatch):
    library, _ = lib

    class Ready:
        def unavailable(self):
            return None

    monkeypatch.setattr(sound_model, "encoder", lambda folder: Ready())
    assert sounds_mod.search_mode(library) == "by sound: 4 sounds indexed for FL test"
    (library.search / sound_index.INDEX_FILE).unlink()
    assert sounds_mod.search_mode(library) == f"by name only: the sound index is not installed in {library.search}"


def test_the_model_says_why_it_cannot_run(tmp_path, monkeypatch):
    encoder = sound_model.Encoder(tmp_path)
    assert "not installed" in encoder.unavailable()
    for name in sound_model.MODEL_FILES:
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(sound_model.importlib.util, "find_spec", lambda name: None)
    assert "cannot run on this machine" in encoder.unavailable()
    with pytest.raises(sound_model.ModelMissing, match="cannot run"):
        encoder.text("kick")
