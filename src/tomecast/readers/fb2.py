"""Read FictionBook 2 (``.fb2``) files into a :class:`~tomecast.book.Book`.

An FB2 file is one XML document: a ``<description>`` with the metadata, one or more ``<body>``
elements of nested ``<section>``s, and base64 ``<binary>`` images. Every titled section starts
a chapter. Footnotes (``<body name="notes">``) are skipped, because a later change handles
them; the book's blurb (``<annotation>``) is metadata and isn't read either.

FB2 files are untrusted input. They're parsed without expanding entities, loading DTDs or
touching the network, and a file that defines entities at all is refused, because a reference
to one would otherwise silently lose its text (and is how "billion laughs" files work).
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from lxml import etree

from tomecast.book import Book, Cover
from tomecast.chunking import normalize_whitespace
from tomecast.readers._markup import (
    Block,
    ChapterStart,
    Event,
    assemble_chapters,
    child_elements,
    descendants,
    element_text,
    first,
    image_type,
    local_name,
    read_limited,
    xml_parser,
)
from tomecast.readers.errors import InvalidEbookError

# Elements that hold one paragraph of text, however much inline markup is inside.
_TEXT_ELEMENTS = frozenset({"p", "v", "subtitle", "text-author", "date", "th", "td"})
# Elements with nothing to read.
_SILENT_ELEMENTS = frozenset({"image", "empty-line", "binary"})
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_AUTHOR_PARTS = ("first-name", "middle-name", "last-name")


def read_fb2(path: Path) -> Book:
    """Read an FB2 file into a :class:`~tomecast.book.Book`.

    Raises:
        InvalidEbookError: if the file isn't well-formed FictionBook XML, defines entities, is
            too large to read safely, or has no readable text.
        OSError: if the file itself can't be opened.
    """
    root = parse_fb2(read_limited(path), path.name)
    events: list[Event] = []
    for body in child_elements(root, "body"):
        if (body.get("name") or "").strip().lower() == "notes":
            continue
        _read_container(body, events)
    chapters = assemble_chapters(events)
    if not chapters:
        raise InvalidEbookError(f"{path.name} has no readable text")
    info = first(root, "title-info")
    title = ""
    authors: tuple[str, ...] = ()
    language: str | None = None
    cover: Cover | None = None
    if info is not None:
        book_title = first(info, "book-title")
        title = element_text(book_title) if book_title is not None else ""
        authors = tuple(name for name in map(_author_name, child_elements(info, "author")) if name)
        lang = first(info, "lang")
        language = element_text(lang) if lang is not None and element_text(lang) else None
        cover = _read_cover(root, info)
    return Book(
        title=title or path.stem,
        authors=authors,
        language=language,
        chapters=tuple(chapters),
        cover=cover,
    )


def parse_fb2(data: bytes, name: str) -> etree._Element:
    """Parse FB2 bytes safely and check that they are a FictionBook."""
    try:
        root = etree.fromstring(data, xml_parser())
    except etree.XMLSyntaxError as error:
        raise InvalidEbookError(f"{name} isn't a well-formed FB2 file: {error}") from error
    if local_name(root) != "fictionbook":
        raise InvalidEbookError(f"{name} isn't an FB2 book: its root element isn't FictionBook")
    if next(root.iter(etree.Entity), None) is not None:
        raise InvalidEbookError(
            f"{name} defines XML entities, which Tomecast refuses to read (they can hide "
            "text or be used to exhaust memory)"
        )
    return root


def _read_container(element: etree._Element, events: list[Event]) -> None:
    """Add the paragraphs of a body, section or other container, in reading order.

    A ``<title>`` directly inside a body or section starts a chapter; anywhere else (a poem's
    title, say) it's just a paragraph. Any text that isn't inside a known paragraph element is
    kept as a paragraph too, so nothing is lost on an unusual file.
    """
    _add_loose_text(element.text, events)
    for child in element:
        name = local_name(child)
        if not name or name in _SILENT_ELEMENTS:  # a comment or processing instruction
            pass
        elif name in _TEXT_ELEMENTS:
            _add_paragraph(element_text(child), events)
        elif name == "title":
            text = " ".join(filter(None, (element_text(p) for p in _title_parts(child))))
            if local_name(element) in {"body", "section"}:
                if text:
                    events.append(ChapterStart(text))
            else:
                _add_paragraph(text, events)
        else:
            _read_container(child, events)
        _add_loose_text(child.tail, events)


def _title_parts(title: etree._Element) -> list[etree._Element]:
    """The elements holding a title's text: its paragraphs, or the title itself if it has none."""
    parts = [child for child in title if local_name(child) in {"p", "subtitle"}]
    return parts or [title]


def _add_paragraph(text: str, events: list[Event]) -> None:
    if text:
        events.append(Block(text))


def _add_loose_text(text: str | None, events: list[Event]) -> None:
    if text:
        _add_paragraph(normalize_whitespace(text), events)


def _author_name(author: etree._Element) -> str:
    parts = [
        element_text(part)
        for name in _AUTHOR_PARTS
        for part in child_elements(author, name)
        if element_text(part)
    ]
    if parts:
        return " ".join(parts)
    nickname = first(author, "nickname")
    return element_text(nickname) if nickname is not None else ""


def _read_cover(root: etree._Element, info: etree._Element) -> Cover | None:
    """Find the ``<coverpage>`` image and decode its ``<binary>``; None if it's missing or isn't
    a PNG or JPEG."""
    coverpage = first(info, "coverpage")
    if coverpage is None:
        return None
    for image in descendants(coverpage, "image"):
        href = (image.get(_XLINK_HREF) or _any_href(image)).strip()
        if not href.startswith("#"):
            continue
        for binary in child_elements(root, "binary"):
            if binary.get("id") == href[1:]:
                return _decode_cover(binary)
    return None


def _any_href(element: etree._Element) -> str:
    """An ``href`` attribute in any namespace (files use ``l:href`` and ``xlink:href`` alike)."""
    for key, value in element.attrib.items():
        if isinstance(key, str) and key.rpartition("}")[2] == "href":
            return str(value)
    return ""


def _decode_cover(binary: etree._Element) -> Cover | None:
    try:
        data = base64.b64decode("".join((binary.text or "").split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    media_type = image_type(data)
    return Cover(data, media_type) if media_type else None
