"""The cleanup rules on their own, for any format: line joining, furniture, links, captions,
footnotes, and front and back matter."""

from __future__ import annotations

import pytest

from tomecast.book import MARKER_END, MARKER_START, Band, Block, Book, Chapter, RawChapter, Role
from tomecast.chunking import split_sentences
from tomecast.cleanup import (
    CleanupOptions,
    Matter,
    clean,
    find_matter,
    is_caption,
    is_chapter_heading,
    is_page_number,
    join_lines,
)

WORDS_200 = " ".join(["word"] * 199) + " end."


def _book(*chapters: tuple[str, tuple[str, ...]], title: str = "A Book") -> Book:
    return Book(title, (), "en", tuple(Chapter(t, p) for t, p in chapters))


def _marked(text: str) -> str:
    """``"Della^1^"`` with the reader's marker brackets instead of carets."""
    parts = text.split("^")
    return "".join(
        f"{MARKER_START}{part}{MARKER_END}" if index % 2 else part
        for index, part in enumerate(parts)
    )


# --- Joining lines -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "joined"),
    [
        (["One dollar and", "eighty-seven cents."], "One dollar and eighty-seven cents."),
        (["sixty cents of it was in pen-", "nies."], "sixty cents of it was in pennies."),
        (["the Anglo-", "Saxon way"], "the Anglo-Saxon way"),
        (["one dollar and eighty-", "seven cents"], "one dollar and eighty-seven cents"),
        (["a self-", "made man"], "a self-made man"),
        (["sterling—", "something fine"], "sterling—something fine"),
        (["the pier­", "glass"], "the pierglass"),
        (["page 1-", "12"], "page 1-12"),
        (["  spaced  ", "", " out "], "spaced out"),
        (["a soft­hyphen inside"], "a softhyphen inside"),
    ],
)
def test_lines_join_into_a_paragraph(lines: list[str], joined: str) -> None:
    assert join_lines(lines) == joined


# --- Page furniture ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12", True),
        ("xiv", True),
        ("- 12 -", True),
        ("— 7 —", True),
        ("Page 12", True),
        ("Page 12 of 300", True),
        ("[12]", True),
        ("12 THE GIFT OF THE MAGI", False),
        ("Twelve", False),
        ("1905.", False),
    ],
)
def test_page_numbers_are_recognised(text: str, expected: bool) -> None:
    assert is_page_number(text) is expected


def _paged(*pages: list[Block]) -> Book:
    blocks = tuple(block for page in pages for block in page)
    raw = (RawChapter("Story", blocks),)
    return Book("Story", (), None, (), raw=raw)


def _page(number: int, header: str, body: str, *, footer: str | None = None) -> list[Block]:
    page = [
        Block((header,), page=number, band=Band.TOP),
        Block((body,), page=number, continues=True),
    ]
    if footer is not None:
        page.append(Block((footer,), page=number, band=Band.BOTTOM))
    return page


def test_repeated_headers_are_found_with_their_digits_ignored() -> None:
    book = _paged(
        _page(0, "12 THE GIFT", "It began.", footer="Della's story"),
        _page(1, "THE GIFT 13", "It went on.", footer="Christmas Eve"),
        _page(2, "14 THE GIFT", "It ended.", footer="Jim's story"),
    )

    result = clean(book)

    assert result.book.chapters[0].paragraphs == (
        "It began.",
        "Della's story",
        "It went on.",
        "Christmas Eve",
        "It ended.",
        "Jim's story",
    )
    assert [(r.rule, r.text, r.page) for r in result.removed] == [
        ("page-numbers", "12 THE GIFT", 0),
        ("page-numbers", "THE GIFT 13", 1),
        ("page-numbers", "14 THE GIFT", 2),
    ]


