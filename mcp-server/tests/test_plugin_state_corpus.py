"""Offline evidence for the opaque plugin-state boundary.

The FL-authored render-test projects are deliberately read in place, like the
larger FL corpus.  They prove where an instance's opaque state lives; they do
not prove a writer or a preset splice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flpkit import FlpError, add_effect, effects_at
from flpkit.codec import Stream, chunks

from fl_mcp.fl.paths import fl_app

_DATA = fl_app() / "Contents/Resources/FL/Data"
_FL_KEYS = _DATA / "System/Render tests/FL Keys.flp"
_EQ = _DATA / "System/Render tests/Fruity Parametric EQ 2.flp"
_BELL = (
    fl_app()
    / "Contents/Resources/FL/Plugins/Fruity/Generators/FL Studio Mobile/Installed"
    / "Synth Presets2/3xOsc/Bells/Bell.fst"
)

_INTERNAL_NAME = 201
_WRAPPER = 212
_STATE = 213
_MIXER_FLAGS = 236

requires_artifacts = pytest.mark.skipif(
    not all(path.is_file() for path in (_FL_KEYS, _EQ, _BELL)),
    reason="FL Studio's authored plugin-state artifacts are not installed",
)


def _events(path: Path) -> tuple[bytes, list[tuple[int, int, bytes]]]:
    header, stream = chunks(path.read_bytes())
    return header, [(event_id, size, bytes(stream[offset : offset + size])) for event_id, offset, size in Stream(stream)]


def _instances(events: list[tuple[int, int, bytes]]) -> list[tuple[bool, bytes, int | None]]:
    """Return (in_mixer, internal_name, opaque_state_bytes) per wrapper.

    A wrapper is an observed 201/212 pair; only the immediately following 213
    is its state.  The optional state is intentionally bytes, not decoded.
    """
    mixer_seen = False
    instances: list[tuple[bool, bytes, int | None]] = []
    for index, (event_id, size, payload) in enumerate(events):
        if event_id == _MIXER_FLAGS:
            mixer_seen = True
        if event_id != _WRAPPER:
            continue
        previous_id, _previous_size, name = events[index - 1]
        assert previous_id == _INTERNAL_NAME
        assert size == 52
        state_size = events[index + 1][1] if events[index + 1][0] == _STATE else None
        instances.append((mixer_seen, name, state_size))
    return instances


@requires_artifacts
def test_fl_authored_projects_locate_generator_and_effect_state_without_decoding_it() -> None:
    """201/212/213 belongs to rack before 236 and mixer slots after it."""
    _header, generator_events = _events(_FL_KEYS)
    _header, mixed_events = _events(_EQ)

    assert _instances(generator_events) == [(False, b"FL Keys\0", 76)]
    assert _instances(mixed_events) == [
        (False, b"3x Osc\0", 92),
        (True, b"Fruity Parametric EQ 2\0", 305),
    ]


@requires_artifacts
def test_fl_authored_channel_state_preset_carries_the_same_opaque_instance_tuple() -> None:
    """A preset is a ChannelState FST, not a detached 213 payload."""
    header, events = _events(_BELL)

    assert int.from_bytes(header[:2], "little", signed=True) == 0x20
    assert _instances(events) == [(False, b"3x Osc\0", 84)]
    assert any(event_id == 192 and payload == b"Bell\0" for event_id, _size, payload in events)


def test_checked_in_effect_pair_replays_through_the_plugin_agnostic_writer(tmp_path: Path) -> None:
    """The native reference is copied exactly; unknown effects fail typed."""
    evidence = Path(__file__).parent / "evidence/plugin-effect"
    candidate = tmp_path / "candidate.flp"
    candidate.write_bytes((evidence / "baseline.flp").read_bytes())

    saved = add_effect(candidate, 0, "Fruity Parametric EQ 2", slot=1)

    assert saved == effects_at(candidate, 0, slot=1)
    assert any(effect.name == "Fruity Parametric EQ 2" and len(effect.chunk) == 519 for effect in saved)
    with pytest.raises(FlpError):
        add_effect(candidate, 0, "not an FL effect", slot=1)
