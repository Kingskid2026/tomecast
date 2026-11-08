"""Command-line interface for Tomecast."""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import platform
import re
import shutil
import signal
import sys
import tempfile
import time
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TextIO

from tomecast import __version__, pronunciations
from tomecast.audio import concatenate_wavs
from tomecast.book import Book, Chapter
from tomecast.chunking import CoverageError, chunk_paragraphs
from tomecast.cleanup import RULES, CleanupOptions, Matter, Removal, clean
from tomecast.convert import (
    ConversionProgress,
    MissingAudioError,
    VoiceEngine,
    audio_problem,
    convert_book,
    plan_chunks,
)
from tomecast.downloads import (
    UPDATE_FAILED_NOTE,
    Asset,
    DownloadError,
    Prepared,
    ProgressCallback,
    build_in_use,
    data_dir,
    failure,
    forget_failures,
    installed_sha256,
    is_installed,
    mark_proven,
    roll_back,
    waiting_check,
)
from tomecast.engines.audiocpp import (
    AUDIOCPP_VULKAN,
    DEFAULT_VOICE,
    EXECUTABLE,
    MAX_CHARS,
    MAX_SPEED,
    MIN_SPEED,
    VOICES,
    AutoKokoro,
    EngineError,
    check_system,
    default_threads,
    glibc_version,
    prepare_voice,
)
from tomecast.hardware import list_devices, vulkan_possible
from tomecast.manifest import ASSETS
from tomecast.names import DEFAULT_MIN_COUNT, find_book_names
from tomecast.packaging import PackagingError
from tomecast.playback import PLAYERS, find_player, play
from tomecast.readers import FORMATS, InvalidEbookError, read_book
from tomecast.speed import (
    audio_minutes,
    engine_speed,
    forget_speeds,
    make_minutes,
    probe,
    words_in,
)
from tomecast.textprep import TextPrep
from tomecast.workdirs import WorkFolderBusy, exclusive, work_folder


class CliError(Exception):
    """A problem to report to the user as ``tomecast: error: …``."""


# Unfinished conversions are kept so they can be resumed; after this long they're removed.


