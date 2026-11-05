"""Read EPUB 2 and EPUB 3 ebooks into a :class:`~tomecast.book.Book`.

An EPUB is a zip file. ``META-INF/container.xml`` points to the package document (the OPF),
which holds the book's metadata, its manifest (every file in the book) and its spine (the
reading order). Chapter titles come from the table of contents: the EPUB 3 navigation
document, or the older EPUB 2 NCX file.

Like every reader, this one must never silently drop real book text (see "Core guarantee:
every sentence gets spoken" in docs/architecture.md). Every block of text in the reading
order becomes a paragraph, and only markup known not to be book text is skipped: the
document head, scripts, styles, page-break markers and Project Gutenberg boilerplate. A book
that can't be read completely raises :class:`InvalidEbookError` instead of coming back
partial.

EPUBs are untrusted input. The zip is read in memory with size limits (a zip-bomb guard), and
XML is parsed without expanding entities, loading DTDs or touching the network.
"""

from __future__ import annotations

import codecs
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import lxml.html
from lxml import etree

from tomecast.book import Book, Chapter, Cover
from tomecast.chunking import normalize_whitespace

# Zip-bomb guard: the most uncompressed data read from one file in the EPUB, and in total.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

_CONTAINER_PATH = "META-INF/container.xml"
_ENCRYPTION_PATH = "META-INF/encryption.xml"
_PACKAGE_MEDIA_TYPE = "application/oebps-package+xml"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"

# Media types (without parameters like "; charset=utf-8") of documents read as (X)HTML. Other
# spine items are read too if they look like markup, except images, which have no text.
_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/xhtml+xml",
        "application/xhtml",
        "text/xhtml",
        "text/html",
        "application/xml",
        "text/xml",
        "application/x-dtbook+xml",
        "text/x-oeb1-document",
    }
)

# What zipfile raises for a corrupt, password-protected or unsupported (compression) member.
_ZIP_READ_ERRORS = (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error)

