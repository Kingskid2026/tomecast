"""Tests for the M4B packager, run against the real bundled ffmpeg on short generated tones."""

from __future__ import annotations

import array
import math
import re
import struct
import subprocess
import sys
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]
import pytest

from tomecast.book import Cover
from tomecast.packaging import (
    AudiobookInfo,
    ChapterAudio,
    PackagedChapter,
    PackagingError,
    write_m4b,
)

FFMPEG: str = imageio_ffmpeg.get_ffmpeg_exe()
RATE = 24_000
TOLERANCE = 0.05  # seconds

INFO = AudiobookInfo(
    title="The Test Book",
    authors=("Ada Writer", "Bo Coauthor"),
    narrator="Kokoro (af_heart)",
    language="en",
    cover=None,
)


# --- Making input files -------------------------------------------------------------------


def write_tone(
    path: Path,
    seconds: float,
    *,
    rate: int = RATE,
    channels: int = 1,
    frequency: float = 440.0,
) -> Path:
    """Write a sine tone as a 16-bit PCM WAV file."""
    frames = round(seconds * rate)
    samples = array.array(
        "h",
        (
            round(8000 * math.sin(2 * math.pi * frequency * i / rate))
            for i in range(frames)
            for _ in range(channels)
        ),
    )
    if sys.byteorder == "big":
        samples.byteswap()  # WAV data is little-endian
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return path


def chapter(
    folder: Path, title: str, parts: list[tuple[float, float]], frequency: float = 440.0
) -> ChapterAudio:
    """A chapter of tones: ``parts`` is a list of (tone seconds, gap seconds after it)."""
    stem = re.sub(r"\W+", "-", title).strip("-") or "untitled"
    segments = tuple(
        write_tone(folder / f"{stem}-{i}.wav", seconds, frequency=frequency)
        for i, (seconds, _) in enumerate(parts)
    )
    return ChapterAudio(title, segments, tuple(gap for _, gap in parts))


def three_chapters(folder: Path) -> list[ChapterAudio]:
    """Chapters lasting 1.6 s, 1.3 s and 2.25 s, each ending in silence."""
    return [
        chapter(folder, "Prologue", [(0.5, 0.2), (0.3, 0.6)], frequency=330),
        chapter(folder, "The Road", [(0.8, 0.5)], frequency=440),
        chapter(folder, "Home", [(0.4, 0.1), (0.6, 0.25), (0.2, 0.7)], frequency=550),
    ]


THREE_CHAPTERS_EXPECTED = [("Prologue", 0.0, 1.6), ("The Road", 1.6, 2.9), ("Home", 2.9, 5.15)]


def tiny_png() -> bytes:
    """A valid 2x2 red PNG, built by hand."""

    def png_chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    width = height = 2
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(rows))
        + png_chunk(b"IEND", b"")
    )


def tiny_jpeg(folder: Path) -> bytes:
    """A valid 16x16 red JPEG, made by the bundled ffmpeg."""
    path = folder / "cover.jpg"
    run_ffmpeg("-f", "lavfi", "-i", "color=red:s=16x16", "-frames:v", "1", str(path))
    return path.read_bytes()


# --- Reading the result back --------------------------------------------------------------


def run_ffmpeg(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", *args],
        capture_output=True,
        check=check,
    )


@dataclass(frozen=True)
class Metadata:
    tags: dict[str, str]
    chapters: list[tuple[str, float, float]]  # (title, start seconds, end seconds)


_FFMETADATA_LINE = re.compile(r"(?:\\.|[^\\\n])*", re.DOTALL)  # a line, with escapes
_FFMETADATA_ENTRY = re.compile(r"((?:\\.|[^\\=])*)=(.*)", re.DOTALL)  # key=value


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text, flags=re.DOTALL)


def read_metadata(path: Path) -> Metadata:
    """Read tags and chapters with ``ffmpeg -i <file> -f ffmetadata -``."""
    text = run_ffmpeg("-i", str(path), "-f", "ffmetadata", "-").stdout.decode("utf-8")
    tags: dict[str, str] = {}
    chapters: list[dict[str, str]] = []
    section = tags
    for line in _FFMETADATA_LINE.findall(text):
        if not line or line.startswith((";", "#")):
            continue
        if line == "[CHAPTER]":
            section = {}
            chapters.append(section)
        elif entry := _FFMETADATA_ENTRY.fullmatch(line):
            section[_unescape(entry[1])] = _unescape(entry[2])

    def seconds(chapter: dict[str, str], key: str) -> float:
        return float(int(chapter[key]) * Fraction(chapter["TIMEBASE"]))

    return Metadata(
        tags,
        [(c.get("title", ""), seconds(c, "START"), seconds(c, "END")) for c in chapters],
    )


