import re
import time
from itertools import pairwise

import pytest

from tomecast import textprep_en
from tomecast.book import Chapter
from tomecast.chunking import split_sentences
from tomecast.pronunciations import Pronunciation, Pronunciations
from tomecast.textprep import PassthroughRules, TextPrep, rules_for, words_lost
from tomecast.textprep_en import (
    EnglishRules,
    cardinal,
    digit_words,
    ordinal,
    roman_value,
    spoken_number,
    year,
)

english = EnglishRules()


def say(text: str) -> str:
    return english.body(text)


# --- number words -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (0, "zero"),
        (7, "seven"),
        (13, "thirteen"),
        (20, "twenty"),
        (21, "twenty-one"),
        (99, "ninety-nine"),
        (100, "one hundred"),
        (101, "one hundred one"),
        (342, "three hundred forty-two"),
        (1000, "one thousand"),
        (1001, "one thousand one"),
        (12_345, "twelve thousand three hundred forty-five"),
        (1_000_000, "one million"),
        (2_500_000_003, "two billion five hundred million three"),
        (999_999_999_999_999, "nine hundred ninety-nine trillion nine hundred ninety-nine "
         "billion nine hundred ninety-nine million nine hundred ninety-nine thousand "
         "nine hundred ninety-nine"),
        (-5, "minus five"),
    ],
)  # fmt: skip
def test_cardinal(number: int, words: str) -> None:
    assert cardinal(number) == words


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "fifth"),
        (8, "eighth"), (9, "ninth"), (11, "eleventh"), (12, "twelfth"), (20, "twentieth"),
        (21, "twenty-first"), (22, "twenty-second"), (100, "one hundredth"),
        (101, "one hundred first"), (1000, "one thousandth"), (0, "zeroth"),
    ],
)  # fmt: skip
def test_ordinal(number: int, words: str) -> None:
    assert ordinal(number) == words


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (1865, "eighteen sixty-five"),
        (1905, "nineteen oh five"),
        (1900, "nineteen hundred"),
        (1776, "seventeen seventy-six"),
        (1066, "ten sixty-six"),
        (1000, "one thousand"),
        (2000, "two thousand"),
        (2005, "two thousand five"),
        (2010, "twenty ten"),
        (2024, "twenty twenty-four"),
        (2100, "two thousand one hundred"),
    ],
)
def test_year(number: int, words: str) -> None:
    assert year(number) == words


def test_spoken_number_keeps_leading_zeros_and_decimals() -> None:
    assert spoken_number("007") == "zero zero seven"
    assert spoken_number("3", "14") == "three point one four"
    assert spoken_number("1,250", "5") == "one thousand two hundred fifty point five"
    assert (
        spoken_number("1" + "0" * 15)
        == "one zero zero zero zero zero zero zero zero zero zero zero zero zero zero zero"
    )
    assert digit_words("305") == "three zero five"


def test_roman_values() -> None:
    assert [roman_value(n) for n in ("I", "IV", "IX", "XIV", "XLII", "MCMXCIV")] == [
        1, 4, 9, 14, 42, 1994,
    ]  # fmt: skip


# --- numbers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("I have 3 cats.", "I have three cats."),
        ("There were 1,250 apples.", "There were one thousand two hundred fifty apples."),
        ("It is 3.14 long.", "It is three point one four long."),
        ("Room 007.", "Room zero zero seven."),
        ("It weighs 5kg.", "It weighs five kg."),
        ("The temperature fell to -5 degrees.", "The temperature fell to minus five degrees."),
        ("Chapter 3.", "Chapter three."),
        ("1,2,3", "one,two,three"),
        ("100,000,000 people", "one hundred million people"),
        ("mp3 and R2-D2", "mp3 and R2-D2"),
        ("catch-22", "catch-twenty-two"),
        ("A 12345678901234567890 id", "A one two three four five six seven eight nine zero one "
         "two three four five six seven eight nine zero id"),
    ],
)  # fmt: skip
def test_cardinal_numbers(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("The 1st, 2nd, 3rd and 4th.", "The first, second, third and fourth."),
        ("The 21st of 100.", "The twenty-first of one hundred."),
        ("the 1,000th customer", "the one thousandth customer"),
        ("the 22ND floor", "the twenty-second floor"),
    ],
)
def test_ordinal_numbers(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("1/2 cup", "one half cup"),
        ("3/4 mile", "three quarters mile"),
        ("2/3 full", "two thirds full"),
        ("1/4 of it", "one quarter of it"),
        ("5/8 inch", "five eighths inch"),
        ("1 1/2 hours", "one and a half hours"),
        ("2 3/4 hours", "two and three quarters hours"),
        ("½ and 2½", "one half and two and a half"),
        ("on 12/25", "on December twenty-fifth"),
        ("on 12/25/2020.", "on December twenty-fifth, twenty twenty."),
        ("5/12 was", "May twelfth was"),
        ("24/7 and 13/45", "twenty-four slash seven and thirteen slash forty-five"),
        ("1/2/3", "one slash two slash three"),
    ],
)
def test_fractions(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("pages 5-7", "pages five to seven"),
        ("ages 10–12", "ages ten to twelve"),
        ("score 3-1", "score three-one"),
        ("1,000-2,000 men", "one thousand to two thousand men"),
        ("Call 555-1234 now", "Call five five five, one two three four now"),
        ("Call (555) 123-4567.", "Call five five five, one two three, four five six seven."),
    ],
)
def test_ranges_and_phone_numbers(written: str, spoken: str) -> None:
    assert say(written) == spoken


