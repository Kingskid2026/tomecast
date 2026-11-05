"""Cleanup: read the book, not the page.

:func:`clean` turns a book as its reader found it into the text that should be spoken. Each
rule is a named switch in :class:`CleanupOptions` with a sensible default, and everything a
rule removes is logged in :attr:`CleanupResult.removed`, so a test (and later the window) can
show exactly what was left out and why.

The rules (see "Cleanup" in docs/architecture.md):

``page-numbers``
    Page numbers and running headers and footers: a line on its own at the top or bottom of
    a page that is a page number ("12", "xiv", "Page 12 of 300"), or whose text (with digits
    ignored) recurs in the same place on several pages. Only for books with pages (PDF).
``footnote-markers``
    Superscript footnote markers ("Della¹ counted" reads "Della counted").
``footnotes``
    Footnote text: dropped (the default), read at the end of its chapter, or kept where
    it was printed.
``links``
    Web addresses and email addresses.
``captions``
    Figure captions ("Figure 3: …", "Fig. 3.", "[Illustration: …]").
``tables``
    Tables, which make no sense read aloud row by row.
Front and back matter (:class:`Matter`), each its own switch
    Copyright page, contents, index, "Also by" lists, next-book previews, and other front
    matter (title page, dedication, short opening sections). These are whole sections: they
    stay in the book, flagged, and are left out of the audiobook by
    :attr:`CleanupResult.kept`, so the window can still list them.

Layout repair isn't a switch: words hyphenated at line ends are rejoined, and a paragraph
broken by a page or column break is put back together. Like every part of Tomecast, cleanup
never drops body text by accident: tests check that every sentence of the body survives.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from tomecast.book import (
    MARKER_END,
    MARKER_START,
    Band,
    Block,
    Book,
    Chapter,
    RawChapter,
    Role,
)

# --- Options and results ---------------------------------------------------------------------


class Matter(StrEnum):
    """A kind of front or back matter: a section that isn't the story."""

    FRONT = "front"  # title page, dedication, edition notes, short opening sections
    COPYRIGHT = "copyright"
    CONTENTS = "contents"
    INDEX = "index"
    ALSO_BY = "also-by"  # "Also by the author"
    PREVIEW = "preview"  # "Read on for an excerpt from…": the next book's opening


Footnotes = Literal["drop", "end", "keep"]


@dataclass(frozen=True, slots=True)
class CleanupOptions:
    """The cleanup switches. The defaults are what most listeners want."""

    page_numbers: bool = True  # remove page numbers and running headers and footers
    footnote_markers: bool = True  # remove superscript footnote markers
    footnotes: Footnotes = "drop"  # footnote text: "drop", read at chapter "end", or "keep"
    links: bool = True  # remove web and email addresses
    captions: bool = True  # remove figure captions
    tables: bool = True  # skip tables
    skip: frozenset[Matter] = frozenset(Matter)  # the front and back matter to leave out

    @classmethod
    def keep_everything(cls) -> CleanupOptions:
        """Every switch off: only layout repair (hyphens, paragraphs over page breaks)."""
        return cls(
            page_numbers=False, footnote_markers=False, footnotes="keep", links=False,
            captions=False, tables=False, skip=frozenset(),
        )  # fmt: skip


RULES: dict[str, str] = {
    "page-numbers": "page numbers and running headers and footers",
    "footnote-markers": "footnote markers",
    "footnotes": "footnote text",
    "links": "web and email addresses",
    "captions": "figure captions",
    "tables": "tables",
    Matter.FRONT: "front matter",
    Matter.COPYRIGHT: "copyright page",
    Matter.CONTENTS: "table of contents",
    Matter.INDEX: "index",
    Matter.ALSO_BY: '"Also by" list',
    Matter.PREVIEW: "preview of another book",
}
"""Every rule's name (as used in :attr:`Removal.rule`) and what it removes, for people."""


