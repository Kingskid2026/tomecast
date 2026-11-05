"""Split text into small, size-capped chunks for a voice model, without losing any of it.

This module is the first half of Tomecast's core guarantee, "every sentence gets spoken"
(see docs/architecture.md). Voice models silently stop when their input is too long, so text
is never sent a page or chapter at a time. Instead:

* paragraphs are split into sentences,
* sentences longer than the engine's cap are split at punctuation, then at spaces,
* the final fragment of a paragraph is always kept, even without closing punctuation,
* every chunk gets an ID, so later stages can prove each one has audio.

:func:`verify_coverage` checks that the chunks contain every character of the original text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum


class Boundary(Enum):
    """What comes after a chunk. It decides the length of the pause that follows it."""

    PART = "part"  # a long sentence continues in the next chunk
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"


@dataclass(frozen=True, slots=True)
class Chunk:
    """A piece of text small enough to send to a voice model in one call."""

    chapter: int
    paragraph: int
    sentence: int
    part: int
    text: str
    boundary: Boundary

    @property
    def speakable(self) -> bool:
        """False for text with no letters or digits, like a ``* * *`` scene break."""
        return any(character.isalnum() for character in self.text)

    @property
    def id(self) -> str:
        """A stable ID like ``c003-p0012-s004-k01``, also used for file names."""
        return f"c{self.chapter:03d}-p{self.paragraph:04d}-s{self.sentence:03d}-k{self.part:02d}"


class CoverageError(RuntimeError):
    """Raised when chunks don't contain exactly the text they were made from."""


# Words that end in a period without ending the sentence.
ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "mx", "dr", "st", "jr", "sr", "prof", "rev", "gen", "col",
        "capt", "lt", "sgt", "gov", "sen", "rep", "hon", "mt", "ft", "ave", "blvd", "rd",
        "no", "nos", "vol", "vols", "ch", "chap", "fig", "figs", "p", "pp", "ed", "eds",
        "vs", "etc", "e.g", "i.e", "cf", "al", "approx", "dept", "est", "inc", "ltd", "co",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    }
)  # fmt: skip

# Curly quotes and dashes are written as escapes so they can't be confused with ASCII ones.
_CLOSING_PUNCTUATION = "\"'\u201d\u2019)]\u00bb"  # closing quotes and brackets
_OPENING_PUNCTUATION = "\"'\u201c\u2018([\u00ab"  # opening quotes and brackets
_DASHES = "\u2014\u2013"  # em dash, en dash
# Sentence ends that need a space after them: . ! ? the ellipsis, and Hindi's danda.
_SENTENCE_END = re.compile(f"[.!?\u2026\u0964]+[{re.escape(_CLOSING_PUNCTUATION)}]*(?=\\s)")
# Chinese and Japanese sentence ends (fullwidth full stop, ! and ?), with no space after them.
_CJK_CLOSING = "\u300d\u300f\u201d\u2019\uff09"  # closing brackets and quotes
_CJK_SENTENCE_END = re.compile(f"[\u3002\uff01\uff1f]+[{_CJK_CLOSING}]*")
# Places a long sentence may be split, best first. Fullwidth (CJK) marks need no space.
_SPLIT_PREFERENCES = (
    re.compile("[;:](?=\\s)|[\uff1b\uff1a]"),
    re.compile(f"[,{_DASHES})](?=\\s)|[\uff0c\u3001]"),
)


def normalize_whitespace(text: str) -> str:
    """Collapse all runs of whitespace into single spaces and trim the ends."""
    return " ".join(text.split())


