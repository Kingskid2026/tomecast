"""Check that a voice actually spoke every word of a chunk, by listening back to it.

Ming-3.0-tts (#11) sometimes skips a few words in the middle of a chunk ("it occurred to her
that she ought to have wondered" comes out as "it occurred to have wondered"). The audio has a
normal length and level, so :func:`tomecast.convert.audio_problem` can't see it. This module is
the listen-back check from docs/architecture.md ("Core guarantee", point 5), designed in
docs/spoken-check.md:

* a speech recogniser (behind the small :class:`Transcriber` interface) writes down what was said,
* :func:`compare` lines the heard words up with the chunk's words and finds the gaps,
* :class:`SkipRule` decides whether a gap is a real skip or the recogniser's own noise,
* :func:`speak_checked` regenerates (new seed, then smaller pieces) until every piece passes,
  and raises :class:`WordsSkippedError` rather than keep audio that is missing words.

Nothing here touches audio or the network directly: synthesis and transcription are passed in,
so the decisions are unit-tested with plain text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

from tomecast.chunking import split_long


class Transcriber(Protocol):
    """Anything that turns a WAV file into the words it heard (audio.cpp, whisper.cpp, …)."""

    def transcribe(self, wav: Path) -> str:
        """Return the transcript of ``wav`` as plain text."""
        ...


# --- Normalising words -----------------------------------------------------------------------

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'"})
_SPLITTERS = re.compile("[\\-\u2010-\u2015/_]+")  # hyphens, dashes and slashes join words
_NOT_WORD = re.compile(r"[^a-z0-9' ]+")


def spoken_words(text: str) -> list[str]:
    """The words of ``text`` in a form that can be compared with a transcript.

    Lowercase, accents removed (``Namárië`` and ``Namarie`` match), apostrophes dropped
    (``Alice's`` and ``Alices``), and hyphens, dashes and slashes split words
    (``daisy-chain`` is ``daisy chain``). Joined and split spellings are matched later by
    their letters, so ``daisychain`` still matches ``daisy chain``.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in decomposed if not unicodedata.combining(c))
    text = text.lower().translate(_APOSTROPHES)
    text = _SPLITTERS.sub(" ", text)
    text = _NOT_WORD.sub(" ", text)
    return [word for word in (w.replace("'", "") for w in text.split()) if word]


def _letters(words: Sequence[str]) -> str:
    return "".join(words)


