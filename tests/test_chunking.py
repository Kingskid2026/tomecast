import random

import pytest

from tomecast.chunking import (
    Boundary,
    Chunk,
    CoverageError,
    chunk_paragraphs,
    split_long,
    split_sentences,
    verify_coverage,
)


@pytest.mark.parametrize(
    ("paragraph", "expected"),
    [
        ("One. Two! Three?", ["One.", "Two!", "Three?"]),
        ("Mr. Smith met Dr. Jones. They talked.", ["Mr. Smith met Dr. Jones.", "They talked."]),
        ("J. R. R. Tolkien wrote. He slept.", ["J. R. R. Tolkien wrote.", "He slept."]),
        ('"Stop!" she cried. He stopped.', ['"Stop!" she cried.', "He stopped."]),
        ('He said, "Go." Then he left.', ['He said, "Go."', "Then he left."]),
        ("Wait... what? Fine.", ["Wait... what?", "Fine."]),
        ("Bring apples, pears, etc. to the party.", ["Bring apples, pears, etc. to the party."]),
        ("It ended without a period", ["It ended without a period"]),
        ("First sentence. And a trailing fragment", ["First sentence.", "And a trailing fragment"]),
        ("  Lots   of\n\nwhitespace.  Here.  ", ["Lots of whitespace.", "Here."]),
        ("", []),
    ],
)  # fmt: skip
def test_split_sentences(paragraph: str, expected: list[str]) -> None:
    assert split_sentences(paragraph) == expected


def test_split_long_keeps_short_sentences_whole() -> None:
    assert split_long("Short sentence.", max_chars=50) == ["Short sentence."]


def test_split_long_prefers_semicolons_then_commas() -> None:
    sentence = "The rain fell all night; the river rose, and the bridge, old and tired, gave way."
    pieces = split_long(sentence, max_chars=40)
    assert pieces[0] == "The rain fell all night;"
    assert all(len(p) <= 40 for p in pieces)
    assert " ".join(pieces) == sentence


def test_split_long_cuts_a_giant_word_without_losing_letters() -> None:
    word = "x" * 25
    pieces = split_long(word, max_chars=10)
    assert pieces == ["x" * 10, "x" * 10, "x" * 5]


def test_split_long_rejects_a_zero_cap() -> None:
    with pytest.raises(ValueError):
        split_long("anything", max_chars=0)


def test_chunks_get_ids_and_boundaries() -> None:
    chunks = chunk_paragraphs(2, ["Chapter Two", "It begins. It ends."], max_chars=100)
    assert [(c.id, c.text, c.boundary) for c in chunks] == [
        ("c002-p0000-s000-k00", "Chapter Two", Boundary.PARAGRAPH),
        ("c002-p0001-s000-k00", "It begins.", Boundary.SENTENCE),
        ("c002-p0001-s001-k00", "It ends.", Boundary.PARAGRAPH),
    ]


def test_long_sentence_parts_are_marked() -> None:
    chunks = chunk_paragraphs(0, ["alpha beta, gamma delta, epsilon zeta."], max_chars=12)
    assert [c.boundary for c in chunks[:-1]] == [Boundary.PART] * (len(chunks) - 1)
    assert chunks[-1].boundary is Boundary.PARAGRAPH
    assert [c.part for c in chunks] == list(range(len(chunks)))


def test_last_sentence_without_a_period_is_never_dropped() -> None:
    # The bug from the old app: the end of the last page went missing.
    paragraphs = ["The first page is fine.", "But the last page ends mid-sentence and"]
    chunks = chunk_paragraphs(0, paragraphs, max_chars=300)
    assert chunks[-1].text == "But the last page ends mid-sentence and"


def test_a_two_thousand_word_paragraph_is_fully_covered_in_small_chunks() -> None:
    words = [f"word{i}" for i in range(2000)]
    paragraph = " ".join(words[:700]) + ", " + " ".join(words[700:]) + "."
    chunks = chunk_paragraphs(0, [paragraph], max_chars=300)
    assert all(len(c.text) <= 300 for c in chunks)
    assert " ".join(c.text for c in chunks) == paragraph


