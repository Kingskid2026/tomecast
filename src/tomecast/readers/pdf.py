"""Read PDF books into a :class:`~tomecast.book.Book`, with pypdfium2 (PDFium, as in Chrome).

A PDF has no paragraphs or chapters, only characters placed on pages. This reader rebuilds
the book from where the characters are:

1. **Characters → segments:** characters drawn one after another on the same baseline, close
   together. A wide gap (a column gutter) starts a new segment.
2. **Bands:** a short line on its own at the very top or bottom of a page (a running header,
   a footer, a page number) is kept apart, so cleanup can decide whether it's page furniture.
3. **Columns:** if the page's narrow segments leave a clear vertical gap down the middle, the
   page has two columns. Lines that cross the gap (a title over both columns) split the page
   into bands that are read in turn; within a two-column band the left column comes first.
4. **Lines → blocks:** lines are grouped into blocks (paragraphs) by size changes, wide gaps
   and indents. Small raised characters ("Della¹") are footnote markers and get wrapped in
   :data:`~tomecast.book.MARKER_START` and :data:`~tomecast.book.MARKER_END`.
5. **Roles:** small text at the foot of a page that starts with a marker is a footnote; text
   noticeably larger than the body is a heading; rows of three or more aligned cells are a
   table.
6. **Chapters** come from the PDF's bookmarks (its outline) when it has them. Otherwise a
   heading line starts a chapter: one in a noticeably larger font than the body, or one that
   reads "Chapter N" on its own. Short pages before the first chapter (title page, copyright
   page, contents) become sections of their own, so cleanup can spot them.

The result keeps this layout in ``Book.raw``, and ``Book.chapters`` holds every block of text
as found. :func:`tomecast.cleanup.clean` then removes page numbers, headers, footnotes and the
like, and joins paragraphs split by page and column breaks.

Encrypted and damaged PDFs raise :class:`InvalidEbookError`, and so do scans (pages that are
pictures with no text layer), which need OCR.
"""

from __future__ import annotations

import ctypes
import itertools
import math
import re
import statistics
import struct
import unicodedata
import zlib
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from tomecast.book import (
    MARKER_END,
    MARKER_START,
    Band,
    Block,
    Book,
    Cover,
    RawChapter,
    Role,
)
from tomecast.cleanup import (
    CleanupOptions,
    clean,
    is_chapter_heading,
    is_page_number,
    join_lines,
)
from tomecast.readers._markup import _repeats
from tomecast.readers.errors import InvalidEbookError

# Lines and gaps are measured in ems (multiples of the font size).
SEGMENT_GAP_EMS = 1.0  # a wider gap between two characters on a line starts a new segment
BAND_SHARE = 0.2  # headers and footers sit in the top or bottom fifth of the page...
BAND_GAP_EMS = 1.8  # ...with at least this much space between them and the text
HEADING_RATIO = 1.15  # text this much larger than the body is a heading
CHAPTER_RATIO = 1.25  # and this much larger starts a chapter
FOOTNOTE_RATIO = 0.9  # footnotes are set smaller than the body
FRONT_PAGE_WORDS = 150  # pages this short before the first chapter are sections of their own
COVER_IMAGE_SHARE = 0.5  # the first page is the cover if pictures fill this much of it
COVER_WIDTH = 600  # pixels
SCAN_SHARE = 0.5  # a PDF is a scan if more than this share of its pages are text-less pictures
SIDEWAYS_SHARE = 0.2  # refuse a PDF if more of its text than this is set sideways
OUTLINE_LEVELS = 2  # bookmark levels used as chapters (chapters, and sections in them)

# Characters to read as something else, or not at all.
_HYPHEN_CODES = {0x02, 0xFFFE}  # PDFium's marks for a hyphen at the end of a line
_SKIPPED_CODES = {0x00, 0xFFFF}
_LIGATURES = range(0xFB00, 0xFB07)
_MARKER_TEXT = re.compile(r"^(?:[0-9*\u2020\u2021\u00a7\u00b6\u2016#,]+|[a-z])$")
_NOTE_START = re.compile(
    rf"^\s*(?:{MARKER_START}|(?:\d{{1,3}}|[*\u2020\u2021\u00a7\u00b6]+)[.)]?\s)"
)
_SENTENCE_END = re.compile(r"[.!?\u2026:][\"'\u201d\u2019)\]]*$")
_JUNK_TITLE = re.compile(
    r"^(?:microsoft\s+word\s*-\s*|untitled\b)|\.(?:docx?|pdf|indd|tex|rtf)$", re.I
)


