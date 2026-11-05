"""Tests for the FB2 reader. Every book is built on the fly by ``format_factory``."""

import time
from pathlib import Path

import pytest
from format_factory import (
    ALICE_1,
    ALICE_2,
    ALICE_3,
    CHAPTERS,
    ISHMAEL,
    expected_source_text,
    fb2_document,
    fb2_sections,
    paragraphs_of,
    png,
    sentences_of,
    spoken_sentences,
    titles_of,
)

from tomecast.readers import InvalidEbookError, read_fb2


def _write(tmp_path: Path, data: bytes, name: str = "book.fb2") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_titled_sections_become_chapters(tmp_path: Path) -> None:
    path = _write(tmp_path, fb2_document(fb2_sections(CHAPTERS)))

    book = read_fb2(path)

    assert titles_of(book) == ["Down the Rabbit-Hole", "The Pool of Tears"]
    assert paragraphs_of(book) == [[ALICE_1, ALICE_2], [ALICE_3, ISHMAEL]]


def test_nested_sections_start_chapters_in_reading_order(tmp_path: Path) -> None:
    body = (
        "<section><title><p>Part</p><p>One</p></title><epigraph><p>An epigraph.</p></epigraph>"
        "<section><title><p>First</p></title><p>Alpha.</p><empty-line/><p>Beta.</p></section>"
        "<section><title><p>Second</p></title><p>Gamma.</p>"
        "<section><title><p>Deeper</p></title><p>Delta.</p></section></section>"
        "<section><p>Untitled sections stay in the chapter before.</p></section></section>"
    )

    book = read_fb2(_write(tmp_path, fb2_document(body)))

    assert titles_of(book) == ["Part One", "First", "Second", "Deeper"]
    assert paragraphs_of(book) == [
        ["An epigraph."],
        ["Alpha.", "Beta."],
        ["Gamma."],
        ["Delta.", "Untitled sections stay in the chapter before."],
    ]


def test_inline_markup_poems_and_tables_are_kept_as_text(tmp_path: Path) -> None:
    body = (
        "<section><title><p>Mixed</p></title>"
        "<p>Some <emphasis>stressed</emphasis> and <strong>bold</strong> words<a l:href='#n1' "
        "type='note'>[1]</a>.</p>"
        "<poem><title><p>A Poem</p></title><stanza><v>Line one,</v><v>line two.</v></stanza>"
        "<text-author>The Poet</text-author></poem>"
        "<cite><p>Quoted.</p></cite><subtitle>* * *</subtitle>"
        "<table><tr><th>Head</th></tr><tr><td>Cell</td></tr></table>"
        "<image l:href='#none'/><p>Last.</p></section>"
    )

    book = read_fb2(_write(tmp_path, fb2_document(body)))

    assert paragraphs_of(book) == [
        [
            "Some stressed and bold words[1].",
            "A Poem",
            "Line one,",
            "line two.",
            "The Poet",
            "Quoted.",
            "* * *",
            "Head",
            "Cell",
            "Last.",
        ]
    ]


def test_the_notes_body_is_skipped(tmp_path: Path) -> None:
    notes = (
        '<body name="notes"><section id="n1"><title><p>1</p></title><p>A footnote.</p></section>'
        "</body>"
    )
    path = _write(tmp_path, fb2_document(fb2_sections(CHAPTERS[:1]), extra=notes))

    book = read_fb2(path)

    assert titles_of(book) == ["Down the Rabbit-Hole"]
    assert "A footnote." not in str(book)


def test_metadata_comes_from_title_info(tmp_path: Path) -> None:
    data = fb2_document(
        fb2_sections(CHAPTERS[:1]),
        title="Alice",
        authors=(("Lewis", "Carroll"), ("Sir John", "Tenniel")),
        lang="en",
    )

    book = read_fb2(_write(tmp_path, data))

    assert book.title == "Alice"
    assert book.authors == ("Lewis Carroll", "Sir John Tenniel")
    assert book.language == "en"


