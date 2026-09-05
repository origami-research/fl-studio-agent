"""Development import path for the controller's frozen wire codec.

The sdist replaces this module with the controller source so installed wheels
carry the same codec that the FL-side controller embeds.
"""

from flmcp_codec import *  # noqa: F403
