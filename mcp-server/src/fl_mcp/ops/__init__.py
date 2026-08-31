"""What each capability IS: one spec-like file per capability (docs/30).

Each module declares its Operation spec (kind + required Capability) with a
CapabilityTag (provenance) and a thin glue function that builds the op and
lets core.router.route pick the lane. Understanding a capability is reading
its file here plus flpkit's format for it - never a third file."""

# Importing the catalog materializes it: each module's Operation.spec
# registers its CapabilityTag in core.provenance at import.
from . import automation, levels, mixer, notes, params, playlist  # noqa: F401
