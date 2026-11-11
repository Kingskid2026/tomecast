"""Build small PDF files for tests, from scratch: text pages, bookmarks, an image, metadata.

Only public-domain text goes into these files (see AGENTS.md). The PDF syntax is written by
hand, so the tests need no PDF library besides the one under test, and every byte is known.

Text uses the standard Times and Helvetica fonts (not embedded; every PDF reader has their
metrics) with WinAnsiEncoding, so text is encoded as Windows-1252. A line is a list of spans;
a span may be raised and smaller, for superscript footnote markers.

The "typeset" fonts set text the way TeX does, for books typeset with ligatures: see
:data:`TYPESET_FONTS`.
"""

from __future__ import annotations

import textwrap
import unicodedata
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# A 6 x 9 inch page, the usual size of a trade paperback, in points.
PAGE_WIDTH = 432.0
PAGE_HEIGHT = 648.0
MARGIN = 54.0
BODY_SIZE = 11.0
LEADING = 14.0

FONTS = {
    "regular": "Times-Roman",
    "bold": "Times-Bold",
    "italic": "Times-Italic",
    "sans": "Helvetica",
}

# Type 3 fonts of plain rectangles, set like a TeX book: "ﬀ", "ﬁ", "ﬂ", "ﬃ" and "ﬄ" in a
# span's text are each drawn as one ligature glyph, as wide as Computer Modern's, and spaces
# are gaps between words, not glyphs. Each font's ToUnicode map says what a ligature glyph
# stands for: its letters ("typeset") or the ligature character ("typeset-forms"). All other
# text in these fonts must be printable ASCII.
TYPESET_FONTS = {"typeset": "letters", "typeset-forms": "forms"}
_TYPESET_LIGATURES = {"\ufb00": (1, 583), "\ufb01": (2, 556), "\ufb02": (3, 556),
                      "\ufb03": (4, 833), "\ufb04": (5, 833)}  # fmt: skip
_TYPESET_LETTER = 500  # every other glyph's width, in thousandths of an em
_TYPESET_SPACE = 333  # the gap between words

_FONT_KEYS = {name: f"F{number}" for number, name in enumerate([*FONTS, *TYPESET_FONTS], start=1)}


@dataclass(frozen=True, slots=True)
class Span:
    """A run of text in one font. ``rise`` lifts it above the line, as superscripts are."""

    text: str
    size: float = BODY_SIZE
    font: str = "regular"
    rise: float = 0.0


@dataclass(frozen=True, slots=True)
class Line:
    """One line of text, drawn from ``(x, y)`` (its baseline) left to right."""

    x: float
    y: float
    spans: tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class Picture:
    """A plain grey image filling a rectangle."""

    x: float
    y: float
    width: float
    height: float


@dataclass(slots=True)
class PdfPage:
    """One page: lines of text and pictures."""

    lines: list[Line] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)
    width: float = PAGE_WIDTH
    height: float = PAGE_HEIGHT


@dataclass(frozen=True, slots=True)
class Bookmark:
    """An outline entry pointing at the top of a page (0-based), with optional children."""

    title: str
    page: int
    children: tuple[Bookmark, ...] = ()
    top: float | None = None  # the y the entry points at; None means the page's top


def spans(text: str, size: float = BODY_SIZE, font: str = "regular") -> tuple[Span, ...]:
    """Split ``text`` into spans, where ``^...^`` marks a superscript (a footnote marker)."""
    parts = text.split("^")
    result: list[Span] = []
    for index, part in enumerate(parts):
        if not part:
            continue
        if index % 2:
            result.append(Span(part, size=round(size * 0.62, 1), font=font, rise=size * 0.36))
        else:
            result.append(Span(part, size=size, font=font))
    return tuple(result)


