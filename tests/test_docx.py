"""Tests for the DOCX reader. Every document is built on the fly by ``format_factory``."""

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Inches
from format_factory import (
    ALICE_1,
    ALICE_2,
    ALICE_3,
    CHAPTERS,
    ISHMAEL,
    build_docx,
    expected_source_text,
    paragraphs_of,
    png,
    sentences_of,
    spoken_sentences,
    titles_of,
    zip_bomb,
)

from tomecast.readers import InvalidEbookError, docx, read_docx


def _chapter_items() -> list[tuple[str, str]]:
    return [
        item
        for title, paragraphs in CHAPTERS
        for item in [("Heading 1", title), *(("Normal", text) for text in paragraphs)]
    ]


def test_heading_1_starts_chapters(tmp_path: Path) -> None:
    items = [("Normal", "Before any heading."), *_chapter_items()]
    book = read_docx(build_docx(tmp_path / "book.docx", items))

    assert titles_of(book) == ["", "Down the Rabbit-Hole", "The Pool of Tears"]
    assert paragraphs_of(book) == [
        ["Before any heading."],
        [ALICE_1, ALICE_2],
        [ALICE_3, ISHMAEL],
    ]


def test_heading_2_starts_chapters_only_when_there_is_no_heading_1(tmp_path: Path) -> None:
    without_h1 = [
        ("Heading 2", "One"),
        ("Normal", "First."),
        ("Heading 2", "Two"),
        ("Normal", "Second."),
    ]
    with_h1 = [("Heading 1", "Part"), ("Heading 2", "Section"), ("Normal", "Text.")]

    book = read_docx(build_docx(tmp_path / "a.docx", without_h1))
    assert titles_of(book) == ["One", "Two"]
    assert paragraphs_of(book) == [["First."], ["Second."]]

    book = read_docx(build_docx(tmp_path / "b.docx", with_h1))
    assert titles_of(book) == ["Part"]
    assert paragraphs_of(book) == [["Section", "Text."]]  # nothing is dropped


def test_a_title_paragraph_starts_a_chapter(tmp_path: Path) -> None:
    items = [("Title", "Alice"), ("Normal", "Once."), ("Heading 1", "One"), ("Normal", "Twice.")]

    book = read_docx(build_docx(tmp_path / "book.docx", items))

    assert titles_of(book) == ["Alice", "One"]
    assert paragraphs_of(book) == [["Once."], ["Twice."]]


def test_table_text_is_kept_and_headers_and_footers_are_skipped(tmp_path: Path) -> None:
    items = [
        ("Heading 1", "Data"),
        ("Normal", "Before the table."),
        ("table", "Name|Age/Alice|7"),
        ("Normal", "After the table."),
    ]
    path = build_docx(tmp_path / "book.docx", items, header="RUNNING HEAD", footer="PAGE FOOT")

    book = read_docx(path)

    assert paragraphs_of(book) == [
        ["Before the table.", "Name", "Age", "Alice", "7", "After the table."]
    ]
    assert "RUNNING HEAD" not in str(book) and "PAGE FOOT" not in str(book)


def _append_xml(paragraph: Any, xml: str) -> None:
    paragraph._p.append(parse_xml(xml))


def test_tracked_insertions_and_text_boxes_are_read_but_deleted_text_is_not(
    tmp_path: Path,
) -> None:
    path = build_docx(tmp_path / "book.docx", [("Normal", "Plain.")])
    document = Document(str(path))
    namespaces = (
        f"{nsdecls('w')} xmlns:v='urn:schemas-microsoft-com:vml' "
        "xmlns:mc='http://schemas.openxmlformats.org/markup-compatibility/2006'"
    )
    paragraph = document.add_paragraph()
    _append_xml(
        paragraph,
        f"<w:ins {nsdecls('w')} w:id='1' w:author='a' w:date='2020-01-01T00:00:00Z'>"
        "<w:r><w:t>Inserted text.</w:t></w:r></w:ins>",
    )
    _append_xml(
        paragraph,
        f"<w:del {nsdecls('w')} w:id='2' w:author='a' w:date='2020-01-01T00:00:00Z'>"
        "<w:r><w:delText>Deleted text.</w:delText></w:r></w:del>",
    )
    box = document.add_paragraph()
    _append_xml(
        box,
        f"<w:r {namespaces}><mc:AlternateContent>"
        "<mc:Choice Requires='wps'><w:txbxContent><w:p><w:r><w:t>Boxed words.</w:t></w:r></w:p>"
        "</w:txbxContent></mc:Choice>"
        "<mc:Fallback><w:pict><v:textbox><w:txbxContent><w:p><w:r><w:t>Boxed words.</w:t></w:r>"
        "</w:p></w:txbxContent></v:textbox></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>",
    )
    document.save(str(path))

    book = read_docx(path)

    assert paragraphs_of(book) == [["Plain.", "Inserted text.", "Boxed words."]]


