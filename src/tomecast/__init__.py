"""Tomecast: turn ebooks into audiobooks on Linux with local, human-sounding voices."""

from importlib.metadata import version as _version

__version__ = _version("tomecast")

__all__ = ["__version__"]