def _speed(value: str) -> float:
    try:
        speed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} isn't a number") from None
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise argparse.ArgumentTypeError(f"must be between {MIN_SPEED} and {MAX_SPEED}")
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
        help="turn an ebook into an M4B audiobook, or MP3s",
        description="Turn an ebook into an M4B audiobook with chapters, or into one MP3 per "
        "chapter (--format mp3).",
    )
    convert.add_argument("book", type=Path, help=f"the ebook to convert ({', '.join(FORMATS)})")
    convert.add_argument(
        "-o", "--output", type=Path,
        help="where to write the audiobook (default: BOOK.m4b, or a BOOK folder with --format mp3)",
    )  # fmt: skip
    convert.add_argument(
        "--format", choices=("m4b", "mp3"), default="m4b",
        help="m4b: one audiobook file with chapters (default); mp3: a folder, one MP3 per chapter",
    )  # fmt: skip
    convert.add_argument(
        "--voice", default=DEFAULT_VOICE, choices=VOICES, metavar="VOICE",
        help=f"Kokoro voice (default: {DEFAULT_VOICE}; see `tomecast voices`)",
    )  # fmt: skip
    convert.add_argument(
        "--speed",
        type=_speed,
        default=1.0,
        help=f"speaking speed, {MIN_SPEED} to {MAX_SPEED} (default: 1.0)",
    )
    convert.add_argument(
        "-y", "--yes", action="store_true", help="download the voice engine without asking"
    )
    convert.add_argument("-f", "--force", action="store_true", help="overwrite the output")
    convert.add_argument(
        "--gpu", action=argparse.BooleanOptionalAction, default=None,
        help="use the graphics card through the driver already installed "
        "(default: on when one is found); --no-gpu uses the CPU",
    )  # fmt: skip
    cleanup = convert.add_argument_group(
        "cleanup", "What to leave out. By default Tomecast reads the book, not the page."
    )
    cleanup.add_argument(
        "--keep-page-numbers",
        action="store_true",
        help="read page numbers and running headers and footers (PDF)",
    )
    cleanup.add_argument(
        "--keep-front-matter",
        action="store_true",
        help="read the front and back matter too: title page, copyright, contents, index, "
        '"Also by" lists and previews of other books',
    )
    cleanup.add_argument(
        "--footnotes",
        choices=("drop", "end"),
        default="drop",
        help="leave footnotes out (drop, the default) or read them at the end of their chapter",
    )

    commands.add_parser("voices", help="list the available voices")

    commands.add_parser(
        "doctor",
        help="print what Tomecast sees on this computer (for bug reports)",
        description="Print the system, graphics and installed-file details a bug report needs.",
    )
    scan = commands.add_parser(
        "scan",
        help="check this computer again: engine updates and speed",
        description=(
            "Check this computer again: try any voice engine update that didn't work here "
            "before (after installing a graphics driver, say), and measure how fast it voices "
            "on the CPU and the graphics card."
        ),
    )
    scan.add_argument(
        "--gpu", action=argparse.BooleanOptionalAction, default=None,
        help="check the graphics card too (default: on when one is found); --no-gpu skips it",
    )  # fmt: skip
    scan.add_argument(
        "-y", "--yes", action="store_true", help="download the voice engine without asking"
    )
    names = commands.add_parser(
        "names",
        help="list the names and unusual words in a book",
        description=(
            "List the capitalised words a book uses often that aren't ordinary English, "
            "with how many times. Names and places are the ones a voice most often gets "
            "wrong: check them with `tomecast preview`, then add fixes to a pronunciation "
            "list (see the README)."
        ),
    )
    names.add_argument("book", type=Path, help=f"the ebook to look through ({', '.join(FORMATS)})")
    names.add_argument(
        "--min-count", type=int, default=DEFAULT_MIN_COUNT, metavar="N",
        help=f"only words used at least N times (default: {DEFAULT_MIN_COUNT})",
    )  # fmt: skip
    names.add_argument(
        "--limit", type=int, default=100, metavar="N",
        help="show at most N words, most frequent first (default: 100; 0 shows all)",
    )  # fmt: skip

    preview = commands.add_parser(
        "preview",
        help="hear how a word or sentence sounds",
        description=(
            "Voice some text and play it (or save it with -o). Your pronunciation lists "
            "and the number and abbreviation rules are applied first, so you hear what the "
            "book would sound like; --raw skips them."
        ),
    )
    preview.add_argument("text", nargs="+", help="the word or sentence to say")
    preview.add_argument(
        "--voice", default=DEFAULT_VOICE, choices=VOICES, metavar="VOICE",
        help=f"Kokoro voice (default: {DEFAULT_VOICE}; see `tomecast voices`)",
    )  # fmt: skip
    preview.add_argument(
        "--speed", type=_speed, default=1.0,
        help=f"speaking speed, {MIN_SPEED} to {MAX_SPEED} (default: 1.0)",
    )  # fmt: skip
    preview.add_argument(
        "-o", "--output", type=Path, help="write a WAV file instead of playing the sound"
    )
    preview.add_argument(
        "--book",
        type=Path,
        help="also use this book's pronunciation list (BOOK.pronunciations.txt)",
    )
    preview.add_argument(
        "--raw", action="store_true",
        help="say the text exactly as typed: no pronunciation lists, no number rules",
    )  # fmt: skip
    preview.add_argument(
        "-y", "--yes", action="store_true", help="download the voice engine without asking"
    )
    preview.add_argument("-f", "--force", action="store_true", help="overwrite the output file")
    preview.add_argument(
        "--gpu", action=argparse.BooleanOptionalAction, default=None,
        help="use the graphics card (default: on when one is found); --no-gpu uses the CPU",
    )  # fmt: skip

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
        if args.command == "doctor":
            return _doctor()
        if args.command == "scan":
            return _scan(args)
        if args.command == "names":
            return _names(args)
        if args.command == "preview":
            return _preview(args)
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
        raise CliError(
            f"the desktop window can't start: {error}. The window needs the graphics libraries "
            "every Linux desktop has (libEGL, libxkbcommon, fontconfig). On a machine without "
            "a desktop, use 'tomecast convert' instead."
        ) from error


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
    output_format: Literal["m4b", "mp3"] = args.format
    output: Path = args.output or (
        book_path.with_suffix("") if output_format == "mp3" else book_path.with_suffix(".m4b")
    )
    if (output.exists() or output.is_symlink()) and not args.force:
        raise CliError(f"{output} already exists (use --force to overwrite it)")
    if not output.parent.is_dir():
        raise CliError(f"the folder {output.parent} doesn't exist")

    try:
        book = read_book(book_path)
        check_system()
    except (InvalidEbookError, EngineError, OSError) as error:
        raise CliError(str(error)) from error
    cleaned = clean(book, _cleanup_options(args))
    book = cleaned.book
    chapters = cleaned.kept
    if not chapters:
        raise CliError(f"no readable text found in {book_path.name}")
    for line in _cleanup_notes(cleaned.removed, book.chapters):
        print(line)

    use_gpu = _wants_gpu(args.gpu)
    root = data_dir()
    prep, warnings = _text_prep(book, book_path, root)
    for warning in warnings:
        print(f"Note: {warning}", file=sys.stderr)
    try:
        builds = _prepare(use_gpu, root, assume_yes=args.yes).builds
        workdir = work_folder(
            root, book_path, args.voice, args.speed, use_gpu=use_gpu, builds=builds
        )
    except OSError as error:
        raise CliError(str(error)) from error

    chunk_count = sum(len(chunks) for chunks in plan_chunks(book, MAX_CHARS, chapters, prep=prep))
    print(
        f"Converting “{book.title}”: {_plural(len(chapters), 'chapter')}, "
        f"{_plural(chunk_count, 'piece')} of text, voice {args.voice}."
    )
    if prep.pronunciations:
        count = len(prep.pronunciations)
        print(f"Using {count} pronunciation {'fix' if count == 1 else 'fixes'}.")
    progress = _ProgressPrinter(sys.stderr)
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(exclusive(workdir))
        except WorkFolderBusy as error:
            raise CliError(str(error)) from None
        try:
            with AutoKokoro(
                root, voice=args.voice, speed=args.speed, use_gpu=use_gpu, builds=builds
            ) as engine:
                print(_hardware_line(engine, args.gpu))
                if engine.warning:
                    print(f"Note: {engine.warning}")
                speedup = engine_speed(root, engine, measuring=_measuring)
                if speedup is not None:
                    print(_estimate_line(words_in(book, chapters), args.speed, speedup))
                result = convert_book(
                    book,
                    engine,
                    output,
                    workdir=workdir,
                    progress=progress.update,
                    format=output_format,
                    prep=prep,
                    chapters=chapters,
                )
        except KeyboardInterrupt:
            progress.finish()
            if note := _saved_note(workdir):
                print(note)
            raise
        except (EngineError, MissingAudioError) as error:
            progress.finish()
            # A switched-to update that crashes (or makes no usable audio) on the book, before
            # it has finished one: the next run uses the build that worked, in a new work
            # folder, so nothing is mixed.
            if roll_back(root, builds, str(error)):
                raise CliError(f"{error}\nNote: {UPDATE_FAILED_NOTE} {AGAIN_NOTE}") from error
            raise CliError(f"{error}\n{_saved_note(workdir)}".rstrip()) from error
        except (PackagingError, CoverageError, OSError, wave.Error) as error:
            progress.finish()
            raise CliError(f"{error}\n{_saved_note(workdir)}".rstrip()) from error
        progress.finish()
    mark_proven(root, builds)
    shutil.rmtree(workdir, ignore_errors=True)

    for warning in result.warnings:
        print(f"Note: {warning}")
    if result.reused_chunks:
        print(f"Picked up where the last run stopped ({_plural(result.reused_chunks, 'piece')}).")
    length = result.chapters[-1].end_seconds if result.chapters else 0.0
    print(
        f"Done: {result.output} ({_duration(length)} of audio, "
        f"{_plural(len(result.chapters), 'chapter')}, "
        f"made in {_duration(time.monotonic() - started)})."
    )
    return 0