def read_pdf(path: Path) -> Book:
    """Read a PDF book into a :class:`~tomecast.book.Book`.

    Raises:
        InvalidEbookError: if the file isn't a PDF or is damaged, needs a password, is a
            scan with no text layer, or has no readable text.
        OSError: if the file itself can't be opened.
    """
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    try:
        document = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as error:
        raise InvalidEbookError(_open_error(path, error)) from error
    try:
        return _read_document(document, path)
    finally:
        document.close()


def _open_error(path: Path, error: Any) -> str:
    code = getattr(error, "err_code", None)
    if code == pdfium_c.FPDF_ERR_PASSWORD:
        return f"{path.name} is protected by a password, so it can't be read"
    if code == pdfium_c.FPDF_ERR_SECURITY:
        return f"{path.name} is encrypted in a way Tomecast can't read"
    return f"{path.name} isn't a PDF, or it's damaged"


def _read_document(document: Any, path: Path) -> Book:
    pages: list[_Page] = []
    for index in range(len(document)):
        try:
            page = document[index]
        except pdfium.PdfiumError as error:
            raise InvalidEbookError(f"page {index + 1} of {path.name} can't be read") from error
        try:
            pages.append(_read_page(page, index))
        finally:
            page.close()

    chars = sum(page.chars for page in pages)
    sideways = sum(page.sideways for page in pages)
    pictures_only = sum(1 for page in pages if page.chars == 0 and page.picture_share > 0.5)
    if (chars == 0 and pictures_only) or (
        pictures_only >= 2 and pictures_only > SCAN_SHARE * len(pages)
    ):
        raise InvalidEbookError(
            f"{path.name} is a scan: its pages are pictures, with no text layer to read. "
            "Reading scanned PDFs (OCR) comes with an add-on"
        )
    if sideways > SIDEWAYS_SHARE * (chars + sideways):
        raise InvalidEbookError(f"{path.name} has text set sideways, which Tomecast can't read yet")
    if chars == 0:
        raise InvalidEbookError(f"{path.name} has no readable text")

    body_size = _body_size(pages)
    blocks = _classify(pages, body_size)
    starts = _outline_starts(document, pages, blocks) or _heading_starts(blocks, body_size)
    raw = _assemble(blocks, starts, pages)
    title, authors = _metadata(document, path)
    book = Book(
        title=title,
        authors=authors,
        language=None,
        chapters=(),
        cover=_cover(document, pages),
        raw=raw,
    )
    # The chapters as found: every block of text, with only the layout repaired.
    as_found = clean(book, CleanupOptions.keep_everything()).book.chapters
    if not as_found:
        raise InvalidEbookError(f"{path.name} has no readable text")
    return Book(book.title, book.authors, None, as_found, book.cover, raw)


# --- Characters ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Char:
    text: str
    x0: float
    x1: float
    baseline: float
    size: float
    space_before: bool


def _page_chars(textpage: Any) -> tuple[list[_Char], int]:
    """The page's characters in drawing order, and how many were set sideways (skipped)."""
    chars: list[_Char] = []
    sideways = 0
    count = textpage.count_chars()
    left, right = ctypes.c_double(), ctypes.c_double()
    bottom, top = ctypes.c_double(), ctypes.c_double()
    origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
    matrix = pdfium_c.FS_MATRIX()
    space = False
    for index in range(count):
        code = int(pdfium_c.FPDFText_GetUnicode(textpage, index))
        # PDFium's line breaks say nothing about spaces: lines are rebuilt from positions.
        if code in (0x0D, 0x0A) or code in _SKIPPED_CODES or 0xE000 <= code <= 0xF8FF:
            continue
        if code in (0x20, 0x09, 0xA0) or (code < 0x20 and code not in _HYPHEN_CODES):
            space = True
            continue
        text = "-" if code in _HYPHEN_CODES else _character(code)
        if not text or text.isspace():
            space = True
            continue
        angle = float(pdfium_c.FPDFText_GetCharAngle(textpage, index))
        if 0.05 < angle < 2 * math.pi - 0.05:
            sideways += 1
            continue
        if not pdfium_c.FPDFText_GetCharBox(textpage, index, left, right, bottom, top):
            continue
        pdfium_c.FPDFText_GetCharOrigin(textpage, index, origin_x, origin_y)
        size = float(pdfium_c.FPDFText_GetFontSize(textpage, index))
        if pdfium_c.FPDFText_GetMatrix(textpage, index, matrix):
            size *= math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c)) or 1.0
        if size <= 0:
            size = max(1.0, top.value - bottom.value)
        chars.append(_Char(text, left.value, right.value, origin_y.value, size, space_before=space))
        space = False
    return chars, sideways


