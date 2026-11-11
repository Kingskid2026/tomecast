"""The optional AI pass for PDF page layout: a layout model's boxes, the PDF's own words.

A layout model (Docling Heron, :mod:`tomecast.readers.heron`) looks at a picture of the page
and labels boxes on it: page header, page footer, footnote, caption, table, section header,
document index and so on. It never reads the words. Every word comes from the PDF itself
(pypdfium2's characters with their positions, the same ones the rules in
:mod:`tomecast.readers.pdf` use), and is put in the box it sits in:

1. **Words:** the page's characters are cut into words exactly where the rules' line builder
   puts spaces, so a word is never split between two boxes.
2. **Boxes:** each word goes to one box: the text-like box it sits in (the most confident
   one if boxes overlap), else a table, picture or index box, else the nearest box within
   half an em. A word in no box is never dropped: nearby left-over words make a box of their
   own ("orphan" text, as Docling does), read as body text unless it's a lone page number
   at the top or bottom of the page.
3. **Reading order** is our own rule, the same one the rules reader uses for columns: if the
   boxes leave a clear gap down the middle, boxes that cross it cut the page into bands read
   top to bottom, and inside a band the left column comes first (recursively, so three
   columns work too). Headers come first, then the text, then the footnotes, then footers.
4. **Blocks:** each box's words are built into lines and paragraphs by the rules' own line
   and paragraph builder, then given the box's role: headers and footers are page furniture
   (cleanup's "page numbers" switch removes them), footnotes are footnotes (dropped or read
   at the chapter's end, as cleanup's switch says), captions and text on pictures are
   captions, tables are tables, section headers and titles are headings.
5. **Every word is accounted for:** :func:`check_words` checks that every word of the page
   that isn't a header or footer is in the blocks exactly once. If it isn't, the page is
   read by the rules instead (``HeronLayout.fallbacks`` says which pages and why), so the AI
   pass can never lose text.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from tomecast.book import MARKER_END, MARKER_START, Band, Role
from tomecast.cleanup import is_page_number
from tomecast.readers.pdf import (
    _NOTE_START,
    BAND_SHARE,
    _Char,
    _first_letter_is_lowercase,
    _group_lines,
    _PageBlock,
    _segments,
    _unit_blocks,
)


class Label(StrEnum):
    """What a layout box holds: Docling's 17 layout classes, in the model's order, plus our
    own label for words no box covered."""

    CAPTION = "caption"
    FOOTNOTE = "footnote"
    FORMULA = "formula"
    LIST_ITEM = "list_item"
    PAGE_FOOTER = "page_footer"
    PAGE_HEADER = "page_header"
    PICTURE = "picture"
    SECTION_HEADER = "section_header"
    TABLE = "table"
    TEXT = "text"
    TITLE = "title"
    DOCUMENT_INDEX = "document_index"
    CODE = "code"
    CHECKBOX_SELECTED = "checkbox_selected"
    CHECKBOX_UNSELECTED = "checkbox_unselected"
    FORM = "form"
    KEY_VALUE_REGION = "key_value_region"
    ORPHAN = "orphan"  # not the model's: words that no box covered


MODEL_LABELS: tuple[Label, ...] = tuple(label for label in Label if label is not Label.ORPHAN)
"""The model's class indices, in order (``MODEL_LABELS[i]`` is class ``i``)."""

# Boxes that hold other things (cells, figures, index entries). A word goes to one of these
# only if no text-like box holds it, so a wrong picture or table box over a paragraph can't
# take the paragraph's words out of the text.
_CONTAINERS = frozenset(
    {Label.TABLE, Label.PICTURE, Label.DOCUMENT_INDEX, Label.FORM, Label.KEY_VALUE_REGION}
)
_FURNITURE = {Label.PAGE_HEADER: Band.TOP, Label.PAGE_FOOTER: Band.BOTTOM}
_ROLES = {
    Label.FOOTNOTE: Role.FOOTNOTE,
    Label.CAPTION: Role.CAPTION,
    Label.PICTURE: Role.CAPTION,  # words drawn on a figure: read only with captions on
    Label.TABLE: Role.TABLE,
    Label.SECTION_HEADER: Role.HEADING,
    Label.TITLE: Role.HEADING,
}
SNAP_EMS = 0.5  # a word just outside a box (boxes are often drawn tight) joins the nearest one
ORPHAN_GAP_EMS = 1.5  # left-over words this close together make one orphan box
NARROW_SHARE = 0.6  # boxes narrower than this share of the text width can be in a column


