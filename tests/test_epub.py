"""Tests for the EPUB reader. Every book here is built on the fly by ``epub_factory``."""

import zipfile
from pathlib import Path
from typing import Literal

import pytest
from epub_factory import (
    XHTML_1_1_DOCTYPE,
    NavPoint,
    Page,
    build_epub,
    tiny_png,
    without_file,
    xhtml,
)

from tomecast.book import Book, Chapter, Cover
from tomecast.readers import InvalidEbookError, epub, read_epub


def spoken_text(book: Book) -> list[str]:
    """Everything that will be voiced, in order: each chapter's title, then its paragraphs."""
    return [text for chapter in book.chapters for text in chapter.spoken_paragraphs()]


def all_paragraphs(book: Book) -> list[str]:
    return [paragraph for chapter in book.chapters for paragraph in chapter.paragraphs]


# --- The core guarantee --------------------------------------------------------------------


def test_no_text_is_lost_across_a_multi_file_book(tmp_path: Path) -> None:
    """Every paragraph of every file reaches the Book, in order, including the last paragraph
    of the last file and the text after the last page break."""
    expected: list[str] = []
    pages: list[Page] = []
    toc: list[NavPoint] = []
    for file_number in range(1, 5):
        name = f"part{file_number}.xhtml"
        heading = f"Chapter {file_number}"
        toc.append(NavPoint(heading, name))
        expected.append(heading)  # spoken once, as the chapter title
        body = [f"<h2>{heading}</h2>"]
        for number in range(1, 31):
            text = f"Paragraph {number} of file {file_number}."
            if number == 30:
                text = f"Paragraph {number} of file {file_number} ends without a period"
            expected.append(text)
            if number % 7 == 0:  # a page break in the middle of a word
                marker = f'<span epub:type="pagebreak" id="page{file_number}-{number}"/>'
                text = text[:6] + marker + text[6:]
            body.append(f"<p>{text}</p>")
        pages.append(Page(name, "\n".join(body)))

    last = pages[-1]
    pages[-1] = Page(
        last.name,
        last.body + '<div epub:type="pagebreak" id="last-page"></div>'
        "<p>Text after the last page break</p>"
        "Loose text at the very end of the book",
    )
    expected += ["Text after the last page break", "Loose text at the very end of the book"]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, toc=toc))

    assert spoken_text(book) == expected
    assert book.chapters[-1].paragraphs[-3:] == (
        "Paragraph 30 of file 4 ends without a period",
        "Text after the last page break",
        "Loose text at the very end of the book",
    )


# --- Table of contents and chapters --------------------------------------------------------


def test_epub3_nav_titles_chapters_and_flattens_nested_lists(tmp_path: Path) -> None:
    pages = [
        Page("part1.xhtml", "<h1>Part One</h1>"),
        Page("chapter1.xhtml", "<h2>Chapter 1</h2><p>It begins.</p>"),
        Page("chapter2.xhtml", "<h2>Two</h2><p>It goes on.</p>"),
    ]
    toc = [
        NavPoint(
            "Part One",
            "part1.xhtml",
            children=[
                NavPoint("Chapter 1", "chapter1.xhtml"),
                NavPoint("The Second", "chapter2.xhtml"),
            ],
        )
    ]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, toc=toc))

    assert book.chapters == (
        Chapter("Part One", ()),  # a title-only chapter is kept, so the title is spoken
        Chapter("Chapter 1", ("It begins.",)),
        Chapter("The Second", ("Two", "It goes on.")),  # the TOC label, not the heading
    )


def test_epub2_ncx_titles_chapters(tmp_path: Path) -> None:
    pages = [
        Page("front.xhtml", "<p>Front matter.</p>"),
        Page("one.xhtml", "<h1>One</h1><p>First.</p>"),
        Page("two.xhtml", "<h1>Two</h1><p>Second.</p>"),
    ]
    toc = [
        NavPoint("Book  One", "front.xhtml", children=[NavPoint("One", "one.xhtml")]),
        NavPoint("Two", "two.xhtml"),
    ]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, version=2, toc=toc))

    assert book.chapters == (
        Chapter("Book One", ("Front matter.",)),
        Chapter("One", ("First.",)),
        Chapter("Two", ("Second.",)),
    )


