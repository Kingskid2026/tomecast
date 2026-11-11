"""Apostrophe-like marks eSpeak reads out by name become plain apostrophes (tomecast.marks).

Which marks eSpeak NG 1.52 reads out by name was measured with its own phoneme output, no
audio (see the module's docstring); these tests check the mapping itself, without eSpeak.
"""

import pytest

from tomecast.marks import APOSTROPHE_LIKE, speakable
from tomecast.pronunciations import Pronunciation, Pronunciations, respelling
from tomecast.textprep import TextPrep


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Hawaiʼi", "Hawai'i"),  # modifier letter apostrophe
        ("donʼt", "don't"),  # some ebooks use it as the apostrophe itself
        ("Kʼicheʼ", "K'iche'"),  # ejectives
        ("Qurʾan", "Qur'an"),  # right half ring: hamza
        ("ʿAli", "'Ali"),  # left half ring: ayin
        ("Ilʹich", "Il'ich"),  # prime: the Russian soft sign
        ("obʺyom", "ob'yom"),  # double prime: the hard sign
        ("ʽo", "'o"),  # reversed comma
        ("naˮki", "na'ki"),  # double apostrophe
        ("Hawaiꞌi", "Hawai'i"),  # small saltillo
        ("ꞋA", "'A"),  # capital saltillo
    ],
)
def test_apostrophe_like_marks_become_apostrophes(written: str, spoken: str) -> None:
    assert speakable(written) == spoken


def test_every_listed_mark_is_mapped_one_for_one() -> None:
    text = "a" + "a".join(APOSTROPHE_LIKE) + "a"
    assert speakable(text) == "a" + "a".join("'" * len(APOSTROPHE_LIKE)) + "a"
    assert len(speakable(text)) == len(text)  # a mark for a mark: nothing is lost
    assert speakable("Ka\u02bc\u02bfa") == "Ka''a"  # a run of marks touching letters


@pytest.mark.parametrize(
    "text",
    [
        "\u02be",  # a transliteration table's row: the sign on its own...
        "\u02be \u02bf",
        "\u02be\u02bf",  # ...or a run of them
        "The sign \u02bc. Next one.",
        "Write \u02bf for ayin and \u02be for hamza.",
        "(\u02b9)",
        "5\u02b910\u02ba",  # digits aren't letters: primes for feet and inches stay
        "A B A\u02b9",  # a lone capital before it: a label ("A prime"), not a word
        "Part D\u02bc",
    ],
)
def test_marks_not_touching_a_word_are_left_as_they_are(text: str) -> None:
    """An apostrophe says nothing: a lone mark would turn into silence, or a scene break."""
    assert speakable(text) == text


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Siobhan\u02bcs", "Siobhan's"),  # at the edge of a word
        ("\u02bfAli", "'Ali"),
        ("I\u02bcm", "I'm"),  # a lone capital with a letter after it is a word
        ("cup o\u02bc tea", "cup o' tea"),  # a lone small letter: dialect, not a label
        ("goin\u02bc home", "goin' home"),
    ],
)
def test_marks_at_the_edge_of_a_word_become_apostrophes(written: str, spoken: str) -> None:
    assert speakable(written) == spoken


def test_no_text_with_something_to_say_is_left_with_nothing() -> None:
    """Every mark, alone or next to letters, digits, spaces and punctuation in every order:
    whatever had a letter or digit still has one, and the length never changes."""
    pieces = ["", " ", "a", "A", "5", ".", "(", *APOSTROPHE_LIKE]
    for mark in APOSTROPHE_LIKE:
        for before in pieces:
            for after in pieces:
                text = before + mark + after
                result = speakable(text)
                assert len(result) == len(text)
                assert any(c.isalnum() for c in result), repr(text)


@pytest.mark.parametrize(
    "text",
    [
        "Hawaiʻi",  # the okina reads as intended, so it stays
        "Hawai'i and Hawai‘i and Hawai’i",  # plain and curly apostrophes
        "Zoë, Ñuñoa, Bjørn, Łódź, Þór, Æðelstan, Nguyễn",
        "the angle θ is small",  # Greek in a book is read as Greek letter names, rightly
        "“Quoted,” she said — and left… ½ § 3",
        "ʃɪˈvɔːn",  # IPA in a book is not this module's business
        "",
    ],
)
def test_everything_else_is_left_as_it_is(text: str) -> None:
    assert speakable(text) == text


def test_respellings_are_made_speakable() -> None:
    assert respelling("ha-WAI-ʼee") == "ha-wai-'ee"
    fixes = Pronunciations((Pronunciation("Zorp", "ZAWRʹp"),))
    assert fixes.apply("Zorp ran.") == "zawr'p ran."


@pytest.mark.parametrize("language", ["en", "fr", None])
def test_text_prep_makes_book_text_speakable_in_every_language(language: str | None) -> None:
    prepared = TextPrep.for_language(language).body("ʿAli read the Qurʾan in Kazanʹ.")
    assert prepared == "'Ali read the Qur'an in Kazan'."


def test_a_fix_still_matches_the_word_as_the_book_spells_it() -> None:
    fixes = Pronunciations((Pronunciation("Qurʾan", "kur-AHN"),))
    prepared = TextPrep.for_language("en", fixes).body("The Qurʾan and Ilʹich.")
    assert prepared == "The kur-ahn and Il'ich."
