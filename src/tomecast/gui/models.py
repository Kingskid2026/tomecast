"""The chapter list (``ChapterModel``) and the estimates shown under it.

Front matter is guessed here until the real cleanup lands (issue #4): sections before the
first numbered chapter whose title looks like a title page, contents, copyright, dedication or
edition note, or which have under 100 words.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)

from tomecast.book import Book, Chapter

# Narration speed used for estimates (Kokoro at speed 1.0 reads about this fast).
WORDS_PER_MINUTE = 158
# How much faster than real time voicing runs, until this session has measured it.
DEFAULT_SPEEDUP = 4.7
# Dots in each row's length meter.
METER_DOTS = 17
# Sections shorter than this, before the first numbered chapter, count as front matter.
FRONT_MATTER_WORDS = 100

_NUMBER_WORD = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    "fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    "eighty|ninety|hundred"
)
_ROMAN = r"(?=[ivxlcdm])m{0,4}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_NUMBER = rf"(?:\d+|{_ROMAN}|(?:{_NUMBER_WORD})(?:[- ](?:{_NUMBER_WORD}))?)"
# "Chapter 3", "PART IV", "Book Two", "Stave I"…
_NUMBERED_HEADING = re.compile(
    rf"^\s*(?:chapter|chap\.|part|book|stave|letter)\s+{_NUMBER}\b", re.IGNORECASE
)
# A title that is only a number, or starts with one followed by punctuation: "3", "IV. Pig".
_BARE_NUMBER = re.compile(r"^\s*(?:\d+|[IVXLCDM]+)(?:[.:)]|\s*$)")
# The "CHAPTER I. " in "CHAPTER I. Down the Rabbit-Hole": rows show titles only (see the mockups).
_CHAPTER_PREFIX = re.compile(
    rf"^\s*(?:chapter|chap\.)\s+{_NUMBER}"
    # then ". ", ": ", " - ", "—" or a space (a bare hyphen belongs to "Twenty-One")
    r"(?:\s*[.:]\s*|\s*[\u2014\u2013]\s*|\s+-\s+|\s+)(?=\S)",
    re.IGNORECASE,
)
_BARE_PREFIX = re.compile(r"^\s*(?:\d+|[IVXLCDM]+)[.:)]\s+(?=\S)")
_FRONT_MATTER_TITLE = re.compile(
    r"\b(?:title\s*page|half[\s-]*title|contents|copyright|dedication|edition|imprint)\b",
    re.IGNORECASE,
)


def is_numbered_chapter(title: str) -> bool:
    """True for titles like "Chapter 3", "CHAPTER III. Pig and Pepper" or "IV."."""
    return bool(_NUMBERED_HEADING.match(title) or _BARE_NUMBER.match(title))


UNTITLED = "Untitled section"
"""What a section with no title is called in the window."""


def display_title(title: str) -> str:
    """The title without a leading "Chapter N.": the window shows chapter titles only.
    A section with no title is called "Untitled section"."""
    for prefix in (_CHAPTER_PREFIX, _BARE_PREFIX):
        stripped = prefix.sub("", title, count=1)
        if stripped != title:
            return stripped.strip() or UNTITLED
    return title.strip() or UNTITLED


def word_count(chapter: Chapter) -> int:
    """Words in the chapter's text (the title isn't counted)."""
    return sum(len(paragraph.split()) for paragraph in chapter.paragraphs)


def guess_front_matter(book: Book) -> list[bool]:
    """Guess which sections are front matter (see the module docstring)."""
    chapters = book.chapters
    first_numbered = next(
        (i for i, chapter in enumerate(chapters) if is_numbered_chapter(chapter.title)), None
    )
    flags = [False] * len(chapters)
    for index, chapter in enumerate(chapters):
        if first_numbered is not None and index >= first_numbered:
            break
        looks_like_it = _looks_like_front_matter(chapter, book.title)
        if first_numbered is None and not looks_like_it:
            break  # without numbered chapters, only the opening run can be front matter
        flags[index] = looks_like_it
    if all(flags):
        return [False] * len(chapters)  # a book that's all "front matter" has none
    return flags


def _looks_like_front_matter(chapter: Chapter, book_title: str) -> bool:
    if word_count(chapter) < FRONT_MATTER_WORDS:
        return True
    if _FRONT_MATTER_TITLE.search(chapter.title):
        return True
    return bool(chapter.title) and _comparable(chapter.title) == _comparable(book_title)


def _comparable(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def _round(value: float) -> int:
    """Round halves up, as the mockups do (Python's round() rounds halves to even)."""
    return math.floor(value + 0.5)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 'S'}"


def _hours_minutes(minutes: int) -> str:
    """``"2 H 47 MIN"`` or ``"36 MIN"``."""
    hours, rest = divmod(minutes, 60)
    return f"{hours} H {rest:02d} MIN" if hours else f"{rest} MIN"


def summarize(words: int, sections: int, total_sections: int, speedup: float) -> dict[str, str]:
    """The four summary lines under the chapter list, as ready-to-show uppercase strings."""
    audio_minutes = words / WORDS_PER_MINUTE
    make_minutes = audio_minutes / speedup if speedup > 0 else 0.0
    if words == 0:
        audio, make = "NO AUDIO", "NOTHING TO MAKE"
    else:
        audio = (
            "UNDER 1 MIN OF AUDIO"
            if _round(audio_minutes) < 1
            else f"ABOUT {_hours_minutes(_round(audio_minutes))} OF AUDIO"
        )
        make = (
            "UNDER 1 MIN TO MAKE HERE"
            if _round(make_minutes) < 1
            else f"ABOUT {_hours_minutes(_round(make_minutes))} TO MAKE HERE"
        )
    noun = "SECTION" if total_sections == 1 else "SECTIONS"
    return {
        "sections": f"{sections:,} OF {total_sections:,} {noun}",
        "words": _plural(words, "WORD"),
        "audio": audio,
        "make": make,
    }