def test_random_text_is_always_fully_covered() -> None:
    rng = random.Random(1234)
    pieces = [
        "Mr.", "Dr.", "J.", "word", "Word", "end.", "End!", "why?", "so…", '"Quote."',
        "(aside)", "comma,", "semi;", "dash —", "e.g.", "x" * 40, "Namárië,", "…",
    ]  # fmt: skip
    for _ in range(300):
        paragraphs = [
            " ".join(rng.choice(pieces) for _ in range(rng.randint(0, 120)))
            for _ in range(rng.randint(1, 5))
        ]
        max_chars = rng.randint(5, 200)
        chunks = chunk_paragraphs(0, paragraphs, max_chars=max_chars)
        assert all(0 < len(c.text) <= max_chars for c in chunks)


def test_verify_coverage_catches_a_missing_chunk() -> None:
    chunks = chunk_paragraphs(0, ["One. Two. Three."], max_chars=100)
    with pytest.raises(CoverageError, match="first difference"):
        verify_coverage(["One. Two. Three."], chunks[:-1])


def test_verify_coverage_catches_changed_text() -> None:
    chunk = Chunk(0, 0, 0, 0, "One. Too.", Boundary.PARAGRAPH)
    with pytest.raises(CoverageError):
        verify_coverage(["One. Two."], [chunk])


def test_japanese_and_chinese_sentences_split_without_spaces() -> None:
    japanese = "猫が来た。犬も来た！本当？"
    assert split_sentences(japanese) == [
        "猫が来た。",
        "犬も来た！",
        "本当？",
    ]
    quoted = "「行こう。」彼は言った。"
    assert split_sentences(quoted) == [
        "「行こう。」",
        "彼は言った。",
    ]


def test_hindi_sentences_split_at_the_danda() -> None:
    hindi = "यह अच्छा है। वह भी।"
    assert len(split_sentences(hindi)) == 2


def test_long_cjk_text_splits_at_commas_not_mid_word() -> None:
    clause = "あ" * 30 + "、"
    sentence = clause * 8 + "。"
    pieces = split_long(sentence, max_chars=100)
    assert all(len(p) <= 100 for p in pieces)
    assert all(p.endswith(("、", "。")) for p in pieces)
    assert "".join(pieces) == sentence


def test_chunks_without_letters_or_digits_are_not_speakable() -> None:
    chunks = chunk_paragraphs(0, ["* * *", "⁂", "It went on. 1984."], max_chars=100)
    assert [c.speakable for c in chunks] == [False, False, True, True]


def test_random_mixed_script_text_is_always_fully_covered() -> None:
    rng = random.Random(99)
    pieces = [
        "猫。", "、", "本当？", "「行こう。」",
        "word.", "Word", "है।", "* * *", "，", "x" * 30,
    ]  # fmt: skip
    for _ in range(300):
        joiner = rng.choice(["", " "])
        paragraphs = [joiner.join(rng.choice(pieces) for _ in range(rng.randint(0, 80)))]
        max_chars = rng.randint(5, 150)
        chunks = chunk_paragraphs(0, paragraphs, max_chars=max_chars)
        assert all(0 < len(c.text) <= max_chars for c in chunks)


@pytest.mark.parametrize(
    ("paragraph", "expected"),
    [
        ("「行くぞ！」と彼は言った。次の日。", ["「行くぞ！」と彼は言った。", "次の日。"]),
        ("「本当？」って聞いた。", ["「本当？」って聞いた。"]),
        ("Wow！” and left. Then home.", ["Wow！” and left.", "Then home."]),
    ],
)
def test_cjk_sentence_ends_that_continue_are_not_split(paragraph: str, expected: list[str]) -> None:
    assert split_sentences(paragraph) == expected