def test_fragments_split_one_file_into_chapters(tmp_path: Path) -> None:
    body = """
        <p>An epigraph before any chapter.</p>
        <h2 id="one">One</h2>
        <p>First.</p>
        <p><a id="two"></a>The second chapter starts at an anchor.</p>
        <a name="three"></a><h2>Three</h2>
        <p>Third.</p>
        <span epub:type="pagebreak" id="page9"/><h2>Four</h2>
        <p>Fourth.</p>
    """
    toc = [
        NavPoint("One", "book.xhtml#one"),
        NavPoint("Two", "book.xhtml#two"),
        NavPoint("Three", "book.xhtml#three"),
        NavPoint("Four", "book.xhtml#page9"),  # a skipped page-break marker can be a target
    ]

    book = read_epub(build_epub(tmp_path / "book.epub", [Page("book.xhtml", body)], toc=toc))

    assert book.chapters == (
        Chapter("", ("An epigraph before any chapter.",)),
        Chapter("One", ("First.",)),
        Chapter("Two", ("The second chapter starts at an anchor.",)),
        Chapter("Three", ("Third.",)),
        Chapter("Four", ("Fourth.",)),
    )


def test_a_file_without_a_toc_entry_continues_the_current_chapter(tmp_path: Path) -> None:
    pages = [
        Page("one.xhtml", "<p>Start of chapter one.</p>"),
        Page("one-continued.xhtml", "<p>More of chapter one.</p>"),
        Page("two.xhtml", "<p>Chapter two.</p>"),
    ]
    toc = [NavPoint("One", "one.xhtml"), NavPoint("Two", "two.xhtml")]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, toc=toc))

    assert book.chapters == (
        Chapter("One", ("Start of chapter one.", "More of chapter one.")),
        Chapter("Two", ("Chapter two.",)),
    )


def test_a_fragment_the_file_lacks_starts_the_chapter_at_the_top(tmp_path: Path) -> None:
    pages = [Page("one.xhtml", "<p>One.</p>"), Page("two.xhtml", "<p>Two.</p>")]
    toc = [NavPoint("One", "one.xhtml"), NavPoint("Two", "two.xhtml#no-such-id")]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, toc=toc))

    assert book.chapters == (Chapter("One", ("One.",)), Chapter("Two", ("Two.",)))


def test_linear_no_documents_are_skipped(tmp_path: Path) -> None:
    pages = [
        Page("one.xhtml", "<p>Read me.</p>"),
        Page("notes.xhtml", "<p>An out-of-line footnote page.</p>", linear=False),
        Page("two.xhtml", "<p>Read me too.</p>"),
    ]

    book = read_epub(build_epub(tmp_path / "book.epub", pages))

    assert all_paragraphs(book) == ["Read me.", "Read me too."]


@pytest.mark.parametrize("version", [2, 3])
def test_missing_toc_makes_each_file_a_chapter_titled_by_its_first_heading(
    tmp_path: Path, version: Literal[2, 3]
) -> None:
    pages = [
        Page("cover.xhtml", '<div><img src="cover.png" alt="Cover"/></div>'),
        Page("one.xhtml", "<h1>Alpha</h1><p>A.</p>"),
        Page("two.xhtml", "<p>No heading here.</p>"),
    ]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, version=version))

    assert book.chapters == (Chapter("Alpha", ("A.",)), Chapter("", ("No heading here.",)))


def test_toc_that_matches_nothing_falls_back_to_one_chapter_per_file(tmp_path: Path) -> None:
    pages = [Page("one.xhtml", "<h1>Alpha</h1><p>A.</p>"), Page("two.xhtml", "<h1>Beta</h1>")]
    toc = [NavPoint("Ghost", "missing.xhtml")]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, toc=toc))

    assert book.chapters == (Chapter("Alpha", ("A.",)), Chapter("Beta", ()))


