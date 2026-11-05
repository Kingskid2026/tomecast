"""Shared pieces for readers that turn (X)HTML into paragraphs and chapters.

The EPUB reader and the HTML reader both walk markup the same way, so the walking, the
forgiving parsing and the chapter assembly live here. Everything is untrusted input: XML is
parsed without expanding entities, loading DTDs or touching the network.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import lxml.html
from lxml import etree

from tomecast.book import Chapter
from tomecast.chunking import normalize_whitespace
from tomecast.readers.errors import InvalidEbookError

# The most bytes read from a single-file book (HTML, FB2, TXT), so a huge file can't use all memory.
MAX_FILE_BYTES = 64 * 1024 * 1024

_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"

# Elements that start and end a paragraph. Text is gathered up to the next block boundary, so
# a block nested in another (a <p> in a <blockquote>) is emitted once, as its own paragraph.
_BLOCK_TAGS = frozenset(
    {
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "address", "hr",
        "ul", "ol", "li", "dl", "dt", "dd",
        "table", "caption", "thead", "tbody", "tfoot", "tr", "td", "th",
        "html", "body", "div", "section", "article", "aside", "header", "footer", "nav", "main",
        "figure", "figcaption", "hgroup", "details", "summary", "center",
    }
)  # fmt: skip
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_NEVER_READ_TAGS = frozenset({"head", "script", "style"})
_GUTENBERG_IDS = frozenset({"pg-header", "pg-footer"})
_SKIPPED_CLASSES = frozenset({"pagenum", "pg-boilerplate"})

_XML_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*?\sencoding\s*=\s*["']([A-Za-z0-9._:-]+)""")
_META_CHARSET = re.compile(rb"""<meta[^>]*?charset\s*=\s*["']?([A-Za-z0-9._:-]+)""", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+")
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
_ROMAN_NUMERAL = re.compile(r"m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


@dataclass(frozen=True, slots=True)
class Block:
    """One paragraph of text, and the level (1 to 6) of the heading it came from, if any."""

    text: str
    heading_level: int = 0

    @property
    def is_heading(self) -> bool:
        return self.heading_level > 0


@dataclass(frozen=True, slots=True)
class ChapterStart:
    """A new chapter starts here."""

    title: str


Event = Block | ChapterStart


class DocumentWalker:
    """Walks one document in reading order, turning its blocks of text into paragraphs.

    Text is gathered until the next block boundary (the start or end of a block element), so
    a block's text, including inline children like ``<em>`` and ``<a>``, becomes one
    paragraph. A container's loose text around nested blocks is kept as paragraphs of its own.
    Images are only read as drop caps (see :func:`_drop_cap_letters`).
    """

    def __init__(self, targets: Mapping[str, Sequence[str]]) -> None:
        self._targets = dict(targets)  # element id -> titles of the chapters starting there
        self._events: list[Event] = []
        self._text: list[str] = []
        self._has_text = False  # whether the paragraph so far has text besides drop caps
        self._images: list[str] = []  # the paragraph's images: drop-cap letters, or ""
        self._detached_drop_cap = ""  # a drop cap that had a block to itself
        self._heading_depth = 0
        self._heading_level = 0  # the level of the outermost heading being read

    def walk(self, root: etree._Element) -> list[Event]:
        """Return the paragraphs and chapter starts of the document under ``root``."""
        self._visit(root)
        self._end_paragraph()
        return self._events

    def _visit(self, element: etree._Element) -> None:
        # libxml2 caps nesting at 256 levels, so this recursion stays well within Python's limit.
        self._start_chapters_at(element)
        name = local_name(element)
        if name == "br":
            self._text.append(" ")
            return
        is_block = name in _BLOCK_TAGS
        is_heading = name in _HEADING_TAGS
        if is_block:
            self._end_paragraph()
        if _is_skipped(element, name):
            return  # the caller still reads the text after it (its tail)
        if name == "img":
            letters = _drop_cap_letters(element)
            self._images.append(letters)
            self._text.append(letters)  # no space after it: a drop cap starts the next word
            return
        if is_heading:
            if self._heading_depth == 0:
                self._heading_level = int(name[1])
            self._heading_depth += 1
        self._add(element.text)
        for child in element:
            if isinstance(child.tag, str):  # an element, not a comment or entity reference
                self._visit(child)
            self._add(child.tail)
        if is_block:
            self._end_paragraph()
        if is_heading:
            self._heading_depth -= 1

    def _start_chapters_at(self, element: etree._Element) -> None:
        for name in anchor_names(element):
            titles = self._targets.pop(name, ())
            if titles:
                self._end_paragraph()
                self._events.extend(ChapterStart(title) for title in titles)
                self._detached_drop_cap = ""  # a drop cap never carries into another chapter

    def _add(self, text: str | None) -> None:
        if text:
            self._text.append(text)
            self._has_text = self._has_text or not text.isspace()

    def _end_paragraph(self) -> None:
        # Without text around it, an image isn't part of a word, so it isn't read here.
        text = normalize_whitespace("".join(self._text)) if self._has_text else ""
        lone_image = self._images[0] if not self._has_text and len(self._images) == 1 else ""
        heading_level = self._heading_level if self._heading_depth > 0 else 0
        self._text.clear()
        self._images.clear()
        self._has_text = False
        if text:
            # A drop cap in a block of its own (Project Gutenberg's illustrated capitals) starts
            # the next paragraph, if that paragraph begins mid-word with a lowercase letter.
            if self._detached_drop_cap and not heading_level and text[0].islower():
                text = self._detached_drop_cap + text
            self._detached_drop_cap = ""
            self._events.append(Block(text, heading_level))
        elif lone_image:
            self._detached_drop_cap = lone_image


def _drop_cap_letters(image: etree._Element) -> str:
    """Return an image's alt text if it's a drop cap's letters, else ``""``.

    Illustrated drop caps draw the first letter of a paragraph as a picture, as in
    ``<img alt="T"/>he quick fox``, so the letter must be read for the word to make sense.
    Only alt text of one or two letters or digits counts: longer alt text describes a picture
    (which isn't read), whatever the image's class says, and ``*`` is an ornament.
    """
    alt = (image.get("alt") or "").strip()
    return alt if len(alt) <= 2 and alt.isalnum() else ""


def _is_skipped(element: etree._Element, name: str) -> bool:
    """Return whether an element, and everything inside it, is known not to be book text."""
    if name in _NEVER_READ_TAGS:
        return True
    if "pagebreak" in epub_types(element) or "doc-pagebreak" in tokens(element.get("role")):
        return True
    if tokens(element.get("class")) & _SKIPPED_CLASSES:
        return True
    return element.get("id") in _GUTENBERG_IDS


def anchor_names(element: etree._Element) -> list[str]:
    """Return the names a link can point to: the ``id``, and ``name`` on old ``<a>`` anchors."""
    names = [element.get("id")]
    if local_name(element) == "a":
        names.append(element.get("name"))
    return [name for name in names if name]


# --- Chapters ------------------------------------------------------------------------------


def assemble_chapters(events: Iterable[Event]) -> list[Chapter]:
    """Group paragraphs into chapters. Text before the first chapter start gets title ``""``."""
    chapters: list[Chapter] = []
    title = ""
    blocks: list[Block] = []

    def finish_chapter() -> None:
        chapter = _make_chapter(title, blocks)
        if chapter is not None:
            chapters.append(chapter)

    for event in events:
        if isinstance(event, ChapterStart):
            finish_chapter()
            title, blocks = event.title, []
        else:
            blocks.append(event)
    finish_chapter()
    return chapters


def _make_chapter(title: str, blocks: Sequence[Block]) -> Chapter | None:
    """Build a chapter, dropping leading headings that repeat its title.

    The title is spoken before the chapter, so a heading saying the same thing would be read
    twice. Returns None for a chapter with no title and no text.
    """
    start = 0
    while start < len(blocks) and blocks[start].is_heading and _repeats(blocks[start].text, title):
        start += 1
    paragraphs = tuple(block.text for block in blocks[start:])
    if not title and not paragraphs:
        return None
    return Chapter(title=title, paragraphs=paragraphs)


def _repeats(heading: str, title: str) -> bool:
    """Return whether a heading says the same as the title, or a run of words in it.

    Case, whitespace and punctuation are ignored, and roman numerals count as numbers, so the
    heading "CHAPTER 1. Loomings" repeats the title "Chapter I. Loomings", and so does
    "Loomings". Matching whole words keeps "Chapter 1" from matching "Chapter 10".
    """
    heading_words, title_words = _comparable_words(heading), _comparable_words(title)
    if not heading_words or not title_words:
        return False
    if "".join(heading_words) == "".join(title_words):
        return True
    size = len(heading_words)
    return any(
        title_words[i : i + size] == heading_words for i in range(len(title_words) - size + 1)
    )


def _comparable_words(text: str) -> list[str]:
    """Lowercase words, with numbers as digits whether written "4", "IV" or "four".

    Number words go up to ninety-nine: "twenty-one" (two words here) becomes "21".
    """
    words: list[str] = []
    after_tens = False  # whether the previous word was "twenty", "thirty" and so on
    for word in _WORD.findall(text.casefold()):
        number = _NUMBER_WORDS.get(word)
        if number is not None and number < 10 and after_tens:
            words[-1] = str(int(words[-1]) + number)
            after_tens = False
            continue
        after_tens = number is not None and number >= 20
        words.append(str(number) if number is not None else _roman_to_digits(word))
    return words


def _roman_to_digits(word: str) -> str:
    """Turn a roman numeral like "xiv" into "14"; return any other word unchanged."""
    if not _ROMAN_NUMERAL.fullmatch(word):
        return word
    values = [_ROMAN_VALUES[letter] for letter in word]
    # A letter worth less than the one after it is subtracted, as in "iv" (4) and "xc" (90).
    total = sum(
        -value if value < next_value else value
        for value, next_value in zip(values, [*values[1:], 0], strict=True)
    )
    return str(total)


# --- Parsing -------------------------------------------------------------------------------


def xml_parser() -> etree.XMLParser:
    """A parser that's safe for untrusted XML: no entity expansion, DTDs or network access."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def parse_xml(data: bytes, name: str) -> etree._Element:
    """Parse a file that must be well-formed XML, such as the package document."""
    try:
        return etree.fromstring(data, xml_parser())
    except etree.XMLSyntaxError as error:
        raise InvalidEbookError(f"{name} isn't well-formed XML: {error}") from error


def parse_document(data: bytes, name: str) -> etree._Element:
    """Parse an XHTML document, falling back to the forgiving HTML parser for broken markup.

    The XML parser is tried first, because it's exact. Many books aren't well-formed XML,
    though, most often because they use HTML entities like ``&nbsp;`` without a DTD. With
    entity expansion off, those either fail to parse or are left as unexpanded references
    (which would lose their characters), and either way the HTML parser reads the file instead.

    The HTML parser recovers from almost anything, but when it hits a resource limit (nesting
    deeper than 256 levels, or a single text over about 10 MB) it stops and returns what it
    has so far. That would silently lose the rest of the file, so it raises instead.
    """
    try:
        root = etree.fromstring(data, xml_parser())
    except etree.XMLSyntaxError:
        pass
    else:
        if next(root.iter(etree.Entity), None) is None:
            return root
    parser = _html_parser(guess_encoding(data))
    try:
        root = lxml.html.document_fromstring(data, parser=parser)
    except etree.ParserError:  # the document has no content at all
        root = etree.Element("html")
    fatal = next((e for e in parser.error_log if e.level == etree.ErrorLevels.FATAL), None)
    if fatal is not None:
        raise InvalidEbookError(f"{name} can't be read completely: {fatal.message}")
    return root


def _html_parser(encoding: str) -> lxml.html.HTMLParser:
    """An HTML parser for the given encoding, or for UTF-8 if libxml2 doesn't know it."""
    try:
        return lxml.html.HTMLParser(
            encoding=encoding,
            no_network=True,
            huge_tree=False,
            remove_comments=True,
            remove_pis=True,
        )
    except LookupError:
        return _html_parser("utf-8")


def guess_encoding(data: bytes) -> str:
    """Find a document's encoding from its byte-order mark, its bytes, its XML declaration or
    its ``<meta>`` tag.

    The HTML parser would otherwise assume Latin-1 and garble UTF-8 text, so this defaults to
    UTF-8, the encoding EPUB requires.
    """
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    utf16 = _utf16_without_bom(data)
    if utf16 is not None:
        return utf16
    match = _XML_ENCODING.match(data) or _META_CHARSET.search(data)
    return match.group(1).decode("ascii") if match else "utf-8"


def _utf16_without_bom(data: bytes) -> str | None:
    """Spot UTF-16 that has no byte-order mark, returning "utf-16le", "utf-16be" or None.

    Markup is mostly ASCII, so near the start of a UTF-16 file every other byte is NUL:
    ``<p`` is ``<\\0p\\0`` in little-endian order and ``\\0<\\0p`` in big-endian order.
    """
    sample = data[:256]
    pairs = len(sample) // 2
    if pairs < 2:
        return None
    even_nuls, odd_nuls = sample[0 : pairs * 2 : 2].count(0), sample[1 : pairs * 2 : 2].count(0)
    if odd_nuls >= pairs / 2 and even_nuls < pairs / 8:
        return "utf-16le"
    if even_nuls >= pairs / 2 and odd_nuls < pairs / 8:
        return "utf-16be"
    return None


def looks_like_markup(data: bytes) -> bool:
    """Return whether a file is text that starts with a tag, as (X)HTML does."""
    try:
        text = data.decode(guess_encoding(data))
    except (UnicodeDecodeError, LookupError):
        return False
    return text.lstrip("\ufeff \t\r\n").startswith("<")


def local_name(element: etree._Element) -> str:
    """Return a tag name without its namespace, lowercased ("" for comments and the like).

    Matching local names lets the same code read XHTML parsed as XML (namespaced tags) or as
    HTML (plain tags), and copes with package files that use unusual namespace prefixes.
    """
    tag = element.tag
    return tag.rpartition("}")[2].lower() if isinstance(tag, str) else ""


def descendants(root: etree._Element, name: str) -> Iterator[etree._Element]:
    """Yield the elements with this local name in ``root`` (itself included), in document order."""
    return (element for element in root.iter() if local_name(element) == name)


def first(root: etree._Element, name: str) -> etree._Element | None:
    """Return the first element with this local name in ``root``, or None."""
    return next(descendants(root, name), None)


def element_text(element: etree._Element) -> str:
    """Return all the text inside an element, whitespace-normalized."""
    return normalize_whitespace("".join(element.itertext()))


def tokens(value: str | None) -> frozenset[str]:
    """Split a space-separated attribute value (``class``, ``epub:type``) into lowercase tokens."""
    return frozenset((value or "").lower().split())


def epub_types(element: etree._Element) -> frozenset[str]:
    """Return an element's ``epub:type`` tokens, whether it was parsed as XML or as HTML."""
    return tokens(element.get(_EPUB_TYPE) or element.get("epub:type"))


def image_type(data: bytes) -> str | None:
    """Return "image/png" or "image/jpeg" from the image's own bytes, or None for other types.

    The bytes are trusted over the manifest, which sometimes says "image/jpg" or names the
    wrong type.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def read_limited(path: Path) -> bytes:
    """Read a whole single-file book, refusing one too large to read safely."""
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise InvalidEbookError(f"{path.name} is too large to read safely")
    return data


def child_elements(element: etree._Element, name: str) -> Iterator[etree._Element]:
    """Yield the direct children with this local name, in document order."""
    return (child for child in element if local_name(child) == name)
