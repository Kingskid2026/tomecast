"""Tests for the plain-text reader. Every file is built here from public-domain text."""

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

from tomecast.readers import InvalidEbookError, read_txt


def _write(tmp_path: Path, text: str | bytes, name: str = "book.txt") -> Path:
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def _wrap(text: str, width: int = 60) -> str:
    """Hard-wrap a paragraph the way old text files do."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return "\n".join([*lines, line])


@pytest.mark.parametrize(
    "heading",
    [
        "Chapter 12",
        "CHAPTER 12",
        "CHAPTER TWELVE",
        "Chapter twenty-one",
        "Chapter XII",
        "CHAPTER XII.",
        "CHAPTER I. Loomings.",
        "Chapter 3: The Pool of Tears",
        "Chapter 4 - A Caucus-Race",
        "Part One",
        "PART II",
        "Book the First",
        "Section 2",
    ],
)
def test_chapter_lines_start_chapters(tmp_path: Path, heading: str) -> None:
    text = f"{ALICE_1}\n\n{heading}\n\n{ALICE_2}\n"

    book = read_txt(_write(tmp_path, text))

    assert titles_of(book) == ["", heading]
    assert paragraphs_of(book) == [[ALICE_1], [ALICE_2]]


@pytest.mark.parametrize(
    "line",
    [
        "Part of the reason was the weather.",
        "Chapter one of the book said so.",
        "Book mild.",
        "Chapter",
        "Chapter - Untitled",
        "She read Chapter 12 twice.",
    ],
)
def test_ordinary_lines_are_not_chapters(tmp_path: Path, line: str) -> None:
    book = read_txt(_write(tmp_path, f"{ALICE_1}\n\n{line}\n\n{ALICE_2}\n", "story.txt"))

    assert titles_of(book) == ["story"]
    assert len(book.chapters[0].paragraphs) == 3


def test_a_chapter_line_needs_no_blank_lines_around_it(tmp_path: Path) -> None:
    book = read_txt(_write(tmp_path, "Chapter 1\nIt began.\nChapter 2\nIt ended.\n"))

    assert titles_of(book) == ["Chapter 1", "Chapter 2"]
    assert paragraphs_of(book) == [["It began."], ["It ended."]]


def test_bare_roman_numerals_are_chapters_when_there_are_several(tmp_path: Path) -> None:
    text = f"I\n\n{ALICE_1}\n\nII\n\n{ALICE_2}\n\nXII.\n\n{ALICE_3}\n"

    book = read_txt(_write(tmp_path, text))

    assert titles_of(book) == ["I", "II", "XII."]
    assert paragraphs_of(book) == [[ALICE_1], [ALICE_2], [ALICE_3]]


def test_a_single_stray_numeral_is_not_a_chapter(tmp_path: Path) -> None:
    book = read_txt(_write(tmp_path, f"{ALICE_1}\n\nV\n\n{ALICE_2}\n", "story.txt"))

    assert titles_of(book) == ["story"]
    assert paragraphs_of(book) == [[ALICE_1, "V", ALICE_2]]


def test_chapter_lines_win_over_bare_numerals(tmp_path: Path) -> None:
    text = "Chapter 1\n\nI\n\nII\n\nText.\n"

    assert titles_of(read_txt(_write(tmp_path, text))) == ["Chapter 1"]


def test_wrapped_lines_are_rejoined_and_blank_lines_separate_paragraphs(tmp_path: Path) -> None:
    text = f"{_wrap(ALICE_1)}\n\n\n{_wrap(ALICE_2)}\n   \n{_wrap(ISHMAEL, 40)}"

    book = read_txt(_write(tmp_path, text))

    assert paragraphs_of(book) == [[ALICE_1, ALICE_2, ISHMAEL]]


def test_a_file_with_no_chapter_lines_is_one_chapter_titled_by_the_file_name(
    tmp_path: Path,
) -> None:
    book = read_txt(_write(tmp_path, f"{ALICE_1}\n\n{ALICE_2}\n", "alice in wonderland.txt"))

    assert len(book.chapters) == 1
    assert book.chapters[0].title == "alice in wonderland"
    assert book.title == "alice in wonderland"
    assert book.chapters[0].paragraphs == (ALICE_1, ALICE_2)


GUTENBERG = f"""\
The Project Gutenberg eBook of Alice's Adventures in Wonderland

This eBook is for the use of anyone anywhere. You may copy it under the license.

Title: Alice's Adventures in Wonderland

Author: Lewis Carroll

Language: English

*** START OF THE PROJECT GUTENBERG EBOOK ALICE'S ADVENTURES IN WONDERLAND ***