AGAIN_NOTE = "Run the same command again to make the book with it."

NO_VULKAN_NOTE = (
    "No usable Vulkan graphics driver was found, so Tomecast will use the CPU. To use the "
    "graphics card, install your distro's Vulkan loader (vulkan-icd-loader, libvulkan1 or "
    "vulkan-loader); Tomecast never installs drivers."
)


def _wants_gpu(requested: bool | None) -> bool:
    """``--gpu`` / ``--no-gpu``, else on when a Vulkan driver and graphics device are there."""
    possible = vulkan_possible()
    if requested and not possible:
        print(f"Note: {NO_VULKAN_NOTE}")
        return False
    return possible if requested is None else requested


def _hardware_line(engine: AutoKokoro, requested: bool | None) -> str:
    if engine.gpu_description:
        return f"Using the graphics card: {engine.gpu_description}."
    if not engine.use_gpu:
        why = "turned off with --no-gpu" if requested is False else "no graphics card found"
        return f"Using the CPU ({why})."
    return "Using the CPU."


def _measuring() -> None:
    print("Measuring how fast this computer voices (once, a few seconds)…")


def _estimate_line(words: int, speed: float, speedup: float) -> str:
    """``"About 2 h 47 min of audio, about 9 min to make here (19.8x real time)."``"""
    audio = _rough_minutes(audio_minutes(words, speed))
    make = _rough_minutes(make_minutes(words, speed, speedup))
    return f"{audio.capitalize()} of audio, {make} to make here ({speedup:.1f}\u00d7 real time)."


