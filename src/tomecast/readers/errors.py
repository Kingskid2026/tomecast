"""The error every reader raises for a file it can't read."""

from __future__ import annotations


class InvalidEbookError(ValueError):
    """Raised when a file can't be read as the book format it claims to be, or can't be read
    completely."""
