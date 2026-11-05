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

import posixpath
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from tomecast.book import Book, Chapter, Cover
from tomecast.readers._markup import (
    Block,
    ChapterStart,
    DocumentWalker,
    Event,
    anchor_names,
    assemble_chapters,
    descendants,
    element_text,
    epub_types,
    first,
    image_type,
    local_name,
    looks_like_markup,
    parse_document,
    parse_xml,
    tokens,
)
from tomecast.readers.errors import InvalidEbookError

__all__ = ["InvalidEbookError", "read_epub"]

# Zip-bomb guard: the most uncompressed data read from one file in the EPUB, and in total.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

_CONTAINER_PATH = "META-INF/container.xml"
_ENCRYPTION_PATH = "META-INF/encryption.xml"
_PACKAGE_MEDIA_TYPE = "application/oebps-package+xml"
_NCX_MEDIA_TYPE = "application/x-dtbncx+xml"

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
        package = parse_xml(archive.read(package_path), package_path)
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
    container = parse_xml(archive.read(container_name), container_name)
    rootfiles = [
        element for element in descendants(container, "rootfile") if element.get("full-path")
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
    manifest_element = first(package, "manifest")
    if manifest_element is None:
        raise InvalidEbookError("the package document has no manifest")
    manifest: dict[str, _Item] = {}
    for element in descendants(manifest_element, "item"):
        item_id, href = element.get("id"), element.get("href")
        if not item_id or not href or item_id in manifest:
            continue
        path, _ = _resolve_href(package_path, href)
        manifest[item_id] = _Item(
            path=archive.canonical(path),
            media_type=(element.get("media-type") or "").partition(";")[0].strip().lower(),
            properties=tokens(element.get("properties")),
            fallback=element.get("fallback") or "",
        )
    return manifest


def _read_spine(package: etree._Element, manifest: Mapping[str, _Item]) -> list[_Item]:
    """Return the documents to read, in reading order.

    Items marked ``linear="no"`` and images are left out. Nothing else is skipped: an itemref
    naming no manifest item is an error, not a missing chapter.
    """
    spine = first(package, "spine")
    if spine is None:
        raise InvalidEbookError("the package document has no spine (reading order)")
    # Some books get the case of IDs wrong, so they're also matched case-insensitively.
    folded_ids: dict[str, str] = {}
    for item_id in manifest:
        folded_ids.setdefault(item_id.casefold(), item_id)
    documents: list[_Item] = []
    for itemref in descendants(spine, "itemref"):
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
    """Return the item to read for a spine item, or None if it has no book text.

    Images have no text, and an NCX (the EPUB 2 table of contents, which some books list in
    the spine by mistake) isn't book text. A spine item that isn't (X)HTML may name a fallback
    that is, as EPUB 3 allows. Failing that, it's still read, as long as it turns out to be
    markup (see :func:`_read_spine_item`).
    """
    if item.media_type == _NCX_MEDIA_TYPE:
        return None
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
    metadata = first(package, "metadata")
    if metadata is None:
        return "", (), None

    def values(name: str) -> list[str]:
        return [text for text in map(element_text, descendants(metadata, name)) if text]

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
    media_type = image_type(data)
    return Cover(data, media_type) if media_type else None


def _epub2_cover_id(package: etree._Element) -> str | None:
    """Return the manifest ID named by EPUB 2's ``<meta name="cover" content="ID">``."""
    metadata = first(package, "metadata")
    if metadata is None:
        return None
    metas = descendants(metadata, "meta")
    return next((meta.get("content") for meta in metas if meta.get("name") == "cover"), None)


def _reject_drm(archive: _Archive, spine: Sequence[_Item]) -> None:
    """Raise if the reading order is encrypted, which would otherwise be read as gibberish."""
    encryption_name = archive.find(_ENCRYPTION_PATH)
    if encryption_name is None:
        return
    encryption = parse_xml(archive.read(encryption_name), encryption_name)
    encrypted: set[str] = set()
    for data in descendants(encryption, "encrypteddata"):
        method = first(data, "encryptionmethod")
        if method is not None and method.get("Algorithm") in _FONT_OBFUSCATION:
            continue
        for reference in descendants(data, "cipherreference"):
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
    root = parse_document(archive.read(nav_path), nav_path)
    toc = next((nav for nav in descendants(root, "nav") if "toc" in epub_types(nav)), None)
    if toc is None:
        return []
    entries: list[_TocEntry] = []
    for link in descendants(toc, "a"):
        href = link.get("href")
        if href and not urlsplit(href).scheme:  # skip links to the web
            path, fragment = _resolve_href(nav_path, href)
            entries.append(_TocEntry(element_text(link), archive.canonical(path), fragment))
    return entries


def _find_ncx(package: etree._Element, manifest: Mapping[str, _Item]) -> _Item | None:
    """Find the NCX through the spine's ``toc`` attribute, else by its media type."""
    spine = first(package, "spine")
    toc_id = spine.get("toc") if spine is not None else None
    if toc_id and toc_id in manifest:
        return manifest[toc_id]
    return next((item for item in manifest.values() if item.media_type == _NCX_MEDIA_TYPE), None)


def _read_ncx(archive: _Archive, ncx_path: str) -> list[_TocEntry]:
    """Read the NCX ``navPoint``s in document order, which flattens nested ones in order."""
    root = parse_document(archive.read(ncx_path), ncx_path)
    nav_map = first(root, "navmap")
    if nav_map is None:
        return []
    entries: list[_TocEntry] = []
    for point in descendants(nav_map, "navpoint"):
        label = _child(point, "navlabel")
        content = _child(point, "content")
        src = content.get("src") if content is not None else None
        if src and not urlsplit(src).scheme:
            path, fragment = _resolve_href(ncx_path, src)
            title = element_text(label) if label is not None else ""
            entries.append(_TocEntry(title, archive.canonical(path), fragment))
    return entries


# --- Content documents ---------------------------------------------------------------------


def _read_chapters(
    archive: _Archive, spine: Sequence[_Item], toc: Sequence[_TocEntry]
) -> list[Chapter]:
    """Read every document in the spine and split the text into chapters."""
    spine_paths = {item.path for item in spine}
    toc_by_path: dict[str, list[_TocEntry]] = {}
    for entry in toc:
        if entry.path in spine_paths:
            toc_by_path.setdefault(entry.path, []).append(entry)

    events: list[Event] = []
    for item in spine:
        root = _read_spine_item(archive, item)
        document_events = _read_document(root, toc_by_path.get(item.path, ()))
        if not toc_by_path:
            # No usable table of contents: each file is a chapter. A file that opens with a
            # heading is titled by it; a heading further in stays where it is, so nothing is
            # spoken out of order.
            opening = next((e for e in document_events if isinstance(e, Block)), None)
            title = opening.text if opening is not None and opening.is_heading else ""
            document_events.insert(0, ChapterStart(title))
        events.extend(document_events)
    return assemble_chapters(events)


def _read_spine_item(archive: _Archive, item: _Item) -> etree._Element:
    """Parse a document from the reading order, refusing one that isn't markup at all."""
    data = archive.read(item.path)
    if item.media_type not in _TEXT_MEDIA_TYPES and not looks_like_markup(data):
        media_type = item.media_type or "no media type"
        raise InvalidEbookError(
            f"{item.path} ({media_type}) is in the reading order but isn't XHTML"
        )
    return parse_document(data, item.path)


def _read_document(root: etree._Element, entries: Sequence[_TocEntry]) -> list[Event]:
    """Turn one document into paragraphs, with chapter starts where the TOC entries point.

    An entry for the whole file, or for an id the file doesn't have, starts its chapter at the
    top of the file. An entry pointing inside skipped markup (such as Project Gutenberg's
    license section) starts no chapter, because nothing there is read.
    """
    ids = {name for element in root.iter() for name in anchor_names(element)}
    events: list[Event] = [ChapterStart(e.label) for e in entries if e.fragment not in ids]
    targets: dict[str, list[str]] = {}
    for entry in entries:
        if entry.fragment in ids:
            targets.setdefault(entry.fragment, []).append(entry.label)
    events.extend(DocumentWalker(targets).walk(root))
    return events


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


def _child(element: etree._Element, name: str) -> etree._Element | None:
    """Return the first direct child with this local name, or None."""
    return next((child for child in element if local_name(child) == name), None)
