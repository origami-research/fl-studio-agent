"""Bundled plugin parameter model loading and session wiring."""

from __future__ import annotations

import json

import pytest
from session_fakes import HELLO, FakeControl, isolate_spool

from fl_mcp import plugin_models
from fl_mcp.plugin_models import load_bundled_param_models
from fl_mcp.session import create_session
from fl_mcp.session import facade as facade_mod


@pytest.fixture(autouse=True)
def isolated_spool(monkeypatch):
    isolate_spool(monkeypatch)


@pytest.fixture()
def bundled_models_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_models, "_bundled_model_directory", lambda: tmp_path)
    load_bundled_param_models.cache_clear()
    yield tmp_path
    load_bundled_param_models.cache_clear()


def _write_model(directory, *, plugin: str = "Test Synth"):
    path = directory / "test-synth.json"
    path.write_text(
        json.dumps(
            {
                "plugin": plugin,
                "parameters": [
                    {
                        "index": 0,
                        "name": "Filter mode",
                        "kind": "enum",
                        "options": ["Lowpass", "Bandpass", "Highpass"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_bundled_param_models_validates_every_json(bundled_models_dir):
    _write_model(bundled_models_dir)

    models = load_bundled_param_models()

    assert models[0].plugin == "Test Synth"
    assert models[0].parameters[0].options == ("Lowpass", "Bandpass", "Highpass")


def test_load_bundled_param_models_rejects_malformed_json(bundled_models_dir):
    (bundled_models_dir / "invalid.json").write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_bundled_param_models()


def test_create_session_uses_bundled_models_for_display_values(monkeypatch, bundled_models_dir):
    _write_model(bundled_models_dir)
    state = {0: 0.0}

    def set_value(args):
        state[args[1]] = args[0]

    control = FakeControl(
        hello=HELLO,
        handlers={
            "plugins.getPluginName": lambda args: "Test Synth",
            "plugins.getParamCount": lambda args: 1,
            "plugins.getParamName": lambda args: "Filter mode",
            "plugins.getParamValue": lambda args: state[args[0]],
            "plugins.setParamValue": set_value,
        },
    )
    monkeypatch.setattr(facade_mod, "MidiControl", lambda: control)

    session = create_session()
    confirmed = session.set_plugin_param(0, "Filter mode", "Bandpass")

    assert session.plugin_param_models == load_bundled_param_models()
    assert (confirmed.value, confirmed.display) == (0.5, "Bandpass")


def test_create_session_allows_model_override(monkeypatch):
    monkeypatch.setattr(facade_mod, "MidiControl", lambda: FakeControl(hello=HELLO))

    session = create_session(plugin_param_models=())

    assert session.plugin_param_models == ()
