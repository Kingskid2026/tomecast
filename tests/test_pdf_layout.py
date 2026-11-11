"""The optional AI pass for PDF page layout: boxes from a layout model, words from the PDF.

These tests never run the model: a fake detector hands out boxes drawn by hand around the
lines ``pdf_factory`` placed (public-domain text from O. Henry's "The Gift of the Magi").
The one test that runs Docling Heron itself is marked ``integration`` and needs the model
file (set ``TOMECAST_HERON_MODEL``); it's skipped otherwise, so no test downloads anything.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy
import pypdfium2 as pdfium
import pytest
from pdf_factory import (
    MARGIN,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    SAMPLE_CHAPTERS,
    PageBuilder,
    PdfPage,
    build_pdf,
    build_sample_book,
    plain,
)

from tomecast.book import Band, Role
from tomecast.chunking import split_sentences
from tomecast.cleanup import CleanupOptions, clean
from tomecast.readers import heron
from tomecast.readers.pdf import _page_chars, read_pdf
from tomecast.readers.pdf_layout import (
    HeronLayout,
    Label,
    LayoutMismatch,
    PageLayoutResult,
    Region,
    check_words,
    layout_page,
    page_words,
    reading_order,
)

TEXT_LEFT = MARGIN - 4
TEXT_RIGHT = PAGE_WIDTH - MARGIN + 40  # lines are set ragged, some a little past the margin


class FakeDetector:
    """Hands out one page's boxes per call, in page order."""

    def __init__(self, pages: Sequence[Sequence[Region]]) -> None:
        self._pages: Iterator[Sequence[Region]] = iter(pages)

    def detect(self, page: Any, width: float, height: float) -> list[Region]:
        return list(next(self._pages))


class BrokenDetector:
    def detect(self, page: Any, width: float, height: float) -> list[Region]:
        raise RuntimeError("the model crashed")


def box(label: Label, x0: float, y0: float, x1: float, y1: float, score: float = 0.9) -> Region:
    return Region(label, score, x0, y0, x1, y1)


HEADER = box(Label.PAGE_HEADER, 0, PAGE_HEIGHT - 40, PAGE_WIDTH, PAGE_HEIGHT - 20)
FOOTER = box(Label.PAGE_FOOTER, 0, 20, PAGE_WIDTH, 45)
NOTES = box(Label.FOOTNOTE, TEXT_LEFT, MARGIN - 4, TEXT_RIGHT, MARGIN + 22)

FIRST = [
    "One dollar and eighty-seven cents. That was all. And sixty",
    "cents of it was in pennies. Pennies saved one and two at a",
    "time by bulldozing the grocer and the vegetable man and the butch-",
    "er until one’s cheeks burned with the silent imputation of",
    "parsimony that such close dealing implied. Three times Della^1^",
    "counted it. One dollar and eighty-seven cents. And the next",
    "day would be Christmas.",
]
SECOND = [
    "There was clearly nothing to do but flop down on the shabby",
    "little couch and howl. So Della did it.",
]
NOTE = "^1^ Della is the wife of James Dillingham Young."


def story_page() -> PdfPage:
    """A heading, two paragraphs, a footnote, a running header and a page number."""
    page = PageBuilder().header("THE GIFT OF THE MAGI")
    page.heading("Chapter One")
    page.paragraph(FIRST)
    page.paragraph(SECOND)
    page.footnotes([NOTE])
    page.footer("3")
    return page.page


def story_boxes() -> list[Region]:
    top = PAGE_HEIGHT - MARGIN
    return [
        box(Label.SECTION_HEADER, TEXT_LEFT, top - 40, TEXT_RIGHT, top + 4),
        box(Label.TEXT, TEXT_LEFT, 420, TEXT_RIGHT, top - 42),
        box(Label.TEXT, TEXT_LEFT, 380, TEXT_RIGHT, 419),
        NOTES,
        HEADER,
        FOOTER,
    ]


def lay_out(path: Path, boxes: Sequence[Region], page: int = 0) -> PageLayoutResult:
    """Lay out one page of ``path`` with ``boxes``, as the AI pass would."""
    document = pdfium.PdfDocument(path)
    try:
        pdf_page = document[page]
        width, height = (float(v) for v in pdf_page.get_size())
        textpage = pdf_page.get_textpage()
        chars, _ = _page_chars(textpage)
        textpage.close()
        pdf_page.close()
    finally:
        document.close()
    return layout_page(chars, boxes, page, width, height)


