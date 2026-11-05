"""Read plain-text books (``.txt``) into a :class:`~tomecast.book.Book`.

Plain text has no structure, so this reader finds it:

- **Encoding:** a byte-order mark wins, then UTF-8, then Windows-1252, then Latin-1 (which
  accepts every byte, so decoding never fails). A file full of control bytes is binary, not
  text, and is refused.
- **Project Gutenberg:** the license text outside the ``*** START`` and ``*** END`` markers is
  dropped, and the ``Title:`` and ``Author:`` lines of the header name the book.
- **Chapters:** a line that is only "Chapter 12", "CHAPTER TWELVE", "Chapter XII. Title",
  "Part One" and so on starts a chapter. Bare capital roman numerals ("XII") on a line of
  their own count too, when at least two of them appear and no "Chapter" lines do.
- **Paragraphs:** blank lines separate paragraphs, and lines wrapped inside a paragraph are
  joined. A file with no chapter lines is one chapter, titled by the book's title.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from tomecast.book import Book
from tomecast.chunking import normalize_whitespace
from tomecast.readers._markup import (
    Block,
    ChapterStart,
    Event,
    assemble_chapters,
    read_limited,
)
from tomecast.readers.errors import InvalidEbookError

_SNIFF_BYTES = 8192
_MAX_CONTROL_RATIO = 0.05  # more control bytes than this and the file is binary
_TEXT_CONTROLS = frozenset(b"\t\n\r\f\v\x1b\x08")
_MAX_CHAPTER_LINE = 100

_GUTENBERG_START = re.compile(
    r"^[ \t]*\*{3}[ \t]*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.IGNORECASE | re.M
)
_GUTENBERG_END = re.compile(
    r"^[ \t]*\*{3}[ \t]*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.IGNORECASE | re.M
)
_GUTENBERG_TITLE = re.compile(r"^Title:[ \t]*(\S.*)$", re.M)
_GUTENBERG_AUTHOR = re.compile(r"^Authors?:[ \t]*(\S.*)$", re.M)

_NUMBER_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|"
    r"nineteenth|twentieth|last|final)"
)
_ROMAN = r"(?-i:(?=[IVXLCDM])M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))"
_NUMBER = rf"(?:\d+|(?:the[ \t]+)?{_NUMBER_WORD}(?:[ \t-]+{_NUMBER_WORD})*|{_ROMAN})"
# "Chapter 12", "CHAPTER TWELVE", "Chapter XII. A title", "Part One", "Book the First - Title".
_CHAPTER_LINE = re.compile(
    rf"(?:chapter|part|book|section)[ \t]+{_NUMBER}[ \t]*"
    r"(?:[.:–—-][ \t]*(?:\S.*)?)?",  # noqa: RUF001
    re.IGNORECASE,
)
_BARE_ROMAN = re.compile(rf"{_ROMAN}\.?")


def read_txt(path: Path) -> Book:
    """Read a plain-text file into a :class:`~tomecast.book.Book`.

    Raises:
        InvalidEbookError: if the file is binary, too large to read safely, or has no text.
        OSError: if the file itself can't be opened.
    """
    data = read_limited(path)
    text = _decode(data, path.name)
    title, authors = _gutenberg_details(text)
    text = _without_gutenberg_boilerplate(text)
    book_title = title or path.stem
    events = _events(text, book_title)
    chapters = assemble_chapters(events)
    if not chapters:
        raise InvalidEbookError(f"{path.name} has no readable text")
    return Book(title=book_title, authors=authors, language=None, chapters=tuple(chapters))


def _decode(data: bytes, name: str) -> str:
    """Decode bytes to text, never failing on odd encodings, but refusing binary files."""
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError as error:
                raise InvalidEbookError(f"{name} isn't a text file: {error.reason}") from error
    sample = data[:_SNIFF_BYTES]
    controls = sum(1 for byte in sample if byte < 0x20 and byte not in _TEXT_CONTROLS)
    if b"\x00" in sample or controls > len(sample) * _MAX_CONTROL_RATIO:
        raise InvalidEbookError(f"{name} isn't a text file: it looks like binary data")
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _gutenberg_details(text: str) -> tuple[str, tuple[str, ...]]:
    """The title and authors from a Project Gutenberg header, or empty values."""
    start = _GUTENBERG_START.search(text)
    if start is None:
        return "", ()
    header = text[: start.start()]
    title = _GUTENBERG_TITLE.search(header)
    author = _GUTENBERG_AUTHOR.search(header)
    return (
        normalize_whitespace(title.group(1)) if title else "",
        (normalize_whitespace(author.group(1)),) if author else (),
    )


def _without_gutenberg_boilerplate(text: str) -> str:
    """Keep only what's between the START and END markers, if the file has them."""
    start = _GUTENBERG_START.search(text)
    if start is not None:
        text = text[start.end() :]
    end = _GUTENBERG_END.search(text)
    return text[: end.start()] if end is not None else text


def _events(text: str, book_title: str) -> list[Event]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n").split("\n")
    keyword_lines = {i for i, line in enumerate(lines) if _is_keyword_chapter_line(line)}
    chapter_lines = keyword_lines or _bare_roman_lines(lines)

    events: list[Event] = []
    paragraph: list[str] = []

    def end_paragraph() -> None:
        joined = normalize_whitespace(" ".join(paragraph))
        paragraph.clear()
        if joined:
            events.append(Block(joined))

    for i, line in enumerate(lines):
        if i in chapter_lines:
            end_paragraph()
            events.append(ChapterStart(normalize_whitespace(line)))
        elif line.strip():
            paragraph.append(line)
        else:
            end_paragraph()
    end_paragraph()
    if not chapter_lines and any(isinstance(event, Block) for event in events):
        events.insert(0, ChapterStart(book_title))
    return events


def _is_keyword_chapter_line(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) <= _MAX_CHAPTER_LINE and _CHAPTER_LINE.fullmatch(stripped) is not None


def _bare_roman_lines(lines: list[str]) -> set[int]:
    """Lines that are just a capital roman numeral with blank lines (or the file's edge) around
    them. One alone is more likely a stray letter than a chapter, so at least two are needed."""

    def blank(index: int) -> bool:
        return not 0 <= index < len(lines) or not lines[index].strip()

    found = {
        i
        for i, line in enumerate(lines)
        if _BARE_ROMAN.fullmatch(line.strip()) and blank(i - 1) and blank(i + 1)
    }
    return found if len(found) >= 2 else set()