def _rough_minutes(minutes: float) -> str:
    """``"under 1 min"``, ``"about 9 min"`` or ``"about 2 h 47 min"`` (rounded as the
    window rounds)."""
    whole = math.floor(minutes + 0.5)
    if whole < 1:
        return "under 1 min"
    hours, rest = divmod(whole, 60)
    return f"about {hours} h {rest:02d} min" if hours else f"about {rest} min"


def _doctor() -> int:
    """Print what a bug report needs: system, graphics and installed files."""
    root = data_dir()
    print(f"Tomecast {__version__}")
    print(f"Distro: {_distro()}")
    glibc = glibc_version()
    print(f"glibc: {f'{glibc[0]}.{glibc[1]}' if glibc else 'not found'}")
    threads = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    print(f"CPU threads: {threads}")
    print(f"Python: {platform.python_version()}")
    print(f"Vulkan possible: {'yes' if vulkan_possible() else 'no'}")
    vulkan = build_in_use(AUDIOCPP_VULKAN, root)
    if vulkan is not None:
        devices = list_devices(vulkan / EXECUTABLE)
        if devices:
            print("Vulkan devices:")
            for device in devices:
                free = (
                    f"{device.free_bytes / 2**30:.1f} of {device.total_bytes / 2**30:.1f} GiB free"
                )
                print(f"  {device.index}: {device.description} ({device.kind}, {free})")
        else:
            print("Vulkan devices: none found")
    else:
        print("Vulkan devices: not checked (the graphics-card engine isn't installed yet)")
    print(f"Data folder: {root}")
    print("Installed:")
    for asset in ASSETS:
        print(f"  {asset.name}{' (opt-in)' if asset.opt_in else ''}: {_install_state(asset, root)}")
    return 0


def _install_state(asset: Asset, root: Path) -> str:
    """``yes``, ``no``, or which older build is in use and why."""
    if is_installed(asset, root):
        return "yes"
    if not asset.archive or build_in_use(asset, root) is None:
        return "no"
    older = f"an older build ({(installed_sha256(asset, root) or '?')[:12]})"
    if (reason := failure(asset, root)) is not None:
        first_line = reason.strip().splitlines()[0] if reason.strip() else "no reason given"
        return f"{older}; the update failed its check here: {first_line}"
    if waiting_check(asset, root):
        return f"{older}; the update is downloaded and will be checked on the next run"
    return f"{older}; the update isn't downloaded yet"


def _scan(args: argparse.Namespace) -> int:
    """Try failed engine updates again, and measure the speed on the CPU and the card."""
    try:
        check_system()
    except EngineError as error:
        raise CliError(str(error)) from error
    root = data_dir()
    use_gpu = _wants_gpu(args.gpu)
    forget_failures(root)
    forget_speeds(root)
    _prepare(use_gpu, root, assume_yes=args.yes)
    print("Measuring how fast this computer voices (a few seconds each)…")
    speeds = probe(root, gpu=use_gpu)
    threads = default_threads()
    if speeds.cpu is not None:
        print(f"CPU, {_plural(threads, 'thread')}: about {speeds.cpu:.1f}\u00d7 real time.")
    else:
        print("CPU: the speed test didn't work (see `tomecast doctor`).")
    if use_gpu:
        if speeds.gpu is None:
            print("Graphics card: not usable, so Tomecast uses the CPU.")
        else:
            faster = (
                f", {speeds.gpu / speeds.cpu:.1f}\u00d7 as fast as the CPU" if speeds.cpu else ""
            )
            print(f"{speeds.gpu_description}: about {speeds.gpu:.1f}\u00d7 real time{faster}.")
    return 0


