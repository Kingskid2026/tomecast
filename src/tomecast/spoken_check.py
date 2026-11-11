"""Check that a voice actually spoke every word of a chunk, by listening back to it.

Ming-3.0-tts (#11) sometimes skips a few words in the middle of a chunk ("it occurred to her
that she ought to have wondered" comes out as "it occurred to have wondered"). The audio has a
normal length and level, so :func:`tomecast.convert.audio_problem` can't see it. This module is
the listen-back check from docs/architecture.md ("Core guarantee", point 5), designed in
docs/spoken-check.md:

* a speech recogniser (behind the small :class:`Transcriber` interface) writes down what was said,
* :func:`compare` lines the heard words up with the chunk's words and finds the gaps,
* :class:`SkipRule` decides whether the gaps are real skips or the recogniser's own noise,
* :func:`speak_checked` regenerates (new seed, then smaller pieces) until every piece passes.
  When the smallest pieces still fail, it hands :class:`WordsSkippedError` to an
  ``on_unfixable`` strategy: :func:`stop` (the default) raises it, :class:`UseOtherVoice`
  voices the chunk with another voice and says so. Audio missing words is never kept silently.

Nothing here touches audio or the network directly: synthesis and transcription are passed in,
so the decisions are unit-tested with plain text.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from tomecast.chunking import split_long, split_sentences
from tomecast.convert import MissingAudioError


class Transcriber(Protocol):
    """Anything that turns a WAV file into the words it heard (audio.cpp, whisper.cpp, …)."""

    def transcribe(self, wav: Path) -> str:
        """Return the transcript of ``wav`` as plain text."""
        ...


class UncheckableTextError(ValueError):
    """The text is in a script the check can't compare yet (only Latin-script words can).

    Raised before anything is voiced, so a check that can't work never passes by default.
    """


# --- Normalising words -----------------------------------------------------------------------

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'"})
# Latin letters that Unicode doesn't decompose into a base letter plus an accent.
_FOLDS = str.maketrans(
    {"\u00e6": "ae", "\u0153": "oe", "\u00f8": "o", "\u0142": "l", "\u0111": "d",
     "\u00f0": "d", "\u00fe": "th", "\u0131": "i"}
)  # fmt: skip
_SPLITTERS = re.compile("[\\-\u2010-\u2015/_]+")  # hyphens, dashes and slashes join words
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*")  # letters and digits of any script
_LATIN = re.compile(r"[a-z0-9]+")
_INITIALISM = re.compile(r"(?<![^\W_])(?:[^\W\d_]\.)+[^\W\d_](?![^\W_])\.?")  # e.g. U.S.A.
_FOREIGN = "*"  # stands for a word in another script: it must be heard as *something*

# Spellings that mean the same spoken words, mapped to one form on both sides. Text prep
# writes "p.m." as "pee em" and "e.g." as "for example"; recognisers write "pm", "PM",
# "p.m.", "E.g." or "eg". Matching both sides to the same form keeps correct audio passing.
_SAME: dict[tuple[str, ...], str] = {
    ("pee", "em"): "pm", ("ay", "em"): "am", ("bee", "see"): "bc", ("bee", "see", "ee"): "bce",
    ("ay", "dee"): "ad", ("see", "ee"): "ce",
    ("for", "example"): "eg", ("et", "cetera"): "etc", ("also", "known", "as"): "aka",
    ("versus",): "vs", ("approximately",): "approx", ("okay",): "ok",
    ("mister",): "mr", ("missus",): "mrs", ("mizz",): "ms", ("doctor",): "dr",
    ("professor",): "prof", ("saint",): "st", ("street",): "st", ("mount",): "mt",
    ("per", "cent"): "percent", ("miles", "per", "hour"): "mph",
    ("kilogram",): "kg", ("kilograms",): "kg", ("kilometre",): "km", ("kilometres",): "km",
    ("kilometer",): "km", ("kilometers",): "km", ("centimetre",): "cm", ("centimetres",): "cm",
    ("centimeter",): "cm", ("centimeters",): "cm", ("millimetre",): "mm",
    ("millimetres",): "mm", ("millimeter",): "mm", ("millimeters",): "mm",
    ("pound",): "lb", ("pounds",): "lb", ("lbs",): "lb", ("ounce",): "oz", ("ounces",): "oz",
}  # fmt: skip
_SAME_LONGEST = max(len(key) for key in _SAME)

# Number words. A gap where both sides are only numbers is the same number written two ways
# ("three fifteen" heard as "315", "two point five" as "two and a half"), not a skip.
_NUMBERS = frozenset(
    {
        "zero", "nought", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty",
        "seventy", "eighty", "ninety", "hundred", "thousand", "million", "billion", "trillion",
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
        "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
        "seventeenth", "eighteenth", "nineteenth", "twentieth", "thirtieth", "fortieth",
        "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth", "hundredth",
        "thousandth", "millionth", "half", "halves", "quarter", "quarters",
    }
)  # fmt: skip
_NUMBER_FILLERS = frozenset({"and", "a", "point", "oh", "oclock"})
_DIGITS = re.compile(r"\d+(?:st|nd|rd|th|s)?")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in decomposed if not unicodedata.combining(c))
    text = text.casefold().translate(_FOLDS).translate(_APOSTROPHES)
    return _SPLITTERS.sub(" ", text)


def _raw_words(text: str) -> list[str]:
    words = []
    folded = _INITIALISM.sub(lambda match: match.group(0).replace(".", ""), _fold(text))
    for token in _TOKEN.findall(folded):
        token = token.replace("'", "")
        latin = "".join(_LATIN.findall(token))
        words.append(latin if latin == token else (latin or _FOREIGN))
    return words


def _same_forms(words: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(words):
        for size in range(min(_SAME_LONGEST, len(words) - i), 0, -1):
            form = _SAME.get(tuple(words[i : i + size]))
            if form is not None:
                out.append(form)
                i += size
                break
        else:
            out.append(words[i])
            i += 1
    return out


def spoken_words(text: str) -> list[str]:
    """The words of ``text`` in a form that can be compared with a transcript.

    Lowercase, accents removed (``Namárië`` and ``Namarie`` match), apostrophes dropped
    (``Alice's`` and ``Alices``), hyphens, dashes and slashes split words (``daisy-chain`` is
    ``daisy chain``), initialisms joined (``U.S.A.`` and ``USA``), and spellings that say the
    same thing made the same (``pee em`` and ``pm``, ``for example`` and ``e.g.``,
    ``kilograms`` and ``kg``). A word in another script becomes ``*``: it must be heard as
    something, but can't be compared letter by letter.
    """
    return _same_forms(_raw_words(text))


def uncheckable(text: str) -> str:
    """Why the check can't compare ``text``, or "" if it can.

    Only Latin-script words can be compared for now. Text with no such words, or where words
    in other scripts outnumber them, can't be checked, rather than passing by default.
    """
    words = _raw_words(text)
    foreign = words.count(_FOREIGN)
    if foreign and foreign >= len(words) - foreign:
        return f"the listen-back check only reads Latin-script text so far ({text[:40]!r})"
    return ""


def _letters(words: Sequence[str]) -> str:
    return "".join(words)


def _similar(a: str, b: str) -> bool:
    """Close enough to be the same spoken word, misheard or spelled differently."""
    if a == b or _FOREIGN in (a, b):
        return True
    shorter = min(len(a), len(b))
    if shorter < 4 or abs(len(a) - len(b)) > max(2, shorter // 3):
        # Short words must match exactly, and a much longer word is a different word
        # ("stairs" in "downstairs"), which the gap-level letter check handles.
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.75


def _edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def _joined(a: str, b: str) -> bool:
    """The same letters written as one word or several: ``down stairs`` and ``downstairs``.

    Only a join or split, with at most one letter different per ten (``Lot Lorien`` for
    ``Lothlorien``). Missing letters beyond that are missing words, so ``downstairs as I``
    heard as ``down stairs`` is not a join."""
    if not a or not b:
        return False
    allowed = max(1, max(len(a), len(b)) // 10)
    return abs(len(a) - len(b)) <= allowed and _edit_distance(a, b) <= allowed


def _numbers_only(words: Sequence[str]) -> bool:
    core = False
    for word in words:
        if word in _NUMBERS or _DIGITS.fullmatch(word):
            core = True
        elif word not in _NUMBER_FILLERS:
            return False
    return core


# --- Aligning ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    """A stretch of the chunk's words that weren't heard as written."""

    start: int  # index of the first expected word in the gap
    expected: tuple[str, ...]  # the chunk's words here
    heard: tuple[str, ...]  # what the recogniser heard in their place (may be empty)
    respelled: int = 0  # expected words that are a respelling or another script's word
    respelled_runs: int = 0  # how many separate respellings those words make up

    @property
    def same_words(self) -> bool:
        """The heard words say the same thing another way: a join or split, or the same
        number written differently."""
        if _joined(_letters(self.expected), _letters(self.heard)):
            return True
        return _numbers_only(self.expected) and _numbers_only(self.heard)

    @property
    def lost(self) -> int:
        """How many words are missing, net of what was heard in their place.

        A misheard word (``Lothlorien`` heard as ``Lothlorian``) loses nothing, and neither does
        a joined or split spelling or a number written differently. ``to her that she ought``
        heard as nothing loses 5. A respelled name counts as heard when *something* was heard
        in its place (the recogniser writes the name, not the respelling), and is lost in full
        when nothing was.
        """
        if self.same_words:
            return 0
        written = len(self.expected) - self.respelled
        spare = max(0, len(self.heard) - self.respelled_runs)
        lost = max(0, written - spare)
        unheard_runs = max(0, self.respelled_runs - len(self.heard))
        if unheard_runs:
            lost += math.ceil(self.respelled * unheard_runs / self.respelled_runs)
        return lost

    @property
    def unclear(self) -> int:
        """Words heard as something else entirely: two or more written words whose letters
        don't match what was heard in their place (Ming slurring *sitting by her* into
        *Sidemiver's*). Returns how many, or 0."""
        written = len(self.expected) - self.respelled
        if written < 2 or not self.heard or self.same_words:
            return 0  # nothing heard is counted by ``lost``
        return written