@dataclass(frozen=True, slots=True)
class Removal:
    """One thing a rule left out."""

    rule: str  # a key of RULES
    text: str
    chapter: int | None  # the index of the chapter it was in, or None if nothing of it is left
    page: int | None = None  # the page it was on (0-based), for books with pages


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """The cleaned book, which sections are front or back matter, and what was removed."""

    book: Book
    matter: tuple[Matter | None, ...]  # one per chapter of ``book``
    skipped: frozenset[int]  # chapters left out: their kind of matter is switched off
    removed: tuple[Removal, ...] = field(default=())

    @property
    def kept(self) -> list[int]:
        """The indices of the chapters to voice, in order."""
        return [i for i in range(len(self.book.chapters)) if i not in self.skipped]

    def summary(self) -> list[str]:
        """One line per rule that removed something: ``"page numbers and running headers and
        footers: 24"``, in the order the rules first removed something."""
        counts: dict[str, int] = {}
        for removal in self.removed:
            counts[removal.rule] = counts.get(removal.rule, 0) + 1
        return [f"{RULES.get(rule, rule)}: {count}" for rule, count in counts.items()]


# --- Cleaning --------------------------------------------------------------------------------


def clean(book: Book, options: CleanupOptions | None = None) -> CleanupResult:
    """Apply the cleanup rules to ``book`` (see the module docstring).

    Books whose reader recorded a layout (``book.raw``) are rebuilt from it. Others are
    cleaned paragraph by paragraph, and their sections checked for front and back matter.
    """
    options = options if options is not None else CleanupOptions()
    raw = book.raw or tuple(
        RawChapter(chapter.title, tuple(Block((paragraph,)) for paragraph in chapter.paragraphs))
        for chapter in book.chapters
    )
    furniture = _find_furniture(raw)
    chapters: list[Chapter] = []
    chapter_lines: list[list[str]] = []
    removed: list[Removal] = []
    for raw_index, raw_chapter in enumerate(raw):
        log: list[tuple[str, str, int | None]] = []
        chapter, lines = _clean_chapter(raw_chapter, raw_index, furniture, options, log)
        index = len(chapters) if chapter is not None else None
        removed.extend(Removal(rule, text, index, page) for rule, text, page in log)
        if chapter is not None:
            chapters.append(chapter)
            chapter_lines.append(lines)

    cleaned = Book(
        title=book.title,
        authors=book.authors,
        language=book.language,
        chapters=tuple(chapters),
        cover=book.cover,
        raw=book.raw,
    )
    matter = _find_matter(chapters, book.title, chapter_lines)
    skipped = frozenset(i for i, kind in enumerate(matter) if kind in options.skip)
    for index in sorted(skipped):
        kind = matter[index]
        assert kind is not None
        removed.append(Removal(kind, _section_name(chapters[index]), index))
    return CleanupResult(cleaned, matter, skipped, tuple(removed))


def find_matter(book: Book) -> tuple[Matter | None, ...]:
    """Which of ``book``'s chapters are front or back matter (see :class:`Matter`)."""
    lines = [list(chapter.paragraphs) for chapter in book.chapters]
    return _find_matter(book.chapters, book.title, lines)


_Log = list[tuple[str, str, int | None]]  # (rule, text, page)


def _clean_chapter(
    raw: RawChapter,
    raw_index: int,
    furniture: set[tuple[int, int]],
    options: CleanupOptions,
    log: _Log,
) -> tuple[Chapter | None, list[str]]:
    """Clean one chapter. Returns it (None if nothing is left) and its lines as printed."""
    flow: list[Block] = []
    notes: list[Block] = []
    lines: list[str] = []
    for block_index, block in enumerate(raw.blocks):
        text = join_lines(block.lines)
        if (raw_index, block_index) in furniture and options.page_numbers:
            log.append(("page-numbers", _plain(text), block.page))
            continue
        lines.extend(block.lines)
        if block.role is Role.TABLE and options.tables:
            log.append(("tables", _plain(" / ".join(block.lines)), block.page))
            continue
        is_figure_text = block.role is Role.CAPTION or (
            block.role is Role.BODY and is_caption(text)
        )
        if is_figure_text and options.captions:
            log.append(("captions", _plain(text), block.page))
            continue
        if block.role is Role.FOOTNOTE and options.footnotes != "keep":
            notes.append(block)
            continue
        flow.append(block)

    paragraphs: list[str] = []
    for block in _join_fragments(flow):
        rows = block.lines if block.role is Role.TABLE else (join_lines(block.lines),)
        for row in rows:
            paragraph = _finish(row, options, log, block.page)
            if paragraph:
                paragraphs.append(paragraph)
    for note in _join_fragments(notes):
        text = join_lines(note.lines)
        if options.footnote_markers:
            text = _strip_note_label(text)
        if options.footnotes == "end":
            paragraph = _finish(text, options, log, note.page)
            if paragraph:
                paragraphs.append(paragraph)
        else:
            log.append(("footnotes", _plain(text), note.page))

    title = _finish(raw.title, options, [], None, links=False)
    if not title and not paragraphs:
        return None, lines
    return Chapter(title, tuple(paragraphs)), lines


