"""Turn written text into speakable text, before it is chunked and voiced.

Voice models read what they are given. "Dr. Smith paid $5.50 on 4 July 1776" has to become
"Doctor Smith paid five dollars and fifty cents on the fourth of July, seventeen seventy-six"
first, or the voice stumbles. This is the "Text prep" box in docs/architecture.md, and it
runs in exactly one place: :func:`tomecast.convert.plan_chunks`, between the
:class:`~tomecast.book.Book` and chunking.

It is deterministic (the same text always gives the same result) and it never removes a word.
Rules only replace a written form with words that say the same thing, and :func:`words_lost`
double-checks every paragraph: if a paragraph would lose a word, it is left as written.

Language comes from the book. English has rules (:mod:`tomecast.textprep_en`); a book in a
language without rules is voiced as written, apart from the pronunciation lists. A new language
plugs in by adding an object with ``heading`` and ``body`` methods to :data:`RULES`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from tomecast.book import Chapter
from tomecast.pronunciations import Pronunciations
from tomecast.textprep_en import ROMAN_PATTERN, EnglishRules


class TextRules(Protocol):
    """The rules for one language."""

    language: str
    consumed_words: frozenset[str]

    def heading(self, text: str) -> str:
        """Prepare a chapter title."""
        ...

    def body(self, text: str) -> str:
        """Prepare running text."""
        ...


class PassthroughRules:
    """For languages without rules yet: the text is voiced as written."""

    language = "und"
    consumed_words: frozenset[str] = frozenset()

    def heading(self, text: str) -> str:
        return text

    def body(self, text: str) -> str:
        return text


# Language code (the part before any "-") to its rules.
RULES: dict[str, TextRules] = {"en": EnglishRules()}
_PASSTHROUGH = PassthroughRules()


def rules_for(language: str | None) -> TextRules:
    """The rules for a book's language code (``"en"``, ``"en-GB"``...). Books that don't
    say what language they are in are treated as English."""
    if not language:
        return RULES["en"]
    primary = language.lower().replace("_", "-").split("-")[0]
    return RULES.get(primary, _PASSTHROUGH)


_ROMAN_WORD = re.compile(ROMAN_PATTERN)


def _words(text: str) -> list[str]:
    """The runs of letters in ``text`` ("½" and digits are not letters)."""
    return "".join(c if c.isalpha() else " " for c in text).split()


def words_lost(before: str, after: str, consumed: Iterable[str] = ()) -> list[str]:
    """The words of ``before`` that are not in ``after``, other than the ones a rule turns
    into other words on purpose (``consumed``: "Dr", "p.m." and so on, and Roman numerals).

    An empty result means no word of the original went missing.
    """
    kept = {word.lower() for word in _words(after)}
    allowed = set(consumed)
    lost = []
    for word in _words(before):
        if word.lower() in kept or word.lower() in allowed:
            continue
        if word.isupper() and _ROMAN_WORD.fullmatch(word):
            continue
        lost.append(word)
    return lost


@dataclass(frozen=True, slots=True)
class TextPrep:
    """Prepares the text of a book for the voice: pronunciation fixes first, then the
    language's rules. Build one with :meth:`for_language`."""

    rules: TextRules
    pronunciations: Pronunciations = field(default_factory=Pronunciations)

    @classmethod
    def for_language(
        cls, language: str | None, pronunciations: Pronunciations | None = None
    ) -> TextPrep:
        return cls(rules_for(language), pronunciations or Pronunciations())

    def heading(self, text: str) -> str:
        """Prepare a chapter title."""
        return self._prepare(text, self.rules.heading)

    def body(self, text: str) -> str:
        """Prepare one paragraph."""
        return self._prepare(text, self.rules.body)

    def chapter(self, chapter: Chapter) -> tuple[str, ...]:
        """The paragraphs to voice for ``chapter``, title first: what
        :meth:`Chapter.spoken_paragraphs` is, after text prep."""
        prepared = [self.body(paragraph) for paragraph in chapter.paragraphs]
        if chapter.title:
            prepared.insert(0, self.heading(chapter.title))
        return tuple(prepared)

    def _prepare(self, text: str, rule: _Rule) -> str:
        fixed = self.pronunciations.apply(text)
        prepared = rule(fixed)
        if prepared == fixed:
            return fixed
        lost = words_lost(fixed, prepared, self.rules.consumed_words)
        has_content = any(c.isalnum() for c in fixed)
        if lost or (has_content and not any(c.isalnum() for c in prepared)):
            return fixed  # a rule went wrong: speak it as written rather than lose a word
        return prepared


class _Rule(Protocol):
    def __call__(self, text: str, /) -> str: ...