def texts(result: PageLayoutResult) -> list[tuple[Role, Band | None, bool, str]]:
    return [
        (block.role, block.band, block.furniture, " ".join(line.text for line in block.lines))
        for block in result.blocks
    ]


# --- One page ----------------------------------------------------------------------------------


def test_boxes_label_the_pdf_words(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])

    result = lay_out(path, story_boxes())

    roles = [(role, band, furniture) for role, band, furniture, _ in texts(result)]
    assert roles == [
        (Role.BODY, Band.TOP, True),
        (Role.HEADING, None, False),
        (Role.BODY, None, False),
        (Role.BODY, None, False),
        (Role.FOOTNOTE, None, False),
        (Role.BODY, Band.BOTTOM, True),
    ]
    assert texts(result)[0][3] == "THE GIFT OF THE MAGI"
    assert texts(result)[-1][3] == "3"
    assert result.furniture_words == ["THE", "GIFT", "OF", "THE", "MAGI", "3"]


def test_the_cleaned_page_is_exactly_the_story(tmp_path: Path) -> None:
    """Header and page number go (even though the header is on one page only), the
    footnote goes, its marker goes, the hyphenated word is joined."""
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    layout = HeronLayout(FakeDetector([story_boxes()]))

    result = clean(read_pdf(path, layout=layout))

    assert layout.fallbacks == []
    assert [(c.title, c.paragraphs) for c in result.book.chapters] == [
        ("Chapter One", (plain(" ".join(FIRST)).replace("butch- er", "butcher"), " ".join(SECOND)))
    ]
    assert {r.rule for r in result.removed} >= {"page-numbers", "footnotes", "footnote-markers"}