def _character(code: int) -> str:
    try:
        text = chr(code)
    except (ValueError, OverflowError):
        return ""
    if code in _LIGATURES:
        return unicodedata.normalize("NFKC", text)
    if unicodedata.category(text) in ("Cc", "Cs", "Co", "Cn"):
        return ""
    return text


# --- Segments and lines ----------------------------------------------------------------------


@dataclass(slots=True)
class _Segment:
    chars: list[_Char]
    x0: float = 0.0
    x1: float = 0.0
    baseline: float = 0.0
    size: float = 0.0

    def finish(self) -> _Segment:
        self.x0 = min(char.x0 for char in self.chars)
        self.x1 = max(char.x1 for char in self.chars)
        self.size = _dominant_size(self.chars)
        main = [c.baseline for c in self.chars if c.size >= 0.9 * self.size]
        self.baseline = statistics.median(main) if main else self.chars[0].baseline
        return self


def _dominant_size(chars: Sequence[_Char]) -> float:
    counts = Counter(round(char.size, 1) for char in chars)
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _segments(chars: Sequence[_Char]) -> list[_Segment]:
    segments: list[_Segment] = []
    current: _Segment | None = None
    for char in chars:
        if current is not None:
            last = current.chars[-1]
            size = max(char.size, last.size)
            gap = char.x0 - last.x1
            if (
                abs(char.baseline - last.baseline) < 0.45 * size
                and -0.5 * size < gap < SEGMENT_GAP_EMS * size
            ):
                current.chars.append(char)
                continue
        current = _Segment([char])
        segments.append(current)
    return [segment.finish() for segment in segments]


@dataclass(slots=True)
class _Line:
    text: str
    x0: float
    x1: float
    baseline: float
    size: float
    segments: list[_Segment] = field(default_factory=list)


def _group_lines(segments: Sequence[_Segment]) -> list[_Line]:
    """Group segments on the same baseline into lines, top to bottom."""
    lines: list[list[_Segment]] = []
    for segment in sorted(segments, key=lambda s: (-s.baseline, s.x0)):
        if lines:
            line = lines[-1]
            size = max(segment.size, max(s.size for s in line))
            if abs(segment.baseline - line[0].baseline) < 0.4 * size:
                line.append(segment)
                continue
        lines.append([segment])
    return [_make_line(sorted(line, key=lambda s: s.x0)) for line in lines]


def _make_line(segments: list[_Segment]) -> _Line:
    chars = [char for segment in segments for char in segment.chars]
    size = _dominant_size(chars)
    main = [c.baseline for c in chars if abs(c.size - size) <= 0.1 * size]
    baseline = statistics.median(main) if main else segments[0].baseline
    return _Line(
        text=_line_text(segments, size, baseline),
        x0=min(s.x0 for s in segments),
        x1=max(s.x1 for s in segments),
        baseline=baseline,
        size=size,
        segments=segments,
    )


def _line_text(segments: Sequence[_Segment], size: float, baseline: float) -> str:
    """The line's text, with spaces where the PDF has them (or leaves a gap), and footnote
    markers (small raised digits and symbols) wrapped in marker brackets."""
    pieces: list[tuple[str, bool]] = []  # (text, raised and small)
    previous: _Char | None = None
    for number, segment in enumerate(segments):
        for char in segment.chars:
            gap = char.x0 - previous.x1 if previous is not None else 0.0
            spaced = previous is not None and (
                char.space_before or gap > 0.25 * min(char.size, previous.size) or (
                    number > 0 and char is segment.chars[0]
                )
            )  # fmt: skip
            raised = char.size <= 0.8 * size and char.baseline >= baseline + 0.15 * size
            if spaced:
                pieces.append((" ", False))
            pieces.append((char.text, raised))
            previous = char
    text: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        if not run:
            return
        marker = "".join(run)
        if _MARKER_TEXT.match(marker) and not (
            marker.isalpha() and text and text[-1][-1:].isdigit()
        ):
            text.append(f"{MARKER_START}{marker}{MARKER_END}")
        else:
            text.append(marker)
        run.clear()

    for piece, raised in pieces:
        if raised:
            run.append(piece)
        else:
            flush_run()
            text.append(piece)
    flush_run()
    return "".join(text).strip()


