"""The chapter list (``ChapterModel``) and the estimates shown under it.

Which sections are front or back matter comes from :mod:`tomecast.cleanup`.
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
from tomecast.cleanup import NUMBER_PATTERN, Matter, find_matter
from tomecast.speed import WORDS_PER_MINUTE, audio_minutes, make_minutes
from tomecast.speed import word_count as word_count

# How much faster than real time voicing runs, until the speed test (or a conversion) has
# measured it on this computer.
DEFAULT_SPEEDUP = 4.7
# Dots in each row's length meter.
METER_DOTS = 17

# The "CHAPTER I. " in "CHAPTER I. Down the Rabbit-Hole": rows show titles only (see the mockups).
_CHAPTER_PREFIX = re.compile(
    rf"^\s*(?:chapter|chap\.)\s+{NUMBER_PATTERN}"
    # then ". ", ": ", " - ", "\u2014" or a space (a bare hyphen belongs to "Twenty-One")
    r"(?:\s*[.:]\s*|\s*[\u2014\u2013]\s*|\s+-\s+|\s+)(?=\S)",
    re.IGNORECASE,
)
_BARE_PREFIX = re.compile(r"^\s*(?:\d+|[IVXLCDM]+)[.:)]\s+(?=\S)")

FRONT_MATTER_LABEL = "FRONT MATTER"
BACK_MATTER_LABEL = "BACK MATTER"


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


def matter_labels(matter: Sequence[Matter | None]) -> list[str]:
    """``"FRONT MATTER"`` for flagged sections before the story, ``"BACK MATTER"`` after it,
    ``""`` for the rest."""
    story = [i for i, kind in enumerate(matter) if kind is None]
    first = story[0] if story else len(matter)
    return [
        "" if kind is None else (FRONT_MATTER_LABEL if i < first else BACK_MATTER_LABEL)
        for i, kind in enumerate(matter)
    ]


def _round(value: float) -> int:
    """Round halves up, as the mockups do (Python's round() rounds halves to even)."""
    return math.floor(value + 0.5)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 'S'}"


def _hours_minutes(minutes: int) -> str:
    """``"2 H 47 MIN"`` or ``"36 MIN"``."""
    hours, rest = divmod(minutes, 60)
    return f"{hours} H {rest:02d} MIN" if hours else f"{rest} MIN"


def summarize(
    words: int, sections: int, total_sections: int, speedup: float, speed: float = 1.0
) -> dict[str, str]:
    """The four summary lines under the chapter list, as ready-to-show uppercase strings.

    ``speed`` is the chosen speaking speed. The audio ends up that much shorter. Making it
    takes as long as voicing it takes (``speedup`` is measured on the voice's own audio, so
    the voice's speed counts), and the time-stretch on top is quick.
    """
    listening = audio_minutes(words, speed)
    making = make_minutes(words, speed, speedup)
    if words == 0:
        audio, make = "NO AUDIO", "NOTHING TO MAKE"
    else:
        audio = (
            "UNDER 1 MIN OF AUDIO"
            if _round(listening) < 1
            else f"ABOUT {_hours_minutes(_round(listening))} OF AUDIO"
        )
        make = (
            "UNDER 1 MIN TO MAKE HERE"
            if _round(making) < 1
            else f"ABOUT {_hours_minutes(_round(making))} TO MAKE HERE"
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
    front_matter: bool  # front or back matter: "Skip front matter" unticks it
    selected: bool
    matter_label: str = ""  # "FRONT MATTER", "BACK MATTER" or ""

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
    MatterLabelRole = Qt.ItemDataRole.UserRole + 8

    _ROLE_NAMES: ClassVar[dict[int, bytes]] = {
        TitleRole: b"title",
        WordsLabelRole: b"wordsLabel",
        MinutesLabelRole: b"minutesLabel",
        MeterLitRole: b"meterLit",
        FrontMatterRole: b"frontMatter",
        SelectedRole: b"selected",
        MatterLabelRole: b"matterLabel",
    }

    selectionChanged = Signal()  # any row's ``selected`` changed, or the rows were replaced

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sections: list[Section] = []
        self._meter_scale = 1.0

    # --- Filling ------------------------------------------------------------------------

    def set_book(
        self,
        book: Book,
        *,
        skip_front_matter: bool,
        matter: Sequence[Matter | None] | None = None,
        keep_selection: bool = False,
    ) -> None:
        """Replace the rows with ``book``'s chapters.

        ``matter`` says which chapters are front or back matter (as found by
        :func:`tomecast.cleanup.clean`); without it, :func:`tomecast.cleanup.find_matter`
        decides. With ``keep_selection`` (the same book, cleaned again), rows keep their
        ticks if they're still the same section.
        """
        kinds = tuple(matter) if matter is not None else find_matter(book)
        previous = {
            (row, section.title, section.front_matter): section.selected
            for row, section in enumerate(self._sections)
        }
        sections: list[Section] = []
        for row, (chapter, kind, label) in enumerate(
            zip(book.chapters, kinds, matter_labels(kinds), strict=True)
        ):
            title = display_title(chapter.title)
            is_matter = kind is not None
            selected = not (is_matter and skip_front_matter)
            if keep_selection:
                selected = previous.get((row, title, is_matter), selected)
            sections.append(
                Section(
                    chapter=chapter,
                    title=title,
                    words=word_count(chapter),
                    front_matter=is_matter,
                    selected=selected,
                    matter_label=label,
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

    def summary(self, speedup: float, speed: float = 1.0) -> dict[str, str]:
        """``{sections, words, audio, make}`` for the rows that are ticked."""
        chosen = [section for section in self._sections if section.selected]
        return summarize(
            sum(section.words for section in chosen),
            len(chosen),
            len(self._sections),
            speedup,
            speed,
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
        if role == self.MatterLabelRole:
            return section.matter_label
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