def test_page_furniture_follows_the_page_numbers_switch(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    book = read_pdf(path, layout=HeronLayout(FakeDetector([story_boxes()])))

    chapters = clean(book, CleanupOptions(page_numbers=False)).book.chapters

    paragraphs = [paragraph for chapter in chapters for paragraph in chapter.paragraphs]
    assert paragraphs[0] == "THE GIFT OF THE MAGI"  # before the chapter heading
    assert paragraphs[-1] == "3"


def test_footnotes_can_be_read_at_the_chapter_end(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    book = read_pdf(path, layout=HeronLayout(FakeDetector([story_boxes()])))

    chapter = clean(book, CleanupOptions(footnotes="end")).book.chapters[0]

    assert chapter.paragraphs[-1] == "Della is the wife of James Dillingham Young."


def test_a_header_on_one_page_only_is_left_out_only_with_the_model(tmp_path: Path) -> None:
    """The rules can only call a header furniture once it repeats; the model's label is
    enough. Everything else on this easy page reads the same either way."""
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    layout = HeronLayout(FakeDetector([story_boxes()]))

    with_rules = clean(read_pdf(path)).book.chapters
    with_boxes = clean(read_pdf(path, layout=layout)).book.chapters

    assert with_rules[0].paragraphs == ("THE GIFT OF THE MAGI",)
    assert with_rules[1:] == with_boxes


def test_an_unsure_footer_box_is_read_unless_cleanup_calls_it_furniture(tmp_path: Path) -> None:
    """A footer box under 0.9 only marks a footer line: one that isn't a page number and
    doesn't repeat is still read (Heron called a formula line a footer at 0.80)."""
    page = PageBuilder()
    page.paragraph(SECOND)
    page.footer("a > b.")
    path = build_pdf(tmp_path / "formula.pdf", [page.page])
    boxes = [
        box(Label.TEXT, TEXT_LEFT, 540, TEXT_RIGHT, PAGE_HEIGHT - MARGIN + 4),
        box(Label.PAGE_FOOTER, 0, 20, PAGE_WIDTH, 45, score=0.8),
    ]

    result = clean(read_pdf(path, layout=HeronLayout(FakeDetector([boxes]))))

    assert result.book.chapters[0].paragraphs[-1] == "a > b."


def test_a_running_header_called_a_section_header_still_goes(tmp_path: Path) -> None:
    """Heron sometimes labels a small running header as a section header. One line in the
    top fifth, no bigger than the text, is a header line instead: it goes once it repeats,
    and the real (big) chapter heading stays."""
    pages = [story_page(), story_page(), story_page()]
    path = build_pdf(tmp_path / "story.pdf", pages)
    as_heading = box(Label.SECTION_HEADER, *(HEADER.x0, HEADER.y0, HEADER.x1, HEADER.y1))
    boxes = [[as_heading if b is HEADER else b for b in story_boxes()]] * 3

    result = clean(read_pdf(path, layout=HeronLayout(FakeDetector(boxes))))

    text = " ".join(p for c in result.book.chapters for p in (c.title, *c.paragraphs))
    assert "MAGI" not in text
    assert [c.title for c in result.book.chapters] == ["Chapter One"] * 3


# --- Words no box holds, and boxes that overlap ------------------------------------------------


def test_words_outside_every_box_are_still_read(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    boxes = [b for b in story_boxes() if b.label is not Label.TEXT]  # the model missed both

    result = lay_out(path, boxes)

    body = [text for role, _, cut, text in texts(result) if role is Role.BODY and not cut]
    assert " ".join(body).startswith("One dollar and eighty-seven cents.")
    assert " ".join(body).endswith("So Della did it.")


def test_a_lone_page_number_no_box_holds_is_furniture(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    boxes = [b for b in story_boxes() if b.label is not Label.PAGE_FOOTER]

    result = lay_out(path, boxes)

    # A footer line, not furniture outright: cleanup removes it because it's a page number.
    assert texts(result)[-1] == (Role.BODY, Band.BOTTOM, False, "3")
    assert result.furniture_words == ["THE", "GIFT", "OF", "THE", "MAGI"]


def test_a_word_in_two_boxes_is_read_once(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    whole = box(Label.TEXT, TEXT_LEFT, 380, TEXT_RIGHT, PAGE_HEIGHT - MARGIN - 42, score=0.6)

    result = lay_out(path, [*story_boxes(), whole])

    words = " ".join(text for _, _, _, text in texts(result)).split()
    assert words.count("Christmas.") == 1
    assert words.count("howl.") == 1


def test_a_picture_box_over_text_does_not_take_its_words(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    picture = box(Label.PICTURE, 0, 0, PAGE_WIDTH, PAGE_HEIGHT, score=0.99)

    result = lay_out(path, [*story_boxes(), picture])

    assert Role.CAPTION not in {role for role, _, _, _ in texts(result)}


def test_text_on_a_picture_is_a_caption(tmp_path: Path) -> None:
    page = PageBuilder()
    page.paragraph(SECOND)
    page.picture(MARGIN + 60, 300, 200, 110)
    page.line("Figure 1: The flat at $8 per week.", size=9, y=362, x=MARGIN + 80)
    path = build_pdf(tmp_path / "figure.pdf", [page.page])
    boxes = [
        box(Label.TEXT, TEXT_LEFT, 540, TEXT_RIGHT, PAGE_HEIGHT - MARGIN + 4),
        box(Label.PICTURE, MARGIN + 60, 300, MARGIN + 260, 410),
    ]

    result = lay_out(path, boxes)

    assert texts(result)[-1] == (Role.CAPTION, None, False, "Figure 1: The flat at $8 per week.")


# --- Reading order -------------------------------------------------------------------------------

LEFT = [
    "Della finished her cry and",
    "attended to her cheeks with",
    "the powder rag. She stood by",
]
RIGHT = [
    "gray fence in a gray backyard.",
    "Tomorrow would be Christmas",
    "Day, and she had only $1.87.",
]


def two_column_page() -> PdfPage:
    """A title across both columns, then two columns drawn left, right, left, right…, then a
    footnote across both, so the drawing order isn't the reading order."""
    page = PageBuilder()
    page.heading("Chapter Two")
    y = page.y
    for row, (left, right) in enumerate(zip(LEFT, RIGHT, strict=True)):
        page.line(left, y=y, x=MARGIN + (18 if row == 0 else 0))
        page.line(right, y=y, x=MARGIN + 174)
        y -= 14
    page.footnotes(["^2^ The backyard is behind the flat."])
    return page.page


def test_two_columns_are_read_left_then_right(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "columns.pdf", [two_column_page()])
    top = PAGE_HEIGHT - MARGIN
    boxes = [  # the model's order says nothing about reading order
        box(Label.TEXT, MARGIN + 170, 500, MARGIN + 330, top - 40),
        NOTES,
        box(Label.TEXT, TEXT_LEFT, 500, MARGIN + 165, top - 40),
        box(Label.SECTION_HEADER, TEXT_LEFT, top - 39, TEXT_RIGHT, top + 4),
    ]

    result = lay_out(path, boxes)

    assert [text for _, _, _, text in texts(result)] == [
        "Chapter Two",
        " ".join(LEFT),
        " ".join(RIGHT),
        "2 The backyard is behind the flat.",
    ]


def test_reading_order_of_boxes() -> None:
    title = box(Label.TITLE, 50, 700, 550, 740)
    left_top = box(Label.TEXT, 50, 500, 290, 690)
    left_low = box(Label.TEXT, 50, 300, 290, 490)
    right_top = box(Label.TEXT, 310, 450, 550, 690)
    right_low = box(Label.TEXT, 310, 300, 550, 440)
    figure = box(Label.PICTURE, 50, 100, 550, 290)
    after = box(Label.TEXT, 50, 50, 290, 90)

    columns = reading_order([after, right_low, figure, left_low, title, right_top, left_top])

    assert [region for column in columns for region in column] == [
        title, left_top, left_low, right_top, right_low, figure, after,
    ]  # fmt: skip


# --- The check -----------------------------------------------------------------------------------


def test_the_check_catches_a_lost_word(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    result = lay_out(path, story_boxes())
    document = pdfium.PdfDocument(path)
    textpage = document[0].get_textpage()
    words = page_words(_page_chars(textpage)[0])
    textpage.close()
    document.close()

    result.blocks[2].lines.pop()  # the last line of the first paragraph goes missing

    with pytest.raises(LayoutMismatch, match="Christmas"):
        check_words(words, result)


def test_a_page_the_model_fails_on_is_read_by_the_rules(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    layout = HeronLayout(BrokenDetector())

    book = read_pdf(path, layout=layout)

    assert layout.fallbacks == [(0, "layout model failed: the model crashed")]
    assert clean(book).book.chapters == clean(read_pdf(path)).book.chapters


# --- Heron's output -----------------------------------------------------------------------------


def test_heron_boxes_map_to_page_coordinates(tmp_path: Path) -> None:
    path = build_pdf(tmp_path / "story.pdf", [story_page()])
    document = pdfium.PdfDocument(path)
    page = document[0]
    labels = numpy.array([9, 5, 9, 4])
    boxes = numpy.array(
        [
            [0.0, 0.0, 320.0, 320.0],  # the top left quarter, in pixels from the top
            [0.0, 0.0, 640.0, 32.0],
            [1.0, 1.0, 321.0, 321.0],  # almost the same as the first, less sure
            [0.0, 600.0, 640.0, 640.0],  # not sure enough
        ]
    )
    scores = numpy.array([0.9, 0.8, 0.7, 0.2])

    regions = heron.to_regions(page, labels, boxes, scores)
    page.close()
    document.close()

    assert [(r.label, round(r.x0), round(r.y0), round(r.x1), round(r.y1)) for r in regions] == [
        (Label.TEXT, 0, round(PAGE_HEIGHT / 2), round(PAGE_WIDTH / 2), round(PAGE_HEIGHT)),
        (Label.PAGE_HEADER, 0, round(PAGE_HEIGHT * 0.95), round(PAGE_WIDTH), round(PAGE_HEIGHT)),
    ]


@pytest.mark.integration
def test_heron_keeps_every_sentence_of_the_sample_book(tmp_path: Path) -> None:
    """Docling Heron itself, on the sample book set on short pages: every sentence of the
    story is spoken, in order, and no page needed the rules."""
    model = os.environ.get("TOMECAST_HERON_MODEL")
    if not model or not Path(model).is_file():
        pytest.skip("set TOMECAST_HERON_MODEL to the Heron ONNX file to run this")
    layout = HeronLayout(heron.HeronModel(Path(model), threads=2))

    result = clean(read_pdf(build_sample_book(tmp_path / "sample.pdf", short_pages=True), layout))

    assert layout.fallbacks == []
    text = " ".join(
        paragraph for index in result.kept for paragraph in result.book.chapters[index].paragraphs
    )
    position = 0
    for _, paragraphs in SAMPLE_CHAPTERS:
        for paragraph in paragraphs:
            for sentence in split_sentences(plain(paragraph)):
                found = text.find(sentence, position)
                assert found >= 0, f"missing or out of order: {sentence!r}"
                position = found + len(sentence)