# --- Pages -----------------------------------------------------------------------------------


@dataclass(slots=True)
class _PageBlock:
    lines: list[_Line]
    page: int
    unit: int  # the column (or band of the page) it's in, in reading order
    band: Band | None = None
    continues: bool = False
    role: Role = Role.BODY

    @property
    def size(self) -> float:
        counts: Counter[float] = Counter()
        for line in self.lines:
            counts[round(line.size, 1)] += len(line.text)
        return max(counts.items(), key=lambda item: (item[1], item[0]))[0]

    @property
    def top(self) -> float:
        return self.lines[0].baseline + self.lines[0].size

    @property
    def text(self) -> str:
        return join_lines(line.text for line in self.lines)


@dataclass(slots=True)
class _Page:
    index: int
    width: float
    height: float
    blocks: list[_PageBlock]
    chars: int
    sideways: int
    picture_share: float
    sizes: Counter[float]


def _read_page(page: Any, index: int) -> _Page:
    width, height = (float(value) for value in page.get_size())
    textpage = page.get_textpage()
    try:
        chars, sideways = _page_chars(textpage)
    finally:
        textpage.close()
    sizes: Counter[float] = Counter(round(char.size, 1) for char in chars)
    blocks = _page_blocks(_segments(chars), index, width, height)
    return _Page(
        index=index,
        width=width,
        height=height,
        blocks=blocks,
        chars=len(chars),
        sideways=sideways,
        picture_share=_picture_share(page, width, height) if len(chars) < 200 else 0.0,
        sizes=sizes,
    )


def _picture_share(page: Any, width: float, height: float) -> float:
    """How much of the page pictures cover (0 to 1, overlaps counted twice)."""
    area = 0.0
    for picture in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=3):
        left, bottom, right, top = picture.get_bounds()
        left, right = max(0.0, float(left)), min(width, float(right))
        bottom, top = max(0.0, float(bottom)), min(height, float(top))
        area += max(0.0, right - left) * max(0.0, top - bottom)
    return min(1.0, area / (width * height)) if width > 0 and height > 0 else 0.0


def _page_blocks(
    segments: list[_Segment], page: int, width: float, height: float
) -> list[_PageBlock]:
    """Turn a page's segments into blocks in reading order: header, columns, footer."""
    if not segments:
        return []
    page_lines = _group_lines(segments)
    band_size = statistics.median(line.size for line in page_lines)
    top, bottom = _bands(page_lines, band_size, height)
    banded = {id(s) for line in (top, bottom) if line is not None for s in line.segments}
    rest = [segment for segment in segments if id(segment) not in banded]

    table_rows = _table_rows(rest)
    tabled = {id(s) for row in table_rows for s in row}
    rest = [segment for segment in rest if id(segment) not in tabled]

    blocks: list[_PageBlock] = []
    if top is not None:
        blocks.append(_PageBlock([top], page, 0, Band.TOP))
    units = _reading_units(rest)
    table_unit = _table_unit(units, table_rows)
    for number, unit in enumerate(units, start=1):
        if number == table_unit:
            blocks.append(_table_block(table_rows, page, 0))
        blocks.extend(_unit_blocks(_group_lines(unit), page, number))
    if table_rows and table_unit > len(units):
        blocks.append(_table_block(table_rows, page, 0))
    if bottom is not None:
        blocks.append(_PageBlock([bottom], page, len(units) + 1, Band.BOTTOM))
    return blocks


def _bands(lines: Sequence[_Line], size: float, height: float) -> tuple[_Line | None, _Line | None]:
    """The header line and the footer line, if the page has them."""
    if not lines:
        return None, None
    left = min(line.x0 for line in lines)
    right = max(line.x1 for line in lines)

    def is_band(line: _Line, neighbour: _Line | None, at_edge: bool) -> bool:
        if not at_edge or line.text.startswith(MARKER_START):
            return False
        if is_page_number(line.text):
            gap_ok = neighbour is None or abs(line.baseline - neighbour.baseline) > 1.2 * size
            return gap_ok
        if neighbour is None:
            return False
        isolated = abs(line.baseline - neighbour.baseline) >= BAND_GAP_EMS * max(size, line.size)
        short = line.x1 - line.x0 <= 0.6 * (right - left) or len(lines) < 4
        return isolated and short and line.size <= 1.1 * size

    first, last = lines[0], lines[-1]
    top = first if is_band(first, lines[1] if len(lines) > 1 else None,
                           first.baseline >= (1 - BAND_SHARE) * height) else None  # fmt: skip
    bottom = None
    if last is not first or top is None:
        before = lines[-2] if len(lines) > 1 else None
        if is_band(last, before, last.baseline <= BAND_SHARE * height) and last is not top:
            bottom = last
    return top, bottom


