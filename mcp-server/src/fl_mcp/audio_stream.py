"""Live master-mix stream endpoint and the ``fl-mcp-listen`` proof CLI.

The Bridge sends fixed 1024-frame PCM packets to loopback UDP.  This module is
the non-audio-side fanout: it republishes those packets unchanged over a local
TCP endpoint discovered from ``bridge_audio.json`` in the session spool.
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import struct
import threading
import time
import wave
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

HEADER = struct.Struct("<4sIIHHQ")
MAGIC = b"FLAU"


@dataclass(frozen=True)
class AudioChunk:
    sequence: int
    sample_rate: int
    channels: int
    frames: int
    drops: int
    pcm: bytes

    @classmethod
    def parse(cls, packet: bytes) -> AudioChunk | None:
        if len(packet) < HEADER.size:
            return None
        magic, sequence, sample_rate, channels, frames, drops = HEADER.unpack_from(packet)
        pcm = packet[HEADER.size :]
        if magic != MAGIC or channels != 2 or frames <= 0 or len(pcm) != frames * channels * 2:
            return None
        return cls(sequence, sample_rate, channels, frames, drops, pcm)

    def wire(self) -> bytes:
        return HEADER.pack(MAGIC, self.sequence, self.sample_rate, self.channels, self.frames, self.drops) + self.pcm


class AudioEndpoint:
    """One session-owned UDP receiver plus a TCP PCM fanout endpoint."""

    def __init__(self, spool_root: Path) -> None:
        self.spool_root = spool_root
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(("127.0.0.1", 0))
        self._udp.settimeout(0.1)
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("127.0.0.1", 0))
        self._tcp.listen()
        self._tcp.settimeout(0.1)
        self._chunks: queue.Queue[AudioChunk] = queue.Queue(maxsize=256)
        self._clients: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fl-mcp-audio", daemon=True)

    @property
    def udp_port(self) -> int:
        return int(self._udp.getsockname()[1])

    @property
    def tcp_port(self) -> int:
        return int(self._tcp.getsockname()[1])

    def start(self) -> None:
        self.spool_root.mkdir(parents=True, exist_ok=True)
        path = self.spool_root / "bridge_audio.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"host": "127.0.0.1", "port": self.tcp_port, "proto": 1}) + "\n")
        temporary.replace(path)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        for client in self._clients:
            client.close()
        self._udp.close()
        self._tcp.close()
        (self.spool_root / "bridge_audio.json").unlink(missing_ok=True)

    def chunks(self, timeout: float = 1.0) -> Iterator[AudioChunk]:
        while not self._stop.is_set():
            try:
                yield self._chunks.get(timeout=timeout)
            except queue.Empty:
                continue

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._tcp.accept()
                client.setblocking(False)
                self._clients.append(client)
            except TimeoutError:
                pass
            try:
                packet, address = self._udp.recvfrom(65536)
            except TimeoutError:
                continue
            if address[0] != "127.0.0.1":
                continue
            chunk = AudioChunk.parse(packet)
            if chunk is None:
                continue
            with suppress(queue.Full):
                self._chunks.put_nowait(chunk)
            wire = chunk.wire()
            alive: list[socket.socket] = []
            for client in self._clients:
                try:
                    client.sendall(wire)
                except OSError:
                    client.close()
                else:
                    alive.append(client)
            self._clients = alive


def _endpoint_from_file(path: Path) -> tuple[str, int]:
    payload = json.loads(path.read_text())
    return str(payload["host"]), int(payload["port"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a Bridge live PCM endpoint into a WAV file.")
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    host, port = _endpoint_from_file(args.port_file)
    deadline = time.monotonic() + args.seconds
    with socket.create_connection((host, port), timeout=2) as conn:
        conn.settimeout(0.5)
        first: AudioChunk | None = None
        chunks: list[AudioChunk] = []
        buffered = b""
        while time.monotonic() < deadline:
            try:
                buffered += conn.recv(65536)
            except TimeoutError:
                continue
            while len(buffered) >= HEADER.size:
                _, _, _, channels, frames, _ = HEADER.unpack_from(buffered)
                length = HEADER.size + channels * frames * 2
                if len(buffered) < length:
                    break
                chunk = AudioChunk.parse(buffered[:length])
                buffered = buffered[length:]
                if chunk is not None:
                    first = first or chunk
                    chunks.append(chunk)
        if first is None:
            raise SystemExit("no PCM chunks received")
        with wave.open(str(args.output), "wb") as output:
            output.setnchannels(first.channels)
            output.setsampwidth(2)
            output.setframerate(first.sample_rate)
            for chunk in chunks:
                output.writeframes(chunk.pcm)
    return 0