def test_a_header_on_too_few_pages_is_text() -> None:
    headers = ["THE GIFT", "THE GIFT", "Della", "Jim", "The Combs", "The Watch"]
    pages = [_page(n, header, f"Text {n}.") for n, header in enumerate(headers)]

    result = clean(_paged(*pages))

    assert result.book.chapters[0].paragraphs[:3] == ("THE GIFT", "Text 0.", "THE GIFT")
    assert "Della" in result.book.chapters[0].paragraphs
    assert result.removed == ()


def test_numbered_running_headers_are_furniture() -> None:
    pages = [_page(n, f"Part {n + 1}", f"Text {n}.") for n in range(5)]

    result = clean(_paged(*pages))

    assert result.book.chapters[0].paragraphs == tuple(f"Text {n}." for n in range(5))


def test_paragraphs_join_across_page_breaks_only_when_they_carry_on() -> None:
    blocks = (
        Block(("She had been saving every",), page=0),
        Block(("penny she could.",), page=1, continues=True),  # carries on mid-sentence
        Block(("Twenty dollars a week doesn't go far.",), page=1),
        Block(("Expenses had been greater.",), page=2, continues=True),  # a new sentence
        Block(("They always are",), page=2),
        Block(("only $1.87.",), page=3, continues=True),  # lowercase: carries on
        Block(("A heading",), role=Role.HEADING, page=3),
        Block(("follows a heading.",), page=4, continues=True),  # headings never join
    )
    book = Book("B", (), None, (), raw=(RawChapter("", blocks),))

    assert clean(book).book.chapters[0].paragraphs == (
        "She had been saving every penny she could.",
        "Twenty dollars a week doesn't go far.",
        "Expenses had been greater.",
        "They always are only $1.87.",
        "A heading",
        "follows a heading.",
    )


# --- Footnotes ---------------------------------------------------------------------------------


def _with_notes() -> Book:
    blocks = (
        Block((_marked("Three times Della^1^ counted it.*"),), page=0),
        Block((_marked("^1^ The wife of James Dillingham"),), role=Role.FOOTNOTE, page=0),
        Block(("Young.",), role=Role.FOOTNOTE, page=1, continues=True),
        Block((_marked("And the next day^†^ would be Christmas."),), page=1),
        Block(("† December the twenty-fifth.",), role=Role.FOOTNOTE, page=1),
    )
    return Book("B", (), None, (), raw=(RawChapter("One", blocks),))


def test_footnotes_are_dropped_by_default() -> None:
    result = clean(_with_notes())

    assert result.book.chapters[0].paragraphs == (
        "Three times Della counted it.*",
        "And the next day would be Christmas.",
    )
    assert [(r.rule, r.text) for r in result.removed] == [
        ("footnote-markers", "1"),
        ("footnote-markers", "†"),
        ("footnotes", "The wife of James Dillingham Young."),
        ("footnotes", "December the twenty-fifth."),
    ]


def test_footnotes_can_be_read_at_chapter_end_or_kept_in_place() -> None:
    at_end = clean(_with_notes(), CleanupOptions(footnotes="end"))
    in_place = clean(_with_notes(), CleanupOptions(footnotes="keep", footnote_markers=False))

    assert at_end.book.chapters[0].paragraphs == (
        "Three times Della counted it.*",
        "And the next day would be Christmas.",
        "The wife of James Dillingham Young.",
        "December the twenty-fifth.",
    )
    assert in_place.book.chapters[0].paragraphs == (
        "Three times Della1 counted it.*",
        "1 The wife of James Dillingham Young.",
        "And the next day† would be Christmas.",
        "† December the twenty-fifth.",
    )


