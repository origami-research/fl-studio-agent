from __future__ import annotations

import socket
import threading
import time
import wave

from fl_mcp.audio_stream import HEADER, MAGIC, AudioChunk, AudioEndpoint, main


def test_endpoint_preserves_pcm_metadata_and_publishes_port(tmp_path) -> None:
    endpoint = AudioEndpoint(tmp_path)
    endpoint.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            pcm = b"\x01\x00\xfe\xff" * 1024
            sender.sendto(HEADER.pack(MAGIC, 7, 48000, 2, 1024, 3) + pcm, ("127.0.0.1", endpoint.udp_port))
        chunks = endpoint.chunks(timeout=0.05)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            chunk = next(chunks)
            if chunk.sequence == 7:
                break
        else:
            raise AssertionError("no packet delivered")
        assert chunk == AudioChunk(7, 48000, 2, 1024, 3, pcm)
        assert '"port"' in (tmp_path / "bridge_audio.json").read_text()
    finally:
        endpoint.close()
    assert not (tmp_path / "bridge_audio.json").exists()


def test_listen_cli_records_the_loopback_endpoint_offline(tmp_path) -> None:
    endpoint = AudioEndpoint(tmp_path)
    endpoint.start()
    output = tmp_path / "heard.wav"
    failure: list[BaseException] = []

    def listen() -> None:
        try:
            main(["--port-file", str(tmp_path / "bridge_audio.json"), "--seconds", "0.3", str(output)])
        except BaseException as exc:  # the thread returns the CLI failure to this test
            failure.append(exc)

    thread = threading.Thread(target=listen)
    thread.start()
    try:
        time.sleep(0.05)  # allow the local TCP listener to connect
        packet = HEADER.pack(MAGIC, 1, 48_000, 2, 1024, 0) + b"\x01\x00\xfe\xff" * 1024
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for _ in range(4):
                sender.sendto(packet, ("127.0.0.1", endpoint.udp_port))
                time.sleep(0.03)
        thread.join(timeout=2)
    finally:
        endpoint.close()
    assert not thread.is_alive()
    assert not failure
    with wave.open(str(output), "rb") as recorded:
        assert recorded.getframerate() == 48_000
        assert recorded.getnframes() >= 1024