@dataclass(frozen=True)
class Probe:
    duration: float
    audio: list[str]  # stream descriptions, e.g. "aac (LC) (mp4a / ...), 24000 Hz, mono, ..."
    video: list[str]


def probe(path: Path) -> Probe:
    """Read the duration and streams from what ``ffmpeg -i <file>`` prints."""
    stderr = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True, check=False
    ).stderr.decode("utf-8")
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    assert duration, stderr
    hours, minutes, secs = duration.groups()
    streams = re.findall(r"Stream #0:\d+\S*: (\w+): (.*)", stderr)
    return Probe(
        duration=int(hours) * 3600 + int(minutes) * 60 + float(secs),
        audio=[description for kind, description in streams if kind == "Audio"],
        video=[description for kind, description in streams if kind == "Video"],
    )


def decode(path: Path) -> array.array[int]:
    """Decode the audiobook's audio back to 16-bit mono samples."""
    pcm = run_ffmpeg("-i", str(path), "-map", "0:a", "-f", "s16le", "-ac", "1", "-").stdout
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def extract_cover(path: Path) -> bytes:
    """The attached cover image, copied out unchanged."""
    return run_ffmpeg("-i", str(path), "-map", "0:v", "-c", "copy", "-f", "image2pipe", "-").stdout


# --- Running the packager -----------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    output: Path
    workdir: Path
    chapters: list[PackagedChapter]


def package(
    tmp_path: Path,
    chapters: list[ChapterAudio],
    info: AudiobookInfo = INFO,
    *,
    bitrate: str = "64k",
    ffmpeg: str | None = None,
) -> Run:
    """Package ``chapters`` into ``tmp_path/out/book.m4b``, using ``tmp_path/work``."""
    output = tmp_path / "out" / "book.m4b"
    workdir = tmp_path / "work"
    output.parent.mkdir(exist_ok=True)
    packaged = write_m4b(chapters, info, output, workdir=workdir, bitrate=bitrate, ffmpeg=ffmpeg)
    return Run(output, workdir, packaged)


@pytest.fixture(scope="module")
def three_chapter_book(tmp_path_factory: pytest.TempPathFactory) -> Run:
    tmp_path = tmp_path_factory.mktemp("book")
    return package(tmp_path, three_chapters(tmp_path / "segments"))


# --- A normal book ------------------------------------------------------------------------


def test_returns_chapter_times_from_exact_frame_counts(three_chapter_book: Run) -> None:
    chapters = three_chapter_book.chapters

    assert all(isinstance(c, PackagedChapter) for c in chapters)
    assert [c.title for c in chapters] == [title for title, _, _ in THREE_CHAPTERS_EXPECTED]
    assert [c.start_seconds for c in chapters] == pytest.approx([0.0, 1.6, 2.9], abs=1e-9)
    assert [c.end_seconds for c in chapters] == pytest.approx([1.6, 2.9, 5.15], abs=1e-9)


def test_chapter_markers_have_the_right_titles_and_times(three_chapter_book: Run) -> None:
    chapters = read_metadata(three_chapter_book.output).chapters

    assert len(chapters) == 3
    for (title, start, end), (want_title, want_start, want_end) in zip(
        chapters, THREE_CHAPTERS_EXPECTED, strict=True
    ):
        assert title == want_title
        assert start == pytest.approx(want_start, abs=TOLERANCE)
        assert end == pytest.approx(want_end, abs=TOLERANCE)


def test_length_is_the_sum_of_segments_and_gaps(three_chapter_book: Run) -> None:
    assert probe(three_chapter_book.output).duration == pytest.approx(5.15, abs=TOLERANCE)
    # Decoded, it's sample-exact: AAC's encoder delay and padding are trimmed.
    assert len(decode(three_chapter_book.output)) == round(5.15 * RATE)


