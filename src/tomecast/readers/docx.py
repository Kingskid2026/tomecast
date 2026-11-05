"""Read Word (``.docx``) documents into a :class:`~tomecast.book.Book`.

A DOCX is a zip file, opened here with ``python-docx``. Paragraphs in the Title and Heading 1
styles start chapters (or Heading 2, when the document has no Heading 1). Text in tables is
kept as paragraphs, in reading order, and headers and footers are skipped, because they sit
outside the document body.

The text is read from the document's XML directly rather than through ``python-docx``'s
paragraph objects, because those leave out text that is real book text: tracked insertions,
and the contents of text boxes. Deleted text is not read.

DOCX files are untrusted input: the zip is checked for sizes that suggest a zip bomb before
anything is decompressed, and XML entities are never expanded.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.opc.exceptions import PackageNotFoundError
from lxml import etree

from tomecast.book import Book, Cover
from tomecast.chunking import normalize_whitespace
from tomecast.readers._markup import (
    Block,
    ChapterStart,
    Event,
    assemble_chapters,
    image_type,
    local_name,
)
from tomecast.readers.errors import InvalidEbookError

# Zip-bomb guard: the most uncompressed data in one file of the DOCX, and in total.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Containers whose paragraphs are read, in order. Anything else (section properties, bookmarks,
# deleted text) holds no body text.
_CONTAINERS = frozenset(
    {
        "tbl",
        "tr",
        "tc",
        "sdt",
        "sdtcontent",
        "ins",
        "customxml",
        "smarttag",
        "moveto",
        "txbxcontent",
    }
)
# Parts of a paragraph that aren't read: deleted text, and the legacy fallback copy of a text box.
_SKIPPED_IN_PARAGRAPH = frozenset({"del", "movefrom", "fallback", "txbxcontent"})
_EMU_PER_INCH = 914_400
_MIN_COVER_EMU = _EMU_PER_INCH  # a cover is at least an inch on each side
_MIN_COVER_ASPECT = 0.8  # and no wider than it is tall (by more than a fifth)
_PROSE_CHARS = 80  # a paragraph this long is text of the book, so a picture after it isn't a cover


def read_docx(path: Path) -> Book:
    """Read a DOCX file into a :class:`~tomecast.book.Book`.

    The cover is the first inline picture, if it is a PNG or JPEG of cover-like size and shape
    that comes before the book's text starts. Title and authors come from the document's core
    properties (File > Properties in Word).

    Raises:
        InvalidEbookError: if the file isn't a DOCX, holds a file too large to read safely, or
            has no readable text.
        OSError: if the file itself can't be opened.
    """
    _check_zip(path)
    try:
        document = Document(str(path))
    except (PackageNotFoundError, KeyError, ValueError, etree.XMLSyntaxError) as error:
        raise InvalidEbookError(f"{path.name} isn't a DOCX file: {error}") from error
    except zipfile.BadZipFile as error:
        raise InvalidEbookError(f"{path.name} isn't a DOCX file: it's not a zip file") from error

    body = document.element.body
    style_names = {style.style_id: style.name or "" for style in document.styles}
    paragraphs = [(_style_name(p, style_names), _text(p), p) for p in _paragraphs(body)]
    chapter_styles = _chapter_styles(name for name, _, _ in paragraphs)

    events: list[Event] = []
    for style, text, _ in paragraphs:
        if not text:
            continue
        events.append(ChapterStart(text) if style in chapter_styles else Block(text))
    chapters = assemble_chapters(events)
    if not chapters:
        raise InvalidEbookError(f"{path.name} has no readable text")

    properties = document.core_properties
    authors = tuple(
        name
        for name in (normalize_whitespace(a) for a in (properties.author or "").split(";"))
        if name
    )
    return Book(
        title=normalize_whitespace(properties.title or "") or path.stem,
        authors=authors,
        language=normalize_whitespace(properties.language or "") or None,
        chapters=tuple(chapters),
        cover=_find_cover(document, paragraphs),
    )


def _check_zip(path: Path) -> None:
    """Refuse a zip whose files claim to expand to more than is safe, before reading any."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
    except zipfile.BadZipFile as error:
        raise InvalidEbookError(f"{path.name} isn't a DOCX file: it's not a zip file") from error
    for info in members:
        if info.file_size > MAX_MEMBER_BYTES:
            raise InvalidEbookError(
                f"{info.filename} in {path.name} is too large to read safely (possible zip bomb)"
            )
    if sum(info.file_size for info in members) > MAX_TOTAL_BYTES:
        raise InvalidEbookError(f"{path.name} is too large to read safely (possible zip bomb)")


