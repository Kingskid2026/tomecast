"""The listen-back check: alignment, the skip rule and the retry policy, on text alone.

Real transcripts below come from Parakeet TDT 0.6B v3 listening to Ming-3.0-tts audio of
*Alice's Adventures in Wonderland* (public domain); see docs/spoken-check.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tomecast.spoken_check import (
    Comparison,
    Listener,
    RetryPlan,
    SkipRule,
    WordsSkippedError,
    check,
    compare,
    speak_checked,
    spoken_words,
)
from tomecast.textprep import TextPrep

PREP = TextPrep.for_language("en").body


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("Alice's daisy-chain—and then", ["alices", "daisy", "chain", "and", "then"]),
        ("“Oh dear! Oh dear!”", ["oh", "dear", "oh", "dear"]),
        ("Namárië, Lothlórien.", ["namarie", "lothlorien"]),
        ("either/or, o’clock", ["either", "or", "oclock"]),
        ("* * *", []),
    ],
)  # fmt: skip
def test_spoken_words(text: str, words: list[str]) -> None:
    assert spoken_words(text) == words


def test_identical_text_passes() -> None:
    text = "Alice was beginning to get very tired of sitting by her sister on the bank."
    passed, comparison = check(text, text)
    assert passed
    assert comparison.gaps == ()
    assert comparison.words_lost == 0


def test_punctuation_and_case_are_ignored() -> None:
    passed, comparison = check(
        "There was nothing so very remarkable in that; nor did Alice think it so very much "
        "out of the way to hear the Rabbit say to itself, “Oh dear!",
        "There was nothing so very remarkable in that, nor did Alice think it so very much "
        "out of the way to hear the rabbit say to itself, Oh dear.",
    )
    assert passed
    assert comparison.words_lost == 0


# Real Ming skips that audio_problem missed (Parakeet transcripts, drop scan of 8 Nov).
REAL_SKIPS = [
    (
        "I shall be late!” (when she thought it over afterwards, it occurred to her that she "
        "ought to have wondered at this, but at the time it all seemed quite natural);",
        "I shall be late. When she thought it over afterwards, it occurred to have wondered at "
        "this, but at the time it all seemed quite natural.",
        "to her that she ought",
    ),
    (
        "In another moment down went Alice after it, never once considering how in the world "
        "she was to get out again.",
        "In another moment down went Alice after it, never once considering how she was to get "
        "out again.",
        "in the world",
    ),
]


@pytest.mark.parametrize(("text", "heard", "missing"), REAL_SKIPS)
def test_real_ming_skips_are_caught(text: str, heard: str, missing: str) -> None:
    passed, comparison = check(text, heard)
    assert not passed
    assert [" ".join(g.expected) for g in comparison.gaps if g.lost] == [missing]
    assert comparison.words_lost == len(missing.split())
    assert missing in comparison.describe()


def test_a_skip_between_repeated_words_is_placed_where_it_happened() -> None:
    # Ming tends to jump from one copy of a word to the next.
    comparison = compare(
        "she ran to the door and to the window and back",
        "she ran to the window and back",
    )
    assert comparison.words_lost == 4
    (gap,) = comparison.gaps
    assert gap.expected == ("to", "the", "door", "and")  # the first copy, where the jump began


def test_a_cut_off_ending_is_a_skip() -> None:
    passed, comparison = check(
        "So she was considering in her own mind as well as she could, for the hot day.",
        "So she was considering in her own mind as well as she",
    )
    assert not passed
    assert comparison.gaps[-1].expected == ("could", "for", "the", "hot", "day")


@pytest.mark.parametrize(
    ("text", "heard"),
    [
        # A misheard name is a substitution, not a skip.
        ("Galadriel whispered, Namárië, as the boats drifted toward Lothlórien.",
         "Galadriel whispered, Namarie, as the boats drifted toward Lot Lorien."),
        # Joined and split spellings.
        ("the pleasure of making a daisy-chain", "the pleasure of making a daisychain"),
        ("she looked up to-day at the sky", "she looked up today at the sky"),
        ("and ran down stairs at once", "and ran downstairs at once"),
        # One word swallowed by the recogniser.
        ("and fortunately was just in time", "unfortunately was just in time"),
        # A single dropped short word is below the threshold.
        ("she had peeped into the book her sister was reading",
         "she had peeped into book her sister was reading"),
        # The recogniser hearing extra words loses nothing.
        ("Oh dear, oh dear, I shall be late.", "Oh dear, oh dear, oh dear, I shall be late."),
    ],
)  # fmt: skip
def test_recogniser_noise_is_not_a_skip(text: str, heard: str) -> None:
    passed, comparison = check(text, heard)
    assert passed, comparison.describe()


def test_numbers_written_as_digits_match_their_words() -> None:
    text = PREP("In 1865 she paid $5.50 to Dr. Smith, on page 12.")
    heard = "In 1865 she paid $5.50 to Dr. Smith on page 12."
    assert not check(text, heard)[0]  # without text prep, the digits don't line up
    passed, comparison = check(text, heard, prepare_heard=PREP)
    assert passed
    assert comparison.words_lost == 0


def test_words_slurred_into_something_else_are_a_skip() -> None:
    # Ming slurred "sitting by her"; Moonshine heard three other words, so nothing is "lost"
    # by count, but the words weren't said.
    passed, comparison = check(
        "Alice was beginning to get very tired of sitting by her sister on the bank,",
        "Alice was beginning to get very tired of Sid and Myver's sister on the bank,",
    )
    assert comparison.words_lost == 0
    assert comparison.most_unclear == 3
    assert not passed
    assert "sitting by her" in comparison.describe()


def test_respelled_names_are_never_lost() -> None:
    # The Names step sent the respelling; the recogniser writes the real name.
    text = "They sailed to noo zee-lund and met her-my-oh-nee there."
    heard = "They sailed to New Zealand and met Hermione there."
    assert not check(text, heard)[0]
    passed, comparison = check(text, heard, respellings=["noo zee-lund", "her-my-oh-nee"])
    assert passed, comparison.describe()
    assert comparison.words_lost == 0
    # A real skip next to a respelling still counts.
    passed, _ = check(text, "They sailed to New Zealand there.", respellings=["her-my-oh-nee"])
    assert not passed


def test_single_words_add_up() -> None:
    text = "one two three four five six seven eight nine ten eleven twelve"
    heard = "one three four six seven nine ten eleven twelve"
    comparison = compare(text, heard)
    assert comparison.largest_gap == 1
    assert comparison.words_lost == 3
    assert SkipRule().skipped(comparison)
    assert not SkipRule(min_total=4).skipped(comparison)


def test_nothing_heard_loses_everything() -> None:
    passed, comparison = check("Down, down, down.", "")
    assert not passed
    assert comparison.words_lost == 3


# --- Retry policy ------------------------------------------------------------------------


class FakeVoice:
    """Pretends to synthesize: remembers each (text, seed) and drops words on chosen calls."""

    def __init__(self, drop: dict[int, str] | None = None, drop_if_longer: int = 10_000) -> None:
        self.calls: list[tuple[str, int]] = []
        self.drop = drop or {}  # call number -> words to leave out
        self.drop_if_longer = drop_if_longer  # always skip a word in text longer than this
        self.spoken: dict[Path, str] = {}

    def __call__(self, text: str, seed: int) -> Path:
        number = len(self.calls)
        self.calls.append((text, seed))
        said = text
        if number in self.drop:
            said = said.replace(self.drop[number], " ")
        if len(text) > self.drop_if_longer:
            words = said.split()
            said = " ".join(words[:2] + words[5:])  # Ming-style: three words gone mid-chunk
        wav = Path(f"/fake/{number}.wav")
        self.spoken[wav] = said
        return wav


class FakeEars:
    """A perfect recogniser: hears exactly what the fake voice said."""

    def __init__(self, voice: FakeVoice) -> None:
        self.voice = voice

    def transcribe(self, wav: Path) -> str:
        return self.voice.spoken[wav]


TEXT = (
    "Either the well was very deep, or she fell very slowly, for she had plenty of time as "
    "she went down to look about her and to wonder what was going to happen next."
)


def test_clean_audio_is_voiced_once() -> None:
    voice = FakeVoice()
    result = speak_checked("c001-p0001-s001-k00", TEXT, voice, Listener(FakeEars(voice)), seed=42)
    assert result.attempts == 1
    assert [p.text for p in result.pieces] == [TEXT]
    assert result.pieces[0].seed == 42
    assert not result.split


def test_a_skip_is_regenerated_with_a_new_seed() -> None:
    voice = FakeVoice(drop={0: "to look about her and"})
    result = speak_checked("c001-p0001-s001-k00", TEXT, voice, Listener(FakeEars(voice)), seed=42)
    assert result.attempts == 2
    assert voice.calls[0] == (TEXT, 42)
    assert voice.calls[1][0] == TEXT
    assert voice.calls[1][1] != 42
    assert [p.text for p in result.pieces] == [TEXT]


def test_a_skip_that_depends_on_the_text_is_split_and_every_word_survives() -> None:
    voice = FakeVoice(drop_if_longer=80)  # every long piece skips, like Ming's text-bound skips
    result = speak_checked("c001-p0001-s001-k00", TEXT, voice, Listener(FakeEars(voice)), seed=42)
    assert result.split
    assert all(len(p.text) <= 80 for p in result.pieces)
    # No text lost: the pieces are the chunk, in order.
    assert " ".join(p.text for p in result.pieces).split() == TEXT.split()
    # Every piece that was kept was checked and passed.
    assert all(p.comparison.words_lost == 0 for p in result.pieces)
    # Two tries of the whole chunk, then one per piece.
    assert result.attempts == 2 + len(result.pieces)
    assert len({seed for _, seed in voice.calls}) == len(voice.calls)


def test_a_piece_that_keeps_failing_is_split_again() -> None:
    voice = FakeVoice(drop_if_longer=40)
    result = speak_checked("c001-p0001-s001-k00", TEXT, voice, Listener(FakeEars(voice)), seed=1)
    assert all(len(p.text) <= 40 for p in result.pieces)
    assert " ".join(p.text for p in result.pieces).split() == TEXT.split()


def test_it_fails_loudly_rather_than_keep_audio_with_missing_words() -> None:
    voice = FakeVoice(drop_if_longer=0)  # never speaks three of the words, whatever happens
    with pytest.raises(WordsSkippedError) as raised:
        speak_checked(
            "c002-p0010-s003-k01", TEXT, voice, Listener(FakeEars(voice)), seed=42,
            plan=RetryPlan(reseeds=1, split_sizes=(80,)),
        )  # fmt: skip
    error = raised.value
    assert error.chunk_id == "c002-p0010-s003-k01"
    assert "c002-p0010-s003-k01" in str(error)
    assert isinstance(error.comparison, Comparison)
    assert error.comparison.words_lost >= 2
    assert error.attempts == len(voice.calls)


def test_no_retries_means_one_try_then_an_error() -> None:
    voice = FakeVoice(drop={0: "to look about her and"})
    with pytest.raises(WordsSkippedError):
        speak_checked(
            "c001-p0001-s001-k00", TEXT, voice, Listener(FakeEars(voice)), seed=42,
            plan=RetryPlan(reseeds=0, split_sizes=()),
        )  # fmt: skip
    assert len(voice.calls) == 1
