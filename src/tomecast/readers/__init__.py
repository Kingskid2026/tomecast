"""Readers turn ebook files into :class:`tomecast.book.Book` objects, one module per format."""

from tomecast.readers.epub import InvalidEbookError, read_epub

__all__ = ["InvalidEbookError", "read_epub"]