# --- Paragraphs ----------------------------------------------------------------------------


def _paragraphs(container: etree._Element) -> Iterator[etree._Element]:
    """Yield the ``w:p`` elements under a container in reading order, descending into tables,
    content controls and text boxes."""
    for child in container:
        name = local_name(child)
        if name == "p":
            yield child
            for box in _text_boxes(child):
                yield from _paragraphs(box)
        elif name in _CONTAINERS:
            yield from _paragraphs(child)


def _text_boxes(element: etree._Element) -> Iterator[etree._Element]:
    """Yield the outermost text boxes under an element, skipping the old-style copy of each one
    that Word stores next to the modern version."""
    for child in element:
        name = local_name(child)
        if name == "txbxcontent":
            yield child
        elif name and name != "fallback":
            yield from _text_boxes(child)


def _walk(element: etree._Element, *, skip: frozenset[str] | set[str]) -> Iterator[etree._Element]:
    """Yield every descendant element in document order, not entering the ``skip`` ones."""
    for child in element:
        name = local_name(child)
        if not name or name in skip:
            continue
        yield child
        yield from _walk(child, skip=skip)


def _text(paragraph: etree._Element) -> str:
    """The paragraph's text, whitespace-normalized. Tabs and line breaks become spaces."""
    parts: list[str] = []
    for element in _walk(paragraph, skip=_SKIPPED_IN_PARAGRAPH):
        name = local_name(element)
        if name == "t":
            parts.append(element.text or "")
        elif name in {"tab", "br", "cr", "ptab"}:
            parts.append(" ")
        elif name == "nobreakhyphen":
            parts.append("-")
    return normalize_whitespace("".join(parts))


def _style_name(paragraph: etree._Element, style_names: dict[str, str]) -> str:
    style = next((c for c in paragraph.iterchildren() if local_name(c) == "ppr"), None)
    if style is None:
        return ""
    for element in style:
        if local_name(element) == "pstyle":
            return style_names.get(element.get(f"{{{_W}}}val") or "", "").strip().lower()
    return ""


def _chapter_styles(style_names: Iterator[str]) -> frozenset[str]:
    """The paragraph styles that start chapters: Title and Heading 1, or Heading 2 if the
    document has no Heading 1."""
    names = set(style_names)
    return frozenset({"title", "heading 1"} if "heading 1" in names else {"title", "heading 2"})


# --- The cover -----------------------------------------------------------------------------


def _find_cover(
    document: DocumentObject, paragraphs: list[tuple[str, str, etree._Element]]
) -> Cover | None:
    """The first inline picture, if it looks like a cover and comes before the book's text."""
    for style, text, paragraph in paragraphs:
        picture = _first_inline_picture(paragraph)
        if picture is not None:
            return _cover_from(document, picture)
        if len(text) >= _PROSE_CHARS and not style.startswith(("heading", "title")):
            return None
    return None


def _first_inline_picture(paragraph: etree._Element) -> etree._Element | None:
    for element in _walk(paragraph, skip={"fallback"}):
        if local_name(element) == "inline":
            return element
    return None


def _cover_from(document: DocumentObject, inline: etree._Element) -> Cover | None:
    extent = inline.find(f"{{{_WP}}}extent")
    blip = inline.find(f".//{{{_A}}}blip")
    if extent is None or blip is None:
        return None
    try:
        width, height = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
    except ValueError:
        return None
    if min(width, height) < _MIN_COVER_EMU or height < width * _MIN_COVER_ASPECT:
        return None
    related = document.part.related_parts.get(blip.get(f"{{{_R}}}embed") or "")
    if related is None:
        return None
    media_type = image_type(related.blob)
    return Cover(related.blob, media_type) if media_type else None
