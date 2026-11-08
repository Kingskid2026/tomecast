"""Check an audiobook from the integration test: it plays, and has one spoken chapter per section.

Usage: python check_audiobook.py BOOK.m4b BOOK.expected.json

Run it with the Python Tomecast is installed in: it uses the same ffmpeg Tomecast does (from
imageio-ffmpeg), so the test needs nothing from the distro. It checks that:

* ffmpeg decodes the whole file without errors, and it's longer than a second;
* there is one chapter per section of the book, with the book's titles, in order, back to back
  from the start to the end of the audio;
* every chapter has sound in it, not silence.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
QUIET_DB = -45.0  # a chapter quieter than this on average is silence, not speech


@dataclass
class ChapterMark:
    title: str
    start: float
    end: float


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def ffmpeg(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", *args], capture_output=True, text=True, check=False
    )


def chapters(book: Path) -> list[ChapterMark]:
    result = ffmpeg("-v", "error", "-i", str(book), "-f", "ffmetadata", "-")
    if result.returncode != 0:
        fail(f"ffmpeg couldn't read the chapters:\n{result.stderr}")
    marks: list[ChapterMark] = []
    for block in result.stdout.split("[CHAPTER]")[1:]:
        fields = dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)
        num, den = (int(n) for n in fields["TIMEBASE"].split("/"))
        marks.append(
            ChapterMark(
                title=fields.get("title", ""),
                start=int(fields["START"]) * num / den,
                end=int(fields["END"]) * num / den,
            )
        )
    return marks


def decode(book: Path, start: float | None = None, end: float | None = None) -> tuple[float, str]:
    """Decode (part of) the audio; return its mean volume in dB and ffmpeg's log."""
    window = [] if start is None or end is None else ["-ss", f"{start}", "-to", f"{end}"]
    result = ffmpeg(
        "-i", str(book), *window, "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"
    )
    if result.returncode != 0 or re.search(r"error|invalid", result.stderr, re.IGNORECASE):
        fail(f"ffmpeg couldn't decode {book.name}:\n{result.stderr[-2000:]}")
    found = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", result.stderr)
    if found is None:
        fail(f"ffmpeg didn't report a volume:\n{result.stderr[-2000:]}")
    assert found is not None
    return float(found.group(1)), result.stderr


def main() -> None:
    book, expected_file = Path(sys.argv[1]), Path(sys.argv[2])
    expected: list[str] = json.loads(expected_file.read_text())["chapters"]

    volume, log = decode(book)
    found = re.search(r"Duration: (\d+):(\d+):([\d.]+)", log)
    if found is None:
        fail(f"ffmpeg didn't report a duration:\n{log[-2000:]}")
    assert found is not None
    hours, minutes, seconds = found.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    stream = re.search(r"Stream #0:0.*?Audio: (.*)", log)
    print(f"{book.name}: {duration:.2f} s, {stream.group(1) if stream else '?'}")
    print(f"  whole book: mean volume {volume:.1f} dB")
    if duration <= 1.0:
        fail(f"the audiobook is only {duration:.2f} s long")

    marks = chapters(book)
    titles = [mark.title for mark in marks]
    if titles != expected:
        fail(f"expected chapters {expected}, found {titles}")
    previous_end = 0.0
    for mark in marks:
        chapter_volume, _ = decode(book, mark.start, mark.end)
        length = mark.end - mark.start
        print(f"  {mark.title}: {length:.2f} s, mean volume {chapter_volume:.1f} dB")
        if abs(mark.start - previous_end) > 0.01:
            fail(f"chapter {mark.title!r} starts at {mark.start:.3f} s, not {previous_end:.3f} s")
        if length < 1.0:
            fail(f"chapter {mark.title!r} is only {length:.2f} s long")
        if chapter_volume < QUIET_DB:
            fail(f"chapter {mark.title!r} is silent ({chapter_volume:.1f} dB)")
        previous_end = mark.end
    if abs(previous_end - duration) > 0.5:
        fail(f"the chapters end at {previous_end:.2f} s, but the audio is {duration:.2f} s long")
    print(f"OK: {len(marks)} chapters, all with speech, {duration:.2f} s in total")


if __name__ == "__main__":
    main()
