from __future__ import annotations

from pathlib import Path


def bundled_workspace() -> Path:
    """Return the read-only runtime assets installed with the Python package."""

    return Path(__file__).resolve().parent / "_assets"
