"""The render check's loudness: render_wav refuses a recording at or under -60 LUFS as
silence, so this meter must read BS.1770 levels, refuse a truncated file, and stay small
in memory for a long song. And read_wav, the samples model code measures."""

from __future__ import annotations

import subprocess
import sys
import wave

import numpy as np
import pytest

from fl_mcp.audio import decode_audio, integrated_lufs, read_wav


def _write(path, samples, sample_rate):
    frames = samples if samples.ndim == 2 else samples[:, None]
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(frames.shape[1])
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes((np.clip(frames, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def test_loudness_of_a_mono_a440(tmp_path):
    sample_rate = 48000
    samples = 0.1 * np.sin(2 * np.pi * 440 * np.arange(sample_rate * 2) / sample_rate)

    assert integrated_lufs(_write(tmp_path / "a4.wav", samples, sample_rate)) == pytest.approx(-23.7, abs=0.3)


def test_empty_wav_raises_clear_boundary_error(tmp_path):
    path = tmp_path / "empty.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)

    with pytest.raises(ValueError, match="no complete audio frames"):
        integrated_lufs(path)


def test_wav_shorter_than_its_header_is_refused(tmp_path):
    # The Bridge writes the header last from its own frame counter, so a failed write (disk
    # full) leaves a header claiming frames that are not on disk; that is not a take.
    sample_rate, claimed, present = 48000, 131072, 33768
    samples = 0.25 * np.sin(2 * np.pi * 220 * np.arange(claimed) / sample_rate)
    path = _write(tmp_path / "truncated.wav", np.repeat(samples[:, None], 2, axis=1), sample_rate)
    data_start = path.stat().st_size - claimed * 4
    path.write_bytes(path.read_bytes()[: data_start + present * 4])

    with pytest.raises(ValueError, match=f"claims {claimed} frames but only {present} are present"):
        integrated_lufs(path)


@pytest.mark.parametrize("sample_rate", [48000, 44100])
def test_a_many_block_render_keeps_its_loudness(tmp_path, sample_rate):
    # 9 s spans many blocks: filter state, partial hops and resampling carry over.
    tone = 0.1 * np.sin(2 * np.pi * 997 * np.arange(9 * sample_rate) / sample_rate)
    path = _write(tmp_path / "tone.wav", np.stack([tone, tone], axis=1), sample_rate)

    assert integrated_lufs(path) == pytest.approx(-20.0, abs=0.1)  # BS.1770: -20 dBFS stereo 1 kHz


def test_silence_reads_under_the_render_checks_floor(tmp_path):
    path = _write(tmp_path / "silent.wav", np.zeros((48000, 2)), 48000)

    assert integrated_lufs(path) <= -60


@pytest.mark.skipif(sys.platform == "win32", reason="resource.getrusage is POSIX-only")
def test_loudness_memory_does_not_grow_with_render_length(tmp_path):
    # A 32-bar take at 85 BPM is ~90 s; the whole-file measurement needed >3 GB for it and
    # paged FL out on an 8 GB worker. Streamed, a 40 s render stays within a few blocks.
    # Measured in a fresh interpreter: its peak RSS growth across the call.
    rate = 44100
    t = np.arange(40 * rate) / rate
    left = 0.4 * np.exp(-((t % 0.5) * 30)) * np.sin(2 * np.pi * 55 * t) + 0.25 * np.sin(2 * np.pi * 110 * t)
    path = _write(tmp_path / "take.wav", np.stack([left, 0.8 * left], axis=1), rate)
    probe = (
        "import resource, sys; from pathlib import Path; from fl_mcp.audio import integrated_lufs\n"
        "unit = 1 if sys.platform == 'darwin' else 1024\n"
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit\n"
        "lufs = integrated_lufs(Path(sys.argv[1]))\n"
        "print((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * unit - before) // 2**20, lufs)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe, str(path)], capture_output=True, text=True, check=True).stdout
    growth_mib, lufs = out.split()

    assert float(lufs) > -30
    assert int(growth_mib) < 64


def _ogg_in_wav(rate: int) -> bytes:
    """A stock sample's layout: a WAV whose format is 0x674F and whose data chunk is an Ogg stream."""
    import io

    import soundfile

    tone = 0.5 * np.sin(2 * np.pi * 440 * np.arange(rate) / rate)
    ogg = io.BytesIO()
    soundfile.write(ogg, np.stack([tone, tone], axis=1), rate, format="OGG", subtype="VORBIS")
    stream = ogg.getvalue()
    fmt = (0x674F).to_bytes(2, "little") + (2).to_bytes(2, "little") + rate.to_bytes(4, "little") + bytes(18)
    body = b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt + b"data" + len(stream).to_bytes(4, "little") + stream
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def test_read_wav_reads_fls_stock_samples_ogg_vorbis_inside_a_wav(tmp_path):
    """FL 2026's Packs samples are Vorbis in a WAV (format 0x674F, the data chunk an Ogg
    stream): the stdlib, scipy and libsndfile refused them on a box, so the model could not
    listen to a sample before choosing it. read_wav unwraps the Ogg stream."""
    rate = 44100
    path = tmp_path / "Kick.wav"
    path.write_bytes(_ogg_in_wav(rate))

    samples, got_rate = read_wav(path)

    assert got_rate == rate and samples.shape[1] == 2
    assert abs(len(samples) - rate) < 2048
    peak = np.abs(np.fft.rfft(samples[:, 0])).argmax() * rate / len(samples)
    assert abs(peak - 440) < 5


def test_read_wav_still_refuses_what_is_not_audio(tmp_path):
    path = tmp_path / "notes.wav"
    path.write_bytes(b"RIFF\x10\x00\x00\x00WAVEjunk\x04\x00\x00\x00abcd")

    with pytest.raises(wave.Error, match="fmt chunk and/or data chunk missing"):  # the stdlib's own error
        read_wav(path)


def test_decode_audio_reads_what_a_model_writes_and_a_stock_sample_it_passes_on():
    """fl.load_sample sends a file the model made as its bytes, and the engine decodes them
    before FL sees anything: a float WAV soundfile wrote, FLAC, and a stock sample's Ogg-in-WAV."""
    import io

    import soundfile

    rate = 48000
    loud = np.stack([np.linspace(-1.5, 1.5, rate)] * 2, axis=1)  # past full scale: float keeps it
    for fmt, subtype in (("WAV", "FLOAT"), ("FLAC", "PCM_24")):
        buffer = io.BytesIO()
        soundfile.write(buffer, loud.clip(-1, 1) if fmt == "FLAC" else loud, rate, format=fmt, subtype=subtype)
        samples, got_rate = decode_audio(buffer.getvalue())
        assert got_rate == rate and samples.shape == (rate, 2), fmt
    assert abs(decode_audio(io.BytesIO(_ogg_in_wav(44100)).getvalue())[0].shape[0] - 44100) < 2048


@pytest.mark.parametrize("blob", [b"", b"not audio at all", b"RIFF\x10\x00\x00\x00WAVEjunk\x04\x00\x00\x00abcd"])
def test_decode_audio_refuses_what_is_not_audio(blob):
    with pytest.raises(ValueError, match=r"not an audio file|no frames"):
        decode_audio(blob)
