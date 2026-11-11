"""The book model every reader produces and the rest of Tomecast consumes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Around a footnote marker (a superscript "1" or "*") in a block's text. Readers wrap markers
# in these so cleanup can remove them, or keep them, as its options say. They are in Unicode's
# private-use area, so they never stand for real text, and cleanup always removes them.
MARKER_START = "\ue000"
MARKER_END = "\ue001"


@dataclass(frozen=True, slots=True)
class Cover:
    """A cover image, kept as raw bytes."""

    data: bytes
    media_type: str  # "image/jpeg" or "image/png"


@dataclass(frozen=True, slots=True)
class Chapter:
    """One chapter: an optional title and its paragraphs of plain text, in reading order."""

    title: str
    paragraphs: tuple[str, ...]

    def spoken_paragraphs(self) -> tuple[str, ...]:
        """The paragraphs to voice, with the chapter title (if any) read first."""
        return (self.title, *self.paragraphs) if self.title else self.paragraphs


class Role(StrEnum):
    """What a block of text is, as far as its reader can tell."""

    BODY = "body"
    HEADING = "heading"  # a heading inside a chapter (chapter titles are the chapter's title)
    FOOTNOTE = "footnote"  # a note's text, from the foot of a page
    CAPTION = "caption"  # text under or over a figure
    TABLE = "table"  # the text of a table, row by row


class Band(StrEnum):
    """Where on its page a block sits, when it's apart from the text: a header or a footer."""

    TOP = "top"
    BOTTOM = "bottom"


@dataclass(frozen=True, slots=True)
class Block:
    """A block of text as laid out: a paragraph, or the part of one in a column of one page.

    ``lines`` are the lines as printed, so a word broken over two of them is still broken
    (cleanup joins them). Footnote markers are wrapped in :data:`MARKER_START` and
    :data:`MARKER_END`.
    """

    lines: tuple[str, ...]
    role: Role = Role.BODY
    page: int | None = None  # the page it's on (0-based), for formats with pages
    band: Band | None = None  # set when it's on its own at the top or bottom of the page
    # The block starts flush at the top of a column (or in lowercase), so it may carry on a
    # paragraph from the previous column or page.
    continues: bool = False
    # A layout model (the optional AI pass) marked it as a page header or footer, so cleanup
    # treats it as page furniture whether or not it repeats.
    furniture: bool = False


@dataclass(frozen=True, slots=True)
class RawChapter:
    """A chapter as its reader found it, before cleanup: a title and blocks of text."""

    title: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True, slots=True)
class Book:
    """A whole book, ready to be voiced.

    ``raw`` holds the book as laid out on the page, for formats that need cleanup to read it
    (PDF): its readers fill ``chapters`` with every block of text as found, and
    :func:`tomecast.cleanup.clean` rebuilds ``chapters`` from ``raw``, leaving out page
    numbers, footnote markers and the like. Formats without a layout leave ``raw`` empty.
    """

    title: str
    authors: tuple[str, ...]
    language: str | None
    chapters: tuple[Chapter, ...]
    cover: Cover | None = None
    raw: tuple[RawChapter, ...] = ()

    @property
    def has_pages(self) -> bool:
        """Whether the book is laid out in pages (a PDF), so it may have page numbers."""
        return any(block.page is not None for chapter in self.raw for block in chapter.blocks)