# --- Page furniture ----------------------------------------------------------------------------

# "12", "xiv", "- 12 -", "Page 12", "Page 12 of 300", "[12]".
_PAGE_NUMBER = re.compile(
    r"^[\s\-\u2013\u2014(\[|]*(?:page\s+)?(?:\d{1,5}|[ivxlcdm]{1,8})(?:\s+of\s+\d{1,5})?"
    r"[\s\-\u2013\u2014)\]|]*$",
    re.IGNORECASE,
)
_ROMAN_WORD = re.compile(r"\b[ivxlcdm]+\b")
_NON_WORD = re.compile(r"[\W_]+")


def is_page_number(text: str) -> bool:
    """Whether a line is just a page number: "12", "xiv", "Page 12 of 300", "- 12 -"."""
    return bool(_PAGE_NUMBER.match(_plain(text)))


def _furniture_key(text: str) -> str:
    """The text with numbers (digits or roman), case and punctuation ignored, so "12 THE
    GIFT" on a left-hand page and "THE GIFT 13" on a right-hand one are the same header."""
    folded = re.sub(r"\d+", " ", _plain(text).casefold())
    folded = _ROMAN_WORD.sub(" ", folded)
    return " ".join(_NON_WORD.sub(" ", folded).split())


def _find_furniture(raw: Sequence[RawChapter]) -> set[tuple[int, int]]:
    """Find page numbers and running headers and footers, as (chapter, block) indices.

    Only blocks the reader found on their own at the top or bottom of a page count. A block
    is furniture if it's a page number, or if its text (digits ignored) is in the same band
    on at least three pages (two, in a book of four pages or fewer).
    """
    pages_by_key: dict[tuple[Band, str], set[int]] = {}
    candidates: list[tuple[int, int, Band, str, str]] = []
    pages: set[int] = set()
    for chapter_index, chapter in enumerate(raw):
        for block_index, block in enumerate(chapter.blocks):
            if block.page is not None:
                pages.add(block.page)
            if block.band is None or block.page is None or block.role is not Role.BODY:
                continue
            text = join_lines(block.lines)
            key = _furniture_key(text)
            pages_by_key.setdefault((block.band, key), set()).add(block.page)
            candidates.append((chapter_index, block_index, block.band, key, text))
    repeats = 2 if len(pages) <= 4 else 3
    return {
        (chapter_index, block_index)
        for chapter_index, block_index, band, key, text in candidates
        if is_page_number(text) or (key and len(pages_by_key[(band, key)]) >= repeats)
    }


# --- Joining lines and blocks ---------------------------------------------------------------

_HYPHENS = ("-", "\u2010")
_SOFT_HYPHEN = "\u00ad"
# Words that keep their hyphen when a line breaks after it: "twenty-/one", "self-/made".
_KEEP_HYPHEN_AFTER = frozenset(
    {
        "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
        "self", "well", "half", "ill", "all", "great", "long", "short", "old", "new",
        "non", "pre", "post", "anti", "semi", "quasi", "ex", "co", "vice", "mid", "low", "high",
    }
)  # fmt: skip
_LAST_WORD = re.compile(r"([^\W\d_]+)$")
# A sentence (or clause) is over: ". ! ? … :" maybe followed by closing quotes or brackets.
_ENDS_SENTENCE = re.compile(r"[.!?\u2026:][\"'\u201d\u2019)\]]*$")


def _starts_lowercase(text: str) -> bool:
    """Whether the first letter of ``text`` (after quotes and the like) is lowercase."""
    for character in text:
        if character.isalpha():
            return character.islower()
        if character.isdigit():
            return False
    return False


