"""Package voiced chapters into one M4B audiobook with chapter markers, tags and a cover.

This is the packager from docs/architecture.md ("7. Packager"). Its input is the WAV files the
voice engine made, in reading order, plus how much silence goes after each one. Its output is
a single ``.m4b`` file: AAC, mono, with a chapter marker per chapter.

How it works:

1. Every segment's WAV header is checked first (mono, 16-bit PCM, one shared sample rate), so
   a bad file fails in seconds rather than after a long encode.
2. Chapter start and end times are computed from those exact frame counts and written to an
   FFmetadata file, together with the book's tags.
3. One ffmpeg process encodes the whole book. Its audio input is raw PCM on stdin: each
   segment's frames are copied across in small blocks, followed by its silence, so memory
   use stays flat however long the book is, and no intermediate audio is written to disk.
4. ffmpeg writes to a hidden temporary file next to the output, which is renamed into place
   only when everything succeeded, so a failed run never leaves a broken audiobook behind.

Why one encode for the whole book instead of one per chapter: every AAC encode starts with
1024 samples of encoder delay ("priming") and pads its last frame. Separately encoded
chapters joined with ``-c copy`` keep that delay and padding at every join, so the audio
drifts later than the chapter markers by roughly 40-85 ms per chapter at 24 kHz. A single
encode is gapless, and the MP4 edit list trims its one priming delay at the start.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import subprocess
import tempfile
import uuid
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import imageio_ffmpeg  # type: ignore[import-untyped]

from tomecast.book import Cover

_SAMPLE_WIDTH = 2  # bytes per sample: 16-bit PCM
_BLOCK_FRAMES = 32_768  # frames copied per step: 64 KiB of mono 16-bit audio
_STDERR_TAIL_LINES = 20
_COVER_SUFFIXES = {"image/jpeg": ".jpg", "image/png": ".png"}
# Characters the FFmetadata format needs escaped with a backslash.
_FFMETADATA_SPECIAL = re.compile(r"[=;#\\\n]")


class PackagingError(RuntimeError):
    """Raised when the audiobook can't be built: bad input audio, a bad cover, or ffmpeg failed."""


@dataclass(frozen=True, slots=True)
class ChapterAudio:
    """One chapter's audio: WAV segments in reading order, each followed by some silence.

    Every segment must be a mono, 16-bit PCM WAV file, and all segments in a book must share
    one sample rate. ``gaps_after[i]`` is the silence, in seconds, inserted after
    ``segments[i]``.
    """

    title: str
    segments: tuple[Path, ...]
    gaps_after: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class AudiobookInfo:
    """The book-level details written into the audiobook's tags."""

    title: str
    authors: tuple[str, ...]
    narrator: str  # e.g. "Kokoro (af_heart)"
    language: str | None
    cover: Cover | None


@dataclass(frozen=True, slots=True)
class PackagedChapter:
    """Where a chapter ended up in the finished audiobook."""

    title: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class PackagedBook:
    """The finished audiobook: where it was written, and where its chapters are."""

    output: Path
    chapters: list[PackagedChapter]


@dataclass(frozen=True, slots=True)
class _Segment:
    path: Path
    frames: int
    gap_frames: int


@dataclass(frozen=True, slots=True)
class _PlannedChapter:
    """A validated chapter, placed on the book's timeline in PCM frames."""

    title: str
    segments: tuple[_Segment, ...]
    start_frame: int
    end_frame: int


