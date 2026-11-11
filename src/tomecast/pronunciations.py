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

Phonetic spellings are not supported: audio.cpp's server can't take phonemes (checked in
v0.9.4; see docs/architecture.md, section 10), and eSpeak reads IPA symbols out by name
("small capital I", "stress"). :func:`is_phonetic` says what counts as one (IPA in
``/slashes/`` or ``[brackets]``, eSpeak's ``[[phoneme]]`` markup, or any IPA symbol at all).
Such a line is skipped with a warning, and the window refuses to save one.

A list may start with a UTF-8 byte-order mark (some editors write one): it is ignored.

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

from tomecast.marks import speakable

GLOBAL_FILE_NAME = "pronunciations.txt"
BOOK_FILE_SUFFIX = ".pronunciations.txt"

_SEPARATOR = re.compile(r"\s*(?:→|->)\s*")
_COMMENT = re.compile(r"(?:^|\s)#.*$")
_CAPITALS = re.compile(r"[A-Za-z]{2,}")
# Symbols only a phonetic alphabet uses. Letters that real spellings use too (æ, ð, þ, ŋ, ø,
# accented letters) are left out, and so are the marks that transliterations of names use:
# the ʻokina (U+02BB), the apostrophe ʼ (U+02BC), the primes ʹ ʺ and the half rings ʾ ʿ.
# (eSpeak would read those marks out by name too, so tomecast.marks makes them apostrophes.)
_IPA = re.compile(
    "["
    "\u0250-\u02af"  # IPA Extensions: ɐ ɑ ə ɛ ɡ ɪ ɔ ʃ ʊ ʌ ʒ ʔ ...
    "\u02b0-\u02b8"  # raised letters for aspiration, palatalisation and so on: ʰ ʲ ʷ
    "\u02c0\u02c1"  # ˀ ˁ
    "\u02c8\u02cc"  # stress: ˈ (primary) and ˌ (secondary)
    "\u02d0\u02d1"  # length: ː (long) and ˑ (half long)
    "\u02de"  # ˞ (r-coloured)
    "\u02e0-\u02e9"  # ˠ ˡ ˢ ˣ ˤ, and the tone letters ˥ ˦ ˧ ˨ ˩
    "\u035c\u0361"  # tie bars, as in t͡ʃ
    "\u1d00-\u1dbf"  # Phonetic Extensions: ᴜ ᵊ ᵻ ᵿ ...
    "]"
)


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


def is_phonetic(spoken: str) -> bool:
    """True if a right-hand side is written in phonetic symbols rather than as a respelling.

    That is IPA between slashes (``/ʃɪˈvɔːn/``) or square brackets (``[ʃɪˈvɔːn]``, the way
    Wikipedia writes it), eSpeak's ``[[phoneme]]`` markup, or any IPA symbol, stress or
    length mark (``ʃɪˈvɔːn``, ``shiv-ˈawn``). The voice can't use any of them: it would read
    the symbols and brackets out. Accented letters (é, ñ, ø, ł, ð) and the marks names use,
    such as the ʻokina in *Hawaiʻi*, are fine.
    """
    spoken = spoken.strip()
    return (
        (len(spoken) > 2 and spoken.startswith("/") and spoken.endswith("/"))
        or (spoken.startswith("[") and spoken.endswith("]"))
        or "[[" in spoken
        or _IPA.search(spoken) is not None
    )


def respelling(value: str) -> str:
    """How a right-hand side is actually spoken: capitalised syllables are lowercased, and
    apostrophe-like marks eSpeak would read out by name are plain apostrophes
    (:func:`tomecast.marks.speakable`).

    ``her-MY-oh-nee`` -> ``her-my-oh-nee``, ``ha-WAI-ʼee`` -> ``ha-wai-'ee``. Single capital
    letters are left alone.
    """
    value = speakable(value)
    return _CAPITALS.sub(lambda m: m.group().lower() if m.group().isupper() else m.group(), value)


@dataclass(frozen=True, slots=True)
class Pronunciations:
    """A set of fixes that can be applied to text. Later entries win over earlier ones."""

    entries: tuple[Pronunciation, ...] = ()
    _pattern: re.Pattern[str] | None = field(default=None, init=False, repr=False, compare=False)
    # Every entry, grouped by its lowercase left side, in list order: (any case?, key, spoken).
    _rules: dict[str, list[tuple[bool, str, str]]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        rules: dict[str, list[tuple[bool, str, str]]] = {}
        # One regex alternative per spelling an entry matches, each carrying its own case
        # rule, so the longest matching entry wins whichever kind it is.
        alternatives: dict[str, int] = {}
        for entry in self.entries:
            key = _normalise_key(entry.written)
            any_case = key == key.lower()
            rules.setdefault(key.lower(), []).append((any_case, key, respelling(entry.spoken)))
            if any_case:
                alternatives["(?i:" + _literal(key) + ")"] = len(key)
            else:
                alternatives[_literal(key)] = len(key)
                if len(key) > 1 and key.upper() != key:
                    alternatives[_literal(key.upper())] = len(key)
        pattern = None
        if alternatives:
            ordered = sorted(alternatives, key=alternatives.__getitem__, reverse=True)
            pattern = re.compile(r"(?<!\w)(?:" + "|".join(ordered) + r")(?!\w)")
        object.__setattr__(self, "_pattern", pattern)
        object.__setattr__(self, "_rules", rules)

    def __len__(self) -> int:
        return len(self.entries)

    def apply(self, text: str) -> str:
        """Replace every whole-word match in ``text`` with its respelling.

        Where several entries match the same words (say a global ``alice`` and a book's
        ``Alice``), the last one in the list wins, so the book's own list beats the global
        one. A capitalised entry's ALL-CAPS form gives way to an entry written in capitals.
        """
        if self._pattern is None:
            return text

        def replace(match: re.Match[str]) -> str:
            found = _normalise_key(match.group())
            candidates = self._rules.get(found.lower(), [])
            written_in_capitals = any(
                not any_case and key == found for any_case, key, _ in candidates
            )
            for any_case, key, spoken in reversed(candidates):
                if any_case or key == found:
                    return spoken
                if found == key.upper() and len(key) > 1 and not written_in_capitals:
                    return spoken
            return match.group()

        return self._pattern.sub(replace, text)

    def then(self, other: Pronunciations) -> Pronunciations:
        """These fixes followed by ``other``'s; where both fix a word, ``other`` wins."""
        return Pronunciations((*self.entries, *other.entries))

    def written_words(self) -> frozenset[str]:
        """The lowercase left sides, for skipping words that already have a fix."""
        return frozenset(_normalise_key(e.written).lower() for e in self.entries)


def _normalise_key(text: str) -> str:
    return " ".join(text.split())


def _literal(key: str) -> str:
    """``key`` as a regex that matches it literally, with any run of whitespace between words."""
    return re.escape(key).replace(r"\ ", r"\s+")


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
        if is_phonetic(spoken):
            warnings.append(
                f"{source}, line {number}: '{spoken}' looks like IPA, and the voice can't use "
                f"phonetic symbols, so the line is skipped; write a respelling for "
                f"'{written}' instead, the way it sounds in plain letters, like shiv-AWN"
            )
            continue
        entries.append(Pronunciation(written, spoken))
    return Pronunciations(tuple(entries)), warnings


def read_list(path: Path) -> str:
    """A list file's text, without the byte-order mark some editors put at the start.

    Raises:
        OSError, UnicodeDecodeError: if it can't be read.
    """
    return path.read_text(encoding="utf-8-sig")


def load(path: Path) -> tuple[Pronunciations, list[str]]:
    """Read a list file. A missing file is an empty list; an unreadable one is a warning."""
    try:
        text = read_list(path)
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
