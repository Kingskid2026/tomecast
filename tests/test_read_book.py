"""Tests for ``read_book``, the one entry point that picks the reader for a file."""

import zipfile
from pathlib import Path

import pytest
from epub_factory import NavPoint, Page, build_epub
from format_factory import (
    CHAPTERS,
    book_in_format,
    build_docx,
    expected_source_text,
    fb2_document,
    fb2_sections,
    sentences_of,
    spoken_sentences,
    titles_of,
)

from tomecast import readers
from tomecast.readers import FORMATS, SUFFIXES, InvalidEbookError, read_book

QML_DROP_SCREEN = Path(readers.__file__).parents[1] / "gui" / "qml" / "screens" / "DropScreen.qml"


def test_formats_are_listed_in_display_order() -> None:
    assert FORMATS == ("EPUB", "PDF", "DOCX", "FB2", "HTML", "TXT")
    assert SUFFIXES == (".epub", ".pdf", ".docx", ".fb2", ".html", ".htm", ".xhtml", ".txt")


def test_the_file_dialog_offers_every_supported_suffix() -> None:
    qml = QML_DROP_SCREEN.read_text()

    missing = [suffix for suffix in SUFFIXES if f"*{suffix}" not in qml]

    assert not missing, f"add {missing} to the file dialog's filter in DropScreen.qml"


@pytest.mark.parametrize("name", ["book.docx", "book.fb2", "book.txt", "book.html", "BOOK.HTM"])
def test_each_format_is_read_by_its_suffix(tmp_path: Path, name: str) -> None:
    book = read_book(book_in_format(tmp_path, name.lower()).rename(tmp_path / name))

    assert titles_of(book) == ["Chapter 1", "Chapter 2"]
    assert [c.paragraphs for c in book.chapters] == [("It began.",), ("It ended.",)]


def test_an_epub_is_read(tmp_path: Path) -> None:
    path = build_epub(
        tmp_path / "book.epub",
        [Page("one.xhtml", "<h1>One</h1><p>Text.</p>")],
        toc=[NavPoint("One", "one.xhtml")],
    )

    assert titles_of(read_book(path)) == ["One"]


def test_the_reader_is_picked_by_content_when_the_suffix_is_wrong(tmp_path: Path) -> None:
    epub = build_epub(
        tmp_path / "e.epub",
        [Page("one.xhtml", "<p>An EPUB.</p>")],
        toc=[NavPoint("EPUB chapter", "one.xhtml")],
    )
    docx = book_in_format(tmp_path, "d.docx")
    fb2 = book_in_format(tmp_path, "f.fb2")

    for source, wrong_name, expected in [
        (epub, "epub-called.docx", ["EPUB chapter"]),
        (epub, "epub-called.txt", ["EPUB chapter"]),
        (docx, "docx-called.epub", ["Chapter 1", "Chapter 2"]),
        (docx, "docx-called.html", ["Chapter 1", "Chapter 2"]),
        (docx, "docx-without-suffix", ["Chapter 1", "Chapter 2"]),
        (fb2, "fb2-called.txt", ["Chapter 1", "Chapter 2"]),
        (fb2, "fb2-called.html", ["Chapter 1", "Chapter 2"]),
    ]:
        renamed = tmp_path / wrong_name
        renamed.write_bytes(source.read_bytes())

        assert titles_of(read_book(renamed)) == expected, wrong_name


def test_a_fb2_with_a_long_preamble_is_still_recognized(tmp_path: Path) -> None:
    data = fb2_document(fb2_sections([("One", ["Text."])]))
    padded = data.replace(b"<FictionBook", b"<!-- " + b"x" * 500 + b" -->\n<FictionBook", 1)
    path = tmp_path / "padded.txt"
    path.write_bytes(padded)

    assert titles_of(read_book(path)) == ["One"]


def test_content_that_is_not_recognizable_falls_back_to_the_suffix(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Just some notes.\n")
    broken_epub = tmp_path / "broken.epub"
    broken_epub.write_bytes(b"not a zip file")

    assert read_book(path).chapters[0].paragraphs == ("Just some notes.",)
    with pytest.raises(InvalidEbookError, match="isn't an EPUB"):
        read_book(broken_epub)


def test_a_zip_that_is_neither_epub_nor_docx_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "hello")

    with pytest.raises(InvalidEbookError, match=r"can't read \.zip files"):
        read_book(path)
    renamed = path.rename(tmp_path / "archive.docx")
    with pytest.raises(InvalidEbookError, match="isn't a DOCX file"):
        read_book(renamed)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("book.azw3", ".azw3 files"),
        ("book.mobi", ".mobi files"),
        ("book", "files without an extension"),
    ],
)
def test_unsupported_files_are_refused_with_a_clear_sentence(
    tmp_path: Path, name: str, kind: str
) -> None:
    path = tmp_path / name
    path.write_bytes(b"BOOKMOBI something")

    with pytest.raises(InvalidEbookError) as error:
        read_book(path)

    assert (
        str(error.value)
        == f"Tomecast can't read {kind}. It reads EPUB, PDF, DOCX, FB2, HTML and TXT books."
    )


def test_a_missing_file_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_book(tmp_path / "missing.docx")


def test_no_text_is_lost_in_any_format(tmp_path: Path) -> None:
    """The same book in every format: every sentence of the source reaches the Book."""
    from html import escape

    docx_items = [
        item
        for title, paragraphs in CHAPTERS
        for item in [("Heading 1", title), *(("Normal", p) for p in paragraphs)]
    ]
    books = {
        "book.docx": build_docx(tmp_path / "book.docx", docx_items),
        "book.fb2": _write(tmp_path / "book.fb2", fb2_document(fb2_sections(CHAPTERS))),
        "book.txt": _write(
            tmp_path / "book.txt",
            "\n\n".join(
                f"Chapter {i}. {t}\n\n" + "\n\n".join(ps) for i, (t, ps) in enumerate(CHAPTERS, 1)
            ),
        ),
        "book.html": _write(
            tmp_path / "book.html",
            "<html><body>"
            + "".join(
                f"<h1>{escape(t)}</h1>" + "".join(f"<p>{escape(p)}</p>" for p in ps)
                for t, ps in CHAPTERS
            )
            + "</body></html>",
        ),
    }
    expected = set(sentences_of(expected_source_text()))

    for name, path in books.items():
        spoken = spoken_sentences(read_book(path))
        assert expected <= spoken, (name, sorted(expected - spoken))


def _write(path: Path, data: bytes | str) -> Path:
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path