@dataclass(frozen=True, slots=True)
class Comparison:
    """The chunk's words lined up with what was heard."""

    expected: tuple[str, ...]
    heard: tuple[str, ...]
    gaps: tuple[Gap, ...]
    missing_sentences: tuple[str, ...] = ()  # sentences of which no word was heard

    @property
    def words_lost(self) -> int:
        return sum(gap.lost for gap in self.gaps)

    @property
    def largest_gap(self) -> int:
        return max((gap.lost for gap in self.gaps), default=0)

    @property
    def most_unclear(self) -> int:
        return max((gap.unclear for gap in self.gaps), default=0)

    @property
    def nothing_heard(self) -> bool:
        return bool(self.expected) and not self.heard

    def describe(self) -> str:
        """A short human-readable list of the missing words, for errors and logs."""
        if self.nothing_heard:
            return "nothing was heard"
        parts = [f"the sentence {sentence!r} (not heard)" for sentence in self.missing_sentences]
        for gap in self.gaps:
            if gap.lost or gap.unclear:
                heard = " ".join(gap.heard) or "nothing"
                parts.append(f'"{" ".join(gap.expected)}" (heard {heard})')
        return "; ".join(parts)


def _align(expected: Sequence[str], heard: Sequence[str]) -> list[tuple[int, int, int, int]]:
    """Minimum-edit alignment; returns the non-matching spans as (a0, a1, b0, b1).

    Plain dynamic programming rather than difflib's longest-block heuristic. Matching words
    cost 0, a substitution, insertion or deletion 1. Chunks are at most a few dozen words, so
    the O(n·m) table is tiny.
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
    # Walk back from the end, taking a match whenever it is on a cheapest path. So when Ming
    # jumps from one copy of a word to a later one, the matches stay with the later copy and
    # the gap is reported at the earlier one, where the jump began.
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


def _words_by_sentence(text: str) -> tuple[list[str], list[tuple[int, int, str]]]:
    """The words of ``text`` and, for each sentence, its word range and text."""
    words: list[str] = []
    sentences: list[tuple[int, int, str]] = []
    for sentence in split_sentences(text):
        start = len(words)
        words.extend(spoken_words(sentence))
        if len(words) > start:
            sentences.append((start, len(words), sentence))
    return words, sentences


def _respelled(expected: Sequence[str], respellings: Iterable[str]) -> list[bool]:
    """Which of the expected words belong to a respelling (or are another script's word)."""
    marks = [word == _FOREIGN for word in expected]
    for respelling in respellings:
        words = spoken_words(respelling)
        size = len(words)
        if not size:
            continue
        for start in range(len(expected) - size + 1):
            if list(expected[start : start + size]) == words:
                marks[start : start + size] = [True] * size
    return marks


def _runs(marks: Sequence[bool]) -> int:
    return sum(1 for i, mark in enumerate(marks) if mark and (i == 0 or not marks[i - 1]))


def _sentence_missing(start: int, end: int, gaps: Sequence[Gap], marks: Sequence[bool]) -> bool:
    covering = [g for g in gaps if g.start < end and g.start + len(g.expected) > start]
    covered = sum(min(end, g.start + len(g.expected)) - max(start, g.start) for g in covering)
    if covered < end - start:
        return False  # at least one of its words was heard as written
    if any(g.same_words for g in covering):
        return False
    heard = [word for g in covering for word in g.heard]
    if not heard:
        return True
    if all(marks[start:end]):
        return False  # a respelled name heard as some other spelling of it
    written = [g.expected[i - g.start] for g in covering for i in range(start, end)
               if g.start <= i < g.start + len(g.expected)]  # fmt: skip
    ratio = SequenceMatcher(None, _letters(written), _letters(heard), autojunk=False).ratio()
    return ratio < 0.5  # "Yes." heard as "Yeah" is misheard; heard as "the the" it's missing


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
    given. Numbers that still differ (``three fifteen`` and ``three hundred fifteen``) match
    as long as both sides are only numbers.

    ``respellings`` are the spoken sides of the book's pronunciation lists (``noo zee-lund``
    for *New Zealand*). The voice was given the respelling but the recogniser writes the
    name, so those words count as heard when anything was heard in their place.

    Raises :class:`UncheckableTextError` for text the check can't compare (another script).
    """
    reason = uncheckable(expected_text)
    if reason:
        raise UncheckableTextError(reason)
    if prepare_heard is not None:
        heard_text = prepare_heard(heard_text)
    expected_words, sentences = _words_by_sentence(expected_text)
    expected = tuple(expected_words)
    heard = tuple(
        word for sentence in split_sentences(heard_text) for word in spoken_words(sentence)
    )
    marks = _respelled(expected, respellings)
    gaps = tuple(
        Gap(
            start=a0,
            expected=expected[a0:a1],
            heard=heard[b0:b1],
            respelled=sum(marks[a0:a1]),
            respelled_runs=_runs(marks[a0:a1]),
        )
        for a0, a1, b0, b1 in _align(expected, heard)
        if a1 > a0  # words heard that weren't written (a repeated word, a filler) lose nothing
    )
    missing = tuple(
        text for start, end, text in sentences if _sentence_missing(start, end, gaps, marks)
    )
    return Comparison(expected=expected, heard=heard, gaps=gaps, missing_sentences=missing)


# --- Deciding ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkipRule:
    """When a comparison counts as skipped words.

    Defaults are measured in docs/spoken-check.md on 376 Ming and Kokoro chunks. Audio with
    words missing fails when:

    * nothing at all was heard (always, whatever the settings),
    * a whole sentence wasn't heard, however short (``whole_sentences``): "Yes." matters,
    * one gap lost ``min_gap`` words or more,
    * the chunk lost ``min_total`` words in all (single words adding up, across every piece of
      a split chunk),
    * or ``min_unclear`` written words in a row were heard as something else entirely.

    One missing word inside a sentence is usually the recogniser (``I shall`` heard as
    ``she'll``), so on its own it passes. Ming's real skips lose 2 to 11 words at a time,
    mostly 3 to 5, so there is a clear gap between them and the noise.
    """

    min_gap: int = 2
    min_total: int = 3
    min_unclear: int = 2
    whole_sentences: bool = True

    def skipped(self, comparison: Comparison, already_lost: int = 0) -> bool:
        """``already_lost`` is what earlier pieces of the same chunk lost."""
        if comparison.nothing_heard:
            return True
        if self.whole_sentences and comparison.missing_sentences:
            return True
        return (
            comparison.largest_gap >= self.min_gap
            or comparison.words_lost + already_lost >= self.min_total
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
    already_lost: int = 0,
) -> tuple[bool, Comparison]:
    """``(passed, comparison)`` for one chunk's text and its transcript."""
    comparison = compare(
        expected_text, heard_text, prepare_heard=prepare_heard, respellings=respellings
    )
    return not rule.skipped(comparison, already_lost), comparison


# --- Regenerating -----------------------------------------------------------------------------


class WordsSkippedError(MissingAudioError):
    """A chunk still misses words after every retry. Never ship such audio silently.

    A :class:`~tomecast.convert.MissingAudioError`, so the command line and the window handle
    it like any other chunk without usable audio (the audio made so far is kept).
    """

    def __init__(
        self, chunk_id: str, chunk_text: str, text: str, comparison: Comparison, tries: int
    ) -> None:
        self.chunk_id = chunk_id
        self.chunk_text = chunk_text  # the whole chunk
        self.text = text  # the smallest piece that kept failing
        self.comparison = comparison
        self.tries = tries  # how many times these words were voiced
        super().__init__(
            f"no usable audio for {chunk_id} ({text[:80]!r}): words were missing or unclear in all "
            f"{tries} tries: {comparison.describe()}"
        )


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """What to try, in order, when a piece of text loses words.

    Measured on Ming (docs/spoken-check.md): skips depend on the text, so the same text with a
    new seed is fixed only about a quarter of the time, while the same words in smaller pieces
    almost always come out whole. So: one new seed (keeps the sentence's prosody), then split
    at ``split_sizes`` characters, each piece checked on its own with fresh seeds.
    """

    reseeds: int = 1
    split_sizes: tuple[int, ...] = (80, 40)
    seed_step: int = 1_000_003  # spreads retry seeds out; a seed is never used twice per chunk

    def __post_init__(self) -> None:
        if self.reseeds < 0:
            raise ValueError("reseeds can't be negative")
        if self.seed_step < 1:
            raise ValueError("seed_step must be positive")
        sizes = self.split_sizes
        if any(size < 1 for size in sizes) or any(a <= b for a, b in pairwise(sizes)):
            raise ValueError("split_sizes must be positive and get smaller")


DEFAULT_PLAN = RetryPlan()


@dataclass(frozen=True, slots=True)
class SpokenPiece:
    """A piece of the chunk's audio, in reading order."""

    text: str
    wav: Path
    seed: int | None
    comparison: Comparison | None
    voice: str | None = None  # another voice used instead, by an ``on_unfixable`` strategy


@dataclass(slots=True)
class CheckedChunk:
    """The pieces that make up a chunk's audio (one, unless it had to be split)."""

    pieces: list[SpokenPiece] = field(default_factory=list)
    attempts: int = 0  # synthesis calls made, retries included
    fallback: WordsSkippedError | None = None  # set when ``on_unfixable`` voiced it instead
    seeds: set[int] = field(default_factory=set)

    @property
    def split(self) -> bool:
        return len(self.pieces) > 1

    @property
    def words_lost(self) -> int:
        return sum(p.comparison.words_lost for p in self.pieces if p.comparison is not None)


Synthesize = Callable[[str, int], Path]  # (text, seed) -> WAV path
OnUnfixable = Callable[[WordsSkippedError], Sequence[SpokenPiece]]
_SEEDS = 2**31 - 1  # keep seeds in a signed 32-bit range


@dataclass(frozen=True, slots=True)
class Listener:
    """Everything needed to judge a piece of audio: the recogniser and how to compare."""

    transcriber: Transcriber
    rule: SkipRule = DEFAULT_RULE
    prepare_heard: Callable[[str], str] | None = None  # the book's text prep, for digits
    respellings: tuple[str, ...] = ()  # spoken sides of the pronunciation lists

    def check(self, text: str, wav: Path, already_lost: int = 0) -> tuple[bool, Comparison]:
        return check(
            text,
            self.transcriber.transcribe(wav),
            rule=self.rule,
            prepare_heard=self.prepare_heard,
            respellings=self.respellings,
            already_lost=already_lost,
        )


def stop(error: WordsSkippedError) -> Sequence[SpokenPiece]:
    """The default ``on_unfixable``: stop the conversion with the error."""
    raise error


@dataclass(frozen=True, slots=True)
class UseOtherVoice:
    """An ``on_unfixable`` strategy: voice the whole chunk with another voice (Kokoro).

    The caller must tell the user (``CheckedChunk.fallback`` is set). With a ``listener``,
    the other voice's audio is checked too, and the original error is raised if it fails.
    """

    synthesize: Callable[[str], Path]
    voice: str
    listener: Listener | None = None

    def __call__(self, error: WordsSkippedError) -> Sequence[SpokenPiece]:
        wav = self.synthesize(error.chunk_text)
        comparison = None
        if self.listener is not None:
            passed, comparison = self.listener.check(error.chunk_text, wav)
            if not passed:
                raise error
        return [SpokenPiece(error.chunk_text, wav, None, comparison, voice=self.voice)]


@dataclass(frozen=True, slots=True)
class _Run:
    chunk_id: str
    chunk_text: str
    synthesize: Synthesize
    listener: Listener
    plan: RetryPlan
    result: CheckedChunk

    def fresh_seed(self, candidate: int) -> int:
        seed = candidate % _SEEDS
        while seed in self.result.seeds:
            seed = (seed + 1) % _SEEDS
        self.result.seeds.add(seed)
        return seed


def speak_checked(
    chunk_id: str,
    text: str,
    synthesize: Synthesize,
    listener: Listener,
    *,
    seed: int,
    plan: RetryPlan = DEFAULT_PLAN,
    on_unfixable: OnUnfixable = stop,
) -> CheckedChunk:
    """Voice ``text`` and make sure every word was spoken, regenerating if not.

    Tries the whole text with ``seed`` and then ``plan.reseeds`` new seeds. If words are still
    missing, splits it at ``plan.split_sizes[0]`` characters (with :func:`split_long`, the same
    splitter as chunking, so no text is lost) and checks each piece the same way, splitting a
    failing piece further at the next size. Words lost by earlier pieces count against later
    ones, so splitting never lets more words through than the whole chunk could lose. No seed
    is used twice. When the smallest pieces still fail, ``on_unfixable`` decides: by default
    :func:`stop` raises :class:`WordsSkippedError`. Raises :class:`UncheckableTextError`
    before voicing anything if the text can't be checked.
    """
    reason = uncheckable(text)
    if reason:
        raise UncheckableTextError(reason)
    result = CheckedChunk()
    run = _Run(chunk_id, text, synthesize, listener, plan, result)
    try:
        _speak(run, text, seed, 0, 0)
    except WordsSkippedError as error:
        result.pieces = list(on_unfixable(error))
        result.fallback = error
    return result


def _speak(run: _Run, text: str, seed: int, level: int, tries_before: int) -> None:
    plan, result = run.plan, run.result
    worst: Comparison | None = None
    for attempt in range(plan.reseeds + 1):
        attempt_seed = run.fresh_seed(seed + attempt * plan.seed_step)
        wav = run.synthesize(text, attempt_seed)
        result.attempts += 1
        passed, comparison = run.listener.check(text, wav, already_lost=result.words_lost)
        if passed:
            result.pieces.append(SpokenPiece(text, wav, attempt_seed, comparison))
            return
        worst = comparison
    assert worst is not None  # reseeds >= 0, so there was at least one attempt
    tries = tries_before + plan.reseeds + 1
    for index in range(level, len(plan.split_sizes)):
        pieces = split_long(text, plan.split_sizes[index])
        if len(pieces) > 1:
            break
    else:  # already as small as the plan goes
        raise WordsSkippedError(run.chunk_id, run.chunk_text, text, worst, tries)
    for number, piece in enumerate(pieces, start=1):
        piece_seed = seed + (index + 1) * 7919 + number * plan.seed_step
        _speak(run, piece, piece_seed, index + 1, tries)
