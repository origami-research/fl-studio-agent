"""Channel selection: a melody must not land on a drum, silently or otherwise."""
from __future__ import annotations

import pytest

from fl_mcp import autonomous


@pytest.fixture
def channels(monkeypatch):
    """A typical FL palette: drums first, melodic later."""
    fake = [
        {"index": 0, "name": "Chords", "melodic": True},
        {"index": 1, "name": "Bass", "melodic": True},
        {"index": 2, "name": "Lead", "melodic": True},
        {"index": 3, "name": "Drums", "melodic": False},
    ]
    monkeypatch.setattr(autonomous, "list_channels", lambda: fake)
    return fake


def test_defaults_to_lead_over_bass(channels):
    """A melody belongs on a lead, not the first melodic channel that appears."""
    assert autonomous.default_channel() == 2


def test_never_defaults_to_a_drum(monkeypatch):
    monkeypatch.setattr(
        autonomous,
        "list_channels",
        lambda: [
            {"index": 0, "name": "808 Kick", "melodic": False},
            {"index": 1, "name": "FLEX Bass", "melodic": True},
        ],
    )
    assert autonomous.default_channel() == 1


def test_all_percussive_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(
        autonomous,
        "list_channels",
        lambda: [{"index": 0, "name": "Kick", "melodic": False}],
    )
    assert autonomous.default_channel() == 0


@pytest.mark.parametrize("name,expected", [("Lead", 2), ("lead", 2), ("BASS", 1), ("chor", 0)])
def test_resolves_names_case_insensitively(channels, name, expected):
    assert autonomous.resolve_channel(name) == expected


def test_resolves_index(channels):
    assert autonomous.resolve_channel(1) == 1


def test_unknown_name_lists_what_is_available(channels):
    with pytest.raises(autonomous.ChannelNotFound, match="Chords, Bass, Lead, Drums"):
        autonomous.resolve_channel("Trumpet")


def test_out_of_range_index_rejected(channels):
    with pytest.raises(autonomous.ChannelNotFound):
        autonomous.resolve_channel(99)


def test_ambiguous_name_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(
        autonomous,
        "list_channels",
        lambda: [
            {"index": 0, "name": "Synth 1", "melodic": True},
            {"index": 1, "name": "Synth 2", "melodic": True},
        ],
    )
    with pytest.raises(autonomous.ChannelNotFound, match="several"):
        autonomous.resolve_channel("synth")


def test_none_means_default(channels):
    assert autonomous.resolve_channel(None) == autonomous.default_channel()


@pytest.mark.parametrize("name", ["Drums", "808 Kick", "Hi-Hats", "Tamb", "FPC"])
def test_percussive_names_flagged(name, monkeypatch):
    monkeypatch.setattr(autonomous, "agent_project", lambda: None)
    lowered = name.lower()
    assert any(word in lowered for word in autonomous._PERCUSSIVE), name
