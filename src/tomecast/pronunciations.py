"""Pronunciation lists: plain-text files that say how to read names and odd words.

A list is a text file with one fix per line::

    # a comment
    Hermione  →  her-my-oh-nee
    Lothlórien -> loth-LOR-ee-en       # "->" works too
    kernel → colonel

* The left side is what the book says, the right side is how to say it. Both are plain
  text: the voice reads the right side in place of the left.
* Matching is by whole word. A left side written in lowercase matches the word in any case
  ("kernel", "Kernel", "KERNEL"). A left side with a capital letter matches that exact
  spelling, and its ALL-CAPS version (for headings).
* Several words work too: ``New Zealand → noo zee-lund``.
* Lines starting with ``#`` are comments, and so is anything after `` #`` on a line.

The right side is a *respelling*, not a phonetic alphabet: write the word the way you'd spell
it so an English reader says it right, with hyphens between syllables. Capital letters
for the stressed syllable are fine for readability; Tomecast lowercases them before speaking,
because the pronouncer (eSpeak NG) would spell out a word in capitals letter by letter.

Exact phonetic spellings in ``/slashes/`` are not supported: audio.cpp's server can't take
phonemes (checked in v0.9.4; see docs/architecture.md, section 10). Such a line is skipped
with a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

GLOBAL_FILE_NAME = "pronunciations.txt"
BOOK_FILE_SUFFIX = ".pronunciations.txt"

_SEPARATOR = re.compile(r"\s*(?:→|->)\s*")
_COMMENT = re.compile(r"(?:^|\s)#.*$")
_CAPITALS = re.compile(r"[A-Za-z]{2,}")


@dataclass(frozen=True, slots=True)
class Pronunciation:
    """One fix: say ``spoken`` wherever the book has ``written``."""

    written: str
    spoken: str


def global_list_path(data_folder: Path) -> Path:
    """The list that applies to every book: ``pronunciations.txt`` in Tomecast's data folder."""
    return data_folder / GLOBAL_FILE_NAME


def book_list_path(book: Path) -> Path:
    """The list for one book: next to the book file, ``Title.pronunciations.txt`` for
    ``Title.epub``. It travels with the book and is easy to find and edit."""
    return book.with_suffix(BOOK_FILE_SUFFIX)


def respelling(value: str) -> str:
    """How a right-hand side is actually spoken: capitalised syllables are lowercased.

    ``her-MY-oh-nee`` -> ``her-my-oh-nee``. Single capital letters are left alone.
    """
    return _CAPITALS.sub(lambda m: m.group().lower() if m.group().isupper() else m.group(), value)


@dataclass(frozen=True, slots=True)
class Pronunciations:
    """A set of fixes that can be applied to text. Later entries win over earlier ones."""

    entries: tuple[Pronunciation, ...] = ()
    _pattern: re.Pattern[str] | None = field(default=None, init=False, repr=False, compare=False)
    _lookup: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        folded: dict[str, str] = {}  # lowercase keys: match in any case
        exact: dict[str, str] = {}  # keys with a capital: match that spelling (and ALL CAPS)
        for entry in self.entries:
            spoken = respelling(entry.spoken)
            key = _normalise_key(entry.written)
            if key == key.lower():
                folded[key] = spoken
            else:
                exact[key] = spoken
                if len(key) > 1 and key.upper() != key:
                    exact.setdefault(key.upper(), spoken)
        lookup = {
            **{"f:" + k: v for k, v in folded.items()},
            **{"e:" + k: v for k, v in exact.items()},
        }
        pattern = None
        if folded or exact:
            # Both groups always exist; an empty one is "(?!)", which never matches.
            any_case = "(?i:" + _alternation(folded) + ")" if folded else "(?!)"
            as_written = _alternation(exact) if exact else "(?!)"
            pattern = re.compile(
                rf"(?<!\w)(?:(?P<folded>{any_case})|(?P<exact>{as_written}))(?!\w)"
            )
        object.__setattr__(self, "_pattern", pattern)
        object.__setattr__(self, "_lookup", lookup)

    def __len__(self) -> int:
        return len(self.entries)

    def apply(self, text: str) -> str:
        """Replace every whole-word match in ``text`` with its respelling."""
        if self._pattern is None:
            return text

        def replace(match: re.Match[str]) -> str:
            if match["folded"] is not None:
                return self._lookup["f:" + _normalise_key(match["folded"]).lower()]
            return self._lookup["e:" + _normalise_key(match["exact"])]

        return self._pattern.sub(replace, text)

    def then(self, other: Pronunciations) -> Pronunciations:
        """These fixes followed by ``other``'s; where both fix a word, ``other`` wins."""
        return Pronunciations((*self.entries, *other.entries))

    def written_words(self) -> frozenset[str]:
        """The lowercase left sides, for skipping words that already have a fix."""
        return frozenset(_normalise_key(e.written).lower() for e in self.entries)


def _normalise_key(text: str) -> str:
    return " ".join(text.split())


def _alternation(keys: dict[str, str]) -> str:
    # Longest first, so "New Zealand" wins over "New".
    ordered = sorted(keys, key=len, reverse=True)
    return "|".join(re.escape(key).replace(r"\ ", r"\s+") for key in ordered)


def parse(text: str, source: str = "pronunciations") -> tuple[Pronunciations, list[str]]:
    """Read a list from its text. Returns the fixes and a warning for every line skipped."""
    entries: list[Pronunciation] = []
    warnings: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        parts = _SEPARATOR.split(line, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            warnings.append(f"{source}, line {number}: expected 'written → say it as'")
            continue
        written, spoken = parts[0].strip(), parts[1].strip()
        if len(spoken) > 2 and spoken.startswith("/") and spoken.endswith("/"):
            warnings.append(
                f"{source}, line {number}: exact phonetic spellings in /slashes/ aren't "
                f"supported (the voice engine can't take them); write a respelling for "
                f"'{written}' instead, like her-my-oh-nee"
            )
            continue
        entries.append(Pronunciation(written, spoken))
    return Pronunciations(tuple(entries)), warnings


def load(path: Path) -> tuple[Pronunciations, list[str]]:
    """Read a list file. A missing file is an empty list; an unreadable one is a warning."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Pronunciations(), []
    except (OSError, UnicodeDecodeError) as error:
        return Pronunciations(), [f"couldn't read {path}: {error}"]
    return parse(text, source=path.name)


def load_for_book(data_folder: Path, book: Path | None) -> tuple[Pronunciations, list[str]]:
    """The global list followed by the book's own (which wins where they disagree)."""
    combined, warnings = load(global_list_path(data_folder))
    if book is not None:
        own, own_warnings = load(book_list_path(book))
        combined = combined.then(own)
        warnings += own_warnings
    return combined, warnings
