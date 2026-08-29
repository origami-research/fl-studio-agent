"""The one source of wall-clock time. A leaf module with no internal deps.

Every timestamp in the codebase (action-log entries, shadow refs, spool
responses) comes from ``now()`` so time has a single origin - and so a future
change to an injectable clock has exactly one place to touch instead of six
copies of ``datetime.now(timezone.utc)``.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    """The current instant, timezone-aware in UTC."""
    return datetime.now(timezone.utc)
