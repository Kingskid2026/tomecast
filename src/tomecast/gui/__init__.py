"""Tomecast's desktop window (Qt Quick). See docs/gui.md.

Importing this package loads nothing from Qt; ``run()`` does, so the terminal commands stay
fast and work without a display.
"""

from __future__ import annotations

from collections.abc import Sequence


def run(argv: Sequence[str] | None = None) -> int:
    """Open the window (optionally with a book) and return the exit code."""
    from tomecast.gui.app import run as _run

    return _run(argv)
