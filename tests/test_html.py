"""Tests for the single-file HTML reader. Every page is built here from public-domain text."""

from html import escape
from pathlib import Path

import pytest
from format_factory import (
    ALICE_1,
    ALICE_2,
    ALICE_3,
    CHAPTERS,
    ISHMAEL,
    expected_source_text,
    paragraphs_of,
    sentences_of,
    spoken_sentences,
    titles_of,
)

from tomecast.readers import InvalidEbookError, read_html


def _page(body: str, *, head: str = "<title>Alice</title>", lang: str = "en") -> str:
    return (
        f'<!DOCTYPE html>\n<html lang="{lang}"><head><meta charset="utf-8">{head}'
        "<style>p { color: red }</style><script>var neverRead = 1;</script></head>"
        f"<body>{body}</body></html>"
    )


def _write(tmp_path: Path, content: str | bytes, name: str = "book.html") -> Path:
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _chapters_html() -> str:
    return "".join(
        f"<h2>{escape(title)}</h2>" + "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
        for title, paragraphs in CHAPTERS
    )


def test_h1_and_h2_headings_start_chapters(tmp_path: Path) -> None:
    body = (
        "<h1>Book One</h1><p>Intro.</p>" + _chapters_html() + "<h3>Not a chapter</h3><p>More.</p>"
    )

    book = read_html(_write(tmp_path, _page(body)))

    assert titles_of(book) == ["Book One", "Down the Rabbit-Hole", "The Pool of Tears"]
    assert paragraphs_of(book) == [
        ["Intro."],
        [ALICE_1, ALICE_2],
        [ALICE_3, ISHMAEL, "Not a chapter", "More."],
    ]


def test_text_before_the_first_heading_is_a_chapter_without_a_title(tmp_path: Path) -> None:
    book = read_html(_write(tmp_path, _page("<p>Preface.</p><h1>One</h1><p>Text.</p>")))

    assert titles_of(book) == ["", "One"]
    assert paragraphs_of(book) == [["Preface."], ["Text."]]


def test_a_page_without_headings_is_one_chapter_titled_by_the_page_title(tmp_path: Path) -> None:
    book = read_html(_write(tmp_path, _page(f"<p>{ALICE_1}</p><p>{ALICE_2}</p>")))

    assert titles_of(book) == ["Alice"]
    assert paragraphs_of(book) == [[ALICE_1, ALICE_2]]
    untitled = read_html(_write(tmp_path, "<p>Just text.</p>", "plain page.htm"))
    assert titles_of(untitled) == ["plain page"]


def test_scripts_styles_and_markup_noise_are_not_read(tmp_path: Path) -> None:
    body = (
        "<h1>Title</h1><p>Some <em>stressed</em> words&nbsp;and&nbsp;an entity &amp; more.</p>"
        "<script>alert('never read')</script><style>.x{}</style>"
        '<span class="pagenum">[Pg 12]</span><div id="pg-footer"><p>License text.</p></div>'
        "<p>Line<br>break.</p>"
    )

    book = read_html(_write(tmp_path, _page(body)))

    assert paragraphs_of(book) == [["Some stressed words and an entity & more.", "Line break."]]
    assert "never read" not in str(book) and "neverRead" not in str(book)


def test_metadata_comes_from_the_head(tmp_path: Path) -> None:
    head = (
        "<title>Through the Looking-Glass</title>"
        '<meta name="author" content="Lewis Carroll"><meta name="DC.creator" content="Dodgson">'
    )

    book = read_html(_write(tmp_path, _page("<h1>One</h1><p>Text.</p>", head=head, lang="en-GB")))

    assert book.title == "Through the Looking-Glass"
    assert book.authors == ("Lewis Carroll", "Dodgson")
    assert book.language == "en-GB"
    assert book.cover is None


def test_the_declared_encoding_is_used(tmp_path: Path) -> None:
    page = '<html><head><meta charset="iso-8859-1"></head><body><p>Café society.</p></body></html>'

    book = read_html(_write(tmp_path, page.encode("latin-1")))

    assert paragraphs_of(book) == [["Café society."]]


def test_xhtml_and_sloppy_html_both_parse(tmp_path: Path) -> None:
    xhtml = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<h1>X</h1><p>Well formed.</p></body></html>"
    )
    sloppy = "<h1>S<p>Unclosed paragraph<p>Another one<br>and a break"

    assert paragraphs_of(read_html(_write(tmp_path, xhtml, "a.xhtml"))) == [["Well formed."]]
    assert paragraphs_of(read_html(_write(tmp_path, sloppy, "b.htm"))) == [
        ["Unclosed paragraph", "Another one and a break"]
    ]


def test_every_sentence_of_the_source_is_in_the_book(tmp_path: Path) -> None:
    book = read_html(_write(tmp_path, _page(_chapters_html())))

    assert not set(sentences_of(expected_source_text())) - spoken_sentences(book)


# --- Safety --------------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "<html><head><title>T</title></head><body></body></html>"])
def test_a_page_with_no_text_is_refused(tmp_path: Path, content: str) -> None:
    with pytest.raises(InvalidEbookError, match="no readable text"):
        read_html(_write(tmp_path, content))


def test_a_page_too_large_to_read_safely_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tomecast.readers import _markup

    monkeypatch.setattr(_markup, "MAX_FILE_BYTES", 1_000)

    with pytest.raises(InvalidEbookError, match="too large"):
        read_html(_write(tmp_path, _page("<p>" + "word " * 1_000 + "</p>")))


def test_a_page_nested_too_deeply_to_read_completely_is_refused(tmp_path: Path) -> None:
    page = "<html><body>" + "<div>" * 400 + "<p>Deep.</p>" + "</div>" * 400 + "</body></html>"

    with pytest.raises(InvalidEbookError, match="can't be read completely"):
        read_html(_write(tmp_path, page))