# --- Links and captions ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paragraph", "cleaned", "removed"),
    [
        (
            "Read it at https://www.gutenberg.org/ebooks/7256.",
            "Read it at.",
            ["https://www.gutenberg.org/ebooks/7256"],
        ),
        ("Free (www.gutenberg.org) for all.", "Free for all.", ["www.gutenberg.org"]),
        ("Write to help@example.org, please.", "Write to, please.", ["help@example.org"]),
        ("See gutenberg.org/ebooks/7256 for more.", "See for more.", ["gutenberg.org/ebooks/7256"]),
        ("He worked at Amazon.com for years.", "He worked at Amazon.com for years.", []),
    ],
)
def test_links_are_removed(paragraph: str, cleaned: str, removed: list[str]) -> None:
    result = clean(_book(("One", (paragraph, "More text."))))

    assert result.book.chapters[0].paragraphs[0] == cleaned
    assert [r.text for r in result.removed if r.rule == "links"] == removed


def test_a_paragraph_that_was_only_a_link_goes() -> None:
    result = clean(_book(("One", ("Text.", "https://www.gutenberg.org"))))
    assert result.book.chapters[0].paragraphs == ("Text.",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Figure 3: The flat at $8 per week.", True),
        ("Fig. 3. The flat.", True),
        ("FIGURE 12.4 — A gray cat.", True),
        ("Table 2: Prices in 1905", True),
        ("Plate IV. The combs.", True),
        ("[Illustration: Della at the glass]", True),
        ("[Illustration]", True),
        ("Figure 3 shows the flat.", False),
        ("Table manners mattered to Jim.", False),
        ("Figure: " + "word " * 90, False),
    ],
)
def test_captions_are_recognised(text: str, expected: bool) -> None:
    assert is_caption(text) is expected


def test_captions_are_removed_from_any_book() -> None:
    result = clean(_book(("One", ("[Illustration: Della]", "Della cried."))))
    kept = clean(
        _book(("One", ("[Illustration: Della]", "Della cried."))), CleanupOptions(captions=False)
    )

    assert result.book.chapters[0].paragraphs == ("Della cried.",)
    assert kept.book.chapters[0].paragraphs == ("[Illustration: Della]", "Della cried.")


def test_a_book_with_nothing_to_clean_is_unchanged() -> None:
    book = _book(
        ("Chapter 1", ("It began , as stories do .", "* * *", "It ended")),
        ("Chapter 2", ("Another.",)),
    )
    result = clean(book)

    assert result.book.chapters == book.chapters
    assert result.removed == ()
    assert result.kept == [0, 1]


# --- Front and back matter ---------------------------------------------------------------------


def test_front_matter_before_the_first_numbered_chapter() -> None:
    book = _book(
        ("Alice's Adventures in Wonderland", ("Lewis Carroll",)),
        ("Contents", ("Down the Rabbit-Hole", "The Pool of Tears")),
        ("", ("Copyright 1865. All rights reserved. ISBN 0-00-000000-0",)),
        ("Preface", (WORDS_200,)),
        ("Dedication", (WORDS_200,)),
        ("CHAPTER I. Down the Rabbit-Hole", (WORDS_200,)),
        ("Contents", (WORDS_200,)),  # between chapters: story, whatever it's called
        ("CHAPTER II. The Pool of Tears", (WORDS_200,)),
        title="Alice's Adventures in Wonderland",
    )

    assert find_matter(book) == (
        Matter.FRONT, Matter.CONTENTS, Matter.COPYRIGHT, None, Matter.FRONT, None, None, None,
    )  # fmt: skip


def test_back_matter_after_the_last_numbered_chapter() -> None:
    book = _book(
        ("Chapter 1", (WORDS_200,)),
        ("Chapter 2", (WORDS_200,)),
        ("Afterword", ("A short word from the editor.",)),
        ("Also by O. Henry", ("Cabbages and Kings", "The Four Million")),
        ("Index", ("Della, 3, 7", "Jim, 4-9", "watch, 12", "combs, 11")),
        ("", ("Della, 3, 7", "Jim, 4-9", "watch, 12", "combs, 11")),  # an index by its lines
    )

    assert find_matter(book) == (None, None, None, Matter.ALSO_BY, Matter.INDEX, Matter.INDEX)


