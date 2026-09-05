"""Seeded generators for the codec property suite's valid inputs."""

from __future__ import annotations

import random

import flpkit as flp


def gen_notes(rng: random.Random) -> list[flp.NoteSpec]:
    cells = rng.sample([(key, start) for key in range(24, 108) for start in range(64)], rng.randint(1, 24))
    return [
        flp.NoteSpec(
            key=key,
            start=start * 0.25,
            length=rng.randint(1, 16) * 0.25,
            velocity=rng.randint(1, 127) / 127,
            pan=rng.randint(0, 64) / 64,
        )
        for key, start in cells
    ]


def gen_clips(rng: random.Random, patterns: tuple[int, ...]) -> list[flp.ClipSpec]:
    cells = rng.sample([(track, start) for track in range(1, 24) for start in range(0, 128, 4)], rng.randint(1, 12))
    return [
        flp.ClipSpec(
            pattern=rng.choice(patterns), track=track, start=float(start), length=float(rng.choice((4, 8, 16)))
        )
        for track, start in cells
    ]


def gen_levels(rng: random.Random) -> dict[str, float | int]:
    return {
        "volume": rng.randint(0, 12800) / 12800,
        "pan": rng.randint(-6400, 6400) / 6400,
        "pitch_semitones": rng.randint(-48, 48),
    }


def gen_points(rng: random.Random) -> list[flp.AutomationPointSpec]:
    positions = sorted(rng.sample(range(128), rng.randint(1, 16)))
    return [
        flp.AutomationPointSpec(
            position=position * 0.25,
            value=rng.randint(0, 1 << 16) / (1 << 16),
            tension=rng.randint(-64, 64) / 64,
        )
        for position in positions
    ]


def gen_tempo(rng: random.Random) -> float:
    return rng.randint(60_000, 400_000) / 1000