def test_title_authors_and_language_come_from_the_core_properties(tmp_path: Path) -> None:
    path = build_docx(
        tmp_path / "file name.docx",
        [("Normal", "Text.")],
        title="Alice in Wonderland",
        author="Lewis Carroll; C. L. Dodgson",
    )

    book = read_docx(path)

    assert book.title == "Alice in Wonderland"
    assert book.authors == ("Lewis Carroll", "C. L. Dodgson")


def test_a_document_without_properties_is_titled_by_its_file_name(tmp_path: Path) -> None:
    document = Document()
    document.core_properties.author = ""
    document.core_properties.title = ""
    document.add_paragraph("Text.")
    document.save(str(tmp_path / "my story.docx"))

    book = read_docx(tmp_path / "my story.docx")

    assert book.title == "my story"
    assert book.authors == ()


def test_the_first_inline_picture_is_the_cover_if_it_looks_like_one(tmp_path: Path) -> None:
    image = png(30, 40)
    items = [("Heading 1", "One"), ("Normal", "Text.")]

    book = read_docx(build_docx(tmp_path / "a.docx", items, picture=(image, 3.0, 4.0)))

    assert book.cover is not None
    assert book.cover.media_type == "image/png"
    assert book.cover.data == image


def test_a_picture_that_does_not_look_like_a_cover_is_ignored(tmp_path: Path) -> None:
    items = [("Normal", "Text.")]
    banner = build_docx(tmp_path / "a.docx", items, picture=(png(40, 10), 6.0, 1.5))
    icon = build_docx(tmp_path / "b.docx", items, picture=(png(8, 8), 0.3, 0.3))
    none = build_docx(tmp_path / "c.docx", items)

    assert read_docx(banner).cover is None  # wider than tall
    assert read_docx(icon).cover is None  # smaller than an inch
    assert read_docx(none).cover is None


def test_a_picture_after_the_text_has_started_is_not_a_cover(tmp_path: Path) -> None:
    path = build_docx(tmp_path / "book.docx", [("Normal", ALICE_1)])
    document = Document(str(path))
    document.add_paragraph().add_run().add_picture(io.BytesIO(png(30, 40)), width=Inches(2))
    document.save(str(path))

    assert read_docx(path).cover is None


def test_every_sentence_of_the_source_is_in_the_book(tmp_path: Path) -> None:
    book = read_docx(build_docx(tmp_path / "book.docx", _chapter_items()))

    missing = set(sentences_of(expected_source_text())) - spoken_sentences(book)

    assert not missing


# --- Safety --------------------------------------------------------------------------------


def test_a_zip_bomb_is_refused_before_it_is_expanded(tmp_path: Path) -> None:
    path = zip_bomb(tmp_path / "bomb.docx", member="word/document.xml", size=70 * 1024 * 1024)
    assert path.stat().st_size < 1024 * 1024  # a small file that claims to be huge

    with pytest.raises(InvalidEbookError, match=r"too large to read safely"):
        read_docx(path)


def test_too_much_data_in_total_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docx, "MAX_TOTAL_BYTES", 100_000)
    path = build_docx(tmp_path / "book.docx", [("Normal", "Text.")])
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("customXml/big.xml", b"x" * 200_000)

    with pytest.raises(InvalidEbookError, match="too large"):
        read_docx(path)


@pytest.mark.parametrize("content", [b"not a zip", b""])
def test_a_file_that_is_not_a_zip_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(content)

    with pytest.raises(InvalidEbookError, match="isn't a DOCX file"):
        read_docx(path)


def test_a_zip_that_is_not_a_docx_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("notes.txt", "hello")

    with pytest.raises(InvalidEbookError, match="isn't a DOCX file"):
        read_docx(path)


def test_a_document_with_no_text_is_refused(tmp_path: Path) -> None:
    path = build_docx(tmp_path / "empty.docx", [("Normal", "   ")])

    with pytest.raises(InvalidEbookError, match="no readable text"):
        read_docx(path)