def _reading_units(segments: list[_Segment]) -> list[list[_Segment]]:
    """Split the page body into units read one after another: one column at a time."""
    if not segments:
        return []
    gutter = _gutter(segments)
    if gutter is None:
        return [segments]
    units: list[list[_Segment]] = []
    region: list[_Segment] = []
    region_is_columns: bool | None = None

    def flush() -> None:
        if not region:
            return
        if region_is_columns:
            left = [s for s in region if s.x0 + s.x1 < 2 * gutter]
            right = [s for s in region if s.x0 + s.x1 >= 2 * gutter]
            units.extend(unit for unit in (left, right) if unit)
        else:
            units.append(list(region))

    for segment in sorted(segments, key=lambda s: (-s.baseline, s.x0)):
        crosses = segment.x0 < gutter - 1 and segment.x1 > gutter + 1
        if region_is_columns is None or crosses != region_is_columns:
            region_is_columns = not crosses
            region.append(segment)
            continue
        flush()
        region = [segment]
        region_is_columns = not crosses
    flush()
    return units


def _gutter(segments: Sequence[_Segment]) -> float | None:
    """The x of the gap between two columns, or None for a one-column page.

    Only "narrow" segments (less than 60% of the text width) count, so a title across both
    columns doesn't hide the gap. The gap must be in the middle half of the text, most of the
    page's text must be narrow, and each side must hold a fair share of it.
    """
    left = min(s.x0 for s in segments)
    right = max(s.x1 for s in segments)
    width = right - left
    if width <= 0:
        return None
    narrow = [s for s in segments if s.x1 - s.x0 < 0.6 * width]
    total = sum(len(s.chars) for s in segments)
    narrow_chars = sum(len(s.chars) for s in narrow)
    if len(narrow) < 6 or narrow_chars < 0.5 * total:
        return None
    # Gaps in the union of the narrow segments' spans, in the middle half of the text.
    spans = sorted((s.x0, s.x1) for s in narrow)
    best: tuple[float, float] | None = None
    reach = spans[0][1]
    for start, end in spans[1:]:
        if start > reach:
            low, high = max(reach, left + 0.25 * width), min(start, left + 0.75 * width)
            if high - low > (best[1] - best[0] if best else 0.0):
                best = (low, high)
        reach = max(reach, end)
    size = statistics.median(s.size for s in narrow)
    if best is None or best[1] - best[0] < 0.6 * size:
        return None
    gutter = (best[0] + best[1]) / 2
    left_chars = sum(len(s.chars) for s in narrow if s.x1 <= gutter)
    right_chars = narrow_chars - left_chars
    left_count = sum(1 for s in narrow if s.x1 <= gutter)
    right_count = len(narrow) - left_count
    if min(left_chars, right_chars) < 0.2 * narrow_chars or min(left_count, right_count) < 3:
        return None
    return gutter


def _table_rows(segments: Sequence[_Segment]) -> list[list[_Segment]]:
    """Rows of a table: three or more rows in a row, each with three or more cells that
    start where the cells of the other rows start."""
    rows = [row.segments for row in _group_lines(segments) if len(row.segments) >= 3]
    if len(rows) < 3:
        return []
    table: list[list[_Segment]] = []
    run: list[list[_Segment]] = []

    def aligned(a: list[_Segment], b: list[_Segment]) -> int:
        size = max(s.size for s in (*a, *b))
        return sum(1 for s in a if any(abs(s.x0 - t.x0) < 0.5 * size for t in b))

    for row in rows:
        if (
            run
            and aligned(run[-1], row) >= 3
            and run[-1][0].baseline - row[0].baseline < 3 * row[0].size
        ):
            run.append(row)
            continue
        if len(run) >= 3:
            table.extend(run)
        run = [row]
    if len(run) >= 3:
        table.extend(run)
    return table


