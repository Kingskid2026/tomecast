"""Read scanned PDF pages (pictures with no text layer) with an OCR model. Prototype for #14.

The PDF reader refuses scans today. This module is the add-on's core, kept apart from
:mod:`tomecast.readers.pdf` until the design is settled:

1. :func:`textless_pages` finds the pages that are pictures with no text layer.
2. :func:`render_page` draws one as a PNG no bigger than the model reads (about a megapixel).
3. An :class:`OcrRunner` turns the picture into text in reading order. The runner is a small
   interface, so the model and the program that runs it (llama.cpp's server, see
   :mod:`tomecast.llama_ocr`) can change without touching the rest.
4. :func:`page_problem` checks each page's text: empty, cut off at the token limit (usually a
   repetition loop), the same line over and over, or too many words that aren't words.
5. :func:`read_scanned` reads every page with the light model (Q4_K_M), then re-reads only the
   pages that look broken with the full one (Q8_0). Only one model is loaded at a time.
6. :func:`page_blocks` turns a page's text into :class:`~tomecast.book.Block` objects, with the
   running header and the page number marked as bands, so cleanup treats a scanned page like
   any other PDF page.

Nothing here drops text: every word the model returns ends up in a block (tested).
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from tomecast.book import Band, Block
from tomecast.cleanup import is_page_number
from tomecast.readers.errors import InvalidEbookError
from tomecast.readers.pdf import _picture_share, _png

# PaddleOCR-VL reads pictures of up to 1280 x 28 x 28 pixels (its processor's max_pixels);
# llama.cpp shrinks anything bigger, so rendering larger only wastes time.
MAX_PIXELS = 1_003_520
PICTURE_SHARE = 0.5  # a text-less page is a scanned page if pictures cover this much of it
UNKNOWN_SHARE = 0.15  # re-read a page if more of its words than this aren't known words
REPEATED_LINES = 4  # ...or if one line comes back this many times in a row
HEADER_WORDS = 8  # a running header is a short line

# "[17]", "(xii)" or "- 17 -" at the very end of a line, after a space.
_TRAILING_PAGE_NUMBER = re.compile(
    r"\s+(\[\s*(?:\d{1,4}|[ivxlc]{1,7})\s*\]|\(\s*(?:\d{1,4}|[ivxlc]{1,7})\s*\)"
    r"|[-\u2013\u2014]\s*\d{1,4}\s*[-\u2013\u2014])$",
    re.IGNORECASE,
)
_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*")


@dataclass(frozen=True, slots=True)
class PageImage:
    """A page drawn as a PNG for the OCR model. ``page`` is 0-based."""

    page: int
    width: int
    height: int
    png: bytes


@dataclass(frozen=True, slots=True)
class OcrResult:
    """What the model read on one page."""

    text: str
    seconds: float
    truncated: bool = False  # it stopped at the token limit instead of finishing


class OcrRunner(Protocol):
    """Reads one page picture into text, in reading order."""

    def read(self, image: PageImage) -> OcrResult: ...


RunnerFactory = Callable[[], AbstractContextManager[OcrRunner]]


@dataclass(frozen=True, slots=True)
class ScannedPage:
    """One page's final text, which model read it, and any problem left after the re-read."""

    page: int
    text: str
    seconds: float  # both reads together when the page was re-read
    reread: bool
    problem: str | None


# --- Finding and drawing scanned pages ------------------------------------------------------


def textless_pages(document: Any) -> list[int]:
    """The pages (0-based) that are pictures with no text layer at all.

    Blank pages (no text, no pictures) aren't listed: there is nothing to read on them.
    """
    found = []
    for index in range(len(document)):
        page = document[index]
        try:
            textpage = page.get_textpage()
            try:
                has_text = bool(textpage.get_text_range().strip())
            finally:
                textpage.close()
            if has_text:
                continue
            width, height = (float(value) for value in page.get_size())
            if _picture_share(page, width, height) >= PICTURE_SHARE:
                found.append(index)
        finally:
            page.close()
    return found


def render_page(document: Any, index: int, max_pixels: int = MAX_PIXELS) -> PageImage:
    """Draw page ``index`` as a PNG of at most ``max_pixels`` pixels, keeping its shape."""
    page = document[index]
    try:
        width, height = (float(value) for value in page.get_size())
        scale = math.sqrt(max_pixels / (width * height)) if width > 0 and height > 0 else 1.0
        # Round down so rounding the pixel size up can't go over the limit.
        while math.ceil(width * scale) * math.ceil(height * scale) > max_pixels:
            scale *= 0.995
        bitmap = page.render(
            scale=scale, force_bitmap_format=pdfium_c.FPDFBitmap_BGR, rev_byteorder=True
        )
        try:
            pixels_wide, pixels_high = int(bitmap.width), int(bitmap.height)
            stride = int(bitmap.stride)
            data = bytes(bitmap.buffer)
        finally:
            bitmap.close()
    finally:
        page.close()
    rows = (data[row * stride : row * stride + pixels_wide * 3] for row in range(pixels_high))
    return PageImage(index, pixels_wide, pixels_high, _png(pixels_wide, pixels_high, rows))


# --- Checking what came back ---------------------------------------------------------------