def write_m4b(
    chapters: Sequence[ChapterAudio],
    info: AudiobookInfo,
    output: Path,
    *,
    workdir: Path,
    bitrate: str = "64k",
    ffmpeg: str | None = None,
    overwrite: bool = True,
) -> PackagedBook:
    """Encode ``chapters`` into an M4B audiobook at ``output`` and return where it ended up
    and its chapter list.

    The audio is encoded once, as mono AAC at ``bitrate``, with one chapter marker per
    chapter, iTunes-style tags from ``info`` (title, album, artist, album artist, composer
    as the narrator, and genre "Audiobook") and the cover, if there is one, as attached
    cover art. Chapter times come from exact PCM frame counts. A chapter with an empty
    title is called "Chapter N", counting from 1.

    Small temporary files (the FFmetadata file, the cover and ffmpeg's log) go in a private
    folder inside ``workdir``, which is removed afterwards. The audiobook is written to a
    temporary file next to ``output`` and renamed over it only on success, so if this raises,
    ``output`` is untouched.

    With ``overwrite=False`` an existing file is never replaced, even if it appears while
    the audiobook is being encoded: the finished file is published with a hard link, which
    fails if the name is taken, and the next free name ("Title (2).m4b", "Title (3).m4b"...)
    is used instead. The returned ``output`` is the name actually used.

    ``ffmpeg`` is the ffmpeg executable to use. It defaults to the one bundled with
    imageio-ffmpeg.

    Raises:
        PackagingError: if a segment is missing or isn't mono 16-bit PCM WAV, the sample
            rates differ, a chapter has no segments or mismatched ``gaps_after``, the cover
            isn't JPEG or PNG, or ffmpeg fails (the message ends with ffmpeg's last lines
            of output).
    """
    sample_rate, planned = _plan(chapters)
    cover_suffix = _cover_suffix(info.cover)
    if not output.parent.is_dir():
        raise PackagingError(f"output folder does not exist: {output.parent}")
    executable = ffmpeg or _bundled_ffmpeg()

    workdir.mkdir(parents=True, exist_ok=True)
    # Hidden, so media libraries don't pick it up half-written, and ending in .m4b, so
    # ffmpeg's iPod muxer doesn't warn about the extension.
    partial = output.with_name(f".{output.stem}.{uuid.uuid4().hex[:8]}.partial.m4b")
    try:
        with tempfile.TemporaryDirectory(prefix="tomecast-m4b-", dir=workdir) as scratch:
            scratch_dir = Path(scratch)
            metadata = scratch_dir / "chapters.ffmetadata"
            metadata.write_text(_ffmetadata(info, planned, sample_rate), encoding="utf-8")
            cover: Path | None = None
            if info.cover is not None:
                cover = scratch_dir / f"cover{cover_suffix}"
                cover.write_bytes(info.cover.data)

            command = _ffmpeg_command(
                executable,
                sample_rate=sample_rate,
                metadata=metadata,
                cover=cover,
                bitrate=bitrate,
                output=partial,
            )
            _encode(command, planned, log_path=scratch_dir / "ffmpeg.log")
        if overwrite:
            partial.replace(output)
        else:
            output = _publish_without_overwriting(partial, output)
    finally:
        partial.unlink(missing_ok=True)  # only still there if something failed

    return PackagedBook(
        output,
        [
            PackagedChapter(c.title, c.start_frame / sample_rate, c.end_frame / sample_rate)
            for c in planned
        ],
    )


def _publish_without_overwriting(partial: Path, target: Path) -> Path:
    """Give ``partial`` its final name: ``target``, or the first free "<stem> (N)<suffix>".

    ``os.link`` fails if the name exists (it does not follow symlinks either), so claiming a
    name is atomic and a file that appears at any moment is never replaced. On a file system
    without hard links, the name is claimed with an exclusive create and the audiobook is
    then renamed over that empty placeholder.
    """
    number = 1
    candidate = target
    while True:
        try:
            os.link(partial, candidate)
        except FileExistsError:
            number += 1
            candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
            continue
        except OSError:  # no hard links here (FAT, some network shares)
            try:
                os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666))
            except FileExistsError:
                number += 1
                candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
                continue
            partial.replace(candidate)
        else:
            partial.unlink(missing_ok=True)
        return candidate


def _plan(chapters: Sequence[ChapterAudio]) -> tuple[int, list[_PlannedChapter]]:
    """Validate every chapter and segment, and place the chapters on the timeline.

    Returns the book's sample rate and the chapters with their start and end frames.
    """
    if not chapters:
        raise PackagingError("there are no chapters to package")

    sample_rate: int | None = None
    first_segment: Path | None = None
    position = 0
    planned: list[_PlannedChapter] = []
    for number, chapter in enumerate(chapters, start=1):
        label = _describe(number, chapter.title)
        if not chapter.segments:
            raise PackagingError(f"{label} has no audio segments")
        if len(chapter.gaps_after) != len(chapter.segments):
            raise PackagingError(
                f"{label} has {len(chapter.segments)} segments but "
                f"{len(chapter.gaps_after)} gaps_after values; they must match"
            )

        start = position
        segments: list[_Segment] = []
        for path, gap in zip(chapter.segments, chapter.gaps_after, strict=True):
            if not (math.isfinite(gap) and gap >= 0):
                raise PackagingError(
                    f"{label}: the gap after {path} must be a number of seconds >= 0, not {gap!r}"
                )
            rate, frames = _read_wav_header(path)
            if sample_rate is None:
                sample_rate, first_segment = rate, path
            elif rate != sample_rate:
                raise PackagingError(
                    f"{path} is {rate} Hz, but {first_segment} is {sample_rate} Hz; "
                    "all segments must share one sample rate"
                )
            gap_frames = round(gap * sample_rate)
            segments.append(_Segment(path, frames, gap_frames))
            position += frames + gap_frames

        if position == start:
            raise PackagingError(f"{label} has no audio: its segments and gaps are all empty")
        title = chapter.title if chapter.title.strip() else f"Chapter {number}"
        planned.append(_PlannedChapter(title, tuple(segments), start, position))

    assert sample_rate is not None  # every chapter has at least one segment
    return sample_rate, planned