@dataclass(slots=True)
class Section:
    """One row of the chapter list: a chapter of the book as read."""

    chapter: Chapter
    title: str  # as shown, without a "Chapter N." prefix
    words: int
    front_matter: bool
    selected: bool

    @property
    def minutes(self) -> float:
        return self.words / WORDS_PER_MINUTE

    @property
    def minutes_label(self) -> str:
        return "<1 min" if self.minutes < 1 else f"{_round(self.minutes)} min"

    @property
    def label(self) -> str:
        """``"A CAUCUS-RACE AND A LONG TALE"``, as the converting screen shows it."""
        return self.title.upper()


class ChapterModel(QAbstractListModel):
    """One row per section of the book, with an editable ``selected`` role."""

    TitleRole = Qt.ItemDataRole.UserRole + 2
    WordsLabelRole = Qt.ItemDataRole.UserRole + 3
    MinutesLabelRole = Qt.ItemDataRole.UserRole + 4
    MeterLitRole = Qt.ItemDataRole.UserRole + 5
    FrontMatterRole = Qt.ItemDataRole.UserRole + 6
    SelectedRole = Qt.ItemDataRole.UserRole + 7

    _ROLE_NAMES: ClassVar[dict[int, bytes]] = {
        TitleRole: b"title",
        WordsLabelRole: b"wordsLabel",
        MinutesLabelRole: b"minutesLabel",
        MeterLitRole: b"meterLit",
        FrontMatterRole: b"frontMatter",
        SelectedRole: b"selected",
    }

    selectionChanged = Signal()  # any row's ``selected`` changed, or the rows were replaced

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sections: list[Section] = []
        self._meter_scale = 1.0

    # --- Filling ------------------------------------------------------------------------

    def set_book(self, book: Book, *, skip_front_matter: bool) -> None:
        """Replace the rows with ``book``'s chapters."""
        front = guess_front_matter(book)
        sections: list[Section] = []
        for chapter, is_front in zip(book.chapters, front, strict=True):
            sections.append(
                Section(
                    chapter=chapter,
                    title=display_title(chapter.title),
                    words=word_count(chapter),
                    front_matter=is_front,
                    selected=not (is_front and skip_front_matter),
                )
            )
        longest = max((section.minutes for section in sections), default=0.0)
        self.beginResetModel()
        self._sections = sections
        # One dot per minute, unless the longest section wouldn't fit in the meter.
        self._meter_scale = min(1.0, METER_DOTS / longest) if longest > 0 else 1.0
        self.endResetModel()
        self.selectionChanged.emit()

    def clear(self) -> None:
        self.beginResetModel()
        self._sections = []
        self.endResetModel()
        self.selectionChanged.emit()

    # --- Reading ------------------------------------------------------------------------

    @property
    def sections(self) -> Sequence[Section]:
        return tuple(self._sections)

    def selected_rows(self) -> list[int]:
        return [row for row, section in enumerate(self._sections) if section.selected]

    def summary(self, speedup: float) -> dict[str, str]:
        """``{sections, words, audio, make}`` for the rows that are ticked."""
        chosen = [section for section in self._sections if section.selected]
        return summarize(
            sum(section.words for section in chosen), len(chosen), len(self._sections), speedup
        )

    def meter_lit(self, row: int) -> int:
        lit = _round(self._sections[row].minutes * self._meter_scale)
        return max(0, min(METER_DOTS, lit))

    # --- Changing the selection ------------------------------------------------------------

    def set_selected(self, row: int, selected: bool) -> bool:
        """Tick or untick one row. Returns False if ``row`` doesn't exist."""
        if not 0 <= row < len(self._sections):
            return False
        if self._sections[row].selected != selected:
            self._sections[row].selected = selected
            index = self.index(row)
            self.dataChanged.emit(index, index, [self.SelectedRole])
            self.selectionChanged.emit()
        return True

    def set_front_matter_selected(self, selected: bool) -> None:
        """Tick or untick every row marked as front matter."""
        changed = False
        for row, section in enumerate(self._sections):
            if section.front_matter and section.selected != selected:
                section.selected = selected
                index = self.index(row)
                self.dataChanged.emit(index, index, [self.SelectedRole])
                changed = True
        if changed:
            self.selectionChanged.emit()

    # --- QAbstractListModel --------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._sections)

    def roleNames(self) -> dict[int, QByteArray]:
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._sections):
            return None
        section = self._sections[index.row()]
        if role in (self.TitleRole, Qt.ItemDataRole.DisplayRole):
            return section.title
        if role == self.WordsLabelRole:
            return f"{section.words:,}"
        if role == self.MinutesLabelRole:
            return section.minutes_label
        if role == self.MeterLitRole:
            return self.meter_lit(index.row())
        if role == self.FrontMatterRole:
            return section.front_matter
        if role == self.SelectedRole:
            return section.selected
        return None

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != self.SelectedRole or not index.isValid():
            return False
        return self.set_selected(index.row(), bool(value))

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable
