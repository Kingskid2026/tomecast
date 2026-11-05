"""Readers turn ebook files into :class:`tomecast.book.Book` objects, one module per format.

Use :func:`read_book` to read any supported file. To add a format, write its reader module and
add one :class:`Format` line to ``_TABLE`` below (plus a content check in :func:`_sniff` if the
format can be recognized from its bytes).
"""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tomecast.book import Book
from tomecast.readers.docx import read_docx
from tomecast.readers.epub import read_epub
from tomecast.readers.errors import InvalidEbookError
from tomecast.readers.fb2 import read_fb2
from tomecast.readers.html import read_html
from tomecast.readers.pdf import read_pdf
from tomecast.readers.txt import read_txt


@dataclass(frozen=True, slots=True)
class Format:
    """One supported book format."""

    name: str  # as people see it, like "EPUB"
    suffixes: tuple[str, ...]  # lowercase, with the dot
    read: Callable[[Path], Book]


# Every supported format, in the order people see them. The window lists exactly these.
_TABLE: tuple[Format, ...] = (
    Format("EPUB", (".epub",), read_epub),
    Format("PDF", (".pdf",), read_pdf),
    Format("DOCX", (".docx",), read_docx),
    Format("FB2", (".fb2",), read_fb2),
    Format("HTML", (".html", ".htm", ".xhtml"), read_html),
    Format("TXT", (".txt",), read_txt),
)

FORMATS: tuple[str, ...] = tuple(format_.name for format_ in _TABLE)
SUFFIXES: tuple[str, ...] = tuple(suffix for format_ in _TABLE for suffix in format_.suffixes)

_BY_NAME = {format_.name: format_ for format_ in _TABLE}
_BY_SUFFIX = {suffix: format_ for format_ in _TABLE for suffix in format_.suffixes}
_SNIFF_BYTES = 4096


def read_book(path: Path) -> Book:
    """Read a book in any supported format.

    The format is found from the file's contents where that's cheap (a zip holding an EPUB or
    a DOCX, an FB2 file's root element), so a book with the wrong file extension still opens.
    Otherwise the extension decides.

    Raises:
        InvalidEbookError: if the format isn't supported, or the file can't be read as that
            format (see the individual readers).
        OSError: if the file itself can't be opened.
    """
    with path.open("rb") as stream:
        head = stream.read(_SNIFF_BYTES)
    format_ = _sniff(path, head) or _BY_SUFFIX.get(path.suffix.lower())
    if format_ is None:
        raise InvalidEbookError(_unsupported_message(path))
    return format_.read(path)


def _sniff(path: Path, head: bytes) -> Format | None:
    """Recognize a format from the file's bytes, or return None if they don't say."""
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = {name.casefold() for name in archive.namelist()}
        except (zipfile.BadZipFile, OSError):
            return None
        if "meta-inf/container.xml" in names:
            return _BY_NAME["EPUB"]
        if "word/document.xml" in names:
            return _BY_NAME["DOCX"]
        return None
    if b"<FictionBook" in head:
        return _BY_NAME["FB2"]
    if head.startswith(b"%PDF-"):
        return _BY_NAME["PDF"]
    return None


def _unsupported_message(path: Path) -> str:
    suffix = path.suffix.lower()
    kind = f"{suffix} files" if suffix else "files without an extension"
    return f"Tomecast can't read {kind}. It reads {_names_sentence()} books."


def _names_sentence() -> str:
    """The format names as a list for a sentence: "EPUB, DOCX and TXT"."""
    return f"{', '.join(FORMATS[:-1])} and {FORMATS[-1]}" if len(FORMATS) > 1 else FORMATS[0]


__all__ = [
    "FORMATS",
    "SUFFIXES",
    "Format",
    "InvalidEbookError",
    "read_book",
    "read_docx",
    "read_epub",
    "read_fb2",
    "read_html",
    "read_pdf",
    "read_txt",
]