def _distro() -> str:
    try:
        text = Path("/etc/os-release").read_text(errors="replace")
    except OSError:
        return "unknown"
    match = re.search(r'^PRETTY_NAME=(?:"([^"]*)"|(\S*))\s*$', text, re.MULTILINE)
    return (match.group(1) or match.group(2)) if match else "unknown"


def _text_prep(book: Book, book_path: Path | None, root: Path) -> tuple[TextPrep, list[str]]:
    """The text prep for ``book``: its language's rules plus the pronunciation lists (the
    global one, then the one next to the book file), and warnings about any bad list lines."""
    fixes, warnings = pronunciations.load_for_book(root, book_path)
    return TextPrep.for_language(book.language, fixes), warnings


def _names(args: argparse.Namespace) -> int:
    book_path: Path = args.book
    if not book_path.is_file():
        raise CliError(f"{book_path} doesn't exist")
    try:
        book = read_book(book_path)
    except (InvalidEbookError, OSError) as error:
        raise CliError(str(error)) from error
    if not book.chapters:
        raise CliError(f"no readable text found in {book_path.name}")

    root = data_dir()
    fixes, warnings = pronunciations.load_for_book(root, book_path)
    for warning in warnings:
        print(f"Note: {warning}", file=sys.stderr)
    found = find_book_names(book, min_count=max(1, args.min_count), skip=fixes.written_words())
    if not found:
        print("No unusual names found.")
        return 0
    shown = found if args.limit <= 0 else found[: args.limit]
    width = max(len(name.word) for name in shown)
    for name in shown:
        print(f"{name.word:<{width}}  {name.count:>5}")
    if len(shown) < len(found):
        print(f"\n…and {len(found) - len(shown)} more (use --limit 0 to see them all).")
    print(
        f"\nHear one with `tomecast preview NAME`. To fix one, add a line like "
        f"`Name → say-it-like-this` to {pronunciations.book_list_path(book_path)} "
        f"(this book) or {pronunciations.global_list_path(root)} (every book)."
    )
    return 0


def _preview(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    output: Path | None = args.output
    if output is not None:
        if output.exists() and not args.force:
            raise CliError(f"{output} already exists (use --force to overwrite it)")
        if not output.parent.is_dir():
            raise CliError(f"the folder {output.parent} doesn't exist")
    elif find_player() is None:
        raise CliError(
            f"no audio player found ({', '.join(PLAYERS)}). Use -o FILE.wav to save the sound "
            "instead."
        )
    try:
        check_system()
    except EngineError as error:
        raise CliError(str(error)) from error

    root = data_dir()
    spoken = text
    if not args.raw:
        book_path: Path | None = args.book
        fixes, warnings = pronunciations.load_for_book(root, book_path)
        for warning in warnings:
            print(f"Note: {warning}", file=sys.stderr)
        spoken = TextPrep.for_language(None, fixes).body(text)
    if not any(character.isalnum() for character in spoken):
        raise CliError("there's nothing to say: give it a word or a sentence")
    if spoken != text:
        print(f"Saying: {spoken}")

    use_gpu = _wants_gpu(args.gpu)
    builds = _prepare(use_gpu, root, assume_yes=args.yes).builds
    with tempfile.TemporaryDirectory(prefix="tomecast-preview-") as folder:
        wav = Path(folder) / "preview.wav"
        try:
            with AutoKokoro(
                root, voice=args.voice, speed=args.speed, use_gpu=use_gpu, builds=builds
            ) as engine:
                if engine.warning:
                    print(f"Note: {engine.warning}", file=sys.stderr)
                _voice_text(engine, spoken, wav)
            if problem := audio_problem(wav, spoken):
                raise CliError(f"the voice made no usable audio: {problem}")
            if output is not None:
                shutil.copyfile(wav, output)
                print(f"Saved {output}")
            else:
                play(wav)
        except (EngineError, OSError, wave.Error, RuntimeError) as error:
            raise CliError(str(error)) from error
    return 0


def _voice_text(engine: VoiceEngine, text: str, output: Path) -> None:
    """Voice ``text`` into ``output``, in pieces if it is longer than the engine takes."""
    pieces = [c.text for c in chunk_paragraphs(0, [text], engine.max_chars) if c.speakable]
    if len(pieces) == 1:
        engine.synthesize(pieces[0], output)
        return
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".pieces-") as folder:
        parts = []
        for index, piece in enumerate(pieces):
            part = Path(folder) / f"{index}.wav"
            engine.synthesize(piece, part)
            parts.append(part)
        concatenate_wavs(parts, output, gap_seconds=0.2)


