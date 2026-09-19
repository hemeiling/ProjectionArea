"""Deprecated entry point, kept so existing commands keep working.

The single-file prototype that lived here has been replaced by the domain
modules under ``backend/`` (see README.md, "What was already here, and what
changed"). Its ``/analyze`` endpoint mis-read PyMuPDF's path item format,
flattened Beziers from the origin rather than from the curve start, and filled
every hole on union, so it is not worth preserving.

``uvicorn backend.app:app`` still starts the current server:
"""

from __future__ import annotations

import warnings

from backend.main import app  # noqa: F401  re-exported for backwards compatibility

warnings.warn(
    "backend.app is deprecated; use 'uvicorn backend.main:app' instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["app"]