def _table_unit(units: Sequence[Sequence[_Segment]], rows: Sequence[Sequence[_Segment]]) -> int:
    """The unit number (1-based) the table goes before, keeping the page's top-down order."""
    if not rows:
        return 0
    table_top = rows[0][0].baseline
    for number, unit in enumerate(units, start=1):
        if max(s.baseline for s in unit) < table_top:
            return number
    return len(units) + 1


def _table_block(rows: Sequence[Sequence[_Segment]], page: int, unit: int) -> _PageBlock:
    lines = [_make_line(list(row)) for row in rows]
    return _PageBlock(lines, page, unit, role=Role.TABLE)


def _unit_blocks(lines: Sequence[_Line], page: int, unit: int) -> list[_PageBlock]:
    """Split one column's lines into blocks (paragraphs, headings, footnotes)."""
    if not lines:
        return []
    left = min(line.x0 for line in lines)
    right = max(line.x1 for line in lines)
    pitches = [
        a.baseline - b.baseline
        for a, b in itertools.pairwise(lines)
        if abs(a.size - b.size) <= 0.1 * a.size and a.baseline > b.baseline
    ]
    pitch = statistics.median(pitches) if pitches else 1.25 * lines[0].size
    blocks: list[_PageBlock] = []
    current: list[_Line] = []
    for line in lines:
        if current and _starts_block(current, line, pitch, left, right):
            blocks.append(_PageBlock(current, page, unit))
            current = []
        current.append(line)
    if current:
        blocks.append(_PageBlock(current, page, unit))
    for number, block in enumerate(blocks):
        first = block.lines[0]
        flush = first.x0 <= left + 0.8 * first.size
        lowercase = _first_letter_is_lowercase(first.text)
        block.continues = flush and (number == 0 or lowercase)
    return blocks


def _starts_block(
    current: Sequence[_Line], line: _Line, pitch: float, left: float, right: float
) -> bool:
    """Whether ``line`` starts a new block after the lines in ``current``."""
    previous = current[-1]
    size = max(previous.size, line.size)
    if abs(previous.size - line.size) > 0.12 * size or line.text.startswith(MARKER_START):
        return True  # a change of type size, or a footnote starting with its marker
    gap = previous.baseline - line.baseline
    if gap > 1.4 * max(pitch, previous.size) + 1 or gap < -0.5 * size:
        return True
    indented = line.x0 > left + 0.8 * size
    if indented and line.x0 > previous.x0 + 0.8 * size:
        return True  # an indented first line after a full one
    if indented and previous.x1 < right - 2.5 * size:
        return True  # an indented line after a short one: one-line paragraphs, dialogue
    # Back to the margin after indented lines that weren't a first-line indent (a quote).
    return len(current) > 1 and line.x0 < previous.x0 - 0.8 * size


def _first_letter_is_lowercase(text: str) -> bool:
    for character in text:
        if character.isalpha():
            return character.islower()
        if character.isdigit():
            return False
    return False


# --- The whole document ----------------------------------------------------------------------


def _body_size(pages: Sequence[_Page]) -> float:
    """The font size most of the text is set in."""
    sizes: Counter[float] = Counter()
    for page in pages:
        sizes.update(page.sizes)
    return max(sizes.items(), key=lambda item: (item[1], -item[0]))[0] if sizes else 10.0


def _classify(pages: Sequence[_Page], body: float) -> list[_PageBlock]:
    """Give every block its role, and return them all in reading order."""
    unfinished_note = False  # the last page's last footnote ran on to this page
    result: list[_PageBlock] = []
    for page in pages:
        for block in page.blocks:
            if block.band is not None and block.size > HEADING_RATIO * body:
                block.band = None  # big text at the top of a page is a heading, not a header
        unfinished_note = _mark_footnotes(page, body, unfinished_note)
        for block in page.blocks:
            if block.role is Role.BODY and _is_heading(block, body):
                block.role = Role.HEADING
        result.extend(page.blocks)
    return result


def _is_heading(block: _PageBlock, body: float) -> bool:
    if len(block.lines) > 4 or len(block.text) > 200:
        return False
    if block.size >= HEADING_RATIO * body:
        return True
    return len(block.lines) == 1 and is_chapter_heading(block.text)