# Font obfuscation is the only use of encryption.xml that doesn't hide the text (it isn't DRM).
_FONT_OBFUSCATION = frozenset(
    {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
)

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
# Image classes that mark a drop cap: "dropcap", "drop-cap", "cap", "capital", but not
# "caption", "landscape" or "escape".
_DROP_CAP_CLASS = re.compile(r"drop|\bcap(?!tion)", re.IGNORECASE)
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


class InvalidEbookError(ValueError):
    """Raised when a file can't be read as an EPUB, or can't be read completely."""


def read_epub(path: Path) -> Book:
    """Read an EPUB 2 or EPUB 3 file into a :class:`~tomecast.book.Book`.

    Chapters follow the book's reading order (its spine) and take their titles from its table
    of contents. When there's no table of contents, or none of it points into the reading
    order, each file in the reading order becomes a chapter titled by its first heading.

    Raises:
        InvalidEbookError: if the file isn't a zip, has no ``META-INF/container.xml`` or no
            package document, is DRM-protected, holds a file too large to read safely,
            references a reading-order file that's missing, or has no readable text.
        OSError: if the file itself can't be opened.
    """
    try:
        zip_file = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise InvalidEbookError(f"{path.name} isn't an EPUB: it's not a zip file") from error

    with zip_file:
        archive = _Archive(zip_file)
        package_path = _find_package_path(archive)
        package = _parse_xml(archive.read(package_path), package_path)
        manifest = _read_manifest(package, package_path, archive)
        spine = _read_spine(package, manifest)
        _reject_drm(archive, spine)
        chapters = _read_chapters(archive, spine, _read_toc(package, manifest, archive))
        if not chapters:
            raise InvalidEbookError(f"{path.name} has no readable text")
        title, authors, language = _read_metadata(package)
        return Book(
            title=title or path.stem,
            authors=authors,
            language=language,
            chapters=tuple(chapters),
            cover=_read_cover(package, manifest, archive),
        )


# --- The zip file --------------------------------------------------------------------------


class _Archive:
    """The files inside an EPUB, read into memory (never extracted) with zip-bomb limits."""

    def __init__(self, zip_file: zipfile.ZipFile) -> None:
        self._zip = zip_file
        self._members = {info.filename: info for info in zip_file.infolist() if not info.is_dir()}
        # Some books get the case of their file names wrong, so names are also matched
        # case-insensitively when there's no exact match.
        self._folded_names: dict[str, str] = {}
        for name in self._members:
            self._folded_names.setdefault(name.casefold(), name)
        self._bytes_read = 0

    def find(self, name: str) -> str | None:
        """Return the name of the file matching ``name``, or None if there's no such file."""
        if name in self._members:
            return name
        return self._folded_names.get(name.casefold())

    def canonical(self, name: str) -> str:
        """Return the stored spelling of ``name``, or ``name`` itself if there's no such file."""
        return self.find(name) or name

    def read(self, name: str) -> bytes:
        """Read one file into memory, refusing files too large to read safely."""
        member = self.find(name)
        if member is None:
            raise InvalidEbookError(f"the EPUB is missing {name}")
        info = self._members[member]
        if info.file_size > MAX_MEMBER_BYTES or self._bytes_read + info.file_size > MAX_TOTAL_BYTES:
            raise InvalidEbookError(f"{member} is too large to read safely (possible zip bomb)")
        try:
            with self._zip.open(info) as stream:
                # zipfile stops at the declared size; the limit here is a second safety net.
                data = stream.read(MAX_MEMBER_BYTES + 1)
        except _ZIP_READ_ERRORS as error:
            raise InvalidEbookError(f"{member} can't be read from the zip: {error}") from error
        self._bytes_read += len(data)
        if len(data) > MAX_MEMBER_BYTES or self._bytes_read > MAX_TOTAL_BYTES:
            raise InvalidEbookError(f"{member} is too large to read safely (possible zip bomb)")
        return data


# --- The package document: metadata, manifest, spine, cover --------------------------------


@dataclass(frozen=True, slots=True)
class _Item:
    """One file listed in the package manifest."""

    path: str  # the file's name inside the zip
    media_type: str  # lowercase, without parameters like "; charset=utf-8"
    properties: frozenset[str]
    fallback: str  # the ID of an item to use instead when this one can't be read, or ""


def _find_package_path(archive: _Archive) -> str:
    """Find the package document (the OPF) through ``META-INF/container.xml``."""
    container_name = archive.find(_CONTAINER_PATH)
    if container_name is None:
        raise InvalidEbookError(f"the EPUB has no {_CONTAINER_PATH}")
    container = _parse_xml(archive.read(container_name), container_name)
    rootfiles = [
        element for element in _descendants(container, "rootfile") if element.get("full-path")
    ]
    if not rootfiles:
        raise InvalidEbookError(f"{_CONTAINER_PATH} doesn't name a package document")
    # A container may list several renditions; the first package document is the default.
    rootfile = next(
        (element for element in rootfiles if element.get("media-type") == _PACKAGE_MEDIA_TYPE),
        rootfiles[0],
    )
    path, _ = _resolve_href("", rootfile.get("full-path", ""))
    package_path = archive.find(path)
    if package_path is None:
        raise InvalidEbookError(f"the package document {path} is missing")
    return package_path


def _read_manifest(
    package: etree._Element, package_path: str, archive: _Archive
) -> dict[str, _Item]:
    """Map manifest item IDs to the files they name."""
    manifest_element = _first(package, "manifest")
    if manifest_element is None:
        raise InvalidEbookError("the package document has no manifest")
    manifest: dict[str, _Item] = {}
    for element in _descendants(manifest_element, "item"):
        item_id, href = element.get("id"), element.get("href")
        if not item_id or not href or item_id in manifest:
            continue
        path, _ = _resolve_href(package_path, href)
        manifest[item_id] = _Item(
            path=archive.canonical(path),
            media_type=(element.get("media-type") or "").partition(";")[0].strip().lower(),
            properties=_tokens(element.get("properties")),
            fallback=element.get("fallback") or "",
        )
    return manifest


def _read_spine(package: etree._Element, manifest: Mapping[str, _Item]) -> list[_Item]:
    """Return the documents to read, in reading order.

    Items marked ``linear="no"`` and images are left out. Nothing else is skipped: an itemref
    naming no manifest item is an error, not a missing chapter.
    """
    spine = _first(package, "spine")
    if spine is None:
        raise InvalidEbookError("the package document has no spine (reading order)")
    # Some books get the case of IDs wrong, so they're also matched case-insensitively.
    folded_ids: dict[str, str] = {}
    for item_id in manifest:
        folded_ids.setdefault(item_id.casefold(), item_id)
    documents: list[_Item] = []
    for itemref in _descendants(spine, "itemref"):
        if (itemref.get("linear") or "").strip().lower() == "no":
            continue
        idref = (itemref.get("idref") or "").strip()
        item = manifest.get(idref) or manifest.get(folded_ids.get(idref.casefold(), ""))
        if item is None:
            raise InvalidEbookError(
                f"the reading order names {idref!r}, which isn't in the manifest"
            )
        document = _document_to_read(item, manifest)
        if document is not None:
            documents.append(document)
    return documents


def _document_to_read(item: _Item, manifest: Mapping[str, _Item]) -> _Item | None:
    """Return the item to read for a spine item, or None for an image (which has no text).

    A spine item that isn't (X)HTML may name a fallback that is, as EPUB 3 allows. Failing
    that, it's still read, as long as it turns out to be markup (see :func:`_read_spine_item`).
    """
    candidate: _Item | None = item
    seen: set[str] = set()
    while candidate is not None and candidate.path not in seen:
        if candidate.media_type in _TEXT_MEDIA_TYPES:
            return candidate
        seen.add(candidate.path)
        candidate = manifest.get(candidate.fallback)
    return None if item.media_type.startswith("image/") else item


def _read_metadata(package: etree._Element) -> tuple[str, tuple[str, ...], str | None]:
    """Return the title (``""`` if missing), the authors in order, and the language."""
    metadata = _first(package, "metadata")
    if metadata is None:
        return "", (), None

    def values(name: str) -> list[str]:
        return [text for text in map(_text, _descendants(metadata, name)) if text]

    titles, creators, languages = values("title"), values("creator"), values("language")
    title = titles[0] if titles else ""
    language = languages[0] if languages else None
    return title, tuple(creators), language


def _read_cover(
    package: etree._Element, manifest: Mapping[str, _Item], archive: _Archive
) -> Cover | None:
    """Find the cover: the EPUB 3 ``cover-image`` item, else EPUB 2's ``<meta name="cover">``."""
    item = next((item for item in manifest.values() if "cover-image" in item.properties), None)
    if item is None:
        item = manifest.get(_epub2_cover_id(package) or "")
    if item is None or archive.find(item.path) is None:
        return None
    data = archive.read(item.path)
    media_type = _image_type(data)
    return Cover(data, media_type) if media_type else None


def _epub2_cover_id(package: etree._Element) -> str | None:
    """Return the manifest ID named by EPUB 2's ``<meta name="cover" content="ID">``."""
    metadata = _first(package, "metadata")
    if metadata is None:
        return None
    metas = _descendants(metadata, "meta")
    return next((meta.get("content") for meta in metas if meta.get("name") == "cover"), None)


def _image_type(data: bytes) -> str | None:
    """Return "image/png" or "image/jpeg" from the image's own bytes, or None for other types.

    The bytes are trusted over the manifest, which sometimes says "image/jpg" or names the
    wrong type.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _reject_drm(archive: _Archive, spine: Sequence[_Item]) -> None:
    """Raise if the reading order is encrypted, which would otherwise be read as gibberish."""
    encryption_name = archive.find(_ENCRYPTION_PATH)
    if encryption_name is None:
        return
    encryption = _parse_xml(archive.read(encryption_name), encryption_name)
    encrypted: set[str] = set()
    for data in _descendants(encryption, "encrypteddata"):
        method = _first(data, "encryptionmethod")
        if method is not None and method.get("Algorithm") in _FONT_OBFUSCATION:
            continue
        for reference in _descendants(data, "cipherreference"):
            path, _ = _resolve_href("", reference.get("URI") or "")
            encrypted.add(archive.canonical(path))
    if any(item.path in encrypted for item in spine):
        raise InvalidEbookError("the EPUB is DRM-protected; only DRM-free books can be read")


# --- The table of contents -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TocEntry:
    """One table-of-contents entry: a chapter title and where that chapter starts."""

    label: str
    path: str  # the file's name inside the zip
    fragment: str  # the id of the element the chapter starts at, or "" for the top of the file


def _read_toc(
    package: etree._Element, manifest: Mapping[str, _Item], archive: _Archive
) -> list[_TocEntry]:
    """Read the table of contents, preferring the EPUB 3 nav document to the EPUB 2 NCX."""
    nav = next((item for item in manifest.values() if "nav" in item.properties), None)
    if nav is not None and archive.find(nav.path) is not None:
        entries = _read_nav(archive, nav.path)
        if entries:
            return entries
    ncx = _find_ncx(package, manifest)
    if ncx is not None and archive.find(ncx.path) is not None:
        return _read_ncx(archive, ncx.path)
    return []


def _read_nav(archive: _Archive, nav_path: str) -> list[_TocEntry]:
    """Read the ``<nav epub:type="toc">`` list. Nested lists are flattened in reading order."""
    root = _parse_document(archive.read(nav_path), nav_path)
    toc = next((nav for nav in _descendants(root, "nav") if "toc" in _epub_types(nav)), None)
    if toc is None:
        return []
    entries: list[_TocEntry] = []
    for link in _descendants(toc, "a"):
        href = link.get("href")
        if href and not urlsplit(href).scheme:  # skip links to the web
            path, fragment = _resolve_href(nav_path, href)
            entries.append(_TocEntry(_text(link), archive.canonical(path), fragment))
    return entries


def _find_ncx(package: etree._Element, manifest: Mapping[str, _Item]) -> _Item | None:
    """Find the NCX through the spine's ``toc`` attribute, else by its media type."""
    spine = _first(package, "spine")
    toc_id = spine.get("toc") if spine is not None else None
    if toc_id and toc_id in manifest:
        return manifest[toc_id]
    return next((item for item in manifest.values() if item.media_type == _NCX_MEDIA_TYPE), None)


def _read_ncx(archive: _Archive, ncx_path: str) -> list[_TocEntry]:
    """Read the NCX ``navPoint``s in document order, which flattens nested ones in order."""
    root = _parse_document(archive.read(ncx_path), ncx_path)
    nav_map = _first(root, "navmap")
    if nav_map is None:
        return []
    entries: list[_TocEntry] = []
    for point in _descendants(nav_map, "navpoint"):
        label = _child(point, "navlabel")
        content = _child(point, "content")
        src = content.get("src") if content is not None else None
        if src and not urlsplit(src).scheme:
            path, fragment = _resolve_href(ncx_path, src)
            title = _text(label) if label is not None else ""
            entries.append(_TocEntry(title, archive.canonical(path), fragment))
    return entries


# --- Content documents ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Block:
    """One paragraph of text, and whether it came from a heading."""

    text: str
    is_heading: bool


@dataclass(frozen=True, slots=True)
class _ChapterStart:
    """A new chapter starts here."""

    title: str


_Event = _Block | _ChapterStart


def _read_chapters(
    archive: _Archive, spine: Sequence[_Item], toc: Sequence[_TocEntry]
) -> list[Chapter]:
    """Read every document in the spine and split the text into chapters."""
    spine_paths = {item.path for item in spine}
    toc_by_path: dict[str, list[_TocEntry]] = {}
    for entry in toc:
        if entry.path in spine_paths:
            toc_by_path.setdefault(entry.path, []).append(entry)

    events: list[_Event] = []
    for item in spine:
        root = _read_spine_item(archive, item)
        document_events = _read_document(root, toc_by_path.get(item.path, ()))
        if not toc_by_path:
            # No usable table of contents: each file is a chapter. A file that opens with a
            # heading is titled by it; a heading further in stays where it is, so nothing is
            # spoken out of order.
            first = next((e for e in document_events if isinstance(e, _Block)), None)
            title = first.text if first is not None and first.is_heading else ""
            document_events.insert(0, _ChapterStart(title))
        events.extend(document_events)
    return _assemble_chapters(events)


def _read_spine_item(archive: _Archive, item: _Item) -> etree._Element:
    """Parse a document from the reading order, refusing one that isn't markup at all."""
    data = archive.read(item.path)
    if item.media_type not in _TEXT_MEDIA_TYPES and not _looks_like_markup(data):
        media_type = item.media_type or "no media type"
        raise InvalidEbookError(
            f"{item.path} ({media_type}) is in the reading order but isn't XHTML"
        )
    return _parse_document(data, item.path)


def _read_document(root: etree._Element, entries: Sequence[_TocEntry]) -> list[_Event]:
    """Turn one document into paragraphs, with chapter starts where the TOC entries point.

    An entry for the whole file, or for an id the file doesn't have, starts its chapter at the
    top of the file. An entry pointing inside skipped markup (such as Project Gutenberg's
    license section) starts no chapter, because nothing there is read.
    """
    ids = {name for element in root.iter() for name in _anchor_names(element)}
    events: list[_Event] = [_ChapterStart(e.label) for e in entries if e.fragment not in ids]
    targets: dict[str, list[str]] = {}
    for entry in entries:
        if entry.fragment in ids:
            targets.setdefault(entry.fragment, []).append(entry.label)
    events.extend(_DocumentWalker(targets).walk(root))
    return events


class _DocumentWalker:
    """Walks one document in reading order, turning its blocks of text into paragraphs.

    Text is gathered until the next block boundary (the start or end of a block element), so
    a block's text, including inline children like ``<em>`` and ``<a>``, becomes one
    paragraph. A container's loose text around nested blocks is kept as paragraphs of its own.
    """

    def __init__(self, targets: Mapping[str, Sequence[str]]) -> None:
        self._targets = dict(targets)  # element id -> titles of the chapters starting there
        self._events: list[_Event] = []
        self._text: list[str] = []
        self._has_text = False  # whether the paragraph so far has text besides drop caps
        self._heading_depth = 0

    def walk(self, root: etree._Element) -> list[_Event]:
        """Return the paragraphs and chapter starts of the document under ``root``."""
        self._visit(root)
        self._end_paragraph()
        return self._events

    def _visit(self, element: etree._Element) -> None:
        # libxml2 caps nesting at 256 levels, so this recursion stays well within Python's limit.
        self._start_chapters_at(element)
        name = _local_name(element)
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
            # No space after it: a drop cap is the start of the word that follows.
            self._text.append(_drop_cap_letters(element))
            return
        if is_heading:
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
        for name in _anchor_names(element):
            titles = self._targets.pop(name, ())
            if titles:
                self._end_paragraph()
                self._events.extend(_ChapterStart(title) for title in titles)

    def _add(self, text: str | None) -> None:
        if text:
            self._text.append(text)
            self._has_text = self._has_text or not text.isspace()

    def _end_paragraph(self) -> None:
        # A drop cap with no text around it is an image on its own, not part of a word.
        text = normalize_whitespace("".join(self._text)) if self._has_text else ""
        self._text.clear()
        self._has_text = False
        if text:
            self._events.append(_Block(text, is_heading=self._heading_depth > 0))


def _drop_cap_letters(image: etree._Element) -> str:
    """Return the alt text of an image that holds the first letter of a word, else ``""``.

    Illustrated drop caps draw a word's first letter as a picture (``<img alt="T"/>he quick
    fox``), so the letter must be read for the word to make sense. An image counts as one when
    its alt text is one or two characters, or when it or its parent has a drop-cap class, and
    the alt text has a letter or digit (so ornaments like ``*`` aren't read). Any other alt
    text describes a picture, which isn't read.
    """
    alt = (image.get("alt") or "").strip()
    if not any(character.isalnum() for character in alt):
        return ""
    parent = image.getparent()
    parent_class = parent.get("class") if parent is not None else None
    classes = f"{image.get('class') or ''} {parent_class or ''}"
    return alt if len(alt) <= 2 or _DROP_CAP_CLASS.search(classes) else ""


def _is_skipped(element: etree._Element, name: str) -> bool:
    """Return whether an element, and everything inside it, is known not to be book text."""
    if name in _NEVER_READ_TAGS:
        return True
    if "pagebreak" in _epub_types(element) or "doc-pagebreak" in _tokens(element.get("role")):
        return True
    if _tokens(element.get("class")) & _SKIPPED_CLASSES:
        return True
    return element.get("id") in _GUTENBERG_IDS


def _anchor_names(element: etree._Element) -> list[str]:
    """Return the names a link can point to: the ``id``, and ``name`` on old ``<a>`` anchors."""
    names = [element.get("id")]
    if _local_name(element) == "a":
        names.append(element.get("name"))
    return [name for name in names if name]


# --- Chapters ------------------------------------------------------------------------------


def _assemble_chapters(events: Iterable[_Event]) -> list[Chapter]:
    """Group paragraphs into chapters. Text before the first chapter start gets title ``""``."""
    chapters: list[Chapter] = []
    title = ""
    blocks: list[_Block] = []

    def finish_chapter() -> None:
        chapter = _make_chapter(title, blocks)
        if chapter is not None:
            chapters.append(chapter)

    for event in events:
        if isinstance(event, _ChapterStart):
            finish_chapter()
            title, blocks = event.title, []
        else:
            blocks.append(event)
    finish_chapter()
    return chapters


def _make_chapter(title: str, blocks: Sequence[_Block]) -> Chapter | None:
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


def _xml_parser() -> etree.XMLParser:
    """A parser that's safe for untrusted XML: no entity expansion, DTDs or network access."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def _parse_xml(data: bytes, name: str) -> etree._Element:
    """Parse a file that must be well-formed XML, such as the package document."""
    try:
        return etree.fromstring(data, _xml_parser())
    except etree.XMLSyntaxError as error:
        raise InvalidEbookError(f"{name} isn't well-formed XML: {error}") from error


def _parse_document(data: bytes, name: str) -> etree._Element:
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
        root = etree.fromstring(data, _xml_parser())
    except etree.XMLSyntaxError:
        pass
    else:
        if next(root.iter(etree.Entity), None) is None:
            return root
    parser = _html_parser(_guess_encoding(data))
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


def _guess_encoding(data: bytes) -> str:
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


def _looks_like_markup(data: bytes) -> bool:
    """Return whether a file is text that starts with a tag, as (X)HTML does."""
    try:
        text = data.decode(_guess_encoding(data))
    except (UnicodeDecodeError, LookupError):
        return False
    return text.lstrip("\ufeff \t\r\n").startswith("<")


# --- Small helpers -------------------------------------------------------------------------


def _resolve_href(base_path: str, href: str) -> tuple[str, str]:
    """Resolve an href found in the file ``base_path`` to a (path in the zip, fragment) pair.

    Hrefs are URL-encoded (``%20`` for a space) and relative to the folder of the file they
    appear in, so ``../`` is resolved. An href with only a fragment points into ``base_path``.
    """
    target, _, fragment = href.strip().partition("#")
    target = unquote(target)
    if not target:
        return base_path, unquote(fragment)
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base_path), target))
    return path.lstrip("/"), unquote(fragment)


def _local_name(element: etree._Element) -> str:
    """Return a tag name without its namespace, lowercased ("" for comments and the like).

    Matching local names lets the same code read XHTML parsed as XML (namespaced tags) or as
    HTML (plain tags), and copes with package files that use unusual namespace prefixes.
    """
    tag = element.tag
    return tag.rpartition("}")[2].lower() if isinstance(tag, str) else ""


def _descendants(root: etree._Element, name: str) -> Iterator[etree._Element]:
    """Yield the elements with this local name in ``root`` (itself included), in document order."""
    return (element for element in root.iter() if _local_name(element) == name)


def _first(root: etree._Element, name: str) -> etree._Element | None:
    """Return the first element with this local name in ``root``, or None."""
    return next(_descendants(root, name), None)


def _child(element: etree._Element, name: str) -> etree._Element | None:
    """Return the first direct child with this local name, or None."""
    return next((child for child in element if _local_name(child) == name), None)


def _text(element: etree._Element) -> str:
    """Return all the text inside an element, whitespace-normalized."""
    return normalize_whitespace("".join(element.itertext()))


def _tokens(value: str | None) -> frozenset[str]:
    """Split a space-separated attribute value (``class``, ``epub:type``) into lowercase tokens."""
    return frozenset((value or "").lower().split())


def _epub_types(element: etree._Element) -> frozenset[str]:
    """Return an element's ``epub:type`` tokens, whether it was parsed as XML or as HTML."""
    return _tokens(element.get(_EPUB_TYPE) or element.get("epub:type"))