class PageBuilder:
    """Lays text out on a page from the top down, like a typesetter with fixed line breaks.

    Tests give the exact lines, so they know where every line ends. ``x`` and ``width``
    describe the column being filled.
    """

    def __init__(
        self,
        *,
        x: float = MARGIN,
        width: float = PAGE_WIDTH - 2 * MARGIN,
        top: float = PAGE_HEIGHT - MARGIN,
        size: float = BODY_SIZE,
        leading: float = LEADING,
    ) -> None:
        self.page = PdfPage()
        self.x = x
        self.width = width
        self.y = top - size  # the first baseline
        self.size = size
        self.leading = leading

    def column(self, x: float, width: float, top: float) -> PageBuilder:
        """Move to another column; text continues from its top."""
        self.x, self.width, self.y = x, width, top - self.size
        return self

    def line(
        self,
        text: str,
        *,
        indent: float = 0.0,
        size: float | None = None,
        font: str = "regular",
        y: float | None = None,
        x: float | None = None,
    ) -> PageBuilder:
        """One line at the current position (or at ``y``/``x``), then move down a line."""
        size = size if size is not None else self.size
        at_y = y if y is not None else self.y
        at_x = x if x is not None else self.x + indent
        self.page.lines.append(Line(at_x, at_y, spans(text, size, font)))
        if y is None:
            self.y -= self.leading * size / self.size
        return self

    def paragraph(
        self,
        lines: Sequence[str],
        *,
        indent: float = 18.0,
        size: float | None = None,
        font: str = "regular",
    ) -> PageBuilder:
        """A paragraph whose first line is indented (``indent=0`` for a flush start)."""
        for number, text in enumerate(lines):
            self.line(text, indent=indent if number == 0 else 0.0, size=size, font=font)
        return self

    def heading(self, text: str, *, size: float = 18.0, font: str = "bold") -> PageBuilder:
        """A heading line in a bigger font, with space around it."""
        self.y -= size - self.size
        self.line(text, size=size, font=font)
        self.y -= self.leading
        return self

    def gap(self, points: float) -> PageBuilder:
        self.y -= points
        return self

    def header(self, text: str, *, size: float = 9.0) -> PageBuilder:
        """A running header in the top margin, centred-ish."""
        self.page.lines.append(
            Line(
                self.page.width / 2 - len(text) * size * 0.25,
                self.page.height - 32,
                spans(text, size),
            )
        )
        return self

    def footer(self, text: str, *, size: float = 9.0) -> PageBuilder:
        """A footer (often the page number) in the bottom margin, centred-ish."""
        self.page.lines.append(
            Line(self.page.width / 2 - len(text) * size * 0.25, 30, spans(text, size))
        )
        return self

    def footnotes(self, notes: Sequence[str], *, size: float = 8.5) -> PageBuilder:
        """Footnote lines at the foot of the text block, under the body, in a smaller font."""
        y = MARGIN + 6 + (len(notes) - 1) * size * 1.25
        for text in notes:
            self.page.lines.append(Line(self.x, y, spans(text, size)))
            y -= size * 1.25
        return self

    def picture(self, x: float, y: float, width: float, height: float) -> PageBuilder:
        self.page.pictures.append(Picture(x, y, width, height))
        return self


def wrap(text: str, width: int = 62, *, hyphenate: bool = False) -> list[str]:
    """Break a paragraph into lines of about ``width`` characters (for long sample text).

    With ``hyphenate``, a long word (eight letters or more) that doesn't fit is split over
    two lines with a hyphen, as typesetters do: "Dillingham" becomes "Dillin-" and "gham".
    """
    if not hyphenate:
        return textwrap.wrap(text, width, break_on_hyphens=False)
    lines: list[str] = []
    line = ""
    for word in text.split():
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line = f"{line} {word}"
        elif word.isalpha() and len(word) >= 8 and len(line) + 5 <= width:
            cut = min(len(word) - 3, width - len(line) - 2)
            lines.append(f"{line} {word[:cut]}-")
            line = word[cut:]
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


# --- Writing the file ------------------------------------------------------------------------


def _pdf_string(text: str) -> bytes:
    """A PDF text string: Windows-1252 bytes in hex, or UTF-16BE (with a BOM) if needed."""
    try:
        data = text.encode("cp1252")
    except UnicodeEncodeError:
        data = b"\xfe\xff" + text.encode("utf-16-be")
    return b"<" + data.hex().encode() + b">"