def test_headings_that_repeat_the_title_are_dropped(tmp_path: Path) -> None:
    chapters = [
        # Case, punctuation and roman numerals don't matter.
        ("Chapter I. Loomings", "<h2>CHAPTER 1. Loomings.</h2><p>Call me Ishmael.</p>"),
        # The label contains the heading.
        ("Chapter II. The Carpet-Bag", "<h2>The Carpet-Bag</h2><p>I stuffed a shirt.</p>"),
        # Two leading headings, both part of the label.
        ("Chapter 3: The Spouter-Inn", "<h2>Chapter 3</h2><h3>The Spouter-Inn</h3><p>Inn.</p>"),
        # Different words: kept.
        ("Chapter 4", "<h2>The Counterpane</h2><p>Upon waking.</p>"),
        # Whole words only: "Chapter 1" is not part of "Chapter 10".
        ("Chapter 10", "<h2>Chapter 1</h2><p>A mislabelled chapter.</p>"),
        # Not a heading element: kept.
        ("Chapter 5", "<p>Chapter 5</p><p>Breakfast.</p>"),
    ]
    pages = [Page(f"c{n}.xhtml", body) for n, (_, body) in enumerate(chapters)]
    toc = [NavPoint(label, f"c{n}.xhtml") for n, (label, _) in enumerate(chapters)]

    book = read_epub(build_epub(tmp_path / "book.epub", pages, toc=toc))

    assert book.chapters == (
        Chapter("Chapter I. Loomings", ("Call me Ishmael.",)),
        Chapter("Chapter II. The Carpet-Bag", ("I stuffed a shirt.",)),
        Chapter("Chapter 3: The Spouter-Inn", ("Inn.",)),
        Chapter("Chapter 4", ("The Counterpane", "Upon waking.")),
        Chapter("Chapter 10", ("Chapter 1", "A mislabelled chapter.")),
        Chapter("Chapter 5", ("Chapter 5", "Breakfast.")),
    )


# --- Paragraphs ----------------------------------------------------------------------------


def test_nested_blocks_are_read_once_and_loose_text_is_kept(tmp_path: Path) -> None:
    body = """
        <blockquote><p>Quoted one.</p><p>Quoted <em>two</em>.</p></blockquote>
        <ul>
          <li>Item one</li>
          <li><p>Item two</p></li>
          <li>Item three<ul><li>Nested item</li></ul></li>
        </ul>
        <div>Loose text <span>in a div</span><p>Inner paragraph.</p>and loose text after.</div>
        <div><div><p>Deeply nested.</p></div></div>
        <table><tr><td>Cell A</td><td>Cell <b>B</b></td></tr></table>
        <dl><dt>Term</dt><dd>Definition</dd></dl>
        <pre>Preformatted
            text</pre>
        <p>Line one<br/>line two, with <a href="#x">a link</a> and <i>it</i>alics.</p>
        <p>   </p>
    """

    book = read_epub(build_epub(tmp_path / "book.epub", [Page("book.xhtml", body)]))

    assert all_paragraphs(book) == [
        "Quoted one.",
        "Quoted two.",
        "Item one",
        "Item two",
        "Item three",
        "Nested item",
        "Loose text in a div",
        "Inner paragraph.",
        "and loose text after.",
        "Deeply nested.",
        "Cell A",
        "Cell B",
        "Term",
        "Definition",
        "Preformatted text",
        "Line one line two, with a link and italics.",
    ]


def test_head_scripts_styles_and_alt_text_are_never_read(tmp_path: Path) -> None:
    body = """
        <p>Visible text.<script>var hidden = "script";</script></p>
        <style>p { color: red; }</style>
        <p><img src="map.png" alt="Alt text of a map"/></p>
        <p>After the image.</p>
    """

    book = read_epub(build_epub(tmp_path / "book.epub", [Page("book.xhtml", body)]))

    assert all_paragraphs(book) == ["Visible text.", "After the image."]