def _cleanup_options(args: argparse.Namespace) -> CleanupOptions:
    return CleanupOptions(
        page_numbers=not args.keep_page_numbers,
        footnotes="end" if args.footnotes == "end" else "drop",
        skip=frozenset() if args.keep_front_matter else frozenset(Matter),
    )


def _cleanup_notes(removed: Sequence[Removal], chapters: Sequence[Chapter]) -> list[str]:
    """What cleanup left out, in a few lines: sections skipped, then counts per rule."""
    sections = [r for r in removed if r.rule in set(Matter)]
    counts: dict[str, int] = {}
    for removal in removed:
        if removal.rule not in set(Matter):
            counts[removal.rule] = counts.get(removal.rule, 0) + 1
    lines = []
    if sections:
        names = ", ".join(f"“{_display_name(r, chapters)}”" for r in sections)
        lines.append(f"Skipping {_plural(len(sections), 'section')}: {names}.")
    if counts:
        parts = ", ".join(f"{RULES[rule]} ({count:,})" for rule, count in counts.items())
        lines.append(f"Left out: {parts}.")
    return lines


def _display_name(removal: Removal, chapters: Sequence[Chapter]) -> str:
    if removal.chapter is not None and chapters[removal.chapter].title:
        return chapters[removal.chapter].title
    return removal.text


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _saved_note(workdir: Path) -> str:
    if any((workdir / "chunks").glob("*.wav")):
        return "The audio made so far is saved. Run the same command again to continue."
    return ""


def _prepare(use_gpu: bool, root: Path, *, assume_yes: bool) -> Prepared:
    """Download what a run needs (after asking), and switch to new engine builds only once
    they pass their self-check. Prints a note when an update was kept back."""

    def consent(missing: Sequence[Asset], updates: bool) -> bool:
        total = sum(asset.size for asset in missing)
        print(
            "Tomecast has an update to download:"
            if updates
            else "Tomecast needs to download these once:"
        )
        for asset in missing:
            print(f"  • {asset.name} ({_megabytes(asset.size)})")
        print(f"Total: {_megabytes(total)}, saved in {root}")
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            if updates:
                print(
                    "Note: Tomecast will keep using the version it has; run again with --yes "
                    "to download the update."
                )
                return False
            raise CliError("downloads are needed; run again with --yes to allow them")
        if input("Download now? [y/N] ").strip().lower() in {"y", "yes"}:
            return True
        if updates:
            print("Tomecast will keep using the version it has.")
            return False
        raise CliError("nothing was downloaded, so the book can't be converted")

    def checking() -> None:
        print("Checking the new voice engine on this computer (a few seconds)…")

    try:
        prepared = prepare_voice(
            root, use_gpu, consent=consent, on_download=_download_printer, on_check=checking
        )
    except DownloadError as error:
        raise CliError(str(error)) from error
    for note in prepared.notes:
        print(f"Note: {note}")
    return prepared


def _download_printer(asset: Asset, number: int, count: int) -> ProgressCallback:
    stream: TextIO = sys.stderr
    finished = False

    def show(done: int, total: int) -> None:
        nonlocal finished
        percent = 100 * done // total if total else 100
        stream.write(f"\r  Downloading {asset.name}: {percent}% of {_megabytes(total)}")
        if done >= total and not finished:
            finished = True
            stream.write("\n")
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
