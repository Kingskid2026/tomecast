"""Command-line interface for Tomecast."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from tomecast import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``tomecast`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="tomecast",
        description="Turn ebooks into audiobooks with local, human-sounding voices.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``tomecast`` command and return its exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    # Nothing to do yet: conversion lands in the v0.1.0 milestone.
    parser.print_help()
    return 0
