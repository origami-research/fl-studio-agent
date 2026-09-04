"""Capability profile loader: precedence, fallback warning, drift alarm.

The loader precedence under test (docs/27 §4): FLMCP_PROFILE env >
<spool>/profile.json > packaged profile matching the installed FL version >
BUILTIN_FALLBACK with a warning naming each hardcoded fact.

Every loader test pins the environment (env var cleared, spool dirs pointed at
a tmp path, FL version detection stubbed) so the machine's real FL install and
real spool can never leak into a result.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from fl_mcp import profile as profile_mod
from fl_mcp.fl import paths as flpaths
from fl_mcp.profile import (
    BUILTIN_FALLBACK,
    ProfileError,
    load_profile,
    packaged_profiles,
    parse_profile,
    profile_to_dict,
)

PACKAGED_NAME = "fl-26.1.5-5384-arm64.json"


@pytest.fixture
def packaged_doc() -> dict:
    return packaged_profiles()[PACKAGED_NAME]


@pytest.fixture
def pinned_env(monkeypatch, tmp_path):
    """No env override, no spool, no detectable FL - the machine cannot leak in."""
    monkeypatch.delenv("FLMCP_PROFILE", raising=False)
    monkeypatch.setattr(flpaths, "fl_settings_dirs", lambda: [])
    monkeypatch.setattr(profile_mod, "detect_fl_version", lambda: None)
    return tmp_path


def _write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


# --- parsing -----------------------------------------------------------------


def test_packaged_profile_parses(packaged_doc):
    profile = parse_profile(packaged_doc)
    assert profile.dispatcher.handled_max_id == 109
    assert profile.dispatcher.vmt_first_method_offset == 200
    assert profile.dispatcher.host_dispatcher_addr == 0x427BC0
    assert profile.dispatcher.unsafe[94].hazard == "crashes_host"
    assert profile.dispatcher.unsafe[99].hazard == "hangs_host"
    assert profile.ppq_default == 96
    vol = next(f for f in profile.structs["TNoteParams"].fields if f.name == "Vol")
    assert (vol.offset, vol.type) == (12, "Single")
    assert profile.structs["TNotesParams"].size == 52
    assert profile.flp.event_size_overrides == {172: 1}
    assert profile.flp.playlist_stride == 88
    assert profile.fst.version_string == "11.5.5"
    assert profile.capabilities.pattern_targeting_live is True
    assert profile.capabilities.channel_targeting_live is False
    assert "FPD_SetPlaying" in profile.push_events
    assert profile.spool_roots == ("Piano roll scripts/FLMCP", "Piano roll scripts")


def test_round_trip_exact(packaged_doc):
    profile = parse_profile(packaged_doc)
    assert parse_profile(profile_to_dict(profile)) == profile


def test_profiles_are_frozen(packaged_doc):
    profile = parse_profile(packaged_doc)
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.ppq_default = 42
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.dispatcher.handled_max_id = 110


def test_wrong_profile_version_rejected(packaged_doc):
    doc = dict(packaged_doc, profile_version=2)
    with pytest.raises(ProfileError):
        parse_profile(doc)


def test_malformed_document_rejected():
    with pytest.raises(ProfileError):
        parse_profile({"profile_version": 1, "meta": {}})


# --- loader precedence -------------------------------------------------------


def test_env_override_wins(pinned_env, monkeypatch, packaged_doc):
    doc = json.loads(json.dumps(packaged_doc))
    doc["ppq"]["default"] = 111
    path = _write(pinned_env / "override.json", doc)
    monkeypatch.setenv("FLMCP_PROFILE", str(path))
    assert load_profile().ppq_default == 111


def test_env_override_missing_fails_loudly(pinned_env, monkeypatch):
    monkeypatch.setenv("FLMCP_PROFILE", str(pinned_env / "nope.json"))
    with pytest.raises(ProfileError):
        load_profile()


def test_spool_profile_beats_packaged(pinned_env, monkeypatch, packaged_doc):
    settings = pinned_env / "FL Studio" / "Settings"
    doc = json.loads(json.dumps(packaged_doc))
    doc["ppq"]["default"] = 222
    _write(settings / "Piano roll scripts" / "FLMCP" / "profile.json", doc)
    monkeypatch.setattr(flpaths, "fl_settings_dirs", lambda: [settings])
    monkeypatch.setattr(profile_mod, "detect_fl_version", lambda: ("26.1.5", 5384))
    assert load_profile().ppq_default == 222


def test_malformed_spool_profile_falls_through(pinned_env, monkeypatch, caplog):
    settings = pinned_env / "FL Studio" / "Settings"
    path = settings / "Piano roll scripts" / "FLMCP" / "profile.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    monkeypatch.setattr(flpaths, "fl_settings_dirs", lambda: [settings])
    monkeypatch.setattr(profile_mod, "detect_fl_version", lambda: ("26.1.5", 5384))
    with caplog.at_level(logging.WARNING, logger="fl_mcp.profile"):
        profile = load_profile()
    assert profile.meta.sources != ("builtin-fallback",)  # reached the packaged tier
    assert any("unusable" in r.message for r in caplog.records)


def test_packaged_profile_matches_detected_version(pinned_env, monkeypatch, caplog):
    monkeypatch.setattr(profile_mod, "detect_fl_version", lambda: ("26.1.5", 5384))
    with caplog.at_level(logging.WARNING, logger="fl_mcp.profile"):
        profile = load_profile()
    assert profile.meta.fl_version == "26.1.5"
    assert profile.meta.fl_build == 5384
    assert profile.meta.sources == ("rtti", "vmt", "live-probe")
    assert not caplog.records  # a matching packaged profile is not a fallback


def test_unknown_fl_version_falls_back_with_named_warning(pinned_env, monkeypatch, caplog):
    monkeypatch.setattr(profile_mod, "detect_fl_version", lambda: ("99.0.0", 1))
    with caplog.at_level(logging.WARNING, logger="fl_mcp.profile"):
        profile = load_profile()
    assert profile is BUILTIN_FALLBACK
    text = caplog.text
    # The warning names each fact that fell back and where it is hardcoded.
    assert "dispatcher.handled_max_id" in text
    assert "fl_dispatch_ledger.h" in text
    assert "dispatcher.vmt_first_method_offset" in text
    assert "structs.TNoteParams/TNotesParams" in text
    assert "ppq.default" in text
    assert "spool_roots" in text


def test_no_fl_detected_falls_back(pinned_env, caplog):
    with caplog.at_level(logging.WARNING, logger="fl_mcp.profile"):
        assert load_profile() is BUILTIN_FALLBACK
    assert any("BUILTIN_FALLBACK" in r.message for r in caplog.records)


# --- drift alarm -------------------------------------------------------------


def test_builtin_fallback_matches_packaged(packaged_doc):
    """Transitional alarm (R4 §A1): the packaged FL 2026 profile agrees with
    every constant BUILTIN_FALLBACK gathers from the tree, byte for byte -
    until the hardcoded constants are deleted, neither may drift alone."""
    packaged = parse_profile(packaged_doc)
    # meta.sources legitimately differ ("builtin-fallback" vs the real sources).
    fallback = dataclasses.replace(
        BUILTIN_FALLBACK,
        meta=dataclasses.replace(BUILTIN_FALLBACK.meta, sources=packaged.meta.sources,
                                 generated=packaged.meta.generated),
    )
    assert fallback == packaged