# --- years, dates and times ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("In 1865 the war ended.", "In eighteen sixty-five the war ended."),
        ("Since 1998 it grew.", "Since nineteen ninety-eight it grew."),
        ("It was built in 1905.", "It was built in nineteen oh five."),
        ("The War of 1812.", "The War of eighteen twelve."),
        ("He died in 44 BC.", "He died in forty-four BC."),
        ("In 1066 AD, they came.", "In ten sixty-six AD, they came."),
        ("From 1914 to 1918.", "From nineteen fourteen to nineteen eighteen."),
        (
            "Between 1914 and 1918 it raged.",
            "Between nineteen fourteen and nineteen eighteen it raged.",
        ),
        ("The war of 1914-1918.", "The war of nineteen fourteen to nineteen eighteen."),
        ("1914-18 was long", "nineteen fourteen to eighteen was long"),
        ("1900-05", "nineteen hundred to oh five"),
        # Not years: quantities.
        ("In 1500 men marched.", "In one thousand five hundred men marched."),
        ("There were 1865 cats.", "There were one thousand eight hundred sixty-five cats."),
        ("The 1960s and the '60s.", "The nineteen sixties and the sixties."),
        ("In the 1900s and 2000s.", "In the nineteen hundreds and two thousands."),
        ("the 1880s", "the eighteen eighties"),
    ],
)  # fmt: skip
def test_years(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("4 July 1776", "the fourth of July, seventeen seventy-six"),
        ("on 4th July 1776.", "on the fourth of July, seventeen seventy-six."),
        ("3 March", "the third of March"),
        ("July 4, 1776", "July fourth, seventeen seventy-six"),
        ("July 4", "July fourth"),
        ("July 4th, 1776, was", "July fourth, seventeen seventy-six, was"),
        ("Sept. 11, 2001", "September eleventh, two thousand one"),
        ("Dec 25", "December twenty-fifth"),
        ("May 2005", "May two thousand five"),
        ("March 1999", "March nineteen ninety-nine"),
        ("2024-03-05", "March fifth, twenty twenty-four"),
        ("2024-13-05", "two thousand twenty-four-thirteen-zero five"),
        ("It was May 32 then", "It was May thirty-two then"),
        ("I may 5 times", "I may five times"),
    ],
)
def test_dates(written: str, spoken: str) -> None:
    assert say(written) == spoken


def _no_word_doubled_or_lost(written: str, prepared: str) -> None:
    words = prepared.lower().split()
    assert [w for w, after in pairwise(words) if w == after] == []
    assert words_lost(written, prepared, english.consumed_words) == []


# Each of these once came out with a word doubled ("the the twenty-first of May") or added.
ORDINAL_DATES = [
    ("the 21st of May", "the twenty-first of May"),
    ("It happened on the 21st of May.", "It happened on the twenty-first of May."),
    ("May 21st", "May twenty-first"),
    ("21st May", "the twenty-first of May"),
    ("on 21st May", "on the twenty-first of May"),
    ("on the 21st May", "on the twenty-first of May"),
    ("the 21 May", "the twenty-first of May"),
    ("the 21 of May", "the twenty-first of May"),
    ("the 1st", "the first"),
    ("He came on the 1st.", "He came on the first."),
    ("the 2nd and 3rd", "the second and third"),
    ("the 2nd and 3rd of May", "the second and third of May"),
    ("The 2nd and 3rd of May were warm.", "The second and third of May were warm."),
    ("from the 1st to the 3rd of May", "from the first to the third of May"),
    ("the 22nd of June, 1815", "the twenty-second of June, eighteen fifteen"),
    (
        "On the 22nd of June, 1815, the battle ended.",
        "On the twenty-second of June, eighteen fifteen, the battle ended.",
    ),
    ("the 4 July 1776", "the fourth of July, seventeen seventy-six"),
    ("the 21st of Mar. 1815", "the twenty-first of March, eighteen fifteen"),
    ("21st-century", "twenty-first-century"),
    ("a 21st-century city", "a twenty-first-century city"),
    ("The 21st of May was warm.", "The twenty-first of May was warm."),
    ("The 21st was a Tuesday.", "The twenty-first was a Tuesday."),
    ("The 21st May 1815", "The twenty-first of May, eighteen fifteen"),
    ("THE 21st of May", "THE twenty-first of May"),
    ("the\n21st of May", "the\ntwenty-first of May"),
    ("Tuesday the 21st of May", "Tuesday the twenty-first of May"),
    ("on the 21st, the 22nd of May.", "on the twenty-first, the twenty-second of May."),
    ("He left. The 21 May was warm.", "He left. The twenty-first of May was warm."),
    # With "of" written, the date gets its "the" too, unless a determiner is already there.
    ("Every 4th of July we met.", "Every fourth of July we met."),
    ("On 21st of May he left.", "On the twenty-first of May he left."),
    ("He was born on 21st of May 1850.", "He was born on the twenty-first of May, eighteen fifty."),
    ("closed from 24th of December until 2nd of January",
     "closed from the twenty-fourth of December until the second of January"),
    ("Monday, 21st of May.", "Monday, the twenty-first of May."),
    ("By 4th of July the war was over.", "By the fourth of July the war was over."),
    # A paragraph starting with an ordinal.
    ("21st of May was warm.", "the twenty-first of May was warm."),
    ("21st May was warm.", "the twenty-first of May was warm."),
    ("1st of all, listen.", "first of all, listen."),
]  # fmt: skip


@pytest.mark.parametrize(("written", "spoken"), ORDINAL_DATES)
def test_ordinal_dates_say_every_word_once(written: str, spoken: str) -> None:
    prepared = TextPrep.for_language("en").body(written)
    assert prepared == spoken
    _no_word_doubled_or_lost(written, prepared)