def test_page_numbers_and_gutenberg_boilerplate_are_skipped(tmp_path: Path) -> None:
    body = """
        <section id="pg-header" class="pg-boilerplate pgheader">
          <h2>The Project Gutenberg eBook of A Test</h2>
          <p>This ebook is for the use of anyone anywhere.</p>
        </section>
        <p>Before the break <span epub:type="pagebreak" id="page2" title="2"/>and after it.</p>
        <p>Old style<span class="pagenum"><a id="Page_3">[Pg 3]</a></span> page number.</p>
        <div epub:type="pagebreak" id="page4">4</div>
        <p>Accessible <span role="doc-pagebreak" aria-label="5">5</span>break.</p>
        <div class="pg-boilerplate"><p>More license text.</p></div>
        <section id="pg-footer"><p>*** END OF THE PROJECT GUTENBERG EBOOK ***</p></section>
    """

    book = read_epub(build_epub(tmp_path / "book.epub", [Page("book.xhtml", body)]))

    assert all_paragraphs(book) == [
        "Before the break and after it.",
        "Old style page number.",
        "Accessible break.",
    ]


def test_toc_entries_pointing_into_skipped_boilerplate_start_no_chapter(tmp_path: Path) -> None:
    body = """
        <h1 id="start">The Story</h1>
        <p>The end.</p>
        <section id="pg-footer"><h2 id="license">The Full Project Gutenberg License</h2></section>
    """
    toc = [NavPoint("The Story", "book.xhtml#start"), NavPoint("License", "book.xhtml#license")]

    book = read_epub(build_epub(tmp_path / "book.epub", [Page("book.xhtml", body)], toc=toc))

    assert book.chapters == (Chapter("The Story", ("The end.",)),)


@pytest.mark.parametrize(
    ("doctype", "declaration"),
    [
        pytest.param("", True, id="no-dtd-so-xml-fails"),
        pytest.param(XHTML_1_1_DOCTYPE, True, id="unloaded-dtd-leaves-entities"),
        pytest.param("", False, id="no-xml-declaration"),
    ],
)
def test_html_entities_fall_back_to_the_html_parser(
    tmp_path: Path, doctype: str, declaration: bool
) -> None:
    body = """
        <h1 id="start">Caf&eacute;</h1>
        <p>One&nbsp;two&mdash;three <span epub:type="pagebreak" id="p2"/>after the break.</p>
        <p>Déjà vu, written as UTF-8.</p>
    """
    source = xhtml(body, doctype=doctype, declaration=declaration)
    toc = [NavPoint("Café", "book.xhtml#start")]

    book = read_epub(
        build_epub(tmp_path / "book.epub", [Page("book.xhtml", source=source)], toc=toc)
    )

    assert book.chapters == (
        Chapter(
            "Café",
            ("One two—three after the break.", "Déjà vu, written as UTF-8."),
        ),
    )


def test_url_encoded_and_parent_relative_hrefs_resolve(tmp_path: Path) -> None:
    pages = [
        Page("Text/Chapter One.xhtml", "<p>First.</p>"),
        # Outside the package folder: the package document says "../Extra/Chapter%20Two.xhtml".
        Page("../Extra/Chapter Two.xhtml", '<h1 id="top">Two</h1><p>Second.</p>'),
    ]
    toc = [  # relative to the nav document, which sits in OEBPS/Nav/
        NavPoint("One", "../Text/Chapter%20One.xhtml"),
        NavPoint("Two", "../../Extra/Chapter%20Two.xhtml#top"),
    ]

    path = build_epub(tmp_path / "book.epub", pages, toc=toc, nav_name="Nav/nav.xhtml")
    book = read_epub(path)

    assert "Extra/Chapter Two.xhtml" in zipfile.ZipFile(path).namelist()
    assert book.chapters == (Chapter("One", ("First.",)), Chapter("Two", ("Second.",)))


# --- Metadata and cover --------------------------------------------------------------------


def test_metadata_with_two_authors(tmp_path: Path) -> None:
    path = build_epub(
        tmp_path / "book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        title="  Moby-Dick;\n   or, The Whale ",
        authors=["Herman  Melville", "A.\nN. Other"],
        language="en-US",
    )

    book = read_epub(path)

    assert book.title == "Moby-Dick; or, The Whale"
    assert book.authors == ("Herman Melville", "A. N. Other")
    assert book.language == "en-US"


def test_missing_title_falls_back_to_the_file_name(tmp_path: Path) -> None:
    path = build_epub(
        tmp_path / "my-book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        title=None,
        authors=[],
        language=None,
    )

    book = read_epub(path)

    assert (book.title, book.authors, book.language) == ("my-book", (), None)