def split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences. Joining the result with spaces gives the input back.

    The input is whitespace-normalized first. A sentence ends at ``.``, ``!``, ``?``, ``…`` or
    ``।`` (plus any closing quotes or brackets) followed by a space, unless the period belongs
    to an abbreviation or an initial, or the next word starts with a lowercase letter (as in
    ``"Stop!" she cried.``). Chinese and Japanese sentences end at the fullwidth full stop,
    exclamation mark or question mark, with or without a space.
    """
    text = normalize_whitespace(paragraph)
    if not text:
        return []

    # Each boundary is (end of sentence, characters to skip after it: the space, if any).
    boundaries = {
        (match.end(), 1) for match in _SENTENCE_END.finditer(text) if _ends_sentence(text, match)
    }
    for match in _CJK_SENTENCE_END.finditer(text):
        end = match.end()
        if end < len(text):
            boundaries.add((end, 1 if text[end] == " " else 0))

    sentences: list[str] = []
    start = 0
    for end, skip in sorted(boundaries):
        if end > start:
            sentences.append(text[start:end])
            start = end + skip
    if start < len(text):
        sentences.append(text[start:])  # the trailing fragment is always kept
    return sentences


def _ends_sentence(text: str, match: re.Match[str]) -> bool:
    punctuation = match.group().rstrip(_CLOSING_PUNCTUATION)
    following = text[match.end() + 1 :].lstrip(_OPENING_PUNCTUATION)
    if following[:1].islower():
        return False
    if punctuation != ".":
        return True
    word = text[: match.start()].rsplit(" ", 1)[-1].lstrip(_OPENING_PUNCTUATION)
    if word.lower() in ABBREVIATIONS:
        return False
    # A single capital letter followed by a period is an initial: "J. R. R. Tolkien".
    return not (len(word) == 1 and word.isupper())


def split_long(sentence: str, max_chars: int) -> list[str]:
    """Split a sentence into pieces of at most ``max_chars`` characters.

    Splits at ``;`` or ``:`` first, then at commas, dashes or closing brackets, then at the
    last space that fits, and only cuts inside a word if a single word is longer than
    ``max_chars``. No characters other than the single spaces between pieces are dropped.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    pieces: list[str] = []
    rest = sentence
    while len(rest) > max_chars:
        cut, skip = _find_cut(rest, max_chars)
        pieces.append(rest[:cut])
        rest = rest[cut + skip :]
    if rest:
        pieces.append(rest)
    return pieces


def _find_cut(text: str, max_chars: int) -> tuple[int, int]:
    """Return (cut index, characters to skip after it) for a text longer than max_chars."""
    window = text[: max_chars + 1]  # a split point may sit right before position max_chars
    minimum = max_chars // 3  # don't make tiny pieces when a better split exists
    for pattern in _SPLIT_PREFERENCES:
        cuts = [m.end() for m in pattern.finditer(window) if minimum <= m.end() <= max_chars]
        if cuts:
            cut = cuts[-1]
            return cut, 1 if text[cut : cut + 1] == " " else 0
    space = window.rfind(" ", 0, max_chars + 1)
    if space > 0:
        return space, 1
    return max_chars, 0  # one enormous word: cut it, dropping nothing


def chunk_paragraphs(chapter: int, paragraphs: Sequence[str], max_chars: int) -> list[Chunk]:
    """Turn one chapter's paragraphs into chunks of at most ``max_chars`` characters."""
    chunks: list[Chunk] = []
    for p_index, paragraph in enumerate(paragraphs):
        sentences = split_sentences(paragraph)
        for s_index, sentence in enumerate(sentences):
            parts = split_long(sentence, max_chars)
            for k_index, part in enumerate(parts):
                if k_index < len(parts) - 1:
                    boundary = Boundary.PART
                elif s_index < len(sentences) - 1:
                    boundary = Boundary.SENTENCE
                else:
                    boundary = Boundary.PARAGRAPH
                chunks.append(Chunk(chapter, p_index, s_index, k_index, part, boundary))
    verify_coverage(paragraphs, chunks)
    return chunks


def verify_coverage(paragraphs: Iterable[str], chunks: Iterable[Chunk]) -> None:
    """Raise :class:`CoverageError` unless the chunks hold every character of the paragraphs.

    Whitespace is ignored for the comparison, because chunking only ever changes spacing.
    """
    expected = "".join("".join(p.split()) for p in paragraphs)
    actual = "".join("".join(c.text.split()) for c in chunks)
    if expected != actual:
        position = next(
            (i for i, (a, b) in enumerate(zip(expected, actual, strict=False)) if a != b),
            min(len(expected), len(actual)),
        )
        raise CoverageError(
            f"chunks don't match the source text (first difference at character {position}: "
            f"expected {expected[position : position + 40]!r}, "
            f"got {actual[position : position + 40]!r})"
        )