def _describe(number: int, title: str) -> str:
    """Name a chapter in an error message, e.g. ``chapter 3 ('The Road')``."""
    return f"chapter {number} ({title!r})" if title.strip() else f"chapter {number}"


def _read_wav_header(path: Path) -> tuple[int, int]:
    """Check that ``path`` is a mono 16-bit PCM WAV file. Return (sample rate, frame count)."""
    with _open_wav(path) as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
    if channels != 1:
        raise PackagingError(f"{path} has {channels} channels; segments must be mono")
    if sample_width != _SAMPLE_WIDTH:
        raise PackagingError(
            f"{path} has {8 * sample_width}-bit samples; segments must be 16-bit PCM"
        )
    return sample_rate, frames


def _open_wav(path: Path) -> wave.Wave_read:
    """Open a WAV file for reading, turning every kind of failure into a PackagingError."""
    if not path.is_file():
        raise PackagingError(f"audio segment not found: {path}")
    try:
        return wave.open(str(path), "rb")
    except (wave.Error, EOFError, OSError) as error:
        raise PackagingError(f"{path} is not a readable PCM WAV file ({error})") from error


def _cover_suffix(cover: Cover | None) -> str:
    """The file extension for the cover image, or "" if there is no cover."""
    if cover is None:
        return ""
    suffix = _COVER_SUFFIXES.get(cover.media_type.lower())
    if suffix is None:
        raise PackagingError(
            f"unsupported cover type {cover.media_type!r}; expected image/jpeg or image/png"
        )
    return suffix


def _ffmetadata(info: AudiobookInfo, chapters: Sequence[_PlannedChapter], sample_rate: int) -> str:
    """Build the FFmetadata file that carries the tags and chapter markers into ffmpeg."""
    authors = ", ".join(info.authors)
    tags = {
        "title": info.title,
        "album": info.title,
        "artist": authors,
        "album_artist": authors,
        "composer": info.narrator,
        "genre": "Audiobook",
        # MP4 has no book-wide language tag (only a per-track ISO 639-2 code), so ffmpeg's
        # MP4 muxer drops this one. It's kept for other output formats.
        "language": info.language or "",
    }
    lines = [";FFMETADATA1"]
    lines += [f"{key}={_escape(value)}" for key, value in tags.items() if value]
    for chapter in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={_frames_to_ms(chapter.start_frame, sample_rate)}",
            f"END={_frames_to_ms(chapter.end_frame, sample_rate)}",
            f"title={_escape(chapter.title)}",
        ]
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    """Escape ``=``, ``;``, ``#``, ``\\`` and newlines with a backslash, as FFmetadata requires."""
    return _FFMETADATA_SPECIAL.sub(lambda match: "\\" + match.group(), value)