def _typeset_text(text: str) -> bytes:
    """Text in a typeset font, as a TJ array: a ligature character is its glyph's code, and a
    space is a gap the width of a space, as TeX leaves it."""
    words = []
    for word in text.split(" "):
        codes = bytearray()
        for character in word:
            if character in _TYPESET_LIGATURES:
                codes.append(_TYPESET_LIGATURES[character][0])
            elif "!" <= character <= "~":
                codes.append(ord(character))
            else:
                raise ValueError(f"the typeset fonts have no {character!r}")
        words.append(b"<" + codes.hex().encode() + b">")
    return b"[" + f" -{_TYPESET_SPACE} ".encode().join(words) + b"] TJ"


def _typeset_font(objects: _Objects, ligatures: str) -> int:
    """A Type 3 font: printable ASCII and five ligatures, each glyph a filled rectangle with
    a little space on either side, and a ToUnicode map from codes to text."""
    widths = {code: _TYPESET_LETTER for code in range(ord("!"), ord("~") + 1)}
    widths.update(_TYPESET_LIGATURES.values())
    glyphs = {
        width: objects.stream(
            f"{width} 0 20 0 {width - 20} 700 d1 20 0 {width - 40} 700 re f".encode()
        )
        for width in sorted(set(widths.values()))
    }
    codes = sorted(widths)
    first, last = codes[0], codes[-1]
    to_unicode = []
    for character, (code, _) in _TYPESET_LIGATURES.items():
        text = unicodedata.normalize("NFKC", character) if ligatures == "letters" else character
        to_unicode.append(f"<{code:02X}> <{text.encode('utf-16-be').hex().upper()}>\n")
    cmap = objects.stream(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CMapName /Typeset def /CMapType 2 def\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        b"1 beginbfrange <21> <7E> <0021> endbfrange\n"
        + f"{len(to_unicode)} beginbfchar\n{''.join(to_unicode)}endbfchar\n".encode()
        + b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )  # fmt: skip
    differences = " ".join(f"{code} /g{code}" for code in codes)
    char_procs = " ".join(f"/g{code} {glyphs[widths[code]]} 0 R" for code in codes)
    width_list = " ".join(str(widths.get(code, 0)) for code in range(first, last + 1))
    return objects.add(
        f"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1000 700] "
        f"/FontMatrix [0.001 0 0 0.001 0 0] /CharProcs << {char_procs} >> "
        f"/Encoding << /Type /Encoding /Differences [{differences}] >> "
        f"/FirstChar {first} /LastChar {last} /Widths [{width_list}] /Resources << >> "
        f"/ToUnicode {cmap} 0 R >>".encode()
    )


def _content(page: PdfPage) -> bytes:
    out: list[bytes] = []
    for number, picture in enumerate(page.pictures, start=1):
        out.append(
            f"q {picture.width:.2f} 0 0 {picture.height:.2f} {picture.x:.2f} {picture.y:.2f} cm "
            f"/Im{number} Do Q".encode()
        )
    for line in page.lines:
        parts = [f"BT {line.x:.2f} {line.y:.2f} Td".encode()]
        for span in line.spans:
            parts.append(
                f"/{_FONT_KEYS[span.font]} {span.size:.2f} Tf {span.rise:.2f} Ts ".encode()
                + (
                    _typeset_text(span.text)
                    if span.font in TYPESET_FONTS
                    else _pdf_string(span.text) + b" Tj"
                )
            )
        parts.append(b"ET")
        out.append(b" ".join(parts))
    return b"\n".join(out)