# Words that can only be a determiner right before a date: "21 May" gets no "the" of its own.
@pytest.mark.parametrize(
    "word",
    ["the", "every", "each", "this", "next", "last", "same", "my", "your", "his", "our",
     "their", "its", "This", "EVERY"],
)  # fmt: skip
def test_a_determiner_is_the_dates_article(word: str) -> None:
    written = f"We met {word} 4 July."
    prepared = TextPrep.for_language("en").body(written)
    assert prepared == f"We met {word} fourth of July."
    _no_word_doubled_or_lost(written, prepared)


# Words that are often something else before a date: it gets its "the", as on main.
@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("He said that 21 May was fine.", "He said that the twenty-first of May was fine."),
        ("We hoped that 4 July 1776 would be remembered.",
         "We hoped that the fourth of July, seventeen seventy-six would be remembered."),
        ("Is it true that 3rd May is a holiday?",
         "Is it true that the third of May is a holiday?"),
        ("I told her 21 May was fine.", "I told her the twenty-first of May was fine."),
        ("They met her 3rd May 1815 in Paris.",
         "They met her the third of May, eighteen fifteen in Paris."),
        ("At last 4 July came.", "At last the fourth of July came."),
        ("At long last 4 July came.", "At long last the fourth of July came."),
        ("any 4 July", "any the fourth of July"),
        ("bathe 4 July", "bathe the fourth of July"),  # "the" inside a word doesn't count
    ],
)  # fmt: skip
def test_a_word_that_may_not_be_a_determiner_keeps_the_dates_article(
    written: str, spoken: str
) -> None:
    prepared = TextPrep.for_language("en").body(written)
    assert prepared == spoken
    _no_word_doubled_or_lost(written, prepared)


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("the 2nd and 3rd May", "the second and third of May"),
        ("the 2nd, 3rd and 4th May", "the second, third and fourth of May"),
        ("the 2nd, 3rd May", "the second, third of May"),
        ("the 2nd or 3rd May", "the second or third of May"),
        ("the 2 and 3 May", "the second and third of May"),
        ("2, 3 and 4 May", "the second, third and fourth of May"),
        ("the 3 & 4 May", "the third and fourth of May"),
        ("the 3-4 May", "the third to fourth of May"),
        ("the 3rd-4th May", "the third to fourth of May"),
        ("3\u20134 May 1815", "the third to fourth of May, eighteen fifteen"),
        ("from the 3rd to 4th May", "from the third to the fourth of May"),
        ("It was over. 2 and 3 May were warm.",
         "It was over. The second and third of May were warm."),
        # Not lists of days: only the last number is part of the date, as on main.
        ("of the 12, 4 May have gone", "of the twelve, the fourth of May have gone"),
        ("the 2nd, 3 May", "the second, the third of May"),
        ("1, 2nd and 3rd May", "one, second and the third of May"),
        ("the 4-3 May", "the four-the third of May"),
        ("pages 40-41 May", "pages forty to forty-one May"),
    ],
)  # fmt: skip
def test_days_listed_before_a_month_are_all_ordinals(written: str, spoken: str) -> None:
    prepared = TextPrep.for_language("en").body(written)
    assert prepared == spoken
    _no_word_doubled_or_lost(written, prepared)


