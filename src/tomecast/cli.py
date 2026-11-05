"""Command-line interface for Tomecast."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import signal
import sys
import time
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from tomecast import __version__
from tomecast.chunking import CoverageError
from tomecast.convert import ConversionProgress, MissingAudioError, convert_book, plan_chunks
from tomecast.downloads import (
    Asset,
    DownloadError,
    ProgressCallback,
    data_dir,
    ensure,
    is_installed,
)
from tomecast.engines.audiocpp import (
    DEFAULT_VOICE,
    MAX_CHARS,
    REQUIRED_ASSETS,
    VOICES,
    EngineError,
    KokoroServer,
    check_system,
)
from tomecast.packaging import PackagingError
from tomecast.readers.epub import InvalidEbookError, read_epub
from tomecast.workdirs import WorkFolderBusy, exclusive, work_folder


class CliError(Exception):
    """A problem to report to the user as ``tomecast: error: …``."""


# Unfinished conversions are kept so they can be resumed; after this long they're removed.


def _speed(value: str) -> float:
    try:
        speed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} isn't a number") from None
    if not 0.5 <= speed <= 2.0:
        raise argparse.ArgumentTypeError("must be between 0.5 and 2.0")
    return speed


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``tomecast`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="tomecast",
        description="Turn ebooks into audiobooks with local, human-sounding voices.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    convert = commands.add_parser(
        "convert",
        help="turn an ebook into an M4B audiobook",
        description="Turn an ebook into an M4B audiobook with chapters.",
    )
    convert.add_argument("book", type=Path, help="the ebook to convert (EPUB for now)")
    convert.add_argument(
        "-o", "--output", type=Path, help="where to write the audiobook (default: BOOK.m4b)"
    )
    convert.add_argument(
        "--voice", default=DEFAULT_VOICE, choices=VOICES, metavar="VOICE",
        help=f"Kokoro voice (default: {DEFAULT_VOICE}; see `tomecast voices`)",
    )  # fmt: skip
    convert.add_argument(
        "--speed", type=_speed, default=1.0, help="speaking speed, 0.5 to 2.0 (default: 1.0)"
    )
    convert.add_argument(
        "-y", "--yes", action="store_true", help="download the voice engine without asking"
    )
    convert.add_argument("-f", "--force", action="store_true", help="overwrite the output file")

    commands.add_parser("voices", help="list the available voices")

    gui = commands.add_parser(
        "gui",
        help="open the desktop window",
        description="Open the Tomecast desktop window, optionally with a book already loaded.",
    )
    gui.add_argument("book", nargs="?", type=Path, help="a book to open in the window")

    entry = commands.add_parser(
        "desktop-entry",
        help="add or remove Tomecast in your app launcher",
        description="Add Tomecast to (or remove it from) your app launcher and EPUB 'Open with'.",
    )
    entry.add_argument("action", choices=("install", "uninstall"), help="what to do")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``tomecast`` command and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # Treat `kill` like Ctrl-C, so the voice engine is stopped and work is saved for resuming.
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        if args.command == "convert":
            return _convert(args)
        if args.command == "voices":
            return _voices()
        if args.command == "gui":
            return _gui(args)
        if args.command == "desktop-entry":
            return _desktop_entry(args)
    except CliError as error:
        print(f"tomecast: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ntomecast: stopped.", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)
    parser.print_help()
    return 0


def _voices() -> int:
    languages = {
        "a": "American English", "b": "British English", "e": "Spanish", "f": "French",
        "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Brazilian Portuguese",
        "z": "Mandarin Chinese",
    }  # fmt: skip
    for code, language in languages.items():
        names = [v for v in VOICES if v.startswith(code)]
        print(f"{language}: {', '.join(names)}")
    print(f"\nDefault: {DEFAULT_VOICE}. Use one with `tomecast convert BOOK --voice NAME`.")
    return 0


def _gui(args: argparse.Namespace) -> int:
    book: Path | None = args.book
    try:
        # Imported here so the other commands never load Qt.
        from tomecast.gui import run

        return run([str(book)] if book else [])
    except ImportError as error:
        raise CliError(f"the desktop window can't start ({error})") from error


def _desktop_entry(args: argparse.Namespace) -> int:
    from tomecast import desktop

    try:
        messages = desktop.install() if args.action == "install" else desktop.uninstall()
    except (OSError, desktop.ForeignEntryError) as error:
        raise CliError(str(error)) from error
    print("\n".join(messages))
    return 0


def _convert(args: argparse.Namespace) -> int:
    book_path: Path = args.book
    if not book_path.is_file():
        raise CliError(f"{book_path} doesn't exist")
    if book_path.suffix.lower() != ".epub":
        raise CliError("only EPUB books are supported so far (PDF and other formats are coming)")
    output: Path = args.output or book_path.with_suffix(".m4b")
    if output.exists() and not args.force:
        raise CliError(f"{output} already exists (use --force to overwrite it)")
    if not output.parent.is_dir():
        raise CliError(f"the folder {output.parent} doesn't exist")

    try:
        book = read_epub(book_path)
        check_system()
    except (InvalidEbookError, EngineError, OSError) as error:
        raise CliError(str(error)) from error
    if not book.chapters:
        raise CliError(f"no readable text found in {book_path.name}")

    root = data_dir()
    try:
        _install(REQUIRED_ASSETS, root, assume_yes=args.yes)
        workdir = work_folder(root, book_path, args.voice, args.speed)
    except OSError as error:
        raise CliError(str(error)) from error

    chunk_count = sum(len(chunks) for chunks in plan_chunks(book, MAX_CHARS))
    print(
        f"Converting “{book.title}”: {_plural(len(book.chapters), 'chapter')}, "
        f"{_plural(chunk_count, 'piece')} of text, voice {args.voice}."
    )
    progress = _ProgressPrinter(sys.stderr)
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(exclusive(workdir))
        except WorkFolderBusy as error:
            raise CliError(str(error)) from None
        try:
            with KokoroServer(root, voice=args.voice, speed=args.speed) as engine:
                result = convert_book(
                    book, engine, output, workdir=workdir, progress=progress.update
                )
        except KeyboardInterrupt:
            progress.finish()
            if note := _saved_note(workdir):
                print(note)
            raise
        except (
            EngineError, MissingAudioError, PackagingError, CoverageError, OSError, wave.Error
        ) as error:  # fmt: skip
            progress.finish()
            raise CliError(f"{error}\n{_saved_note(workdir)}".rstrip()) from error
        progress.finish()
    shutil.rmtree(workdir, ignore_errors=True)

    for warning in result.warnings:
        print(f"Note: {warning}")
    if result.reused_chunks:
        print(f"Picked up where the last run stopped ({_plural(result.reused_chunks, 'piece')}).")
    length = result.chapters[-1].end_seconds if result.chapters else 0.0
    print(
        f"Done: {output} ({_duration(length)} of audio, "
        f"{_plural(len(result.chapters), 'chapter')}, "
        f"made in {_duration(time.monotonic() - started)})."
    )
    return 0


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _saved_note(workdir: Path) -> str:
    if any((workdir / "chunks").glob("*.wav")):
        return "The audio made so far is saved. Run the same command again to continue."
    return ""


def _install(assets: Sequence[Asset], root: Path, *, assume_yes: bool) -> None:
    missing = [asset for asset in assets if not is_installed(asset, root)]
    if not missing:
        return
    total = sum(asset.size for asset in missing)
    print("Tomecast needs to download these once:")
    for asset in missing:
        print(f"  • {asset.name} ({_megabytes(asset.size)})")
    print(f"Total: {_megabytes(total)}, saved in {root}")
    if not assume_yes:
        if not sys.stdin.isatty():
            raise CliError("downloads are needed; run again with --yes to allow them")
        if input("Download now? [y/N] ").strip().lower() not in {"y", "yes"}:
            raise CliError("nothing was downloaded, so the book can't be converted")
    for asset in missing:
        try:
            ensure(asset, root, progress=_download_printer(asset.name, sys.stderr))
        except DownloadError as error:
            raise CliError(str(error)) from error
        print(file=sys.stderr)


def _download_printer(name: str, stream: TextIO) -> ProgressCallback:
    def show(done: int, total: int) -> None:
        percent = 100 * done // total if total else 100
        stream.write(f"\r  Downloading {name}: {percent}% of {_megabytes(total)}")
        stream.flush()

    return show


class _ProgressPrinter:
    """Shows conversion progress on one updating line (or occasional lines in logs)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._interactive = stream.isatty()
        self._started = time.monotonic()
        self._last_percent = -1
        self._shown = False

    def update(self, progress: ConversionProgress) -> None:
        if progress.chunks_done == 0 or progress.stage == "packaging":
            return
        percent = 100 * progress.chunks_done // max(1, progress.chunks_total)
        elapsed = time.monotonic() - self._started
        remaining = elapsed / progress.chunks_done * (progress.chunks_total - progress.chunks_done)
        line = (
            f"Chapter {progress.chapter_index + 1}/{progress.chapter_count} · "
            f"{progress.chunks_done:,}/{progress.chunks_total:,} pieces · {percent}% · "
            f"about {_duration(remaining)} left"
        )
        if self._interactive:
            self._stream.write(f"\r{line}\033[K")
            self._stream.flush()
            self._shown = True
        elif percent // 10 > self._last_percent // 10:
            self._stream.write(line + "\n")
        self._last_percent = percent

    def finish(self) -> None:
        if self._shown:
            self._stream.write("\n")
            self._shown = False


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:,.0f} MB"


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