class _Objects:
    """PDF objects numbered from 1, written with a cross-reference table."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    def reserve(self) -> int:
        self.bodies.append(b"")
        return len(self.bodies)

    def set(self, number: int, body: bytes) -> None:
        self.bodies[number - 1] = body

    def add(self, body: bytes) -> int:
        number = self.reserve()
        self.set(number, body)
        return number

    def stream(self, data: bytes, extra: bytes = b"", *, compress: bool = True) -> int:
        if compress:
            data = zlib.compress(data)
            extra += b" /Filter /FlateDecode"
        return self.add(
            b"<< /Length " + str(len(data)).encode() + extra + b" >>\nstream\n" + data
            + b"\nendstream"
        )  # fmt: skip

    def write(self, root: int, info: int | None, encrypt: int | None) -> bytes:
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(self.bodies, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.bodies) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        trailer = f"<< /Size {len(self.bodies) + 1} /Root {root} 0 R".encode()
        if info is not None:
            trailer += f" /Info {info} 0 R".encode()
        if encrypt is not None:
            file_id = b"<" + bytes(range(16)).hex().encode() + b">"
            trailer += f" /Encrypt {encrypt} 0 R /ID [".encode() + file_id + file_id + b"]"
        out += b"trailer\n" + trailer + b" >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
        return bytes(out)


def build_pdf(
    path: Path,
    pages: Sequence[PdfPage],
    *,
    title: str | None = None,
    author: str | None = None,
    outline: Sequence[Bookmark] = (),
    encrypted: bool = False,
) -> Path:
    """Write ``pages`` to ``path`` as a PDF and return the path.

    ``encrypted=True`` adds a password-security dictionary whose checks fail, so the file
    can't be opened without a password (as a reader sees a password-protected PDF).
    """
    objects = _Objects()
    catalog = objects.reserve()
    page_tree = objects.reserve()
    fonts = {
        name: objects.add(
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>"
            .encode()
        )
        for name, base in FONTS.items()
    }  # fmt: skip
    for name, ligatures in TYPESET_FONTS.items():
        fonts[name] = _typeset_font(objects, ligatures)
    font_resources = " ".join(f"/{_FONT_KEYS[name]} {number} 0 R" for name, number in fonts.items())
    grey = objects.stream(
        bytes([150, 150, 150] * 4),
        b" /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB"
        b" /BitsPerComponent 8",
    )
    page_numbers = []
    for page in pages:
        content = objects.stream(_content(page))
        images = " ".join(f"/Im{n} {grey} 0 R" for n in range(1, len(page.pictures) + 1))
        page_numbers.append(
            objects.add(
                f"<< /Type /Page /Parent {page_tree} 0 R /MediaBox [0 0 {page.width:.0f} "
                f"{page.height:.0f}] /Resources << /Font << {font_resources} >> "
                f"/XObject << {images} >> >> /Contents {content} 0 R >>".encode()
            )
        )
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects.set(page_tree, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode())
    catalog_body = f"<< /Type /Catalog /Pages {page_tree} 0 R".encode()
    if outline:
        outlines = _write_outline(objects, outline, page_numbers, pages)
        catalog_body += f" /Outlines {outlines} 0 R /PageMode /UseOutlines".encode()
    objects.set(catalog, catalog_body + b" >>")

    info = None
    if title is not None or author is not None:
        entries = b""
        if title is not None:
            entries += b" /Title " + _pdf_string(title)
        if author is not None:
            entries += b" /Author " + _pdf_string(author)
        info = objects.add(b"<<" + entries + b" >>")
    encrypt = None
    if encrypted:
        junk = b"<" + bytes(range(32)).hex().encode() + b">"
        encrypt = objects.add(
            b"<< /Filter /Standard /V 1 /R 2 /O " + junk + b" /U " + junk + b" /P -4 >>"
        )
    path.write_bytes(objects.write(catalog, info, encrypt))
    return path


def _write_outline(
    objects: _Objects,
    outline: Sequence[Bookmark],
    page_numbers: Sequence[int],
    pages: Sequence[PdfPage],
) -> int:
    root = objects.reserve()

    def write_level(entries: Sequence[Bookmark], parent: int) -> tuple[int, int, int]:
        numbers = [objects.reserve() for _ in entries]
        total = 0
        for index, (entry, number) in enumerate(zip(entries, numbers, strict=True)):
            page = pages[entry.page]
            top = entry.top if entry.top is not None else page.height
            body = (
                b"<< /Title " + _pdf_string(entry.title)
                + f" /Parent {parent} 0 R /Dest [{page_numbers[entry.page]} 0 R /XYZ 0 "
                f"{top:.0f} 0]".encode()
            )  # fmt: skip
            if index > 0:
                body += f" /Prev {numbers[index - 1]} 0 R".encode()
            if index + 1 < len(numbers):
                body += f" /Next {numbers[index + 1]} 0 R".encode()
            if entry.children:
                first, last, count = write_level(entry.children, number)
                body += f" /First {first} 0 R /Last {last} 0 R /Count {count}".encode()
                total += count
            objects.set(number, body + b" >>")
            total += 1
        return numbers[0], numbers[-1], total

    first, last, count = write_level(outline, root)
    objects.set(
        root, f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {count} >>".encode()
    )
    return root


# --- A sample book: the opening of "The Gift of the Magi" (O. Henry, 1905, public domain) ----

SAMPLE_TITLE = "The Gift of the Magi"
SAMPLE_AUTHOR = "O. Henry"

# Three made-up section titles over the story's opening paragraphs. ``^1^`` is a footnote
# marker; the note itself is SAMPLE_NOTE.
SAMPLE_CHAPTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "I. One Dollar and Eighty-Seven Cents",
        (
            "One dollar and eighty-seven cents. That was all. And sixty cents of it was in "
            "pennies. Pennies saved one and two at a time by bulldozing the grocer and the "
            "vegetable man and the butcher until one’s cheeks burned with the silent "
            "imputation of parsimony that such close dealing implied. Three times Della^1^ "
            "counted it. One dollar and eighty-seven cents. And the next day would be "
            "Christmas.",
            "There was clearly nothing to do but flop down on the shabby little couch and howl. "
            "So Della did it. Which instigates the moral reflection that life is made up of "
            "sobs, sniffles, and smiles, with sniffles predominating.",
            "While the mistress of the home is gradually subsiding from the first stage to the "
            "second, take a look at the home. A furnished flat at $8 per week. It did not "
            "exactly beggar description, but it certainly had that word on the lookout for the "
            "mendicancy squad.",
        ),
    ),
    (
        "II. Mr. James Dillingham Young",
        (
            "In the vestibule below was a letter-box into which no letter would go, and an "
            "electric button from which no mortal finger could coax a ring. Also appertaining "
            "thereunto was a card bearing the name “Mr. James Dillingham Young.”",
            "The “Dillingham” had been flung to the breeze during a former period of "
            "prosperity when its possessor was being paid $30 per week. Now, when the income "
            "was shrunk to $20, the letters of “Dillingham” looked blurred, as though "
            "they were thinking seriously of contracting to a modest and unassuming D. But "
            "whenever Mr. James Dillingham Young came home and reached his flat above he was "
            "called “Jim” and greatly hugged by Mrs. James Dillingham Young, already "
            "introduced to you as Della. Which is all very good.",
            "Della finished her cry and attended to her cheeks with the powder rag. She stood "
            "by the window and looked out dully at a gray cat walking a gray fence in a gray "
            "backyard. Tomorrow would be Christmas Day, and she had only $1.87 with which to "
            "buy Jim a present. She had been saving every penny she could for months, with this "
            "result. Twenty dollars a week doesn’t go far. Expenses had been greater than "
            "she had calculated. They always are. Only $1.87 to buy a present for Jim. Her "
            "Jim. Many a happy hour she had spent planning for something nice for him. "
            "Something fine and rare and sterling—something just a little bit near to "
            "being worthy of the honor of being owned by Jim.",
        ),
    ),
    (
        "III. The Pier-Glass",
        (
            "There was a pier-glass between the windows of the room. Perhaps you have seen a "
            "pier-glass in an $8 flat. A very thin and very agile person may, by observing his "
            "reflection in a rapid sequence of longitudinal strips, obtain a fairly accurate "
            "conception of his looks. Della, being slender, had mastered the art.",
            "Suddenly she whirled from the window and stood before the glass. Her eyes were "
            "shining brilliantly, but her face had lost its color within twenty seconds. "
            "Rapidly she pulled down her hair and let it fall to its full length.",
        ),
    ),
)
SAMPLE_NOTE = "^1^ Della is Mrs. James Dillingham Young."
SAMPLE_CAPTION = "Figure 1: The flat at $8 per week."


def plain(text: str) -> str:
    """``text`` without its ``^...^`` footnote markers: what should be spoken."""
    return "".join(part for index, part in enumerate(text.split("^")) if index % 2 == 0)


class Typesetter:
    """Flows paragraphs onto pages: a running header, a page number in the footer, chapter
    headings, a figure, footnotes at the foot of the page where their marker lands, and a
    page set in two columns."""

    def __init__(
        self,
        header: str,
        *,
        first_number: int = 1,
        bottom: float = MARGIN + 30,
        hyphenate: bool = False,
    ) -> None:
        self.header = header
        self.bottom = bottom  # text stops here: higher means shorter pages
        self.hyphenate = hyphenate
        self.number = first_number
        self.pages: list[PdfPage] = []
        self.builder: PageBuilder | None = None
        self.notes: list[str] = []  # waiting for the line with their marker

    def new_page(self, *, header: bool = True) -> PageBuilder:
        self.finish_page()
        self.builder = PageBuilder()
        if header:
            self.builder.header(self.header.upper())
        return self.builder

    def finish_page(self) -> None:
        if self.builder is not None:
            self.builder.footer(str(self.number))
            self.pages.append(self.builder.page)
            self.number += 1
            self.builder = None

    def room(self, lines: int = 1) -> PageBuilder:
        """The page being filled, or a new one if fewer than ``lines`` lines fit."""
        if self.builder is None or self.builder.y - (lines - 1) * LEADING < self.bottom:
            return self.new_page()
        return self.builder

    def heading(self, text: str) -> None:
        builder = self.room(4)
        builder.gap(12).heading(text, size=16)

    def paragraph(self, text: str, *, width: int = 62) -> None:
        for number, line in enumerate(wrap(text, width, hyphenate=self.hyphenate)):
            builder = self.room()
            builder.line(line, indent=18 if number == 0 else 0)
            if "^" in line and self.notes:
                builder.footnotes(self.notes)
                self.notes = []

    def figure(self, caption: str) -> None:
        builder = self.room(10)
        top = builder.y + BODY_SIZE
        builder.picture(MARGIN + 60, top - 110, 200, 100)
        builder.y = top - 110 - 14
        builder.line(caption, size=9, x=MARGIN + 60)
        builder.gap(8)

    def two_columns(self, text: str) -> None:
        """A page with ``text`` in two columns. Lines are drawn left, right, left, right…,
        so the drawing order isn't the reading order."""
        builder = self.new_page()
        lines = wrap(text, 30)
        half = (len(lines) + 1) // 2
        left, right = lines[:half], lines[half:]
        y = builder.y
        for row in range(half):
            builder.line(left[row], y=y, x=MARGIN + (18 if row == 0 else 0))
            if row < len(right):
                builder.line(right[row], y=y, x=MARGIN + 174)
            y -= LEADING
        builder.y = y
        self.finish_page()


