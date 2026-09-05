"""Make PyFLP's empty event enum work on Python versions that reject it."""

from __future__ import annotations

import enum
from typing import Any

_patched = False


class FLPCompatError(RuntimeError):
    pass


def _needs_patch(event_enum: Any) -> bool:
    return not event_enum.__members__


def _apply_patch() -> None:
    try:
        from pyflp._events import EventEnum, _EventEnumMeta
    except ImportError as exc:
        raise FLPCompatError("pyflp is not installed. Install it with: pip install pyflp") from exc

    if not _needs_patch(EventEnum):
        return

    original_call = _EventEnumMeta.__call__

    def call_with_missing_fallback(cls, value=enum._auto_null, *args, **kwargs):
        if cls is EventEnum and not cls.__members__:
            resolved = cls._missing_(value)
            if resolved is not None:
                return resolved
        return original_call(cls, value, *args, **kwargs)

    _EventEnumMeta.__call__ = call_with_missing_fallback


def import_pyflp():
    global _patched
    import pyflp

    if not _patched:
        _apply_patch()
        _patched = True
    return pyflp


def parse(path):
    """Parse a project with the compatibility patch applied."""
    pyflp = import_pyflp()
    try:
        return pyflp.parse(str(path))
    except TypeError as exc:
        if "has no members" in str(exc):
            raise FLPCompatError("The PyFLP compatibility patch did not take effect.") from exc
        raise