def _frames_to_ms(frames: int, sample_rate: int) -> int:
    """Convert a frame position to whole milliseconds, rounding to the nearest one."""
    return (frames * 1000 + sample_rate // 2) // sample_rate


def _bundled_ffmpeg() -> str:
    """Path to the ffmpeg binary that ships with imageio-ffmpeg."""
    try:
        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except RuntimeError as error:
        raise PackagingError(f"no ffmpeg binary available: {error}") from error


def _ffmpeg_command(
    executable: str,
    *,
    sample_rate: int,
    metadata: Path,
    cover: Path | None,
    bitrate: str,
    output: Path,
) -> list[str]:
    """The ffmpeg command that encodes PCM from stdin into the finished M4B."""
    command = [
        executable,
        "-hide_banner",
        "-nostats",
        "-loglevel", "warning",
        "-y",
        # Input 0: the whole book as raw 16-bit mono PCM, streamed on stdin.
        "-f", "s16le", "-ar", str(sample_rate), "-ch_layout", "mono", "-i", "pipe:0",
        # Input 1: the tags and chapter markers.
        "-f", "ffmetadata", "-i", _ffmpeg_path(metadata),
    ]  # fmt: skip
    if cover is not None:
        command += ["-i", _ffmpeg_path(cover)]  # input 2: the cover image
    command += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover is not None:
        # Copy the image unchanged, stored as cover art rather than as a video track.
        command += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    command += [
        "-c:a", "aac", "-b:a", bitrate, "-ac", "1",
        "-f", "ipod",  # the MP4 flavour ffmpeg uses for .m4a/.m4b
        "-brand", "M4B ",  # the major brand players use to recognise audiobooks
        "-movflags", "+faststart",  # index at the front, so players can start right away
        _ffmpeg_path(output),
    ]  # fmt: skip
    return command


def _ffmpeg_path(path: Path) -> str:
    """A path ffmpeg can't misread: absolute and marked as a file.

    ffmpeg treats ``name:rest`` as a protocol (so ``Dune: Messiah.m4b`` fails) and names
    starting with ``-`` as options; ``file:`` plus an absolute path avoids both.
    """
    return f"file:{path.absolute()}"


def cover_is_usable(cover: Cover, ffmpeg: str | None = None) -> bool:
    """True if ffmpeg can decode ``cover`` cleanly, so it's safe to embed.

    Checked before a book is voiced, so a damaged cover can't fail packaging at the very end.
    """
    try:
        result = subprocess.run(
            [ffmpeg or _bundled_ffmpeg(), "-hide_banner", "-v", "error",
             "-i", "pipe:0", "-f", "null", "-"],
            input=cover.data,
            capture_output=True,
            check=False,
            timeout=60,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stderr.strip()


def _encode(command: list[str], chapters: Sequence[_PlannedChapter], *, log_path: Path) -> None:
    """Run the ffmpeg ``command`` and stream the book's audio into it.

    ffmpeg's output goes to ``log_path`` rather than a pipe, so it can never fill up and
    stall ffmpeg while we're busy writing audio. If anything goes wrong here (a bad segment,
    ffmpeg exiting early, Ctrl+C), ffmpeg is stopped before this returns or raises.
    """
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log
            )
        except OSError as error:
            raise PackagingError(f"couldn't run ffmpeg ({command[0]}): {error}") from error

    assert process.stdin is not None  # always set with stdin=PIPE
    fed_everything = False
    try:
        try:
            _stream_pcm(chapters, process.stdin)
            process.stdin.close()
            fed_everything = True
        except BrokenPipeError:
            pass  # ffmpeg stopped reading; its log says why
        return_code = process.wait()
    finally:
        if process.poll() is None:  # still running, so we're leaving early
            process.kill()
            process.wait()
        with contextlib.suppress(BrokenPipeError):
            process.stdin.close()  # a no-op unless streaming stopped part way

    if return_code != 0 or not fed_everything:
        raise PackagingError(
            f"ffmpeg failed (exit code {return_code}). Its last lines of output:\n"
            f"{_tail(log_path, _STDERR_TAIL_LINES)}"
        )


def _stream_pcm(chapters: Sequence[_PlannedChapter], sink: IO[bytes]) -> None:
    """Write every segment's frames, each followed by its silence, to ``sink``."""
    silence = bytes(_BLOCK_FRAMES * _SAMPLE_WIDTH)
    for chapter in chapters:
        for segment in chapter.segments:
            _copy_frames(segment, sink)
            remaining = segment.gap_frames
            while remaining > 0:
                step = min(remaining, _BLOCK_FRAMES)
                sink.write(silence[: step * _SAMPLE_WIDTH])
                remaining -= step


def _copy_frames(segment: _Segment, sink: IO[bytes]) -> None:
    """Copy a segment's frames to ``sink`` a block at a time, checking none are missing."""
    with _open_wav(segment.path) as wav:
        remaining = segment.frames
        while remaining > 0:
            data = wav.readframes(min(remaining, _BLOCK_FRAMES))
            if not data:
                raise PackagingError(
                    f"{segment.path} ended {remaining} frames early; its header says "
                    f"{segment.frames} frames, so the file is probably truncated"
                )
            sink.write(data)
            remaining -= len(data) // _SAMPLE_WIDTH


def _tail(path: Path, count: int) -> str:
    """The last ``count`` lines of a text file, or a note if it is empty."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-count:]) if lines else "(ffmpeg printed nothing)"
