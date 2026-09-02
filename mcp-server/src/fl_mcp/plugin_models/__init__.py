"""Verified plugin parameter semantics bundled with the server."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from fl_mcp.schemas import PluginParamModel


def _bundled_model_directory():
    return resources.files(__name__)


@lru_cache
def load_bundled_param_models() -> tuple[PluginParamModel, ...]:
    """Load every bundled model, failing loudly if any capture is invalid."""
    directory = _bundled_model_directory()
    return tuple(
        PluginParamModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.name.endswith(".json")
    )