@pytest.mark.parametrize("version", [2, 3])
def test_cover_image_is_extracted(tmp_path: Path, version: Literal[2, 3]) -> None:
    png = tiny_png()
    path = build_epub(
        tmp_path / "book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        version=version,
        cover=png,
    )

    assert read_epub(path).cover == Cover(png, "image/png")


def test_cover_type_comes_from_the_image_bytes(tmp_path: Path) -> None:
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(16)
    path = build_epub(
        tmp_path / "book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        cover=jpeg,
        cover_media_type="image/jpg",  # a common mistake for image/jpeg
    )

    assert read_epub(path).cover == Cover(jpeg, "image/jpeg")


def test_unsupported_cover_formats_are_ignored(tmp_path: Path) -> None:
    path = build_epub(
        tmp_path / "book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        cover=b"GIF89a\x01\x00\x01\x00",
        cover_media_type="image/gif",
    )

    book = read_epub(path)

    assert book.cover is None
    assert all_paragraphs(book) == ["Text."]


# --- Files that can't be read --------------------------------------------------------------


def test_a_file_that_is_not_a_zip_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    path.write_text("This is plain text, not an EPUB.")

    with pytest.raises(InvalidEbookError, match="not a zip"):
        read_epub(path)


def test_a_zip_without_container_xml_is_invalid(tmp_path: Path) -> None:
    path = build_epub(tmp_path / "book.epub", [Page("book.xhtml", "<p>Text.</p>")])
    without_file(path, "META-INF/container.xml")

    with pytest.raises(InvalidEbookError, match=r"container\.xml"):
        read_epub(path)


def test_a_missing_package_document_is_invalid(tmp_path: Path) -> None:
    path = build_epub(tmp_path / "book.epub", [Page("book.xhtml", "<p>Text.</p>")])
    without_file(path, "OEBPS/content.opf")

    with pytest.raises(InvalidEbookError, match="package document"):
        read_epub(path)


def test_a_missing_reading_order_file_fails_loudly(tmp_path: Path) -> None:
    pages = [Page("one.xhtml", "<p>One.</p>"), Page("two.xhtml", "<p>Two.</p>")]
    path = without_file(build_epub(tmp_path / "book.epub", pages), "OEBPS/two.xhtml")

    with pytest.raises(InvalidEbookError, match=r"missing OEBPS/two\.xhtml"):
        read_epub(path)


def test_a_book_with_no_text_is_invalid(tmp_path: Path) -> None:
    pages = [Page("cover.xhtml", '<p><img src="cover.png" alt="Cover"/></p>')]

    with pytest.raises(InvalidEbookError, match="no readable text"):
        read_epub(build_epub(tmp_path / "book.epub", pages))


def test_drm_protected_books_are_refused(tmp_path: Path) -> None:
    encryption = b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/book.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""
    path = build_epub(
        tmp_path / "book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        extra_files={"META-INF/encryption.xml": encryption},
    )

    with pytest.raises(InvalidEbookError, match="DRM"):
        read_epub(path)


def test_obfuscated_fonts_are_not_mistaken_for_drm(tmp_path: Path) -> None:
    encryption = b"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/fonts/serif.otf"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""
    path = build_epub(
        tmp_path / "book.epub",
        [Page("book.xhtml", "<p>Text.</p>")],
        extra_files={"META-INF/encryption.xml": encryption},
    )

    assert all_paragraphs(read_epub(path)) == ["Text."]


def test_a_file_too_large_to_read_safely_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(epub, "MAX_MEMBER_BYTES", 4_000)
    pages = [Page("small.xhtml", "<p>Small.</p>"), Page("big.xhtml", f"<p>{'word ' * 2_000}</p>")]

    with pytest.raises(InvalidEbookError, match=r"big\.xhtml is too large"):
        read_epub(build_epub(tmp_path / "book.epub", pages))


def test_too_much_data_in_total_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epub, "MAX_TOTAL_BYTES", 20_000)
    pages = [Page(f"part{n}.xhtml", f"<p>{'word ' * 1_000}</p>") for n in range(10)]

    with pytest.raises(InvalidEbookError, match="too large"):
        read_epub(build_epub(tmp_path / "book.epub", pages))