def test_a_preview_runs_from_its_introduction_to_the_end() -> None:
    book = _book(
        ("Chapter 1", (WORDS_200,)),
        ("Chapter 2", (WORDS_200,)),
        ("Chapter 3", (WORDS_200,)),
        ("Chapter 4", (WORDS_200,)),
        ("", ("Read on for an excerpt from The Ransom of Red Chief.",)),
        ("Chapter 1", (WORDS_200,)),  # the other book's chapters start again at 1
        ("Chapter 2", (WORDS_200,)),
    )

    assert find_matter(book) == (None,) * 4 + (Matter.PREVIEW,) * 3


def test_a_preview_by_title_after_the_story() -> None:
    book = _book(
        ("Chapter 1", (WORDS_200,)),
        ("Chapter 2", (WORDS_200,)),
        ("A Sneak Peek at Book Two", (WORDS_200,)),
        ("Also by the Author", ("Book Two",)),
    )

    assert find_matter(book) == (None, None, Matter.PREVIEW, Matter.ALSO_BY)


def test_a_chapter_called_preview_mid_book_is_story() -> None:
    book = _book(
        ("Chapter 1", (WORDS_200,)),
        ("Chapter 2", (WORDS_200,)),
        ("Chapter 3: The Preview", (WORDS_200,)),
        ("Chapter 4", (WORDS_200,)),
    )
    assert find_matter(book) == (None,) * 4


def test_without_numbered_chapters_only_the_opening_and_closing_runs_count() -> None:
    book = _book(
        ("Copyright", ("All rights reserved.",)),
        ("Spring", (WORDS_200,)),
        ("Haiku", ("Short poem.",)),  # short, but in the middle
        ("Winter", (WORDS_200,)),
        ("Index", ("Spring, 3", "Winter, 9", "Haiku, 5")),
    )
    assert find_matter(book) == (Matter.COPYRIGHT, None, None, None, Matter.INDEX)


def test_a_book_that_is_all_front_matter_has_none() -> None:
    book = _book(("Title Page", ("A Book",)), ("Contents", ("One",)))
    assert find_matter(book) == (None, None)


def test_each_kind_of_matter_is_its_own_switch() -> None:
    book = _book(
        ("Contents", ("One", "Two")),
        ("Copyright", ("All rights reserved.",)),
        ("Chapter 1", (WORDS_200,)),
        ("Index", ("Della, 3", "Jim, 4", "combs, 5")),
    )

    assert clean(book).kept == [2]
    assert clean(book, CleanupOptions(skip=frozenset())).kept == [0, 1, 2, 3]
    assert clean(book, CleanupOptions(skip=frozenset({Matter.INDEX}))).kept == [0, 1, 2]
    skipped = clean(book).removed
    assert [(r.rule, r.text, r.chapter) for r in skipped] == [
        (Matter.CONTENTS, "Contents", 0),
        (Matter.COPYRIGHT, "Copyright", 1),
        (Matter.INDEX, "Index", 3),
    ]


@pytest.mark.parametrize(
    ("line", "heading"),
    [
        ("Chapter 3", True),
        ("CHAPTER III", True),
        ("Chapter Twenty-One", True),
        ("PART TWO: Home", True),
        ("Chapter 3. The Watch", True),
        ("Chapter 3 was the hardest to write.", False),
        ("Chapters 3 and 4", False),
    ],
)
def test_chapter_heading_lines(line: str, heading: bool) -> None:
    assert is_chapter_heading(line) is heading


def test_cleaning_never_loses_a_body_sentence_of_an_ordinary_book() -> None:
    paragraphs = (
        "One dollar and eighty-seven cents. That was all. And sixty cents of it was in pennies.",
        "Mr. James Dillingham Young came home. He was called “Jim.” Which is all very good.",
        "Della, being slender, had mastered the art—of looking.",
    )
    result = clean(_book(("Chapter 1", paragraphs)))

    text = " ".join(result.book.chapters[0].paragraphs)
    for paragraph in paragraphs:
        for sentence in split_sentences(paragraph):
            assert sentence in text
