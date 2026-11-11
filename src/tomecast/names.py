"""The name finder: list the unusual capitalised words of a book before converting it.

Voice models guess how unfamiliar words sound. Names and places (Hermione, Lothlórien) are the
usual trouble, so before converting, :func:`find_names` lists the capitalised words that
appear often and aren't ordinary English, with how many times. The user can then check how
they sound (``tomecast preview``) and add fixes to a pronunciation list.

Words are found the way the voice and the pronunciation lists see them, with apostrophe-like
marks as apostrophes (:func:`tomecast.marks.speakable`), so a book that writes the
possessive "Siobhan's" with U+02BC lists Siobhan once, not twice. Each name is shown as the
book spells it (its commonest spelling).

"Ordinary English" is a list of about 22,000 words made from public-domain books
(``src/tomecast/data/common_words.txt``, built by ``scripts/make_wordlist.py``). A word is
also skipped when the book itself uses it in lowercase ("table"), since then it's not a name.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from importlib import resources

from tomecast.book import Book
from tomecast.chunking import ABBREVIATIONS
from tomecast.marks import speakable

_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*")
_SENTENCE_END = ".!?…:"
_OPENERS = "\"'([\u201c\u2018«"
# "Zorp's" is Zorp; "I'm" and "Don't" are an ordinary word plus a contraction.
_SUFFIX = re.compile(r"['\u2019](?:s|m|ll|ve|d|re|t)$", re.IGNORECASE)

DEFAULT_MIN_COUNT = 3


@dataclass(frozen=True, slots=True)
class Name:
    """A capitalised word that may need a pronunciation fix, and how often the book uses it."""

    word: str
    count: int


@cache
def common_words() -> frozenset[str]:
    """The list of ordinary English words that are not worth listing as names."""
    text = (resources.files("tomecast") / "data" / "common_words.txt").read_text(encoding="utf-8")
    return frozenset(line for line in text.splitlines() if line and not line.startswith("#"))


def book_paragraphs(book: Book) -> Iterable[str]:
    """Every title and paragraph of ``book``."""
    for chapter in book.chapters:
        yield from chapter.spoken_paragraphs()


def find_names(
    paragraphs: Iterable[str],
    *,
    min_count: int = DEFAULT_MIN_COUNT,
    skip: Iterable[str] = (),
    common: frozenset[str] | None = None,
) -> list[Name]:
    """Find frequent capitalised words that aren't ordinary English.

    A word qualifies when it is capitalised, isn't in the common-word list (``common``,
    by default :func:`common_words`), is never written in lowercase in the text, and
    appears at least ``min_count`` times. ``skip`` holds words to leave out, such as ones
    that already have a pronunciation fix. The most frequent come first.
    """
    ordinary = common if common is not None else common_words()
    skipped = {speakable(word).lower() for word in skip}
    capitalised: Counter[str] = Counter()
    mid_sentence: Counter[str] = Counter()
    lowercase: set[str] = set()
    spellings: dict[str, Counter[str]] = {}  # each word as the book writes it

    for paragraph in paragraphs:
        spoken = speakable(paragraph)  # as long as the paragraph, so the offsets carry over
        for match in _WORD.finditer(spoken):
            word = _SUFFIX.sub("", match.group())
            if not word:
                continue
            if word[0].islower():
                lowercase.add(word.lower())
                continue
            if len(word) < 2 or word.isupper():
                continue  # "I", "A", and ALL-CAPS headings or acronyms
            capitalised[word] += 1
            spellings.setdefault(word, Counter())[
                paragraph[match.start() : match.start() + len(word)]
            ] += 1
            if not _starts_sentence(spoken, match.start()):
                mid_sentence[word] += 1

    names = [
        Name(spellings[word].most_common(1)[0][0], count)
        for word, count in capitalised.items()
        if count >= min_count
        and mid_sentence[word] > 0  # a word only ever seen after a full stop is just a word
        and word.lower() not in ordinary
        and word.lower() not in lowercase
        and word.lower() not in skipped
    ]
    return sorted(names, key=lambda name: (-name.count, name.word))


def _starts_sentence(paragraph: str, index: int) -> bool:
    before = paragraph[:index].rstrip(_OPENERS + " \t\u00a0")
    if not before:
        return True
    if before[-1] not in _SENTENCE_END:
        return False
    if before[-1] == ".":
        # "Mr. Smith" and "J. R. R. Tolkien": the full stop belongs to an abbreviation.
        word = before.rsplit(None, 1)[-1].rstrip(".").lstrip(_OPENERS)
        return not (word.lower() in ABBREVIATIONS or (len(word) == 1 and word.isupper()))
    return True


def find_book_names(
    book: Book,
    *,
    min_count: int = DEFAULT_MIN_COUNT,
    skip: Iterable[str] = (),
) -> list[Name]:
    """:func:`find_names` for a whole book. This is the function the window calls."""
    return find_names(book_paragraphs(book), min_count=min_count, skip=skip)
