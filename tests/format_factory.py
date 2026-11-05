"""Build small DOCX, FB2, TXT and HTML books for tests, so no real (or copyrighted) books are
needed. The text is from public-domain books (Lewis Carroll, Herman Melville)."""

from __future__ import annotations

import base64
import io
import zipfile
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape

from docx import Document
from docx.shared import Inches

from tomecast.book import Book
from tomecast.chunking import normalize_whitespace, split_sentences

ALICE_1 = (
    "Alice was beginning to get very tired of sitting by her sister on the bank, and of "
    "having nothing to do: once or twice she had peeped into the book her sister was reading, "
    "but it had no pictures or conversations in it, “and what is the use of a book,” "
    "thought Alice “without pictures or conversations?”"
)
ALICE_2 = (
    "So she was considering in her own mind (as well as she could, for the hot day made her "
    "feel very sleepy and stupid), whether the pleasure of making a daisy-chain would be worth "
    "the trouble of getting up and picking the daisies, when suddenly a White Rabbit with pink "
    "eyes ran close by her."
)
ALICE_3 = (
    "“Curiouser and curiouser!” cried Alice (she was so much surprised, that for the "
    "moment she quite forgot how to speak good English). Now I’m opening out like the "
    "largest telescope that ever was!"
)
ISHMAEL = (
    "Call me Ishmael. Some years ago—never mind how long precisely—having little or "
    "no money in my purse, and nothing particular to interest me on shore, I thought I would "
    "sail about a little and see the watery part of the world."
)

# A small two-chapter book: (chapter title, paragraphs).
CHAPTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Down the Rabbit-Hole", (ALICE_1, ALICE_2)),
    ("The Pool of Tears", (ALICE_3, ISHMAEL)),
)


def sentences_of(paragraphs: Sequence[str]) -> list[str]:
    """Every non-empty sentence of some source paragraphs, whitespace-normalized."""
    return [
        normalize_whitespace(sentence)
        for paragraph in paragraphs
        for sentence in split_sentences(paragraph)
        if normalize_whitespace(sentence)
    ]


def spoken_sentences(book: Book) -> set[str]:
    """Every sentence the book will voice (chapter titles included)."""
    spoken = [text for chapter in book.chapters for text in chapter.spoken_paragraphs()]
    return set(sentences_of(spoken))


def expected_source_text() -> list[str]:
    """The sample book's chapter titles and paragraphs, in order."""
    return [text for title, paragraphs in CHAPTERS for text in (title, *paragraphs)]


def paragraphs_of(book: Book) -> list[list[str]]:
    return [list(chapter.paragraphs) for chapter in book.chapters]


def titles_of(book: Book) -> list[str]:
    return [chapter.title for chapter in book.chapters]


# --- DOCX ----------------------------------------------------------------------------------


def png(width: int, height: int) -> bytes:
    """A valid PNG of this size (solid grey), for covers."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    row = b"\x00" + b"\x80\x80\x80" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def build_docx(
    path: Path,
    items: Sequence[tuple[str, str]],
    *,
    title: str = "",
    author: str = "",
    picture: tuple[bytes, float, float] | None = None,
    header: str = "",
    footer: str = "",
) -> Path:
    """Write a DOCX of ``(style, text)`` items; a ``table`` item's text has cells split by "|"
    and rows by "/". ``picture`` is (PNG bytes, width in inches, height in inches), placed first."""
    document = Document()
    if title:
        document.core_properties.title = title
    if author:
        document.core_properties.author = author
    if header:
        document.sections[0].header.paragraphs[0].text = header
    if footer:
        document.sections[0].footer.paragraphs[0].text = footer
    if picture is not None:
        data, width, height = picture
        document.add_paragraph().add_run().add_picture(
            io.BytesIO(data), width=Inches(width), height=Inches(height)
        )
    for style, text in items:
        if style == "table":
            rows = [row.split("|") for row in text.split("/")]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for row, cells in zip(table.rows, rows, strict=True):
                for cell, cell_text in zip(row.cells, cells, strict=True):
                    cell.text = cell_text
        else:
            document.add_paragraph(text, style=style)
    document.save(str(path))
    return path


# --- FB2 -----------------------------------------------------------------------------------

FB2_NAMESPACES = (
    'xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink"'
)


def fb2_document(
    body: str,
    *,
    title: str = "Sample",
    authors: Sequence[tuple[str, str]] = (("Lewis", "Carroll"),),
    lang: str = "en",
    cover: bytes | None = None,
    extra: str = "",
    encoding: str = "utf-8",
) -> bytes:
    """A complete FB2 file as bytes, around ``body`` (the markup of the main ``<body>``)."""
    author_xml = "".join(
        f"<author><first-name>{escape(first)}</first-name><last-name>{escape(last)}</last-name></author>"
        for first, last in authors
    )
    cover_xml = '<coverpage><image l:href="#cover.png"/></coverpage>' if cover is not None else ""
    binary = (
        '<binary id="cover.png" content-type="image/png">'
        f"{base64.b64encode(cover).decode()}</binary>"
        if cover is not None
        else ""
    )
    text = (
        f'<?xml version="1.0" encoding="{encoding}"?>\n'
        f"<FictionBook {FB2_NAMESPACES}><description><title-info><genre>prose</genre>"
        f"{author_xml}<book-title>{escape(title)}</book-title>"
        f"<annotation><p>A blurb that is metadata, not book text.</p></annotation>"
        f"{cover_xml}<lang>{lang}</lang></title-info></description>"
        f"<body>{body}</body>{extra}{binary}</FictionBook>"
    )
    return text.encode(encoding)


def fb2_sections(chapters: Sequence[tuple[str, Sequence[str]]]) -> str:
    return "".join(
        f"<section><title><p>{escape(title)}</p></title>"
        + "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
        + "</section>"
        for title, paragraphs in chapters
    )


# --- ZIP files -----------------------------------------------------------------------------


def zip_bomb(path: Path, *, member: str, size: int, extra: dict[str, bytes] | None = None) -> Path:
    """A small zip file whose ``member`` expands to ``size`` bytes of zeros."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
        archive.writestr(member, bytes(size))
    return path


def book_in_format(folder: Path, name: str) -> Path:
    """A two-chapter book ("Chapter 1" and "Chapter 2") saved as ``name``, in the format its
    extension says."""
    path = folder / name
    suffix = path.suffix
    if suffix == ".docx":
        build_docx(
            path,
            [
                ("Heading 1", "Chapter 1"),
                ("Normal", "It began."),
                ("Heading 1", "Chapter 2"),
                ("Normal", "It ended."),
            ],
        )
    elif suffix == ".fb2":
        path.write_bytes(
            fb2_document(fb2_sections([("Chapter 1", ["It began."]), ("Chapter 2", ["It ended."])]))
        )
    elif suffix == ".txt":
        path.write_text("Chapter 1\n\nIt began.\n\nChapter 2\n\nIt ended.\n")
    else:
        path.write_text("<h1>Chapter 1</h1><p>It began.</p><h1>Chapter 2</h1><p>It ended.</p>")
    return path