def build_sample_book(path: Path, *, title: str = SAMPLE_TITLE, short_pages: bool = False) -> Path:
    """The sample book: a title page, a short note page, contents, then three chapters with
    page furniture, a footnote, a figure and a two-column page.

    ``short_pages`` sets the chapters on pages of about seven lines with hyphenated line
    ends, so paragraphs, sentences and words are split by many more page breaks.
    """
    title_page = PageBuilder(top=PAGE_HEIGHT - 160)
    title_page.heading(title, size=24).gap(20).line(SAMPLE_AUTHOR, size=14)
    note_page = PageBuilder(top=PAGE_HEIGHT - 200)
    note_page.line("First published in 1905.", indent=0)
    note_page.line("This edition is in the public domain.", indent=0)
    contents = PageBuilder()
    contents.heading("Contents", size=16)
    for number, (heading, _) in enumerate(SAMPLE_CHAPTERS):
        contents.line(f"{heading} ........ {number * 2 + 1}")

    setter = (
        Typesetter(title, bottom=PAGE_HEIGHT - 150, hyphenate=True)
        if short_pages
        else Typesetter(title)
    )
    setter.notes = [SAMPLE_NOTE]
    for number, (heading, paragraphs) in enumerate(SAMPLE_CHAPTERS):
        setter.new_page()
        setter.heading(heading)
        for index, paragraph in enumerate(paragraphs):
            if number == 1 and index == 2:
                setter.two_columns(paragraph)
                continue
            setter.paragraph(paragraph)
            if number == 0 and index == 1:
                setter.figure(SAMPLE_CAPTION)
    setter.finish_page()
    pages = [title_page.page, note_page.page, contents.page, *setter.pages]
    return build_pdf(path, pages, title=title, author=SAMPLE_AUTHOR)
