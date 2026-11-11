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
    text = "a".join(APOSTROPHE_LIKE)
    assert speakable(text) == "a".join("'" * len(APOSTROPHE_LIKE))
    assert len(speakable(text)) == len(text)  # a mark for a mark: nothing is lost


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