def _mark_footnotes(page: _Page, body: float, unfinished: bool) -> bool:
    """Mark the small blocks at the foot of each column that start with a marker as
    footnotes. Returns whether the page's last footnote runs on to the next page."""
    last_note: _PageBlock | None = None
    units: dict[int, list[_PageBlock]] = {}
    for block in page.blocks:
        if block.band is None and block.role is Role.BODY:
            units.setdefault(block.unit, []).append(block)
    for blocks in units.values():
        foot: list[_PageBlock] = []
        for block in reversed(blocks):
            if block.size > FOOTNOTE_RATIO * body or block.top > 0.5 * page.height:
                break
            foot.insert(0, block)
        while foot and not (_NOTE_START.match(foot[0].lines[0].text) or unfinished):
            foot.pop(0)
        for block in foot:
            block.role = Role.FOOTNOTE
            # A note without its own label carries on the one before it (maybe from the
            # previous page).
            block.continues = not _NOTE_START.match(block.lines[0].text)
            last_note = block
        unfinished = False
    if last_note is None:
        return False
    return not _SENTENCE_END.search(last_note.lines[-1].text.rstrip())


# --- Chapters --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Start:
    """A chapter starts at ``block`` (an index into the document's blocks)."""

    block: int
    title: str
    title_blocks: int = 0  # how many blocks from ``block`` on make up the title
    from_outline: bool = False


def _outline_starts(
    document: Any, pages: Sequence[_Page], blocks: Sequence[_PageBlock]
) -> list[_Start]:
    """Chapter starts from the PDF's bookmarks, if it has at least two usable ones."""
    first_on_page: dict[int, list[int]] = {}
    for index, block in enumerate(blocks):
        first_on_page.setdefault(block.page, []).append(index)
    starts: list[_Start] = []
    try:
        bookmarks = list(document.get_toc(max_depth=OUTLINE_LEVELS))
    except pdfium.PdfiumError:
        return []
    for bookmark in bookmarks:
        title = " ".join(str(bookmark.get_title() or "").split())
        destination = bookmark.get_dest()
        if not title or destination is None:
            continue
        page = destination.get_index()
        if page is None or not 0 <= int(page) < len(pages):
            continue
        target = _block_at(int(page), _destination_top(destination), first_on_page, blocks)
        if target is not None:
            starts.append(_Start(target, title, from_outline=True))
    if len({start.block for start in starts}) < 2:
        return []
    return sorted(starts, key=lambda start: start.block)  # stable: same block keeps its order


def _destination_top(destination: Any) -> float | None:
    try:
        mode, values = destination.get_view()
    except (pdfium.PdfiumError, ValueError, TypeError):
        return None
    values = list(values)
    if mode == pdfium_c.PDFDEST_VIEW_XYZ and len(values) >= 2:
        return float(values[1]) or None
    if mode in (pdfium_c.PDFDEST_VIEW_FITH, pdfium_c.PDFDEST_VIEW_FITBH) and values:
        return float(values[0]) or None
    return None


def _block_at(
    page: int,
    top: float | None,
    first_on_page: dict[int, list[int]],
    blocks: Sequence[_PageBlock],
) -> int | None:
    """The first block at or below ``top`` on ``page``, else the next page's first block."""
    for index in first_on_page.get(page, []):
        if top is None or blocks[index].top <= top + 2:
            return index
    later = [p for p in first_on_page if p > page]
    return first_on_page[min(later)][0] if later else None


def _heading_starts(blocks: Sequence[_PageBlock], body: float) -> list[_Start]:
    """Chapter starts at large headings and "Chapter N" lines. A run of headings (with only
    page headers between them) makes one title: "Chapter One. The Gift"."""
    starts: list[_Start] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        starts_chapter = block.role is Role.HEADING and (
            block.size >= CHAPTER_RATIO * body or is_chapter_heading(block.text)
        )
        if not starts_chapter:
            index += 1
            continue
        parts = [block.text]
        end = index + 1
        while (
            end < len(blocks)
            and blocks[end].role is Role.HEADING
            and len(parts) < 3
            and sum(map(len, parts)) < 150
        ):
            parts.append(blocks[end].text)
            end += 1
        starts.append(_Start(index, _join_title(parts), title_blocks=end - index))
        index = end
    return starts


def _join_title(parts: Sequence[str]) -> str:
    title = ""
    for part in parts:
        part = " ".join(part.replace(MARKER_START, "").replace(MARKER_END, "").split())
        if not title:
            title = part
        elif _SENTENCE_END.search(title):
            title = f"{title} {part}"
        else:
            title = f"{title}. {part}"
    return title


