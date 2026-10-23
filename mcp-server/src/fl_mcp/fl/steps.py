"""The outcome type every fl-mcp-install step returns."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class InstallStep(BaseModel):
    """One install step's outcome; ``WARN`` means the step needs a human or FL restart."""

    status: Literal["OK", "WARN", "FAIL"]
    detail: str

    def __str__(self) -> str:
        return f"{self.status} {self.detail}"
