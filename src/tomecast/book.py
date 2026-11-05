"""The book model every reader produces and the rest of Tomecast consumes."""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True, slots=True)
class Book:
    """A whole book, ready to be voiced."""

    title: str
    authors: tuple[str, ...]
    language: str | None
    chapters: tuple[Chapter, ...]
    cover: Cover | None = None