CHAPTER I. Down the Rabbit-Hole

{ALICE_1}

CHAPTER II. The Pool of Tears

{ALICE_3}

*** END OF THE PROJECT GUTENBERG EBOOK ALICE'S ADVENTURES IN WONDERLAND ***

Updated editions will replace the previous one. Donations are welcome.
"""


def test_project_gutenberg_boilerplate_outside_the_markers_is_dropped(tmp_path: Path) -> None:
    book = read_txt(_write(tmp_path, GUTENBERG, "pg11.txt"))

    assert titles_of(book) == ["CHAPTER I. Down the Rabbit-Hole", "CHAPTER II. The Pool of Tears"]
    assert paragraphs_of(book) == [[ALICE_1], [ALICE_3]]
    assert book.title == "Alice's Adventures in Wonderland"
    assert book.authors == ("Lewis Carroll",)
    assert "Donations" not in str(book) and "license" not in str(book)


def test_a_start_marker_alone_drops_only_the_header(tmp_path: Path) -> None:
    text = f"License header.\n\n*** START OF THIS PROJECT GUTENBERG EBOOK X ***\n\n{ALICE_1}\n"

    book = read_txt(_write(tmp_path, text, "x.txt"))

    assert paragraphs_of(book) == [[ALICE_1]]


def test_every_sentence_of_the_source_is_in_the_book(tmp_path: Path) -> None:
    text = "\n\n".join(
        f"Chapter {number}. {title}\n\n" + "\n\n".join(_wrap(p) for p in paragraphs)
        for number, (title, paragraphs) in enumerate(CHAPTERS, start=1)
    )

    book = read_txt(_write(tmp_path, text))

    expected = sentences_of(
        [t for n, (title, ps) in enumerate(CHAPTERS, 1) for t in (f"Chapter {n}. {title}", *ps)]
    )
    assert not set(expected) - spoken_sentences(book)
    assert expected_source_text()  # the shared sample is what the other formats use


# --- Encodings -----------------------------------------------------------------------------


def test_utf8_with_and_without_a_byte_order_mark(tmp_path: Path) -> None:
    text = "Café “quoted” — done.\n"

    plain = read_txt(_write(tmp_path, text.encode("utf-8"), "a.txt"))
    bom = read_txt(_write(tmp_path, b"\xef\xbb\xbf" + text.encode("utf-8"), "b.txt"))

    assert plain.chapters[0].paragraphs == bom.chapters[0].paragraphs == (text.strip(),)


def test_utf16_with_a_byte_order_mark(tmp_path: Path) -> None:
    book = read_txt(_write(tmp_path, ALICE_1.encode("utf-16"), "u.txt"))

    assert book.chapters[0].paragraphs == (ALICE_1,)


def test_windows_1252_text_falls_back_without_crashing(tmp_path: Path) -> None:
    data = "She said “hello” – and left café.".encode("cp1252")

    book = read_txt(_write(tmp_path, data))

    assert book.chapters[0].paragraphs == ("She said “hello” – and left café.",)


def test_bytes_that_fit_no_encoding_still_decode_as_latin_1(tmp_path: Path) -> None:
    # 0x81 and 0x8d are undefined in Windows-1252, so only Latin-1 can read them.
    book = read_txt(_write(tmp_path, b"Odd \x81 byte \xe9 here.\n"))

    assert book.chapters[0].paragraphs == ("Odd \x81 byte \xe9 here.",)


def test_windows_and_old_mac_line_endings(tmp_path: Path) -> None:
    for newline in ("\r\n", "\r"):
        text = newline.join(["Chapter 1", "", "First.", "", "Chapter 2", "", "Second."])

        book = read_txt(_write(tmp_path, text, "n.txt"))

        assert paragraphs_of(book) == [["First."], ["Second."]]


# --- Safety --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        bytes(range(256)) * 40,
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(500),
        b"MZ\x90\x00\x03\x00\x00\x00\x04\x00" + bytes(2000),
        b"\x00" * 100,
    ],
)
def test_a_binary_file_renamed_to_txt_is_refused(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(InvalidEbookError, match="isn't a text file"):
        read_txt(_write(tmp_path, data, "picture.txt"))


@pytest.mark.parametrize("text", ["", "   \n\n \t\n"])
def test_a_file_with_no_text_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(InvalidEbookError, match="no readable text"):
        read_txt(_write(tmp_path, text))


def test_a_file_too_large_to_read_safely_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tomecast.readers import _markup

    monkeypatch.setattr(_markup, "MAX_FILE_BYTES", 1_000)

    with pytest.raises(InvalidEbookError, match="too large"):
        read_txt(_write(tmp_path, "word " * 1_000))