def join_lines(lines: Iterable[str]) -> str:
    """Join printed lines into one paragraph, rejoining words hyphenated at line ends.

    ``"pen-"`` + ``"nies"`` becomes ``"pennies"``, but a hyphen before a capital (``"Anglo-"``
    + ``"Saxon"``) or after a word like "twenty" or "self" is kept. A soft hyphen always
    goes, and a line ending in a dash is joined without a space.
    """
    text = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if not text:
            text = line
        elif text.endswith(_SOFT_HYPHEN):
            text = text[:-1] + line
        elif text.endswith(_HYPHENS) and len(text) > 1 and not text[-2].isspace():
            word = _LAST_WORD.search(text[:-1])
            keep = word is None or word.group(1).casefold() in _KEEP_HYPHEN_AFTER
            if text[-2].isalpha() and line[0].isalpha() and line[0].islower() and not keep:
                text = text[:-1] + line  # "pen-" + "nies"
            else:
                text = text + line  # "Anglo-" + "Saxon", "1-" + "12"
        elif text.endswith(("\u2014", "\u2013", "/")):
            text = text + line
        else:
            text = f"{text} {line}"
    return text.replace(_SOFT_HYPHEN, "")


def _join_fragments(blocks: Sequence[Block]) -> list[Block]:
    """Put back together paragraphs that a page or column break split into two blocks.

    A block that ``continues`` (it starts flush at the top of a column, or in lowercase)
    joins the body block before it, unless that one ended a sentence and this one starts a
    new one. Headings, headers and footers never join.
    """
    result: list[Block] = []
    for block in blocks:
        previous = result[-1] if result else None
        if (
            previous is not None
            and block.continues
            and block.role is previous.role
            and block.role in (Role.BODY, Role.FOOTNOTE)
            and previous.band is None
            and block.band is None
            and _carries_on(previous.lines, block.lines)
        ):
            result[-1] = Block(
                (*previous.lines, *block.lines),
                previous.role,
                previous.page,
                None,
                previous.continues,
            )
        else:
            result.append(block)
    return result


def _carries_on(before: Sequence[str], after: Sequence[str]) -> bool:
    last = _plain(before[-1]).rstrip() if before else ""
    first = after[0].lstrip() if after else ""
    if not last or not first:
        return False
    if last.endswith((*_HYPHENS, _SOFT_HYPHEN, "\u2014", "\u2013")):
        return True
    return _starts_lowercase(first) or not _ENDS_SENTENCE.search(last)


# --- Paragraph text: markers, links, captions -------------------------------------------------

_MARKER = re.compile(f"{MARKER_START}([^{MARKER_END}]*){MARKER_END}")
_STRAY_MARKS = re.compile(f"[{MARKER_START}{MARKER_END}]")
# A footnote's own label at its start: a marker, "1", "1.", "12)", "*", "†".
_NOTE_LABEL = re.compile(
    rf"^\s*(?:{MARKER_START}[^{MARKER_END}]*{MARKER_END}|\d{{1,3}}[.)]?|[*\u2020\u2021\u00a7\u00b6#]+)\s*"
)
_TLD = r"(?:com|org|net|edu|gov|int|mil|info|biz|io|co|uk|us|ca|au|de|fr|eu)"
_URL = re.compile(
    r"(?:\b(?:https?|ftp)://|\bwww\.)[^\s<>\"\u201c\u201d]+"
    rf"|\b(?:[a-z0-9-]+\.)+{_TLD}/[^\s<>\"\u201c\u201d]*"
    r"|\bmailto:[^\s<>\"]+"
    r"|\b[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z]{2,}\b",
    re.IGNORECASE,
)
_URL_TRAILING = ".,;:!?)]}'\"\u2019\u201d"
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|<\s*>|\{\s*\}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?\u2026)\]])")
_CAPTION = re.compile(
    r"^\s*(?:"
    r"(?:fig(?:ure)?\.?|plate|illus(?:tration)?\.?|table|map|chart|diagram|exhibit)\s+"
    r"(?:\d+[\d.\-]*[a-z]?|[ivxlc]+)\s*[:.\u2014\u2013|]"
    r"|\[\s*illustration\b[^\]]*\]\s*$"
    r")",
    re.IGNORECASE,
)
CAPTION_MAX_WORDS = 80


