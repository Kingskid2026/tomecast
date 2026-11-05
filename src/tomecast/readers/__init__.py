"""Readers turn ebook files into :class:`tomecast.book.Book` objects, one module per format."""

from tomecast.readers.epub import InvalidEbookError, read_epub

# The formats Tomecast can read today, in the order people see them. The window lists only
# these, so add a format here in the same pull request as its reader.
FORMATS: tuple[str, ...] = ("EPUB",)

__all__ = ["FORMATS", "InvalidEbookError", "read_epub"]
