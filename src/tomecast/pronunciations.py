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

The window edits these files with :func:`edit` and :func:`save`, which change only the line
for the word being fixed and keep every other line and comment as the user wrote it.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
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


# --- Editing a list file ------------------------------------------------------------------------


def same_written(written: str, other: str) -> bool:
    """True if the left sides ``written`` and ``other`` are the same, apart from spacing."""
    return _normalise_key(written) == _normalise_key(other)


def matches(written: str, word: str) -> bool:
    """True if a fix whose left side is ``written`` applies to the whole of ``word``.

    The same rule :class:`Pronunciations` uses: a lowercase left side matches any case, one
    with a capital matches that spelling and its ALL-CAPS version.
    """
    key, word = _normalise_key(written), _normalise_key(word)
    if key == key.lower():
        return word.lower() == key
    return word == key or (len(key) > 1 and word == key.upper())


def edit(text: str, written: str, spoken: str | None) -> str:
    """``text`` (a list file's contents) with the fix for ``written`` set to ``spoken``, or
    removed when ``spoken`` is ``None``. Every other line, comment and blank line is kept.

    Lines are found by their left side (:func:`same_written`). Changing a fix that is there
    rewrites the right side of its last line (the one that wins), keeping the left side, the
    arrow, the spacing and any comment; a new fix goes at the end as ``written → spoken``.
    Removing a fix removes every line for it, so no older line takes its place.
    """
    lines = text.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if _left_side(line) == _normalise_key(written)]
    if spoken is None:
        return "".join(line for i, line in enumerate(lines) if i not in found)
    if found:
        lines[found[-1]] = _with_right_side(lines[found[-1]], spoken)
        return "".join(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith(("\n", "\r")):
        text += newline
    return f"{text}{_normalise_key(written)} → {spoken}{newline}"


def _left_side(line: str) -> str | None:
    """The left side of a fix line (spacing normalised), or None for comments and blanks."""
    content = _COMMENT.sub("", line.rstrip("\r\n")).strip()
    parts = _SEPARATOR.split(content, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return _normalise_key(parts[0])


def _with_right_side(line: str, spoken: str) -> str:
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    comment = _COMMENT.search(body)
    content, tail = (body[: comment.start()], body[comment.start() :]) if comment else (body, "")
    arrow = _SEPARATOR.search(content)
    assert arrow is not None  # only called for lines that have a left side
    right = content[arrow.end() :]
    padding = right[len(right.rstrip()) :]  # keeps a comment lined up where it was
    return content[: arrow.end()] + spoken + padding + tail + ending


def save(path: Path, text: str) -> None:
    """Write a list file: to a temporary file next to it, then moved into place, so a list is
    never left half written. A list that is a symbolic link stays one (the file it points to
    is written), and a list left with nothing in it is removed rather than kept empty.

    Raises:
        OSError: if the folder can't be written to.
    """
    if not text.strip() and not path.is_symlink():
        path.unlink(missing_ok=True)
        return
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    # A plain open (not mkstemp), so a new list gets the usual permissions for the user.
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as file:
            file.write(text)
        if target.exists():
            shutil.copymode(target, temporary)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