def is_caption(text: str) -> bool:
    """Whether a paragraph is a figure or table caption ("Figure 3: …", "[Illustration]")."""
    plain = _plain(text)
    return bool(_CAPTION.match(plain)) and len(plain.split()) <= CAPTION_MAX_WORDS


def _plain(text: str) -> str:
    """The text with marker wrappers removed (the markers' own text stays)."""
    return _STRAY_MARKS.sub("", text)


def _strip_note_label(text: str) -> str:
    return _NOTE_LABEL.sub("", text, count=1)


def _finish(
    text: str,
    options: CleanupOptions,
    log: _Log,
    page: int | None,
    *,
    links: bool = True,
) -> str:
    """The paragraph as it will be spoken: markers and links handled, spacing tidied."""

    removals = len(log)

    def marker(match: re.Match[str]) -> str:
        if options.footnote_markers:
            log.append(("footnote-markers", match.group(1), page))
            return ""
        return match.group(1)

    text = _STRAY_MARKS.sub("", _MARKER.sub(marker, text))
    if links and options.links:

        def link(match: re.Match[str]) -> str:
            url = match.group(0)
            kept = ""
            while url and url[-1] in _URL_TRAILING:
                url, kept = url[:-1], url[-1] + kept
            log.append(("links", url, page))
            return kept

        removed = _URL.sub(link, text)
        if removed != text:
            text = _EMPTY_BRACKETS.sub("", removed)
    text = " ".join(text.split())
    if len(log) > removals:  # something came out: no space left before the punctuation after it
        text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    return text


# --- Numbered chapters ------------------------------------------------------------------------

_NUMBER_WORD = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    "fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    "eighty|ninety|hundred"
)
_ROMAN = r"(?=[ivxlcdm])m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
NUMBER_PATTERN = rf"(?:\d+|{_ROMAN}|(?:{_NUMBER_WORD})(?:[- ](?:{_NUMBER_WORD}))?)"
"""A chapter number: "12", "XII" or "Twelve" (matched case-insensitively)."""

# "Chapter 3", "PART IV", "Book Two", "Stave I"…
_NUMBERED_HEADING = re.compile(
    rf"^\s*(?:chapter|chap\.|part|book|stave|letter)\s+{NUMBER_PATTERN}\b", re.IGNORECASE
)
# A title that is only a number, or starts with one followed by punctuation: "3", "IV. Pig".
_BARE_NUMBER = re.compile(r"^\s*(?:\d+|[IVXLCDM]+)(?:[.:)]|\s*$)")
# A whole line that is a chapter heading: "Chapter 3", "CHAPTER III. The Pool of Tears".
_HEADING_LINE = re.compile(
    rf"^\s*(?:chapter|chap\.|part|book|stave)\s+{NUMBER_PATTERN}"
    r"(?:\s*$|\s*[.:\u2014\u2013-]\s*\S.{0,80}$)",
    re.IGNORECASE,
)


def is_numbered_chapter(title: str) -> bool:
    """True for titles like "Chapter 3", "CHAPTER III. Pig and Pepper" or "IV."."""
    return bool(_NUMBERED_HEADING.match(title) or _BARE_NUMBER.match(title))


def is_chapter_heading(line: str) -> bool:
    """Whether a line on its own reads as a chapter heading: "Chapter 3", "PART TWO: Home".

    Stricter than :func:`is_numbered_chapter`, because this is checked against lines of body
    text: "Chapter 3 was the hardest to write." isn't a heading.
    """
    return bool(_HEADING_LINE.match(line))


# --- Front and back matter -----------------------------------------------------------------

# Sections shorter than this, before the first numbered chapter, count as front matter.
FRONT_MATTER_WORDS = 100
# Copyright pages, "Also by" lists and preview introductions are short.
SHORT_SECTION_WORDS = 400