def test_each_chapter_starts_speaking_at_its_marker(three_chapter_book: Run) -> None:
    """No drift: each chapter's first sound is where its marker says, not later."""
    samples = decode(three_chapter_book.output)
    for packaged in three_chapter_book.chapters:
        marker = round(packaged.start_seconds * RATE)
        search_from = max(0, marker - RATE // 10)  # 0.1 s of the previous chapter's silence
        onset = next(i for i in range(search_from, len(samples)) if abs(samples[i]) > 4000)
        assert abs(onset - marker) / RATE < 0.005, packaged.title


def test_has_exactly_one_mono_aac_stream_and_no_cover(three_chapter_book: Run) -> None:
    streams = probe(three_chapter_book.output)

    assert len(streams.audio) == 1
    assert streams.audio[0].startswith("aac")
    assert "24000 Hz, mono" in streams.audio[0]
    assert streams.video == []


def test_writes_book_tags(three_chapter_book: Run) -> None:
    tags = read_metadata(three_chapter_book.output).tags

    assert tags["title"] == "The Test Book"
    assert tags["album"] == "The Test Book"
    assert tags["artist"] == "Ada Writer, Bo Coauthor"
    assert tags["album_artist"] == "Ada Writer, Bo Coauthor"
    assert tags["composer"] == "Kokoro (af_heart)"
    assert tags["genre"] == "Audiobook"
    assert tags["major_brand"].strip() == "M4B"


def test_success_leaves_no_temporary_files(three_chapter_book: Run) -> None:
    assert list(three_chapter_book.workdir.iterdir()) == []
    assert list(three_chapter_book.output.parent.iterdir()) == [three_chapter_book.output]


def test_replaces_an_existing_output(tmp_path: Path) -> None:
    old = tmp_path / "out" / "book.m4b"
    old.parent.mkdir()
    old.write_bytes(b"an older audiobook")

    run = package(tmp_path, [chapter(tmp_path, "Only", [(0.3, 0.1)])])

    assert len(probe(run.output).audio) == 1


# --- Covers and titles --------------------------------------------------------------------


@pytest.mark.parametrize(("media_type", "codec"), [("image/png", "png"), ("image/jpeg", "mjpeg")])
def test_cover_is_attached_as_cover_art(tmp_path: Path, media_type: str, codec: str) -> None:
    data = tiny_png() if media_type == "image/png" else tiny_jpeg(tmp_path)
    info = replace(INFO, cover=Cover(data, media_type))

    run = package(tmp_path, [chapter(tmp_path, "Only", [(0.3, 0.1)])], info)

    streams = probe(run.output)
    assert len(streams.audio) == 1
    assert len(streams.video) == 1
    assert streams.video[0].startswith(codec)
    assert "(attached pic)" in streams.video[0]
    assert extract_cover(run.output) == data


def test_empty_chapter_titles_become_chapter_n(tmp_path: Path) -> None:
    chapters = [
        chapter(tmp_path, "Opening", [(0.3, 0.1)]),
        chapter(tmp_path, "", [(0.3, 0.1)]),
        chapter(tmp_path, "   ", [(0.3, 0.1)]),
    ]

    run = package(tmp_path, chapters)

    expected = ["Opening", "Chapter 2", "Chapter 3"]
    assert [c.title for c in run.chapters] == expected
    assert [title for title, _, _ in read_metadata(run.output).chapters] == expected


def test_special_characters_in_titles_survive(tmp_path: Path) -> None:
    titles = ["Odds = evens; #1 \\ two", "Line one\nLine two", "Namárië"]
    chapters = [chapter(tmp_path, title, [(0.3, 0.1)]) for title in titles]
    info = replace(INFO, title="Q = A; #1 \\ Namárië")

    run = package(tmp_path, chapters, info)

    metadata = read_metadata(run.output)
    assert [title for title, _, _ in metadata.chapters] == titles
    assert metadata.tags["title"] == "Q = A; #1 \\ Namárië"


# --- Bad input ----------------------------------------------------------------------------


def _mismatched_rates(folder: Path) -> list[ChapterAudio]:
    fast = write_tone(folder / "fast.wav", 0.3, rate=22_050)
    return [chapter(folder, "One", [(0.3, 0.1)]), ChapterAudio("Two", (fast,), (0.1,))]


def _stereo(folder: Path) -> list[ChapterAudio]:
    return [ChapterAudio("One", (write_tone(folder / "stereo.wav", 0.3, channels=2),), (0.1,))]


def _eight_bit(folder: Path) -> list[ChapterAudio]:
    path = folder / "eight-bit.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(RATE)
        wav.writeframes(bytes([128]) * 1000)
    return [ChapterAudio("One", (path,), (0.1,))]


def _not_a_wav(folder: Path) -> list[ChapterAudio]:
    path = folder / "notes.wav"
    path.write_text("this is not audio")
    return [ChapterAudio("One", (path,), (0.1,))]


def _missing_file(folder: Path) -> list[ChapterAudio]:
    return [ChapterAudio("One", (folder / "missing.wav",), (0.1,))]


def _wrong_gap_count(folder: Path) -> list[ChapterAudio]:
    two = chapter(folder, "Two", [(0.3, 0.1), (0.3, 0.1)])
    return [chapter(folder, "One", [(0.3, 0.1)]), replace(two, gaps_after=(0.1,))]


def _negative_gap(folder: Path) -> list[ChapterAudio]:
    return [chapter(folder, "One", [(0.3, -0.5)])]


def _empty_chapter(folder: Path) -> list[ChapterAudio]:
    return [chapter(folder, "One", [(0.3, 0.1)]), ChapterAudio("Empty", (), ())]


def _silent_chapter(folder: Path) -> list[ChapterAudio]:
    return [chapter(folder, "One", [(0.0, 0.0)])]


def _no_chapters(folder: Path) -> list[ChapterAudio]:
    return []


@pytest.mark.parametrize(
    ("make_chapters", "message"),
    [
        (_mismatched_rates, r"fast\.wav is 22050 Hz, but .*One-0\.wav is 24000 Hz"),
        (_stereo, r"stereo\.wav has 2 channels; segments must be mono"),
        (_eight_bit, r"eight-bit\.wav has 8-bit samples; segments must be 16-bit PCM"),
        (_not_a_wav, r"notes\.wav is not a readable PCM WAV file"),
        (_missing_file, r"audio segment not found: .*missing\.wav"),
        (_wrong_gap_count, r"chapter 2 \('Two'\) has 2 segments but 1 gaps_after values"),
        (_negative_gap, r"chapter 1 \('One'\): the gap after .*One-0\.wav must be .* >= 0"),
        (_empty_chapter, r"chapter 2 \('Empty'\) has no audio segments"),
        (_silent_chapter, r"chapter 1 \('One'\) has no audio"),
        (_no_chapters, r"no chapters"),
    ],
)
def test_bad_input_is_rejected_before_encoding(
    tmp_path: Path, make_chapters: Callable[[Path], list[ChapterAudio]], message: str
) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()

    with pytest.raises(PackagingError, match=message):
        package(tmp_path, make_chapters(segments))

    assert list((tmp_path / "out").iterdir()) == []
    assert not (tmp_path / "work").exists()


def test_unsupported_cover_type_is_rejected(tmp_path: Path) -> None:
    info = replace(INFO, cover=Cover(b"GIF89a", "image/gif"))

    with pytest.raises(PackagingError, match="unsupported cover type 'image/gif'"):
        package(tmp_path, [chapter(tmp_path, "Only", [(0.3, 0.1)])], info)


# --- Failures part way through ------------------------------------------------------------


def test_ffmpeg_failure_reports_its_output_and_cleans_up(tmp_path: Path) -> None:
    old = tmp_path / "out" / "book.m4b"
    old.parent.mkdir()
    old.write_bytes(b"an older audiobook")
    chapters = three_chapters(tmp_path / "segments")

    with pytest.raises(PackagingError, match="ffmpeg failed") as error:
        package(tmp_path, chapters, bitrate="nonsense")

    assert "nonsense" in str(error.value)  # the tail of ffmpeg's output is included
    assert old.read_bytes() == b"an older audiobook"
    assert list(old.parent.iterdir()) == [old]
    assert list((tmp_path / "work").iterdir()) == []


def test_truncated_segment_stops_the_encode_and_cleans_up(tmp_path: Path) -> None:
    chapters = three_chapters(tmp_path / "segments")
    cut = chapters[1].segments[0]
    with cut.open("r+b") as wav_file:
        wav_file.truncate(cut.stat().st_size - 4000)

    with pytest.raises(PackagingError, match=r"The-Road-0\.wav ended 2000 frames early"):
        package(tmp_path, chapters)

    assert list((tmp_path / "out").iterdir()) == []
    assert list((tmp_path / "work").iterdir()) == []


def test_missing_ffmpeg_is_reported(tmp_path: Path) -> None:
    chapters = [chapter(tmp_path, "Only", [(0.3, 0.1)])]

    with pytest.raises(PackagingError, match="couldn't run ffmpeg"):
        package(tmp_path, chapters, ffmpeg=str(tmp_path / "no-such-ffmpeg"))

    assert list((tmp_path / "work").iterdir()) == []