@dataclass(frozen=True, slots=True)
class Region:
    """A box the layout model found, in PDF page coordinates (points, y going up)."""

    label: Label
    score: float
    x0: float
    y0: float  # bottom
    x1: float
    y1: float  # top

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)


class Detector(Protocol):
    """Finds the layout boxes on one page (Docling Heron, or a fake in tests)."""

    def detect(self, page: Any, width: float, height: float) -> list[Region]: ...


class LayoutMismatch(RuntimeError):
    """The blocks built from the boxes don't hold every word of the page exactly once."""


# --- Words -----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Word:
    chars: list[_Char]

    @property
    def text(self) -> str:
        return "".join(char.text for char in self.chars)

    @property
    def size(self) -> float:
        return max(char.size for char in self.chars)

    def box(self) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1): from just under the baseline to about the cap height."""
        return (
            min(char.x0 for char in self.chars),
            min(char.baseline - 0.2 * char.size for char in self.chars),
            max(char.x1 for char in self.chars),
            max(char.baseline + 0.7 * char.size for char in self.chars),
        )


def page_words(chars: Sequence[_Char]) -> list[_Word]:
    """The page's words, cut exactly where :func:`tomecast.readers.pdf._line_text` puts a
    space: at a segment break, a space character, or a gap wider than a quarter em."""
    words: list[_Word] = []
    for segment in _segments(chars):
        previous: _Char | None = None
        for char in segment.chars:
            gap = char.x0 - previous.x1 if previous is not None else 0.0
            if previous is None or char.space_before or gap > 0.25 * min(char.size, previous.size):
                words.append(_Word([char]))
            else:
                words[-1].chars.append(char)
            previous = char
    return words


def _tokens(text: str) -> list[str]:
    """The words of a built line, with footnote-marker brackets taken out."""
    return text.replace(MARKER_START, "").replace(MARKER_END, "").split()


# --- Words into boxes ------------------------------------------------------------------------


def _inside(box: tuple[float, float, float, float], region: Region) -> bool:
    x = (box[0] + box[2]) / 2
    y = (box[1] + box[3]) / 2
    return region.x0 <= x <= region.x1 and region.y0 <= y <= region.y1


def _distance(box: tuple[float, float, float, float], region: Region) -> float:
    dx = max(region.x0 - box[2], box[0] - region.x1, 0.0)
    dy = max(region.y0 - box[3], box[1] - region.y1, 0.0)
    return max(dx, dy)


def _choose(box: tuple[float, float, float, float], size: float, regions: Sequence[Region]) -> int:
    """The index of the region a word goes to, or -1 if none is close enough."""
    holding = [i for i, region in enumerate(regions) if _inside(box, region)]
    if holding:
        # Text-like boxes before containers; then the most confident; then the smallest.
        return min(
            holding,
            key=lambda i: (
                regions[i].label in _CONTAINERS,
                -regions[i].score,
                regions[i].area,
            ),
        )
    near = [(d, i) for i, region in enumerate(regions) if (d := _distance(box, region)) <= (
        SNAP_EMS * size
    )]  # fmt: skip
    return min(near)[1] if near else -1


def _orphan_regions(words: Sequence[_Word], height: float) -> list[tuple[Region, list[_Word]]]:
    """Group left-over words into boxes of their own: words close together share one."""
    boxes = [word.box() for word in words]
    parent = list(range(len(words)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            reach = ORPHAN_GAP_EMS * max(words[i].size, words[j].size)
            dx = max(b[0] - a[2], a[0] - b[2], 0.0)
            dy = max(b[1] - a[3], a[1] - b[3], 0.0)
            if dx <= reach and dy <= reach:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(words)):
        groups.setdefault(find(i), []).append(i)
    result: list[tuple[Region, list[_Word]]] = []
    for members in groups.values():
        group = [words[i] for i in members]
        x0 = min(boxes[i][0] for i in members)
        y0 = min(boxes[i][1] for i in members)
        x1 = max(boxes[i][2] for i in members)
        y1 = max(boxes[i][3] for i in members)
        label = Label.ORPHAN
        # A page number the model missed, alone in the top or bottom fifth of the page.
        if is_page_number(" ".join(word.text for word in group)):
            if y0 >= (1 - BAND_SHARE) * height:
                label = Label.PAGE_HEADER
            elif y1 <= BAND_SHARE * height:
                label = Label.PAGE_FOOTER
        result.append((Region(label, 0.0, x0, y0, x1, y1), group))
    return result


# --- Reading order ---------------------------------------------------------------------------


def _gutter(regions: Sequence[Region]) -> float | None:
    """The x of a clear gap between columns of boxes, or None.

    Only boxes narrower than :data:`NARROW_SHARE` of the text width count, so a title across
    both columns doesn't hide the gap. The gap must be in the middle half of the text, with
    at least one narrow box on each side.
    """
    if len(regions) < 2:
        return None
    left = min(region.x0 for region in regions)
    right = max(region.x1 for region in regions)
    width = right - left
    if width <= 0:
        return None
    narrow = sorted((r for r in regions if r.x1 - r.x0 < NARROW_SHARE * width), key=lambda r: r.x0)
    if len(narrow) < 2:
        return None
    best: tuple[float, float] | None = None
    reach = narrow[0].x1
    for region in narrow[1:]:
        if region.x0 > reach:
            low, high = max(reach, left + 0.25 * width), min(region.x0, left + 0.75 * width)
            if high - low > (best[1] - best[0] if best else 0.0):
                best = (low, high)
        reach = max(reach, region.x1)
    if best is None or best[1] - best[0] < 4.0:  # points: narrower isn't a gutter
        return None
    gutter = (best[0] + best[1]) / 2
    if not any(r.x1 <= gutter for r in narrow) or not any(r.x0 >= gutter for r in narrow):
        return None
    return gutter


def reading_order(regions: Sequence[Region], depth: int = 0) -> list[list[Region]]:
    """The boxes in reading order, as columns: lists of boxes read one after another.

    Boxes are taken top to bottom. If there's a gutter, a run of boxes that don't cross it
    is read left column first, then right (each side ordered the same way, so a column can
    have columns of its own); a box that crosses the gutter is read where it is.
    """
    by_top = sorted(regions, key=lambda r: (-r.y1, r.x0))
    gutter = _gutter(by_top) if depth < 3 else None
    if gutter is None:
        return [by_top] if by_top else []
    columns: list[list[Region]] = []
    run: list[Region] = []

    def flush() -> None:
        if not run:
            return
        left = [r for r in run if r.x0 + r.x1 < 2 * gutter]
        right = [r for r in run if r.x0 + r.x1 >= 2 * gutter]
        for side in (left, right):
            columns.extend(reading_order(side, depth + 1))
        run.clear()

    for region in by_top:
        if region.x0 < gutter - 1 and region.x1 > gutter + 1:
            flush()
            columns.append([region])
        else:
            run.append(region)
    flush()
    return columns


# --- Blocks ----------------------------------------------------------------------------------


@dataclass(slots=True)
class PageLayoutResult:
    """One page laid out from its boxes: the blocks, and what went where (for checks)."""

    blocks: list[_PageBlock]
    regions: list[Region]  # every box words went to, orphans included
    furniture_words: list[str] = field(default_factory=list)  # header and footer words
    body_words: list[str] = field(default_factory=list)  # every other word of the page


def layout_page(
    chars: Sequence[_Char],
    regions: Sequence[Region],
    page: int,
    width: float,
    height: float,
) -> PageLayoutResult:
    """Lay out one page from its characters and the model's boxes (see the module docstring).

    Raises:
        LayoutMismatch: if the blocks don't hold every non-furniture word exactly once.
    """
    words = page_words(chars)
    regions = list(regions)
    members: list[list[_Word]] = [[] for _ in regions]
    left_over: list[_Word] = []
    for word in words:
        chosen = _choose(word.box(), word.size, regions)
        if chosen < 0:
            left_over.append(word)
        else:
            members[chosen].append(word)
    for region, group in _orphan_regions(left_over, height):
        regions.append(region)
        members.append(group)
    used = [(region, group) for region, group in zip(regions, members, strict=True) if group]

    furniture = [(r, g) for r, g in used if r.label in _FURNITURE]
    body_size = statistics.median(word.size for word in words) if words else 0.0
    banded = {id(r): band for r, g in used if (band := _heading_in_band(r, g, body_size, height))}
    notes = [(r, g) for r, g in used if r.label is Label.FOOTNOTE]
    flow = [
        (r, g)
        for r, g in used
        if r.label not in _FURNITURE and r.label is not Label.FOOTNOTE and id(r) not in banded
    ]
    words_of = {id(region): group for region, group in used}

    blocks: list[_PageBlock] = []
    unit = 0
    for region, group in sorted(
        (item for item in furniture if item[0].label is Label.PAGE_HEADER),
        key=lambda item: (-item[0].y1, item[0].x0),
    ):
        blocks.append(_furniture_block(region, group, page, unit))
    for region, group in used:
        if banded.get(id(region)) is Band.TOP:
            blocks.append(_band_block(group, Band.TOP, page, unit))
    for column in reading_order([region for region, _ in flow]):
        unit += 1
        blocks.extend(_column_blocks(column, words_of, page, unit))
    for column in reading_order([region for region, _ in notes]):
        unit += 1
        blocks.extend(_column_blocks(column, words_of, page, unit))
    unit += 1
    for region, group in used:
        if banded.get(id(region)) is Band.BOTTOM:
            blocks.append(_band_block(group, Band.BOTTOM, page, unit))
    for region, group in sorted(
        (item for item in furniture if item[0].label is Label.PAGE_FOOTER),
        key=lambda item: (-item[0].y1, item[0].x0),
    ):
        blocks.append(_furniture_block(region, group, page, unit))

    result = PageLayoutResult(
        blocks=blocks,
        regions=[region for region, _ in used],
        furniture_words=[word.text for _, group in furniture for word in group],
        body_words=[
            word.text for region, group in used if region.label not in _FURNITURE for word in group
        ],
    )
    check_words(words, result)
    return result


def _heading_in_band(
    region: Region, words: Sequence[_Word], body_size: float, height: float
) -> Band | None:
    """Whether a heading box is more likely a running header or footer: one line in the top
    or bottom fifth of the page, set no bigger than the page's usual text. (Heron sometimes
    calls a running header a section header.) Such a line becomes a header or footer line
    that cleanup removes only if it repeats, like the rules reader's bands, so a real heading
    there is still read."""
    if region.label not in (Label.SECTION_HEADER, Label.TITLE) or not words:
        return None
    if max(word.size for word in words) > 1.1 * body_size:
        return None
    if len(_group_lines(_segments(_chars(words)))) != 1:
        return None
    if region.y0 >= (1 - BAND_SHARE) * height:
        return Band.TOP
    if region.y1 <= BAND_SHARE * height:
        return Band.BOTTOM
    return None


def _band_block(words: list[_Word], band: Band, page: int, unit: int) -> _PageBlock:
    return _PageBlock(_group_lines(_segments(_chars(words))), page, unit, band=band)


def _furniture_block(region: Region, words: list[_Word], page: int, unit: int) -> _PageBlock:
    lines = _group_lines(_segments(_chars(words)))
    return _PageBlock(lines, page, unit, band=_FURNITURE[region.label], furniture=True)


def _chars(words: Sequence[_Word]) -> list[_Char]:
    """The words' characters, in the page's drawing order."""
    return [char for word in words for char in word.chars]


def _column_blocks(
    column: Sequence[Region], words_of: dict[int, list[_Word]], page: int, unit: int
) -> list[_PageBlock]:
    """The blocks of one column of boxes, in order, with their roles and "may carry on"."""
    blocks: list[_PageBlock] = []
    margin = min(
        (char.x0 for region in column for word in words_of[id(region)] for char in word.chars),
        default=0.0,
    )
    for region in column:
        lines = _group_lines(_segments(_chars(words_of[id(region)])))
        if not lines:
            continue
        role = _ROLES.get(region.label, Role.BODY)
        if role in (Role.TABLE, Role.HEADING):
            built = [_PageBlock(lines, page, unit, role=role)]
        else:
            built = _unit_blocks(lines, page, unit)
            for block in built:
                block.role = role
        for block in built:
            first = block.lines[0]
            if role is Role.FOOTNOTE:
                # A note without its own label carries on the one before it.
                block.continues = not _NOTE_START.match(first.text)
            elif role is Role.BODY:
                flush = first.x0 <= margin + 0.8 * first.size
                top_of_column = not blocks
                block.continues = flush and (
                    top_of_column or _first_letter_is_lowercase(first.text)
                )
            else:
                block.continues = False
            blocks.append(block)
    return blocks


def check_words(words: Sequence[_Word], result: PageLayoutResult) -> None:
    """Check that the blocks hold every word of the page that isn't a header or footer,
    exactly once, and that the header and footer words plus those make up the whole page.

    Raises:
        LayoutMismatch: naming a few of the words that are missing or doubled.
    """
    page = Counter(word.text for word in words)
    assigned = Counter(result.body_words) + Counter(result.furniture_words)
    if assigned != page:
        raise LayoutMismatch(_difference(page, assigned))
    spoken = Counter(
        token
        for block in result.blocks
        if not block.furniture
        for line in block.lines
        for token in _tokens(line.text)
    )
    expected = Counter(result.body_words)
    if spoken != expected:
        raise LayoutMismatch(_difference(expected, spoken))


def _difference(expected: Counter[str], got: Counter[str]) -> str:
    missing = list((expected - got).elements())[:5]
    extra = list((got - expected).elements())[:5]
    return f"words missing: {missing}, words extra: {extra}"


# --- The pass --------------------------------------------------------------------------------


@dataclass(slots=True)
class HeronLayout:
    """The AI layout pass for :func:`tomecast.readers.pdf.read_pdf` (its ``layout``).

    Pages whose boxes can't be trusted (the model failed, or :func:`check_words` found a
    word missing or doubled) are left to the rules, and listed in ``fallbacks``.
    """

    detector: Detector
    fallbacks: list[tuple[int, str]] = field(default_factory=list)  # (page, reason)
    pages: list[PageLayoutResult] = field(default_factory=list)
    seconds: float = 0.0  # time spent in the model and the layout, for the speed estimate

    def page_blocks(
        self, page: Any, index: int, chars: list[_Char], width: float, height: float
    ) -> list[_PageBlock] | None:
        started = time.perf_counter()
        try:
            regions = self.detector.detect(page, width, height)
            result = layout_page(chars, regions, index, width, height)
        except LayoutMismatch as error:
            self.fallbacks.append((index, str(error)))
            return None
        except Exception as error:  # the model itself failed on this page: use the rules
            self.fallbacks.append((index, f"layout model failed: {error}"))
            return None
        finally:
            self.seconds += time.perf_counter() - started
        self.pages.append(result)
        return result.blocks