def test_the_cover_is_the_coverpage_binary(tmp_path: Path) -> None:
    cover = png(20, 30)
    book = read_fb2(_write(tmp_path, fb2_document(fb2_sections(CHAPTERS[:1]), cover=cover)))

    assert book.cover is not None
    assert (book.cover.media_type, book.cover.data) == ("image/png", cover)
    assert (
        read_fb2(_write(tmp_path, fb2_document(fb2_sections(CHAPTERS[:1])), "b.fb2")).cover is None
    )


def test_a_cover_that_is_not_an_image_is_ignored(tmp_path: Path) -> None:
    data = fb2_document(fb2_sections(CHAPTERS[:1]), cover=b"not an image")

    assert read_fb2(_write(tmp_path, data)).cover is None


def test_a_legacy_encoding_declared_in_the_file_is_honoured(tmp_path: Path) -> None:
    line = "Я помню чудное мгновенье."
    body = fb2_sections([("Стихи", [line])])

    book = read_fb2(_write(tmp_path, fb2_document(body, encoding="windows-1251")))

    assert paragraphs_of(book) == [[line]]
    assert titles_of(book) == ["Стихи"]


def test_every_sentence_of_the_source_is_in_the_book(tmp_path: Path) -> None:
    book = read_fb2(_write(tmp_path, fb2_document(fb2_sections(CHAPTERS))))

    assert not set(sentences_of(expected_source_text())) - spoken_sentences(book)


def test_text_outside_a_known_element_is_not_lost(tmp_path: Path) -> None:
    body = (
        "<section><title><p>T</p></title>Stray words.<unknown>Inside unknown.</unknown></section>"
    )

    book = read_fb2(_write(tmp_path, fb2_document(body)))

    assert paragraphs_of(book) == [["Stray words.", "Inside unknown."]]


# --- Safety --------------------------------------------------------------------------------


def test_a_billion_laughs_file_is_refused_quickly(tmp_path: Path) -> None:
    entities = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{n} "{f"&lol{n - 1};" * 10 if n > 1 else "&lol;" * 10}">'
        for n in range(1, 10)
    )
    data = (
        f'<?xml version="1.0"?><!DOCTYPE FictionBook [{entities}]>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section>'
        "<title><p>Laughs</p></title><p>&lol9;</p></section></body></FictionBook>"
    ).encode()
    started = time.monotonic()

    with pytest.raises(InvalidEbookError, match=r"entit"):
        read_fb2(_write(tmp_path, data))

    assert time.monotonic() - started < 5


def test_an_external_entity_is_refused_and_nothing_is_read(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    data = (
        f'<?xml version="1.0"?><!DOCTYPE FictionBook [<!ENTITY x SYSTEM "{secret.as_uri()}">]>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section>'
        "<p>&x;</p></section></body></FictionBook>"
    ).encode()

    with pytest.raises(InvalidEbookError, match="entit"):
        read_fb2(_write(tmp_path, data))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not xml at all", "well-formed"),
        (b"<FictionBook><body><section><p>unclosed", "well-formed"),
        (b"<html><body><p>Hello</p></body></html>", "isn't an FB2 book"),
        (b"", "well-formed"),
    ],
)
def test_files_that_are_not_fb2_are_refused(tmp_path: Path, data: bytes, message: str) -> None:
    with pytest.raises(InvalidEbookError, match=message):
        read_fb2(_write(tmp_path, data))


def test_a_book_with_no_text_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidEbookError, match="no readable text"):
        read_fb2(_write(tmp_path, fb2_document("<section><empty-line/></section>")))


def test_a_file_too_large_to_read_safely_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tomecast.readers import _markup

    monkeypatch.setattr(_markup, "MAX_FILE_BYTES", 1_000)
    path = _write(tmp_path, fb2_document(fb2_sections([("Big", ["word " * 1_000])])))

    with pytest.raises(InvalidEbookError, match="too large"):
        read_fb2(path)