def unknown_word_share(text: str, known: frozenset[str]) -> float:
    """The share of words (two letters or more) that aren't in ``known`` (lowercase words).

    Capitalised words count as known, because they're usually names, which no word list has.
    """
    joined = re.sub(r"-[ \t]*\n\s*", "", text)  # "pen-" + "nies" is one word
    words = [word for word in _WORD.findall(joined) if len(word) >= 2]
    if not words:
        return 0.0
    unknown = sum(
        1
        for word in words
        if not word[0].isupper() and word.casefold().replace("\u2019", "'") not in known
    )
    return unknown / len(words)


def page_problem(result: OcrResult, known: frozenset[str]) -> str | None:
    """Why a page's text looks broken, or ``None`` if it looks fine."""
    if not result.text.strip():
        return "no text"
    if result.truncated:
        return "cut off at the token limit"
    lines = [line.strip() for line in result.text.splitlines() if line.strip()]
    run = 1
    for before, line in itertools.pairwise(lines):
        run = run + 1 if line == before else 1
        if run >= REPEATED_LINES:
            return f"a line repeated {run} times"
    share = unknown_word_share(result.text, known)
    if share > UNKNOWN_SHARE:
        return f"{share:.0%} of its words aren't words"
    return None


# --- Reading a scan --------------------------------------------------------------------------


def read_scanned(
    path: Path,
    light: RunnerFactory,
    full: RunnerFactory | None,
    known: frozenset[str],
    *,
    pages: Sequence[int] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[ScannedPage]:
    """OCR the scanned pages of ``path``: all with ``light``, broken ones again with ``full``.

    ``pages`` defaults to :func:`textless_pages`. ``progress(done, total)`` is called after
    each page of the first pass. The light model is closed before the full one is opened.
    """
    try:
        document = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as error:
        raise InvalidEbookError(f"{path.name} isn't a PDF, or it's damaged") from error
    try:
        wanted = list(pages) if pages is not None else textless_pages(document)
        first: dict[int, OcrResult] = {}
        with light() as runner:
            for done, index in enumerate(wanted, start=1):
                first[index] = runner.read(render_page(document, index))
                if progress is not None:
                    progress(done, len(wanted))
        problems = {index: page_problem(result, known) for index, result in first.items()}
        broken = [index for index in wanted if problems[index] is not None]
        second: dict[int, OcrResult] = {}
        if broken and full is not None:
            with full() as runner:
                for index in broken:
                    second[index] = runner.read(render_page(document, index))
    finally:
        document.close()

    results = []
    for index in wanted:
        result, problem, reread = first[index], problems[index], index in second
        seconds = result.seconds
        if reread:
            again = second[index]
            seconds += again.seconds
            # The 8-bit read wins, unless it came back empty and the first one didn't.
            if again.text.strip() or not result.text.strip():
                result, problem = again, page_problem(again, known)
        results.append(ScannedPage(index, result.text, seconds, reread, problem))
    return results


# --- Turning a page's text into blocks -------------------------------------------------------


def page_blocks(page: ScannedPage) -> list[Block]:
    """The page's text as blocks: a running header and a page number as bands, then paragraphs.

    PaddleOCR-VL writes each paragraph on one line, with words split at line ends already
    rejoined, so each line is a paragraph. If the text has blank lines, they separate the
    paragraphs instead, and lines inside one are kept as printed, so
    :func:`~tomecast.cleanup.join_lines` can rejoin hyphenated words. A page number in
    brackets that the model ran onto the last line ("rare and [17]") is split off as the
    footer. The first block may carry on the paragraph from the page before.
    """
    lines = [line.strip() for line in page.text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    header = footer = None
    if len(lines) > 1 and _is_running_header(lines[0]):
        header = lines.pop(0)
    if len(lines) > 1 and is_page_number(lines[-1]):
        footer = lines.pop()
    elif len(lines) > 1 and is_page_number(lines[0]) and header is None:
        header = lines.pop(0)
    elif lines and (glued := _TRAILING_PAGE_NUMBER.search(lines[-1])) and glued.start() > 0:
        footer = glued.group(1)
        lines[-1] = lines[-1][: glued.start()]

    blocks = []
    if header is not None:
        blocks.append(Block((header,), page=page.page, band=Band.TOP))
    for number, paragraph in enumerate(_paragraphs(lines)):
        blocks.append(Block(tuple(paragraph), page=page.page, continues=number == 0))
    if footer is not None:
        blocks.append(Block((footer,), page=page.page, band=Band.BOTTOM))
    return blocks


def _is_running_header(line: str) -> bool:
    """A short line in capitals ("THE GIFT OF THE MAGI", "12 THE FOUR MILLION")."""
    if is_page_number(line):
        return True
    letters = [char for char in line if char.isalpha()]
    return (
        len(line.split()) <= HEADER_WORDS
        and bool(letters)
        and all(char.isupper() for char in letters)
    )


def _paragraphs(lines: Sequence[str]) -> list[list[str]]:
    if all(lines):  # no blank lines: one paragraph per line
        return [[line] for line in lines]
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    return [paragraph for paragraph in paragraphs if paragraph]


# --- Time estimate ---------------------------------------------------------------------------


def estimate_seconds(
    page_count: int,
    seconds_per_page: float,
    *,
    reread_share: float = 0.1,
    full_slowdown: float = 1.2,
) -> float:
    """How long OCR should take: every page once, plus re-reading a share of them at 8-bit.

    ``seconds_per_page`` comes from timing one page on this machine (or from the hardware
    scan's table); the 8-bit model reads a little slower than the 4-bit one.
    """
    return page_count * seconds_per_page * (1 + reread_share * full_slowdown)