_TITLES: dict[Matter, re.Pattern[str]] = {
    Matter.CONTENTS: re.compile(r"^\W*(?:table\s+of\s+)?contents\W*$", re.IGNORECASE),
    Matter.COPYRIGHT: re.compile(r"\b(?:copyright|imprint|legal\s+notice)\b", re.IGNORECASE),
    Matter.INDEX: re.compile(r"^\W*(?:general\s+|subject\s+)?index\b", re.IGNORECASE),
    Matter.ALSO_BY: re.compile(
        r"^\W*(?:also\s+(?:by|available)|other\s+(?:books|titles|works)\s+by|"
        r"(?:more\s+)?books\s+by|by\s+the\s+same\s+author)\b",
        re.IGNORECASE,
    ),
    Matter.PREVIEW: re.compile(
        r"\b(?:excerpt|sneak\s+peek|preview|coming\s+soon|read\s+on|bonus\s+chapter|"
        r"first\s+chapter\s+of)\b",
        re.IGNORECASE,
    ),
    Matter.FRONT: re.compile(
        r"\b(?:title\s*page|half[\s-]*title|dedication|edition|imprint|frontispiece)\b",
        re.IGNORECASE,
    ),
}
_COPYRIGHT_TEXT = re.compile(
    r"\ball\s+rights\s+reserved\b|\bcopyright\s*(?:\u00a9|\(c\))?\s*\d{4}|\u00a9\s*\d{4}|"
    r"\bISBN\b|\bprinted\s+in\s+the\b|\blibrary\s+of\s+congress\b",
    re.IGNORECASE,
)
_ALSO_BY_TEXT = re.compile(
    r"^\W*(?:also\s+by|other\s+books\s+by|books\s+by|by\s+the\s+same\s+author)\b", re.IGNORECASE
)
_PREVIEW_TEXT = re.compile(
    r"^\W*(?:read\s+on\s+for|turn\s+the\s+page\s+for|keep\s+reading\s+for|coming\s+soon|"
    r"an\s+excerpt\s+from|here\s+is\s+an?\s+(?:excerpt|preview|sneak\s+peek)|"
    r"a\s+sneak\s+peek|preview\s+of)\b",
    re.IGNORECASE,
)
# A contents or index line ends with a page number: "Down the Rabbit-Hole ..... 1".
_ENDS_WITH_PAGE = re.compile(r"[^\W\d_].*?(?:\.{2,}|\s)\s*(?:\d{1,4}|[ivxlc]{1,6})\s*$", re.I)
_INDEX_LINE = re.compile(r"[^\W\d_].*?,\s*\d{1,4}(?:\s*[-\u2013,]\s*\d{1,4})*\s*$")


def _words(chapter: Chapter) -> int:
    return sum(len(paragraph.split()) for paragraph in chapter.paragraphs)


def _section_name(chapter: Chapter) -> str:
    if chapter.title:
        return chapter.title
    words = " ".join(chapter.paragraphs).split()
    return " ".join(words[:8]) + (" \u2026" if len(words) > 8 else "")


def _looks_listed(lines: Sequence[str], pattern: re.Pattern[str]) -> bool:
    """Whether most of these lines (at least three) match ``pattern``."""
    texts = [_plain(line).strip() for line in lines if any(c.isalpha() for c in line)]
    matches = sum(1 for text in texts if pattern.search(text))
    return matches >= 3 and matches >= 0.5 * len(texts)