def _similar(a: str, b: str) -> bool:
    """Close enough to be the same spoken word, misheard or spelled differently."""
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter < 4 or abs(len(a) - len(b)) > max(2, shorter // 3):
        # Short words must match exactly, and a much longer word is a different word
        # ("stairs" in "downstairs"), which the gap-level letter check handles.
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.75


# --- Aligning ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    """A stretch of the chunk's words that weren't heard as written."""

    start: int  # index of the first expected word in the gap
    expected: tuple[str, ...]  # the chunk's words here
    heard: tuple[str, ...]  # what the recogniser heard in their place (may be empty)
    respelled: int = 0  # how many of the expected words are a pronunciation respelling

    @property
    def _matches_by_letters(self) -> bool:
        return _similar(_letters(self.expected), _letters(self.heard))

    @property
    def lost(self) -> int:
        """How many words are missing, net of what was heard in their place.

        A misheard word (``Lothlorien`` heard as ``Lot Lorian``) loses nothing, and neither does
        a joined or split spelling. ``to her that she ought`` heard as nothing loses 5.
        Respelled words never count: the recogniser writes the name, not the respelling.
        """
        if self._matches_by_letters:
            return 0
        return max(0, len(self.expected) - self.respelled - len(self.heard))

    @property
    def unclear(self) -> int:
        """Words heard as something else entirely: two or more written words whose letters
        don't match what was heard in their place (Ming slurring *sitting by her* into
        *Sidemiver's*). Returns how many, or 0."""
        written = len(self.expected) - self.respelled
        if written < 2 or self._matches_by_letters:
            return 0
        return written


@dataclass(frozen=True, slots=True)
class Comparison:
    """The chunk's words lined up with what was heard."""

    expected: tuple[str, ...]
    heard: tuple[str, ...]
    gaps: tuple[Gap, ...]

    @property
    def words_lost(self) -> int:
        return sum(gap.lost for gap in self.gaps)

    @property
    def largest_gap(self) -> int:
        return max((gap.lost for gap in self.gaps), default=0)

    @property
    def most_unclear(self) -> int:
        return max((gap.unclear for gap in self.gaps), default=0)

    def describe(self) -> str:
        """A short human-readable list of the missing words, for errors and logs."""
        parts = []
        for gap in self.gaps:
            if gap.lost or gap.unclear:
                heard = " ".join(gap.heard) or "nothing"
                parts.append(f'"{" ".join(gap.expected)}" (heard {heard})')
        return "; ".join(parts)


def _align(expected: Sequence[str], heard: Sequence[str]) -> list[tuple[int, int, int, int]]:
    """Minimum-edit alignment; returns the non-matching spans as (a0, a1, b0, b1).

    Plain dynamic programming rather than difflib's longest-block heuristic, so a skip that
    jumps between two copies of a repeated word is still lined up at the cheapest place.
    Matching words cost 0, a substitution, insertion or deletion 1. Chunks are at most a few
    dozen words, so the O(n·m) table is tiny.
    """
    n, m = len(expected), len(heard)
    same = [[_similar(a, b) for b in heard] for a in expected]
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
    for j in range(1, m + 1):
        cost[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diagonal = cost[i - 1][j - 1] + (0 if same[i - 1][j - 1] else 1)
            cost[i][j] = min(diagonal, cost[i - 1][j] + 1, cost[i][j - 1] + 1)
    # Walk back, collecting the matched pairs. Ties prefer a match, then a deletion (a word
    # not heard), so a missing word is reported where it was skipped, not shifted.
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        if same[i - 1][j - 1] and cost[i][j] == cost[i - 1][j - 1]:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif cost[i][j] == cost[i - 1][j] + 1:
            i -= 1
        elif cost[i][j] == cost[i][j - 1] + 1:
            j -= 1
        else:  # substitution
            i, j = i - 1, j - 1
    pairs.reverse()
    spans = []
    a = b = 0
    for pa, pb in [*pairs, (n, m)]:
        if pa > a or pb > b:
            spans.append((a, pa, b, pb))
        a, b = pa + 1, pb + 1
    return spans


def compare(
    expected_text: str,
    heard_text: str,
    *,
    prepare_heard: Callable[[str], str] | None = None,
    respellings: Iterable[str] = (),
) -> Comparison:
    """Line up the chunk's text with a transcript and find the words that weren't spoken.

    ``expected_text`` is the text that was sent to the voice (after text prep, so numbers are
    already words). Recognisers write numbers as digits ("1865", "$5.50"), so the transcript
    goes through ``prepare_heard`` first: pass Tomecast's own text prep for the book's language
    (``TextPrep.for_language(lang).body``), and digits come out as the same words the voice was
    given.

    ``respellings`` are the spoken sides of the book's pronunciation lists (``noo zee-lund``
    for *New Zealand*). The voice was given the respelling but the recogniser writes the
    name, so those words are never counted as lost.
    """
    if prepare_heard is not None:
        heard_text = prepare_heard(heard_text)
    expected = tuple(spoken_words(expected_text))
    heard = tuple(spoken_words(heard_text))
    respelled = _respelled(expected, respellings)
    gaps = tuple(
        Gap(
            start=a0,
            expected=expected[a0:a1],
            heard=heard[b0:b1],
            respelled=sum(respelled[a0:a1]),
        )
        for a0, a1, b0, b1 in _align(expected, heard)
        if a1 > a0  # words heard that weren't written (a repeated word, a filler) lose nothing
    )
    return Comparison(expected=expected, heard=heard, gaps=gaps)


def _respelled(expected: Sequence[str], respellings: Iterable[str]) -> list[bool]:
    """Which of the expected words belong to a respelling."""
    marks = [False] * len(expected)
    for respelling in respellings:
        words = spoken_words(respelling)
        size = len(words)
        if not size:
            continue
        for start in range(len(expected) - size + 1):
            if list(expected[start : start + size]) == words:
                marks[start : start + size] = [True] * size
    return marks


# --- Deciding ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkipRule:
    """When a comparison counts as skipped words.

    Defaults are measured in docs/spoken-check.md on 376 Ming and Kokoro chunks: a gap of two
    or more missing words is a skip, and so are two or more words heard as something else
    entirely (slurred). One missing word on its own is usually the recogniser (``I shall``
    heard as ``she'll``), so single words only count when ``min_total`` of them add up in one
    chunk. Ming's real skips lose 2 to 11 words at a time, mostly 3 to 5, so there is a clear gap
    between them and the noise.
    """

    min_gap: int = 2  # one gap losing this many words is a skip
    min_total: int = 3  # or this many lost words across the chunk
    min_unclear: int = 2  # or this many words in a row heard as something else entirely

    def skipped(self, comparison: Comparison) -> bool:
        return (
            comparison.largest_gap >= self.min_gap
            or comparison.words_lost >= self.min_total
            or comparison.most_unclear >= self.min_unclear
        )


DEFAULT_RULE = SkipRule()


def check(
    expected_text: str,
    heard_text: str,
    *,
    rule: SkipRule = DEFAULT_RULE,
    prepare_heard: Callable[[str], str] | None = None,
    respellings: Iterable[str] = (),
) -> tuple[bool, Comparison]:
    """``(passed, comparison)`` for one chunk's text and its transcript."""
    comparison = compare(
        expected_text, heard_text, prepare_heard=prepare_heard, respellings=respellings
    )
    return not rule.skipped(comparison), comparison


# --- Regenerating -----------------------------------------------------------------------------


class WordsSkippedError(RuntimeError):
    """Raised when a chunk still misses words after every retry. Never ship such audio."""

    def __init__(self, chunk_id: str, text: str, comparison: Comparison, attempts: int) -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.comparison = comparison
        self.attempts = attempts
        super().__init__(
            f"The voice kept leaving out words in {chunk_id} after {attempts} tries: "
            f"{comparison.describe()}. Text: {text!r}"
        )


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """What to try, in order, when a piece of text loses words.

    Measured on Ming (docs/spoken-check.md): skips depend on the text, so the same text with a
    new seed is fixed only about a quarter of the time, while the same words in smaller pieces
    almost always come out whole. So: one new seed (keeps the sentence's prosody), then split
    at ``split_sizes`` characters, each piece checked on its own with a fresh seed.
    """

    reseeds: int = 1
    split_sizes: tuple[int, ...] = (80, 40)
    seed_step: int = 1_000_003  # a large odd step, so retry seeds never repeat a nearby seed


DEFAULT_PLAN = RetryPlan()


@dataclass(frozen=True, slots=True)
class SpokenPiece:
    """A piece of the chunk whose audio passed the check, in reading order."""

    text: str
    wav: Path
    seed: int
    comparison: Comparison


@dataclass(slots=True)
class CheckedChunk:
    """The pieces that make up a chunk's audio (one, unless it had to be split)."""

    pieces: list[SpokenPiece] = field(default_factory=list)
    attempts: int = 0  # synthesis calls made, retries included

    @property
    def split(self) -> bool:
        return len(self.pieces) > 1


Synthesize = Callable[[str, int], Path]  # (text, seed) -> WAV path
_SEEDS = 2**31 - 1  # keep retry seeds in a signed 32-bit range


@dataclass(frozen=True, slots=True)
class Listener:
    """Everything needed to judge a piece of audio: the recogniser and how to compare."""

    transcriber: Transcriber
    rule: SkipRule = DEFAULT_RULE
    prepare_heard: Callable[[str], str] | None = None  # the book's text prep, for digits
    respellings: tuple[str, ...] = ()  # spoken sides of the pronunciation lists

    def check(self, text: str, wav: Path) -> tuple[bool, Comparison]:
        return check(
            text,
            self.transcriber.transcribe(wav),
            rule=self.rule,
            prepare_heard=self.prepare_heard,
            respellings=self.respellings,
        )


def speak_checked(
    chunk_id: str,
    text: str,
    synthesize: Synthesize,
    listener: Listener,
    *,
    seed: int,
    plan: RetryPlan = DEFAULT_PLAN,
) -> CheckedChunk:
    """Voice ``text`` and make sure every word was spoken, regenerating if not.

    Tries the whole text with ``seed`` and then ``plan.reseeds`` new seeds. If words are still
    missing, splits it at ``plan.split_sizes[0]`` characters (with :func:`split_long`, the same
    splitter as chunking, so no text is lost) and checks each piece the same way, splitting a
    failing piece further at the next size. Raises :class:`WordsSkippedError` when the smallest
    pieces still lose words: losing text silently is never an option.
    """
    result = CheckedChunk()
    _speak(chunk_id, text, synthesize, listener, plan, seed, 0, result)
    return result


def _speak(
    chunk_id: str,
    text: str,
    synthesize: Synthesize,
    listener: Listener,
    plan: RetryPlan,
    seed: int,
    level: int,
    result: CheckedChunk,
) -> None:
    worst: Comparison | None = None
    for attempt in range(plan.reseeds + 1):
        attempt_seed = (seed + attempt * plan.seed_step) % _SEEDS
        wav = synthesize(text, attempt_seed)
        result.attempts += 1
        passed, comparison = listener.check(text, wav)
        if passed:
            result.pieces.append(SpokenPiece(text, wav, attempt_seed, comparison))
            return
        worst = comparison
    assert worst is not None
    for index in range(level, len(plan.split_sizes)):
        pieces = split_long(text, plan.split_sizes[index])
        if len(pieces) > 1:
            break
    else:  # already as small as the plan goes
        raise WordsSkippedError(chunk_id, text, worst, result.attempts)
    for number, piece in enumerate(pieces, start=1):
        piece_seed = (seed + (index + 1) * 7919 + number * plan.seed_step) % _SEEDS
        _speak(chunk_id, piece, synthesize, listener, plan, piece_seed, index + 1, result)