def _without_sentence_starts(text: str) -> str:
    """``say(text)`` with sentence starts left unmarked: main's behaviour for them."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(textprep_en, "_mark_sentence_starts", lambda text: text)
        return say(text)


# A number that starts a sentence after a clear sentence end ("!", "?", or a period after an
# ordinary lowercase word) is said with a capital letter: the chunker only ends a sentence
# before a word that doesn't start in lowercase. The sentence count stays what it is for the
# text as written.
SENTENCE_STARTS = [
    ("He left. 21st May was warm.", "He left. The twenty-first of May was warm."),
    ("He left. 21st of May was warm.", "He left. The twenty-first of May was warm."),
    ("He left. The 21st of May was warm.", "He left. The twenty-first of May was warm."),
    ("He left. 2 and 3 May were warm.", "He left. The second and third of May were warm."),
    ("Stop! 1st of all, listen.", "Stop! First of all, listen."),
    ("Why? 21st-century life.", "Why? Twenty-first-century life."),
    ("Stop!! 5 men came.", "Stop!! Five men came."),
    ("Who?! 5 men came.", "Who?! Five men came."),
    ('He left. "21st May," she said.', 'He left. "The twenty-first of May," she said.'),
    ('"I left." 5 men came.', '"I left." Five men came.'),
    ("(He left.) 5 men came.", "(He left.) Five men came."),
    ("He left.\n5 men came.", "He left.\nFive men came."),
    ("He left. 5 men came.", "He left. Five men came."),
    ("The bird began to fly. 5 more followed.", "The bird began to fly. Five more followed."),
    ("The day was long. 5 men left.", "The day was long. Five men left."),
    ("It was over. $5 was lost.", "It was over. Five dollars was lost."),
    ("It was done. 50% of it.", "It was done. Fifty percent of it."),
    ("It was fine. 1/2 of it.", "It was fine. One half of it."),
    ("He left. 1960s music played.", "He left. Nineteen sixties music played."),
    ("He left. \u201970s music played.", "He left. Seventies music played."),
    ("It ended. 5-7 men came.", "It ended. Five to seven men came."),
    ("It was done. 3:45 p.m. came.", "It was done. Three forty-five pee em came."),
    ("He left. -5 degrees.", "He left. Minus five degrees."),
    ("He left. \u22125 degrees.", "He left. Minus five degrees."),
    ("He left. ~5 men came.", "He left. ~Five men came."),
    ("He left. No. 5 was next.", "He left. Number five was next."),
]  # fmt: skip


@pytest.mark.parametrize(("written", "spoken"), SENTENCE_STARTS)
def test_a_number_starting_a_sentence_keeps_the_sentence_apart(written: str, spoken: str) -> None:
    prepared = say(written)
    assert prepared == spoken
    assert len(split_sentences(prepared)) == len(split_sentences(written))
    _no_word_doubled_or_lost(written, prepared)


def test_a_spelled_out_abbreviation_starting_a_sentence_keeps_it_apart() -> None:
    # As written, the chunker also splits after "A.D."; once it is words, it rightly doesn't.
    prepared = say("He left. A.D. 476 was the year.")
    assert prepared == "He left. Ay dee four hundred seventy-six was the year."
    assert len(split_sentences(prepared)) == 2


# Every input from both reviews of #67, with main's sentence count. None of them has a clear
# sentence end before its number, so nothing is capitalised and the count stays main's.
MAINS_SENTENCES = [
    ("On Fri. 5 May 1865 he left for London.", 1),
    ("Tues. 21st May. Rain all day.", 2),
    ("The U.S. $5 billion plan failed.", 1),
    ("The U.S. 1st Army and the U.S. 3rd Army met.", 1),
    ("It weighed 5 lbs. 3 oz. at birth.", 1),
    ("It weighed 12 lb. 6 oz. and cost 10s. 6d. in all.", 1),
    ("The trip took 3 hrs. 20 min. by rail.", 1),
    ("He lived in Washington, D.C. 5 years.", 1),
    ("Born ca. 1850 in Washington, D.C. 3 miles away.", 1),
    ("It was B.C. 50% of the time.", 1),
    ("It was B.C. 3 years ago.", 1),
    ("William I (r. 1066-87) and Bede (d. 735) and Smith (b. 1850, d. 1900).", 1),
    ("See sec. 5 and art. 12 of the treaty.", 1),
    ("He counted... 3... 2... 1... and jumped.", 1),
    ('"Stop!" 5 men cried.', 1),
    ("Abel, 12. 15. Baker, 3. 9.", 2),
    ("Chapter One ........ 1", 1),
    ("He met Maj. Smith. 5 men came.", 1),
    ("At 3 a.m. 5 men came.", 1),
    ("At 3 p.m. $5 was lost.", 1),
    ("He woke at 6 a.m. 20 minutes later he left.", 1),
    ("It fell in 44 B.C. 15 years later he died.", 1),
    ("It fell in 44 B.C. 5 men saw it.", 1),
    ("It fell in 476 A.D. 5 men saw it.", 1),
    ("At 3 a.m. 3:45 p.m. was later.", 1),
    ("He came at 3 p.m. 1960s music played.", 1),
    ("He left at 3 p.m. 5 men came.", 1),
    ("at 10 a.m. 300 soldiers marched.", 1),
    ("It was 3 p.m. 5 minutes later he left.", 1),
    ("At 5 p.m. 21 May was warm.", 1),
    ("He came at 3 p.m. 5 men followed.", 1),
    ("He came at 10 a.m. 5 men followed.", 1),
    ("He came at 3 p.m. $5 was owed.", 1),
    ("It ended at 3 p.m. 1865 was over.", 1),
    ("He left at 11:05 AM. 5 men came.", 1),
    ("Then at 3 a.m. 5 men came.", 1),
    ("And in 44 B.C. 15 legions marched.", 1),
    ("On Monday at 3 a.m. 5 men came.", 1),
    ("Trains left at 9 a.m. 1 p.m. and 5 p.m. daily.", 1),
    ("But by 3 p.m. 20 had died.", 1),
    ("Shortly after 3 a.m. 2 shots rang out.", 1),
    ("That night at 11 p.m. 3 men arrived.", 1),
    ("From 9 a.m. to 5 p.m. 300 people visited.", 1),
    ("Between 9 a.m. and 5 p.m. 300 people visited.", 1),
    ("From 1200 B.C. to 800 B.C. 5 cities fell.", 1),
    ("But at 3 a.m. 5 men came.", 1),
    ("He said at 3 a.m. 5 men came.", 1),
    ("Between 9 a.m. 12 people arrived.", 1),
    ("He wrote in 44 B.C. 15 letters.", 1),
    ("On the 4th of July at 9 p.m. 10,000 fireworks lit the sky.", 1),
    ("Then at 3 p.m. 5 men came.", 1),
    ("On Tuesday at 3 p.m. 20 men came.", 1),
    ("Each day at 8 a.m. 20 buses leave.", 1),
    ("At exactly 3 p.m. 20 people arrived.", 1),
    ("Between 2 and 3 p.m. 40 men arrived.", 1),
    ("In the year 476 A.D. 5 kings ruled.", 1),
    ("The meeting ran from 9 a.m. until 5 p.m. 12 people spoke.", 1),
    ("At Marathon in 490 B.C. 10,000 Athenians defeated the Persians.", 1),
    ("Shortly before 9 p.m. 300 guests had arrived.", 1),
    ("Around 3 p.m. 5 men came.", 1),
    ("After 44 B.C. 5 cities fell.", 1),
    ("At about 3 p.m. 5 men came.", 1),
    ("At 11 AM. 5 men came.", 1),
    ("As Paul says in Rom. 8:28 all things work together.", 1),
    ("Compare Matt. 5 and Isa. 53 for the same thought.", 1),
    ("See Ps. 23 and Ex. 3 for the divine name.", 1),
    ("Read Bk. 3 of the Iliad and Sect. 4 of the Ethics.", 1),
    ("See vv. 3-5 and ll. 10-12 of the poem.", 1),
    ("The 2nd Bn. 300 strong held the ridge.", 1),
    ("He served in the Govt. 5 years.", 1),
    ("In Pt. 2 the plot thickens.", 1),
    ("Beethoven's Sym. 5 opens with four notes.", 1),
    ("See Eq. 4 and Prop. 7 below.", 1),
    ("See Heb. 11:1 for faith.", 1),
    ("See 1 Cor. 13 on love.", 1),
    ("Cf. Luke 2. 14 shows it.", 1),
    ("Turn to Gen. 1 and Rev. 21:4.", 1),
    ("The ship lay at lat. 51 N, long. 3 W.", 1),
    ("It cost Rs. 5 in all.", 1),
    ("He sold 3 qrs. 5 lbs. of wheat.", 1),
    ("It rose 3 deg. 5 min. in an hour.", 1),
    ("Bread, cheese, &c. 5 baskets in all.", 1),
    ("See ch. iii. 5 for this.", 1),
    ("Part II. 3 Men in a Boat", 1),
]


@pytest.mark.parametrize(("written", "sentences"), MAINS_SENTENCES)
def test_no_sentence_is_split_where_main_kept_it_whole(written: str, sentences: int) -> None:
    prepared = say(written)
    assert prepared == _without_sentence_starts(written)
    assert len(split_sentences(prepared)) == sentences
    _no_word_doubled_or_lost(written, prepared)


@pytest.mark.parametrize(
    "word",
    [
        # Not ordinary words: too short, no vowel, capitalised, or a lowercase Roman numeral.
        "in", "go", "vv", "ll", "ff", "lbs", "hrs", "yds", "yrs", "pts", "qrs", "cwt", "mph", "sqq",
        "Rom", "Matt", "Fri", "Smith", "Gone", "U.S", "p.m", "a.m", "iii", "xiv", "mix",
        # The chunker's abbreviations.
        "etc", "approx", "vol", "fig", "dept", "est", "gen", "rev", "prof", "sept", "oct",
        # Lowercase abbreviations that look like words.
        "art", "arts", "sec", "secs", "sect", "sects", "para", "paras", "cap", "caps", "ver",
        "vers", "ref", "refs", "fol", "fols", "ibid", "cit", "loc", "seq", "ser", "suppl",
        "supp", "app", "illus", "esp", "incl", "misc", "viz", "circ", "abt", "inst", "ult",
        "prox", "orig", "repr", "trans", "lit", "abbr", "adj", "adv", "pron", "fem", "masc",
        "neut", "plur", "obs", "def", "min", "mins", "gal", "gals", "doz", "deg", "degs",
        "lat", "bce",
    ],
)  # fmt: skip
def test_a_period_after_an_abbreviation_is_not_a_clear_sentence_end(word: str) -> None:
    written = f"See {word}. 5 of them."
    assert say(written) == _without_sentence_starts(written)
    assert say(written).endswith(" five of them.")


@pytest.mark.parametrize(
    "written",
    [
        "The ship lay at lat. 51 N, long. 3 W.",  # "long." after "lat." is longitude
        "He counted... 3... 2... 1... and jumped.",  # an ellipsis
        "He waited\u2026 5 men came.",
        "Chapter One ........ 1",  # a dot leader
        '"Stop!" 5 men cried.',  # "!" or "?" before a closing quote
        '"Why?" 5 men asked.',
        "He said hello.5 men came.",  # no space after the period
        "He left. it was 5 men.",  # a sentence written in lowercase stays that way
    ],
)
def test_a_number_after_an_unclear_end_stays_lowercase(written: str) -> None:
    assert say(written) == _without_sentence_starts(written)


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("the 2-3-4 May", "the two to three-the fourth of May"),  # as on main
        ("2 and 3-4 May", "two and three-the fourth of May"),  # as on main
    ],
)
def test_a_range_is_two_days(written: str, spoken: str) -> None:
    assert say(written) == spoken


def test_a_paragraph_already_holding_the_marker_character_is_left_alone() -> None:
    # Sentence starts are marked with a private-use character while the rules run. A
    # paragraph that has one of its own is prepared without marks and keeps it.
    assert say("He left. 5 men\ue000 came.") == "He left. five men\ue000 came."
    assert "\ue000" not in say("He left. 5 men came.")


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("He wrote it down on 21 May. \u1f8d\u03b4\u03b7\u03c2 was the word he used at 5 p.m.",
         "He wrote it down on the twenty-first of May. \u1f8d\u03b4\u03b7\u03c2 was the word "
         "he used at five pee em."),
        ("He wrote it down. \u01c5emal came at 5.", "He wrote it down. \u01c5emal came at five."),
    ],
)  # fmt: skip
def test_only_letters_the_rules_wrote_are_capitalised(written: str, spoken: str) -> None:
    # A titlecase letter (Greek with a subscript iota, "Dz") is the book's own: it isn't
    # changed, so the paragraph isn't thrown back to its written form.
    assert TextPrep.for_language("en").body(written) == spoken


# Sentence ends and starts that come up in books, for the property test below.
_ENDS = [
    "He left.", "It was 1865.", "At 3 p.m.", "He left at 3 p.m.", "in 44 B.C.", "On Fri.",
    "The U.S.", "Mr.", "J.", "e.g.", "Stop!", '"Stop!"', "Why?", "He counted...", "12 lb.",
    "See sec.", "Born ca.", "(d.", "Main St.", "etc.", "He met Maj.", "on the 3rd.", "Fig.",
    "As Paul says in Rom.", "See vv.", "The day was long.", "at lat. 51, long.", "see ch. iii.",
    "It was done.", "(He left.)", '"I left."', "",
]  # fmt: skip
_STARTS = [
    "5 men came.", "21 May was warm.", "21st of May was warm.", "the 21st of May came.",
    "The 21 May came.", "$5 was lost.", "50% left.", "-5 degrees.", "1960s music.", "1/2 of it.",
    "3:45 p.m. came.", "No. 5 was next.", "A.D. 476 came.", "2 and 3 May came.",
    "3-4 May came.", "that 21 May came.", "every 4 July came.", "at last 4 July came.",
    '"21st May," she said.', "(5 men) came.", "Smith came.", "it was fine.",
    "1st of all, listen.", "~5 men came.", "\u00bd of it.", "8:28 all things.",
]  # fmt: skip
_CLEAR_END = re.compile(
    r"(?:(?:^|\s)[\"'(\[\u201c\u2018]*[a-z]*[aeiouy][a-z]*\.[\"')\]\u201d\u2019]*|[!?])$"
)


def _sentence_ends(text: str) -> tuple[str, set[int]]:
    """The text as the chunker sees it, and where each sentence but the last ends in it."""
    sentences = split_sentences(text)
    ends, position = set(), 0
    for sentence in sentences[:-1]:
        position += len(sentence) + 1
        ends.add(position - 1)
    return " ".join(sentences), ends


def _corpus() -> list[str]:
    written = [w for w, _ in ORDINAL_DATES + SENTENCE_STARTS] + [w for w, _ in MAINS_SENTENCES]
    written += SAMPLE_PARAGRAPHS
    written += [f"{end} {start}".strip() for end in _ENDS for start in _STARTS]
    return written


def test_sentence_starts_only_change_case_and_only_split_after_a_clear_end() -> None:
    """Over a corpus of sentences, marking sentence starts changes nothing but the case of
    letters the rules wrote, and splits a sentence only right after a clear sentence end."""
    for written in _corpus():
        prepared, reference = say(written), _without_sentence_starts(written)
        assert prepared.lower() == reference.lower(), written
        text, ends = _sentence_ends(prepared)
        _, reference_ends = _sentence_ends(reference)
        for end in ends - reference_ends:
            assert _CLEAR_END.search(text[:end]), (written, text[:end])
        assert words_lost(written, prepared, english.consumed_words) == [], written
        assert " the the " not in f" {prepared.lower()} ", written


@pytest.mark.parametrize(
    "paragraph",
    [
        "Index: " + ", ".join(str(n % 30 + 1) for n in range(10_000)),
        "Index: " + "-".join(str(n % 30 + 1) for n in range(10_000)),
        "Index: " + " and ".join(str(n % 30 + 1) for n in range(10_000)),
        "Scores: " + ", ".join(f"{n % 5}-{n % 3}" for n in range(10_000)),
        "Wait" + "." * 50_000 + "then 5 men came.",  # marks with no space after them
        ("He left. 5 men came on 21 May. " * 5_000),
    ],
    ids=["commas", "hyphens", "ands", "scores", "dots", "sentences"],
)
def test_a_long_paragraph_is_prepared_in_time_that_grows_with_its_length(paragraph: str) -> None:
    started = time.perf_counter()
    TextPrep.for_language("en").body(paragraph)
    assert time.perf_counter() - started < 5  # linear: well under a second; quadratic: minutes


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("at 3:45 p.m. on Friday", "at three forty-five pee em on Friday"),
        ("at 3:45 p.m. Then", "at three forty-five pee em. Then"),
        ("at 10 a.m.", "at ten ay em."),
        ("at 3pm", "at three pee em"),
        ("at 11:05 AM.", "at eleven oh five ay em."),
        ("at 12:00 p.m.,", "at twelve pee em,"),
        ("at 12:00", "at twelve o'clock"),
        ("at 9:05", "at nine oh five"),
        ("at 14:30", "at fourteen thirty"),
        ("at 14:00", "at fourteen hundred"),
        ("John 3:16", "John three sixteen"),
    ],
)
def test_times(written: str, spoken: str) -> None:
    assert say(written) == spoken


# --- money and percentages --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("$5", "five dollars"),
        ("$1", "one dollar"),
        ("$5.50", "five dollars and fifty cents"),
        ("$5.05.", "five dollars and five cents."),
        ("$0.99", "ninety-nine cents"),
        ("$1.01", "one dollar and one cent"),
        ("$0.00", "zero dollars"),
        ("$1,250,000", "one million two hundred fifty thousand dollars"),
        ("$3.5 million", "three point five million dollars"),
        ("$1 billion", "one billion dollars"),
        ("$5.123", "five point one two three dollars"),
        ("£3", "three pounds"),
        ("£1.01", "one pound and one penny"),
        ("£2.50", "two pounds and fifty pence"),
        ("€10", "ten euros"),
        ("¥500", "five hundred yen"),
        ("paid $ 5 for it", "paid five dollars for it"),
        # After a/an, an amount describing the next word: "an eight-dollar flat".
        ("lived in an $8 flat.", "lived in an eight-dollar flat."),
        ("a £5 note", "a five-pound note"),
        ("a $20 bill and", "a twenty-dollar bill and"),
        ("a $1.50 coffee", "a one dollar and fifty cents coffee"),  # cents: plain amount
        ("a $5 or $6 tip", "a five dollars or six dollars tip"),  # next word isn't a noun
        ("an $8 per week rent", "an eight dollars per week rent"),
        ("the $8 flat", "the eight dollars flat"),  # only after a/an
        ("12%", "twelve percent"),
        ("3.5%", "three point five percent"),
        ("100 %", "one hundred percent"),
        ("1,000%", "one thousand percent"),
    ],
)
def test_money_and_percent(written: str, spoken: str) -> None:
    assert say(written) == spoken


# --- abbreviations ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Dr. Smith", "Doctor Smith"),
        ("Mr. and Mrs. Jones and Ms. Lee", "Mister and Missus Jones and Mizz Lee"),
        ("Prof. Green and Gen. Lee", "Professor Green and General Lee"),
        ("Capt. Ahab, Lt. Dan, Sgt. Pepper", "Captain Ahab, Lieutenant Dan, Sergeant Pepper"),
        ("Rev. Brown and Gov. Cuomo", "Reverend Brown and Governor Cuomo"),
        ("Mt. Everest and Ft. Worth", "Mount Everest and Fort Worth"),
        ("DR. WATSON said", "DOCTOR WATSON said"),
        ("See the Dr. again", "See the Doctor again"),
        # Only a title when a name follows.
        ("a gen. rule", "a gen. rule"),
        ("the col. was here", "the col. was here"),
        ("Sammy Davis Jr. sang.", "Sammy Davis Junior sang."),
        ("Martin Luther King, Jr. spoke", "Martin Luther King, Junior spoke"),
        ("Acme Inc. and Foo Ltd. merged", "Acme Incorporated and Foo Limited merged"),
    ],
)
def test_titles(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("St. Paul", "Saint Paul"),
        ("Visit St. Paul and St. Louis.", "Visit Saint Paul and Saint Louis."),
        ("Baker St. is long.", "Baker Street is long."),
        ("He lives on Baker St. in London.", "He lives on Baker Street in London."),
        ("on 5th St.", "on fifth Street."),
        ("Elm Dr. is near.", "Elm Drive is near."),
        ("Elm Dr. Smith is a doctor", "Elm Doctor Smith is a doctor"),
        (
            "Fifth Ave. and Sunset Blvd. and Mill Rd.",
            "Fifth Avenue and Sunset Boulevard and Mill Road.",
        ),
        ("a st. bernard", "a st. bernard"),
    ],
)
def test_saints_and_streets(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("trees, e.g. oak", "trees, for example oak"),
        ("trees (e.g., oak)", "trees (for example, oak)"),
        ("that is, i.e. the plot", "that is, that is the plot"),
        ("apples, pears, etc. Then", "apples, pears, et cetera. Then"),
        ("apples, pears, etc., and more", "apples, pears, et cetera, and more"),
        ("apples, pears, etc.", "apples, pears, et cetera."),
        ("Smith et al. found it", "Smith and others found it"),
        ("us vs. them", "us versus them"),
        ("us vs them", "us versus them"),
        ("cf. page", "compare page"),
        ("approx. 5 men", "approximately five men"),
        ("No. 5 and Nos. 6 and 7", "number five and numbers six and seven"),
        ("p. 12 and pp. 13-15", "page twelve and pages thirteen to fifteen"),
        ("Fig. 3 and Figs. 4 and 5", "figure three and figures four and five"),
        ("Vol. 2, ch. 4", "volume two, chapter four"),
        ("c. 1850", "circa eighteen fifty"),
        ("in 476 A.D. Then", "in four hundred seventy-six ay dee. Then"),
        ("in 476 A.D. 500 followed", "in four hundred seventy-six ay dee five hundred followed"),
        ("in 44 B.C.", "in forty-four bee see."),
        ("No. ", "No. "),
        ("Tom & Jerry", "Tom and Jerry"),
        ("§ 5", "section five"),
    ],
)
def test_latin_and_references(written: str, spoken: str) -> None:
    assert say(written) == spoken


# --- Roman numerals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Henry VIII", "Henry the eighth"),
        ("Elizabeth II was queen", "Elizabeth the second was queen"),
        ("Louis XIV's court", "Louis the fourteenth's court"),
        ("Henry V", "Henry the fifth"),
        ("Pope John XXIII", "Pope John the twenty-third"),
        ("World War II", "World War two"),
        ("Chapter XII begins", "Chapter twelve begins"),
        ("see CHAPTER IV", "see CHAPTER four"),
        ("Part I and Act III", "Part one and Act three"),
        ("Vol. III", "Volume three"),
        ("Book VII", "Book seven"),
        ("Type II diabetes", "Type two diabetes"),
        ("Phase I trial", "Phase one trial"),
        # Left alone.
        ("Malcolm X", "Malcolm X"),
        ("Vitamin C", "Vitamin C"),
        ("Group C", "Group C"),
        ("Then I went", "Then I went"),
        ("as I act I can", "as I act I can"),
        ("the MIX CD", "the MIX CD"),
        ("Super Mario MD", "Super Mario MD"),
    ],
)
def test_roman_numerals(written: str, spoken: str) -> None:
    assert say(written) == spoken


@pytest.mark.parametrize(
    ("heading", "spoken"),
    [
        ("XIV", "fourteen"),
        ("XIV.", "fourteen."),
        ("XIV. The Return", "fourteen. The Return"),
        ("I", "one"),
        ("I. Down the Rabbit-Hole", "one. Down the Rabbit-Hole"),
        ("Chapter XII", "Chapter twelve"),
        ("Chapter 12: The Storm", "Chapter twelve: The Storm"),
        ("I think so", "I think so"),
        ("MIX", "MIX"),
        ("Mix", "Mix"),
        ("CD", "CD"),
        ("Book Two", "Book Two"),
        ("Henry VIII", "Henry the eighth"),
    ],
)
def test_headings(heading: str, spoken: str) -> None:
    assert english.heading(heading) == spoken


def test_a_heading_year_has_no_context_so_it_is_a_number() -> None:
    assert english.heading("1865") == "one thousand eight hundred sixty-five"


# --- a whole sentence -------------------------------------------------------------------------


def test_the_example_from_the_docs() -> None:
    assert say("Dr. Smith paid $5.50 on 4 July 1776.") == (
        "Doctor Smith paid five dollars and fifty cents on the fourth of July, "
        "seventeen seventy-six."
    )


def test_text_with_nothing_to_change_is_returned_as_is() -> None:
    text = "“I don’t know,” she said. It was fine—really—and quiet."
    assert say(text) == text


def test_prepared_text_has_no_digits_left_for_ordinary_numbers() -> None:
    text = "The 3 men paid $20 on 5 May 2020 at 4:30 p.m. for 12 of 1,500 items (50%)."
    assert not any(c.isdigit() for c in say(text))


# --- no word is ever lost ---------------------------------------------------------------------

SAMPLE_PARAGRAPHS = [
    "Dr. Smith paid $5.50 on 4 July 1776, and Mrs. Jones saw him at 3:45 p.m.",
    "Henry VIII met Louis XIV at the Battle of 1812; Malcolm X was not there.",
    "In 1865 the war ended. By 1900 there were 1,250,000 people in St. Louis, i.e. most of them.",
    "Chapter XII: Mr. Darcy walked 5km on the 1st of May, vs. Ms. Bennet's 3 miles (approx. 50%).",
    "He bought apples, pears, etc. for $3 and left at 10 a.m. on Baker St. in London.",
    "The 1960s were over by the '70s; see Fig. 3, p. 12, No. 5 and Nos. 6-7, ch. 4.",
    "Galadriel whispered, “Namárië,” as the boats drifted toward Lothlórien.",
    "See Elm Dr. and Fifth Ave. or Sammy Davis Jr. at 12:00 for 1/2 cup & 2½ more.",
    "Call 555-1234 about the 22nd, e.g. 2024-03-05, or A.D. 476 and 44 B.C.",
    "— … * * *",
    "42",
    "Version 2.0 of Part III, Act I: “3 men” in the 20th century.",
]


@pytest.mark.parametrize("paragraph", SAMPLE_PARAGRAPHS)
def test_prep_never_drops_a_word(paragraph: str) -> None:
    prepared = TextPrep.for_language("en").body(paragraph)
    assert words_lost(paragraph, prepared, english.consumed_words) == []
    if any(c.isalnum() for c in paragraph):
        assert any(c.isalnum() for c in prepared)


@pytest.mark.parametrize("paragraph", SAMPLE_PARAGRAPHS)
def test_every_input_word_is_in_the_output_or_has_a_known_expansion(paragraph: str) -> None:
    """Spelled out: each word of the input is kept, or is one the rules expand on purpose."""
    expansions = {
        "dr", "mr", "mrs", "ms", "st", "jr", "sr", "e", "g", "i", "etc", "vs", "approx", "fig",
        "p", "pp", "no", "nos", "ch", "a", "m", "d", "b", "c", "th", "nd", "rd", "s", "x",
        "km", "ave", "et", "al", "cf",
    }  # fmt: skip
    prepared_words = set("".join(c if c.isalpha() else " " for c in say(paragraph)).lower().split())
    for word in "".join(c if c.isalpha() else " " for c in paragraph).split():
        low = word.lower()
        roman = word.isupper() and len(word) <= 5 and set(word) <= set("IVXLCDM")
        assert low in prepared_words or low in expansions or roman, f"lost {word!r}"


def test_a_rule_that_would_lose_a_word_is_skipped() -> None:
    class Greedy:
        language = "xx"
        consumed_words: frozenset[str] = frozenset()

        def heading(self, text: str) -> str:
            return self.body(text)

        def body(self, text: str) -> str:
            return text.replace("lost", "")

    prep = TextPrep(Greedy())
    assert prep.body("a word lost here") == "a word lost here"


def test_a_rule_that_empties_the_text_is_skipped() -> None:
    class Empties:
        language = "xx"
        consumed_words: frozenset[str] = frozenset()

        def heading(self, text: str) -> str:
            return ""

        def body(self, text: str) -> str:
            return ""

    assert TextPrep(Empties()).body("1999") == "1999"


def test_words_lost_names_the_missing_words() -> None:
    assert words_lost("the quick brown fox", "the fox") == ["quick", "brown"]
    assert words_lost("Dr. Who", "Doctor Who", {"dr"}) == []
    assert words_lost("Henry VIII", "Henry the eighth") == []
    assert words_lost("5½ cats", "five and a half cats") == []


# --- TextPrep ---------------------------------------------------------------------------------


def test_text_prep_applies_pronunciations_first_then_the_rules() -> None:
    fixes = Pronunciations((Pronunciation("Hermione", "her-MY-oh-nee"),))
    prep = TextPrep.for_language("en", fixes)
    assert prep.body("Hermione paid $5.") == "her-my-oh-nee paid five dollars."


def test_a_pronunciation_fix_can_cover_an_abbreviation() -> None:
    fixes = Pronunciations((Pronunciation("Dr. Who", "the Doctor"),))
    assert TextPrep.for_language("en", fixes).body("Dr. Who and Dr. Smith") == (
        "the Doctor and Doctor Smith"
    )


def test_chapter_prepares_the_title_as_a_heading_and_the_rest_as_text() -> None:
    chapter = Chapter("XII. The Storm", ("It cost $5.", "Dr. Who came."))
    assert TextPrep.for_language("en").chapter(chapter) == (
        "twelve. The Storm",
        "It cost five dollars.",
        "Doctor Who came.",
    )


def test_a_chapter_with_no_title_has_only_paragraphs() -> None:
    assert TextPrep.for_language("en").chapter(Chapter("", ("3 men",))) == ("three men",)


@pytest.mark.parametrize(
    ("language", "is_english"),
    [(None, True), ("", True), ("en", True), ("en-GB", True), ("EN_us", True),
     ("fr", False), ("zh-CN", False), ("de", False)],
)  # fmt: skip
def test_rules_come_from_the_books_language(language: str | None, is_english: bool) -> None:
    assert (rules_for(language) is rules_for("en")) is is_english


def test_languages_without_rules_are_voiced_as_written() -> None:
    assert isinstance(rules_for("fr"), PassthroughRules)
    prep = TextPrep.for_language("fr", Pronunciations((Pronunciation("Paris", "pah-ree"),)))
    assert prep.body("Le Dr. Paris a 5 chats.") == "Le Dr. pah-ree a 5 chats."
    assert prep.heading("Chapitre XII") == "Chapitre XII"
