"""The operating-system boundary for the FL runtime.

Nothing imports this package yet.  Step 1 establishes the typed seam without
changing the current session path.
"""

from .runtime import (
    BrokerEndpoint,
    FLInstall,
    MacPlatform,
    Platform,
    PlatformUnsupportedError,
    ProcessSweep,
    UnsupportedPlatform,
    WindowsPlatform,
    platform_for,
)

__all__ = [
    "BrokerEndpoint",
    "FLInstall",
    "MacPlatform",
    "Platform",
    "PlatformUnsupportedError",
    "ProcessSweep",
    "UnsupportedPlatform",
    "WindowsPlatform",
    "platform_for",
]
