"""Read a single HTML file (``.html``, ``.htm`` or ``.xhtml``) into a :class:`~tomecast.book.Book`.

This takes the same path as an EPUB chapter document: the markup is turned into paragraphs by
the shared walker in :mod:`tomecast.readers._markup`, with the same skipped markup (scripts,
styles, page-break markers, Project Gutenberg boilerplate). Top-level headings (``<h1>`` and
``<h2>``) start chapters and give them their titles; deeper headings stay as paragraphs.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from tomecast.book import Book
from tomecast.readers._markup import (
    Block,
    ChapterStart,
    DocumentWalker,
    Event,
    assemble_chapters,
    descendants,
    element_text,
    first,
    local_name,
    parse_document,
    read_limited,
)
from tomecast.readers.errors import InvalidEbookError

_CHAPTER_HEADING_LEVELS = (1, 2)
_AUTHOR_META_NAMES = frozenset({"author", "dc.creator", "dcterms.creator"})


def read_html(path: Path) -> Book:
    """Read one HTML file into a :class:`~tomecast.book.Book`.

    ``<h1>`` and ``<h2>`` headings start chapters. Text before the first heading becomes a
    chapter without a title, and a file with no such headings is a single chapter titled by the
    page's ``<title>`` (or the file name).

    Raises:
        InvalidEbookError: if the file is too large to read safely, can't be parsed completely,
            or has no readable text.
        OSError: if the file itself can't be opened.
    """
    root = parse_document(read_limited(path), path.name)
    book_title = _page_title(root) or path.stem
    events = _split_at_headings(DocumentWalker({}).walk(root))
    has_text = any(isinstance(event, Block) for event in events)
    if has_text and not any(isinstance(event, ChapterStart) for event in events):
        events.insert(0, ChapterStart(book_title))
    chapters = assemble_chapters(events)
    if not chapters:
        raise InvalidEbookError(f"{path.name} has no readable text")
    return Book(
        title=book_title,
        authors=_authors(root),
        language=_language(root),
        chapters=tuple(chapters),
    )


def _split_at_headings(events: list[Event]) -> list[Event]:
    """Turn each ``<h1>``/``<h2>`` block into the start of a chapter with that title."""
    result: list[Event] = []
    for event in events:
        if isinstance(event, Block) and event.heading_level in _CHAPTER_HEADING_LEVELS:
            result.append(ChapterStart(event.text))
        else:
            result.append(event)
    return result


def _page_title(root: etree._Element) -> str:
    title = first(root, "title")
    return element_text(title) if title is not None else ""


def _authors(root: etree._Element) -> tuple[str, ...]:
    authors: list[str] = []
    for meta in descendants(root, "meta"):
        name = (meta.get("name") or "").strip().lower()
        content = " ".join((meta.get("content") or "").split())
        if name in _AUTHOR_META_NAMES and content and content not in authors:
            authors.append(content)
    return tuple(authors)


def _language(root: etree._Element) -> str | None:
    """The page's language: ``lang`` (or ``xml:lang``) on ``<html>``, else a ``<meta>`` tag."""
    if local_name(root) == "html":
        for attribute in ("lang", "{http://www.w3.org/XML/1998/namespace}lang"):
            value = (root.get(attribute) or "").strip()
            if value:
                return value
    for meta in descendants(root, "meta"):
        name = (meta.get("name") or meta.get("http-equiv") or "").strip().lower()
        value = (meta.get("content") or "").strip()
        if name in {"language", "dc.language", "content-language"} and value:
            return value
    return None