def _assemble(
    blocks: Sequence[_PageBlock], starts: Sequence[_Start], pages: Sequence[_Page]
) -> tuple[RawChapter, ...]:
    """Cut the document's blocks into chapters at ``starts``."""
    chapters: list[RawChapter] = []
    first = starts[0].block if starts else len(blocks)
    if starts:
        chapters.extend(_front_sections(blocks[:first]))
    else:
        chapters.append(RawChapter("", tuple(_block(b) for b in blocks)))
    for number, start in enumerate(starts):
        end = starts[number + 1].block if number + 1 < len(starts) else len(blocks)
        body = list(blocks[start.block + start.title_blocks : end])
        if start.from_outline:
            body = _drop_repeated_headings(body, start.title)
        chapters.append(RawChapter(start.title, tuple(_block(b) for b in body)))
    return tuple(chapter for chapter in chapters if chapter.title or chapter.blocks)


def _drop_repeated_headings(body: list[_PageBlock], title: str) -> list[_PageBlock]:
    """Drop the headings at a bookmarked chapter's start that say its title again, since the
    title is read first. Page headers before them stay (cleanup decides about those)."""
    kept: list[_PageBlock] = []
    leading = True
    for block in body:
        if leading and block.band is not None:
            kept.append(block)
            continue
        if leading and block.role is Role.HEADING and _repeats(block.text, title):
            continue
        leading = False
        kept.append(block)
    return kept


def _front_sections(blocks: Sequence[_PageBlock]) -> list[RawChapter]:
    """The pages before the first chapter, as sections.

    While the pages are short (a title page, a copyright page, a dedication), each is a
    section of its own, so cleanup can tell what each one is. From the first long page on,
    pages run together, except that a page opening with a heading ("Contents", "Preface")
    starts a section titled by it. So a long text is never cut up by page.
    """
    by_page: dict[int, list[_PageBlock]] = {}
    for block in blocks:
        by_page.setdefault(block.page, []).append(block)
    sections: list[list[_PageBlock]] = []
    all_short = True
    for page_blocks in by_page.values():
        all_short = all_short and sum(len(b.text.split()) for b in page_blocks) < FRONT_PAGE_WORDS
        content = [block for block in page_blocks if block.band is None]
        opens_with_heading = bool(content) and content[0].role is Role.HEADING
        if all_short or opens_with_heading or not sections:
            sections.append(list(page_blocks))
        else:
            sections[-1].extend(page_blocks)
    chapters: list[RawChapter] = []
    for section in sections:
        content = [block for block in section if block.band is None]
        title = ""
        if content and content[0].role is Role.HEADING:
            title = _join_title([content[0].text])
            section = [block for block in section if block is not content[0]]
        chapters.append(RawChapter(title, tuple(_block(block) for block in section)))
    return chapters


def _block(block: _PageBlock) -> Block:
    return Block(
        lines=tuple(line.text for line in block.lines),
        role=block.role,
        page=block.page,
        band=block.band,
        continues=block.continues,
    )


# --- Metadata and cover ----------------------------------------------------------------------


def _metadata(document: Any, path: Path) -> tuple[str, tuple[str, ...]]:
    try:
        info = document.get_metadata_dict(skip_empty=True)
    except pdfium.PdfiumError:
        info = {}
    title = " ".join(str(info.get("Title", "")).split())
    if _JUNK_TITLE.search(title):
        title = ""
    authors = tuple(
        name for name in (" ".join(part.split()) for part in str(info.get("Author", "")).split(";"))
        if name
    )  # fmt: skip
    return title or path.stem, authors


def _cover(document: Any, pages: Sequence[_Page]) -> Cover | None:
    """The first page, rendered small, if pictures fill most of it."""
    if not pages or pages[0].picture_share < COVER_IMAGE_SHARE:
        return None
    page = document[0]
    try:
        scale = COVER_WIDTH / pages[0].width if pages[0].width > 0 else 1.0
        bitmap = page.render(
            scale=scale, force_bitmap_format=pdfium_c.FPDFBitmap_BGR, rev_byteorder=True
        )
        width, height, stride = int(bitmap.width), int(bitmap.height), int(bitmap.stride)
        data = bytes(bitmap.buffer)
        bitmap.close()
    except pdfium.PdfiumError:
        return None
    finally:
        page.close()
    rows = (data[row * stride : row * stride + width * 3] for row in range(height))
    return Cover(_png(width, height, rows), "image/png")


def _png(width: int, height: int, rows: Iterator[bytes]) -> bytes:
    """Encode rows of RGB pixels as a PNG file."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )  # fmt: skip

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )  # fmt: skip
