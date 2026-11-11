"""The listen-back check: alignment, the skip rule and the retry policy, on text alone.

Real transcripts below come from Parakeet TDT 0.6B v3 listening to Ming-3.0-tts audio of
*Alice's Adventures in Wonderland* (public domain); see docs/spoken-check.md.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tomecast.convert import MissingAudioError
from tomecast.spoken_check import (
    Comparison,
    Listener,
    RetryPlan,
    SkipRule,
    SpokenPiece,
    UncheckableTextError,
    UseOtherVoice,
    WordsSkippedError,
    check,
    compare,
    speak_checked,
    spoken_words,
    uncheckable,
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
        ("Stra\u00dfe, \u00c6sir", ["strasse", "aesir"]),
        ("the U.S.A. and the USA", ["the", "usa", "and", "the", "usa"]),
        ("three pee em, 3 p.m., 3 PM", ["three", "pm", "3", "pm", "3", "pm"]),
        ("for example, e.g., E.g.", ["eg", "eg", "eg"]),
        ("two kilograms, 2 kg", ["two", "kg", "2", "kg"]),
        ("He wrote \u03bb\u03cc\u03b3\u03bf\u03c2.", ["he", "wrote", "*"]),
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


SENTENCE = "She had peeped into the book her sister was reading, but it had no pictures."


def test_two_missing_words_are_a_skip_and_one_is_not() -> None:
    # The headline threshold: a gap of two words fails, one word inside a sentence passes.
    two = compare(SENTENCE, SENTENCE.replace("the book ", ""))
    assert two.largest_gap == 2
    assert SkipRule().skipped(two)
    assert check(SENTENCE, SENTENCE.replace("the book ", ""))[0] is False
    one = compare(SENTENCE, SENTENCE.replace("the ", "", 1))
    assert one.largest_gap == 1
    assert check(SENTENCE, SENTENCE.replace("the ", "", 1))[0] is True
    assert not SkipRule(min_gap=3).skipped(two)


def test_a_jump_between_repeated_words_is_reported_at_the_first_copy() -> None:
    # Ming tends to jump from one copy of a word to a later one. Walking back from the end,
    # matches stay with the later copy, so the gap is where the jump began.
    comparison = compare(
        "she ran to the door and to the window and back",
        "she ran to the window and back",
    )
    assert comparison.words_lost == 4
    (gap,) = comparison.gaps
    assert gap.start == 2
    assert gap.expected == ("to", "the", "door", "and")


def test_how_words_match() -> None:
    # Words under four letters must match exactly: "cat" heard as "cap" is a substitution.
    (gap,) = compare("the cat sat", "the cap sat").gaps
    assert (gap.expected, gap.heard) == (("cat",), ("cap",))
    # Longer words match when they are at least 75% alike and of similar length.
    assert compare("toward lothlorien", "toward lothlorian").gaps == ()
    (gap,) = compare("a gardener", "a gordon").gaps  # too different
    assert gap.lost == 0  # but one word for one word loses nothing
    # A much longer word is a different word; the gap then matches by its letters.
    (gap,) = compare("ran down stairs", "ran downstairs").gaps
    assert gap.same_words
    assert gap.lost == 0


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
        # A single dropped short word inside a sentence is below the threshold.
        ("she had peeped into the book her sister was reading",
         "she had peeped into book her sister was reading"),
        # The recogniser hearing extra words loses nothing.
        ("Oh dear, oh dear, I shall be late.", "Oh dear, oh dear, oh dear, I shall be late."),
        # A one-word sentence misheard, not missing.
        ("Yes. She went in.", "Yeah. She went in."),
    ],
)  # fmt: skip
def test_recogniser_noise_is_not_a_skip(text: str, heard: str) -> None:
    passed, comparison = check(text, heard)
    assert passed, comparison.describe()


@pytest.mark.parametrize(
    ("text", "heard"),
    [
        ("Yes.", ""),
        ("No!", "the the"),
        ("“Why?”", "thank you for watching"),
        ("Stop! He left the room at once.", "He left the room at once."),
        ("He left the room at once. Never.", "He left the room at once."),
    ],
)  # fmt: skip
def test_a_missing_sentence_fails_however_short(text: str, heard: str) -> None:
    passed, comparison = check(text, heard)
    assert not passed, comparison.describe()
    assert comparison.missing_sentences or comparison.nothing_heard


def test_which_rule_catches_a_missing_one_word_sentence() -> None:
    text, heard = "Stop! He left the room at once.", "He left the room at once."
    comparison = compare(text, heard)
    assert comparison.missing_sentences == ("Stop!",)
    assert comparison.words_lost == 1
    assert not SkipRule(whole_sentences=False).skipped(comparison)  # the sentence rule did it
    # Nothing heard at all always fails, whatever the settings.
    lenient = SkipRule(min_gap=99, min_total=99, min_unclear=99, whole_sentences=False)
    assert lenient.skipped(compare("Yes.", ""))


def test_numbers_written_as_digits_match_their_words() -> None:
    text = PREP("In 1865 she paid $5.50 to Dr. Smith, on page 12.")
    heard = "In 1865 she paid $5.50 to Dr. Smith on page 12."
    assert not check(text, heard)[0]  # without text prep, the digits don't line up
    passed, comparison = check(text, heard, prepare_heard=PREP)
    assert passed
    assert comparison.words_lost == 0


@pytest.mark.parametrize(
    ("source", "heard"),
    [
        ("At 3:15 p.m. he left.", "At 315 pm he left."),
        ("At 3:15 p.m. he left.", "At 3.15 PM he left."),
        ("At 3:15 p.m. he left.", "At 3 15 p.m. he left."),
        ("At 3:15 p.m. he left.", "At three fifteen PM he left."),
        ("At 3:15 p.m. he had 1,000 men.", "At 3.15 PM he had 1000 men."),
        ("e.g. the U.S.A. is large.", "E.g. the USA is large."),
        ("It was 50% off, about 2.5 kg.",
         "It was fifty percent off, about two and a half kilograms."),
        ("Mr. Smith met Dr. Jones in 1865.", "Mr Smith met Dr Jones in eighteen sixty five."),
    ],
)  # fmt: skip
def test_recogniser_spellings_that_text_prep_doesnt_undo(source: str, heard: str) -> None:
    passed, comparison = check(PREP(source), heard, prepare_heard=PREP)
    assert passed, comparison.describe()


def test_a_skip_inside_a_number_is_still_caught() -> None:
    passed, comparison = check(
        PREP("In 1965 it rained."), "In nineteen it rained.", prepare_heard=PREP
    )
    assert not passed
    assert "sixty five" in comparison.describe()


@pytest.mark.parametrize(
    ("text", "heard"),
    [
        ("He ran downstairs as I watched her go.", "He ran down stairs watched her go."),
        ("He ran downstairs to a room below.", "He ran down stairs room below."),
        ("He went downstairs if I was there.", "He went down stairs was there."),
        ("I saw the daisy-chain as I fell down.", "I saw the daisychain fell down."),
    ],
)  # fmt: skip
def test_two_skipped_words_next_to_a_joined_word_are_caught(text: str, heard: str) -> None:
    passed, comparison = check(text, heard)
    assert not passed, comparison.describe()


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


RESPELLINGS = ["noo zee-lund", "her-my-oh-nee"]


def test_respelled_names_heard_as_the_name_pass() -> None:
    # The Names step sent the respelling; the recogniser writes the real name.
    text = "They sailed to noo zee-lund and met her-my-oh-nee there."
    heard = "They sailed to New Zealand and met Hermione there."
    assert not check(text, heard)[0]
    passed, comparison = check(text, heard, respellings=RESPELLINGS)
    assert passed, comparison.describe()
    assert comparison.words_lost == 0
    # A sentence that is only a name passes when the name is heard.
    assert check("Her-my-oh-nee!", "Hermione!", respellings=RESPELLINGS)[0]
    # A real skip next to a respelling still counts.
    assert not check(text, "They sailed to New Zealand there.", respellings=RESPELLINGS)[0]


@pytest.mark.parametrize(
    ("text", "heard"),
    [
        ("They sailed far away and met her-my-oh-nee there.",
         "They sailed far away and met there."),
        ("She smiled. “Her-my-oh-nee!” He turned around.", "She smiled. He turned around."),
        ("They sailed to noo zee-lund and met her there.", "They sailed and met her there."),
    ],
)  # fmt: skip
def test_a_skipped_respelled_name_is_caught(text: str, heard: str) -> None:
    passed, comparison = check(text, heard, respellings=RESPELLINGS)
    assert not passed, comparison.describe()


def test_single_words_add_up() -> None:
    text = "one two three four five six seven eight nine ten eleven twelve"
    heard = "one three four six seven nine ten eleven twelve"
    comparison = compare(text, heard)
    assert comparison.largest_gap == 1
    assert comparison.words_lost == 3
    assert SkipRule().skipped(comparison)
    assert not SkipRule(min_total=4).skipped(comparison)
    assert SkipRule(min_total=4).skipped(comparison, already_lost=1)


def test_nothing_heard_loses_everything() -> None:
    passed, comparison = check("Down, down, down.", "")
    assert not passed
    assert comparison.nothing_heard
    assert comparison.words_lost == 3


@pytest.mark.parametrize(
    "text",
    [
        "Он сказал, что придёт.",
        "他说他会来。",
        "Είπε ότι θα έρθει.",
        "उसने कहा कि वह आएगा।",
    ],
)  # fmt: skip
def test_text_in_another_script_is_refused_not_passed(text: str) -> None:
    assert uncheckable(text)
    with pytest.raises(UncheckableTextError):
        check(text, "")
    with pytest.raises(UncheckableTextError):
        check(text, text)


def test_one_word_in_another_script_must_still_be_heard_as_something() -> None:
    text = "He wrote λόγος on the board."
    assert uncheckable(text) == ""
    assert check(text, "He wrote logos on the board.")[0]
    comparison = compare(text, "He wrote on the board.")
    assert comparison.words_lost == 1


# --- Retry policy ------------------------------------------------------------------------


class FakeVoice:
    """Pretends to synthesize: remembers each (text, seed) and drops words as told.

    ``say(text, call_number)`` returns what was "said"; the default says the text, minus
    ``drop[call_number]``, and skips three words mid-text when it is longer than
    ``drop_if_longer``.
    """

    def __init__(
        self,
        drop: dict[int, str] | None = None,
        drop_if_longer: int = 10_000,
        say: Callable[[str, int], str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, int]] = []
        self.drop = drop or {}
        self.drop_if_longer = drop_if_longer
        self.say = say
        self.spoken: dict[Path, str] = {}

    def __call__(self, text: str, seed: int) -> Path:
        number = len(self.calls)
        self.calls.append((text, seed))
        if self.say is not None:
            said = self.say(text, number)
        else:
            said = text
            if number in self.drop:
                said = said.replace(self.drop[number], " ")
            if len(text) > self.drop_if_longer:
                words = said.split()
                said = " ".join(words[:2] + words[5:])  # Ming-style: three words gone
        wav = Path(f"/fake/{number}.wav")
        self.spoken[wav] = said
        return wav

    @property
    def seeds(self) -> list[int]:
        return [seed for _, seed in self.calls]


class FakeEars:
    """A recogniser that hears what the fake voice said, optionally in its own spelling."""

    def __init__(self, voice: FakeVoice, spelling: Callable[[str], str] | None = None) -> None:
        self.voice = voice
        self.spelling = spelling

    def transcribe(self, wav: Path) -> str:
        said = self.voice.spoken[wav]
        return self.spelling(said) if self.spelling else said


def listener(voice: FakeVoice) -> Listener:
    return Listener(FakeEars(voice))


ID = "c001-p0001-s001-k00"
TEXT = (
    "Either the well was very deep, or she fell very slowly, for she had plenty of time as "
    "she went down to look about her and to wonder what was going to happen next."
)


def test_clean_audio_is_voiced_once() -> None:
    voice = FakeVoice()
    result = speak_checked(ID, TEXT, voice, listener(voice), seed=42)
    assert result.attempts == 1
    assert [p.text for p in result.pieces] == [TEXT]
    assert result.pieces[0].seed == 42
    assert not result.split
    assert result.fallback is None


def test_a_skip_is_regenerated_with_a_new_seed() -> None:
    voice = FakeVoice(drop={0: "to look about her and"})
    result = speak_checked(ID, TEXT, voice, listener(voice), seed=42)
    assert result.attempts == 2
    assert voice.calls[0] == (TEXT, 42)
    assert voice.calls[1][0] == TEXT
    assert voice.calls[1][1] != 42
    assert [p.text for p in result.pieces] == [TEXT]


def test_a_dropped_one_word_sentence_is_regenerated() -> None:
    text = "Stop! He left the room at once."
    voice = FakeVoice(drop={0: "Stop!"})
    result = speak_checked(ID, text, voice, listener(voice), seed=42)
    assert result.attempts == 2
    assert [p.text for p in result.pieces] == [text]


def test_a_silent_one_word_chunk_fails() -> None:
    voice = FakeVoice(say=lambda text, number: "")
    with pytest.raises(WordsSkippedError) as raised:
        speak_checked(ID, "Yes.", voice, listener(voice), seed=42)
    assert raised.value.tries == 2  # the chunk and one reseed; one word can't be split
    assert "nothing was heard" in str(raised.value)


def test_a_skip_that_depends_on_the_text_is_split_and_every_word_survives() -> None:
    voice = FakeVoice(drop_if_longer=80)  # every long piece skips, like Ming's text-bound skips
    result = speak_checked(ID, TEXT, voice, listener(voice), seed=42)
    assert result.split
    assert all(len(p.text) <= 80 for p in result.pieces)
    # No text lost: the pieces are the chunk, in order.
    assert " ".join(p.text for p in result.pieces).split() == TEXT.split()
    # Every piece that was kept was checked and passed.
    assert all(p.comparison is not None and p.comparison.words_lost == 0 for p in result.pieces)
    # Two tries of the whole chunk, then one per piece.
    assert result.attempts == 2 + len(result.pieces)


def test_a_piece_that_keeps_failing_is_split_again() -> None:
    voice = FakeVoice(drop_if_longer=40)
    result = speak_checked(ID, TEXT, voice, listener(voice), seed=1)
    assert all(len(p.text) <= 40 for p in result.pieces)
    assert " ".join(p.text for p in result.pieces).split() == TEXT.split()


def drop_one_word_each(text: str, number: int) -> str:
    """Three separated single words gone from long text; one word gone from short pieces."""
    words = text.split()
    if len(text) > 40:
        return " ".join(w for i, w in enumerate(words) if i not in (3, 9, 15))
    return " ".join(words[:1] + words[2:])


def test_words_lost_add_up_across_the_pieces_of_a_chunk() -> None:
    # Each 40-character piece loses one word and would pass on its own, but the chunk as a
    # whole may not lose more than it could unsplit (3 words fail it).
    voice = FakeVoice(say=drop_one_word_each)
    with pytest.raises(WordsSkippedError):
        speak_checked(ID, TEXT, voice, listener(voice), seed=42)


def test_pieces_may_lose_single_words_while_the_chunk_stays_under_the_limit() -> None:
    def say(text: str, number: int) -> str:
        if len(text) > 80:
            return drop_one_word_each(text, number)
        return text.replace("Either ", "")  # only the first piece loses a word

    voice = FakeVoice(say=say)
    result = speak_checked(ID, TEXT, voice, listener(voice), seed=42)
    assert result.split
    assert result.words_lost == 1


def test_retry_seeds_never_repeat() -> None:
    tried: set[str] = set()

    def say(text: str, number: int) -> str:
        first_try = text not in tried
        tried.add(text)
        if len(text) > 40 or first_try:  # long text skips, and every piece fails once
            words = text.split()
            return " ".join(words[:1] + words[3:])
        return text

    voice = FakeVoice(say=say)
    result = speak_checked(ID, TEXT, voice, listener(voice), seed=42)
    assert result.split
    assert len(voice.seeds) > len(result.pieces) * 2  # pieces were reseeded
    assert len(set(voice.seeds)) == len(voice.seeds)


def test_seeds_stay_in_a_signed_32_bit_range() -> None:
    voice = FakeVoice(drop_if_longer=40)
    speak_checked(ID, TEXT, voice, listener(voice), seed=2**31 - 2)
    assert all(0 <= seed < 2**31 - 1 for seed in voice.seeds)
    assert len(set(voice.seeds)) == len(voice.seeds)


@pytest.mark.parametrize(
    "plan",
    [
        {"reseeds": -1},
        {"split_sizes": (40, 80)},
        {"split_sizes": (80, 0)},
        {"seed_step": 0},
    ],
)
def test_retry_plan_rejects_settings_that_make_no_sense(plan: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"\w"):
        RetryPlan(**plan)  # type: ignore[arg-type]


def test_it_fails_loudly_rather_than_keep_audio_with_missing_words() -> None:
    voice = FakeVoice(drop_if_longer=0)  # never speaks three of the words, whatever happens
    with pytest.raises(WordsSkippedError) as raised:
        speak_checked(
            "c002-p0010-s003-k01", TEXT, voice, listener(voice), seed=42,
            plan=RetryPlan(reseeds=1, split_sizes=(80,)),
        )  # fmt: skip
    error = raised.value
    assert isinstance(error, MissingAudioError)  # handled like any chunk without usable audio
    assert error.chunk_id == "c002-p0010-s003-k01"
    assert error.chunk_text == TEXT
    assert error.text == "Either the well was very deep, or she fell very slowly,"
    assert isinstance(error.comparison, Comparison)
    assert error.comparison.words_lost >= 2
    # Tries of the failing words: twice as part of the chunk, twice as the piece itself.
    assert error.tries == 4
    message = str(error)
    assert message.startswith("no usable audio for c002-p0010-s003-k01 ")
    assert repr(error.text) in message  # the text
    assert '"well was very" (heard nothing)' in message  # the missing words
    assert "in all 4 tries" in message


def test_no_retries_means_one_try_then_an_error() -> None:
    voice = FakeVoice(drop={0: "to look about her and"})
    with pytest.raises(WordsSkippedError):
        speak_checked(
            ID, TEXT, voice, listener(voice), seed=42, plan=RetryPlan(reseeds=0, split_sizes=())
        )
    assert len(voice.calls) == 1


def test_text_that_cant_be_checked_is_refused_before_voicing() -> None:
    voice = FakeVoice()
    with pytest.raises(UncheckableTextError):
        speak_checked(ID, "他说他会来。", voice, listener(voice), seed=1)
    assert voice.calls == []


# --- When nothing fixes it --------------------------------------------------------------


def test_an_unfixable_chunk_can_be_voiced_with_another_voice() -> None:
    ming = FakeVoice(drop_if_longer=0)  # always skips
    kokoro_calls: list[str] = []

    def kokoro(text: str) -> Path:
        kokoro_calls.append(text)
        return Path("/fake/kokoro.wav")

    result = speak_checked(
        ID, TEXT, ming, listener(ming), seed=42,
        plan=RetryPlan(split_sizes=(80,)), on_unfixable=UseOtherVoice(kokoro, voice="Kokoro"),
    )  # fmt: skip
    assert kokoro_calls == [TEXT]  # the whole chunk, in one voice
    assert result.pieces == [SpokenPiece(TEXT, Path("/fake/kokoro.wav"), None, None, "Kokoro")]
    assert isinstance(result.fallback, WordsSkippedError)  # so the caller can tell the user


def test_the_other_voice_can_be_checked_too() -> None:
    ming = FakeVoice(drop_if_longer=0)
    kokoro = FakeVoice()
    other = UseOtherVoice(lambda text: kokoro(text, 0), voice="Kokoro", listener=listener(kokoro))
    result = speak_checked(
        ID, TEXT, ming, listener(ming), seed=42, plan=RetryPlan(split_sizes=()), on_unfixable=other
    )
    (piece,) = result.pieces
    assert piece.comparison is not None and piece.comparison.words_lost == 0
    bad = FakeVoice(drop_if_longer=0)
    failing = UseOtherVoice(lambda text: bad(text, 0), voice="Kokoro", listener=listener(bad))
    with pytest.raises(WordsSkippedError):
        speak_checked(
            ID, TEXT, ming, listener(ming), seed=42, plan=RetryPlan(split_sizes=()),
            on_unfixable=failing,
        )  # fmt: skip


# --- The Listener's settings reach the check ----------------------------------------------


def test_the_listener_uses_its_text_prep() -> None:
    source = "It cost $5.50, No. 5 on the list."
    voice = FakeVoice()
    ears = FakeEars(voice, spelling=lambda said: source)  # writes digits, as recognisers do
    result = speak_checked(ID, PREP(source), voice, Listener(ears, prepare_heard=PREP), seed=1)
    assert result.attempts == 1
    voice = FakeVoice()
    ears = FakeEars(voice, spelling=lambda said: source)
    with pytest.raises(WordsSkippedError):
        speak_checked(ID, PREP(source), voice, Listener(ears), seed=1)


def test_the_listener_uses_its_respellings() -> None:
    text = "They met her-my-oh-nee by the river."
    voice = FakeVoice()
    ears = FakeEars(voice, spelling=lambda said: "They met Hermione by the river.")
    result = speak_checked(ID, text, voice, Listener(ears, respellings=("her-my-oh-nee",)), seed=1)
    assert result.attempts == 1
    voice = FakeVoice()
    ears = FakeEars(voice, spelling=lambda said: "They met Hermione by the river.")
    with pytest.raises(WordsSkippedError):
        speak_checked(ID, text, voice, Listener(ears), seed=1)


def test_the_listener_uses_its_rule() -> None:
    lenient = SkipRule(min_gap=99, min_total=99, min_unclear=99, whole_sentences=False)
    voice = FakeVoice(drop={0: "to look about her and"})
    result = speak_checked(ID, TEXT, voice, Listener(FakeEars(voice), rule=lenient), seed=1)
    assert result.attempts == 1  # the lenient rule let the skip through
    voice = FakeVoice(drop={0: "to look about her and"})
    assert speak_checked(ID, TEXT, voice, listener(voice), seed=1).attempts == 2