def _comparable(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def _kind_by_title(chapter: Chapter, book_title: str) -> Matter | None:
    for kind, pattern in _TITLES.items():
        if kind is not Matter.PREVIEW and pattern.search(chapter.title):
            return kind
    if chapter.title and _comparable(chapter.title) == _comparable(book_title):
        return Matter.FRONT
    return None


def _kind_by_text(chapter: Chapter, lines: Sequence[str], *, front: bool) -> Matter | None:
    opening = chapter.paragraphs[0] if chapter.paragraphs else ""
    short = _words(chapter) < SHORT_SECTION_WORDS
    if short and _ALSO_BY_TEXT.match(opening):
        return Matter.ALSO_BY
    if short and _COPYRIGHT_TEXT.search(" ".join(chapter.paragraphs)):
        return Matter.COPYRIGHT
    if _looks_listed(lines, _INDEX_LINE) and not front:
        return Matter.INDEX
    if _looks_listed(lines, _ENDS_WITH_PAGE):
        return Matter.CONTENTS if front else Matter.INDEX
    return None


def _is_preview_start(chapter: Chapter) -> bool:
    opening = chapter.paragraphs[0] if chapter.paragraphs else ""
    if _TITLES[Matter.PREVIEW].search(chapter.title):
        return True
    return _words(chapter) < SHORT_SECTION_WORDS and bool(_PREVIEW_TEXT.match(opening))


def _find_matter(
    chapters: Sequence[Chapter], book_title: str, lines: Sequence[Sequence[str]]
) -> tuple[Matter | None, ...]:
    """Flag the front and back matter among ``chapters``.

    Front matter is looked for before the first numbered chapter ("Chapter 1", "IV."): a
    section is front matter if its title or text says what it is (contents, copyright,
    "Also by", title page, dedication…) or if it's under 100 words. Back matter is looked for
    after the last numbered chapter: index, "Also by", copyright, and previews of another
    book, which run from their introduction ("Read on for an excerpt…") to the end. In a
    book without numbered chapters, only the opening and closing runs of such sections
    count. A book that would be all front and back matter has none.
    """
    count = len(chapters)
    kinds: list[Matter | None] = [None] * count
    numbered = [i for i, chapter in enumerate(chapters) if is_numbered_chapter(chapter.title)]
    first = numbered[0] if numbered else None
    last = numbered[-1] if numbered else None

    for index, chapter in enumerate(chapters):
        if first is not None and index >= first:
            break
        kind = _kind_by_title(chapter, book_title) or _kind_by_text(
            chapter, lines[index], front=True
        )
        if kind is None and _words(chapter) < FRONT_MATTER_WORDS:
            kind = Matter.FRONT
        if kind is Matter.INDEX:  # an index can't come first
            kind = Matter.CONTENTS
        if first is None and kind is None:
            break  # without numbered chapters, only the opening run can be front matter
        kinds[index] = kind

    story_start = next((i for i, kind in enumerate(kinds) if kind is None), count)
    # A preview of another book runs from its introduction to the end. It comes after the
    # last numbered chapter, or the numbering starts again with it (the excerpt's "Chapter 1").
    preview = next(
        (
            i
            for i in range(max(story_start + 1, count // 2), count)
            if _is_preview_start(chapters[i])
            and (last is None or i > last or _numbering_restarts(chapters, i))
        ),
        count,
    )
    back_start = max(story_start + 1, (last + 1) if last is not None else 0)
    for index in range(count - 1, story_start, -1):
        chapter = chapters[index]
        by_title = _kind_by_title(chapter, book_title)
        kind = by_title if by_title is not Matter.FRONT else None
        kind = kind or _kind_by_text(chapter, lines[index], front=False)
        if index >= preview:
            kinds[index] = kind or Matter.PREVIEW
        elif index >= back_start and (kind is not None or last is not None):
            kinds[index] = kind  # after the last numbered chapter, each section on its own
        else:
            break  # without numbered chapters, only the closing run can be back matter

    if all(kind is not None for kind in kinds):
        return (None,) * count  # a book that's all front matter has none
    return tuple(kinds)


_CHAPTER_NUMBER = re.compile(
    rf"^\s*(?:(?:chapter|chap\.|part|book|stave|letter)\s+)?({NUMBER_PATTERN})\b", re.IGNORECASE
)
_SMALL_NUMBERS = {
    word: value
    for value, words in enumerate(
        [(), ("one", "first", "i"), ("two", "second", "ii"), ("three", "third", "iii")]
    )
    for word in words
}


def _chapter_number(title: str) -> int | None:
    """The number of a numbered chapter (only 1 to 3 are needed here), or None."""
    match = _CHAPTER_NUMBER.match(title)
    if match is None or not is_numbered_chapter(title):
        return None
    number = match.group(1).casefold()
    if number.isdigit():
        return int(number)
    return _SMALL_NUMBERS.get(number, 99)


def _numbering_restarts(chapters: Sequence[Chapter], index: int) -> bool:
    """Whether chapter numbers go back to 1 at or soon after ``index``, after reaching 2+."""
    earlier = [_chapter_number(chapter.title) for chapter in chapters[:index]]
    if max((n for n in earlier if n is not None), default=0) < 2:
        return False
    later = (_chapter_number(chapter.title) for chapter in chapters[index : index + 3])
    return next((n for n in later if n is not None), None) == 1
