"""Build small EPUB 2 and EPUB 3 files for tests, so no real (or copyrighted) books are needed."""

from __future__ import annotations

import posixpath
import struct
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

XHTML_1_1_DOCTYPE = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
)


@dataclass(frozen=True)
class Page:
    """One XHTML file in the reading order."""

    name: str  # path relative to the package document's folder, like "Text/chapter 1.xhtml"
    body: str = ""  # markup placed inside <body>
    linear: bool = True
    source: str | None = None  # a complete file, used as-is instead of wrapping ``body``
    media_type: str = "application/xhtml+xml"  # as written in the manifest
    encoding: str = "utf-8"  # how the file is stored (Python codec name)


@dataclass(frozen=True)
class NavPoint:
    """One table-of-contents entry, with optional nested entries."""

    label: str
    href: str  # written into the nav document or NCX as-is
    children: Sequence[NavPoint] = ()


def xhtml(
    body: str, *, doctype: str = "<!DOCTYPE html>", declaration: bool = True, title: str = "Page"
) -> str:
    """Wrap body markup in a complete XHTML file whose head holds text that must never be read."""
    xml_declaration = '<?xml version="1.0" encoding="utf-8"?>\n' if declaration else ""
    return f"""{xml_declaration}{doctype}
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head>
  <title>{escape(title)}</title>
  <style>p {{ text-indent: 1em; }}</style>
  <script>var neverRead = "Script text in the head";</script>
</head>
<body>
{body}
</body>
</html>
"""


def tiny_png() -> bytes:
    """Return a valid 1x1 PNG image."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1 pixels, 8-bit RGB
    pixels = zlib.compress(b"\x00\xff\x00\x00")  # one row: a filter byte and a red pixel
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


def build_epub(
    path: Path,
    pages: Sequence[Page],
    *,
    version: Literal[2, 3] = 3,
    toc: Sequence[NavPoint] | None = None,
    title: str | None = "Test Book",
    authors: Sequence[str] = ("Test Author",),
    language: str | None = "en",
    cover: bytes | None = None,
    cover_media_type: str = "image/png",
    package_dir: str = "OEBPS",
    nav_name: str = "nav.xhtml",
    extra_files: Mapping[str, bytes] | None = None,
) -> Path:
    """Write an EPUB file to ``path`` and return ``path``.

    EPUB 3 books get a nav document (with a landmarks list before the table of contents) and
    mark the cover with ``properties="cover-image"``. EPUB 2 books get an NCX and a
    ``<meta name="cover">``. ``toc=None`` leaves out the table of contents entirely. Hrefs in
    the package document are URL-encoded, the way real books write them.
    """
    files: dict[str, bytes] = {}
    manifest: list[str] = []
    spine: list[str] = []

    def add(name: str, data: bytes, item_id: str, media_type: str, properties: str = "") -> None:
        files[posixpath.normpath(posixpath.join(package_dir, name))] = data
        extra = f" properties={quoteattr(properties)}" if properties else ""
        manifest.append(
            f"<item id={quoteattr(item_id)} href={quoteattr(quote(name))} "
            f"media-type={quoteattr(media_type)}{extra}/>"
        )

    for number, page in enumerate(pages, start=1):
        source = page.source if page.source is not None else xhtml(page.body)
        add(page.name, source.encode(page.encoding), f"page{number}", page.media_type)
        linear = "" if page.linear else ' linear="no"'
        spine.append(f'<itemref idref="page{number}"{linear}/>')

    if toc is not None and version == 3:
        add(nav_name, _nav_document(toc).encode("utf-8"), "nav", "application/xhtml+xml", "nav")
    if toc is not None and version == 2:
        add("toc.ncx", _ncx_document(toc).encode("utf-8"), "ncx", "application/x-dtbncx+xml")

    cover_meta = ""
    if cover is not None:
        extension = "png" if cover_media_type == "image/png" else "img"
        properties = "cover-image" if version == 3 else ""
        add(f"images/cover.{extension}", cover, "cover-image", cover_media_type, properties)
        if version == 2:
            cover_meta = '<meta name="cover" content="cover-image"/>'

    metadata = [
        '<dc:identifier id="uid">urn:uuid:00000000-0000-4000-8000-000000000000</dc:identifier>',
        *([f"<dc:title>{escape(title)}</dc:title>"] if title is not None else []),
        *(f"<dc:creator>{escape(author)}</dc:creator>" for author in authors),
        *([f"<dc:language>{escape(language)}</dc:language>"] if language is not None else []),
        cover_meta,
    ]
    spine_toc = ' toc="ncx"' if toc is not None and version == 2 else ""
    indent = "\n    "  # f-strings can't hold backslashes before Python 3.12
    package = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{version}.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    {indent.join(metadata)}
  </metadata>
  <manifest>
    {indent.join(manifest)}
  </manifest>
  <spine{spine_toc}>
    {indent.join(spine)}
  </spine>
</package>
"""
    package_path = posixpath.join(package_dir, "content.opf")
    container = f"""<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path={quoteattr(package_path)} media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as epub:
        # The mimetype file must come first, uncompressed.
        epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        epub.writestr("META-INF/container.xml", container)
        epub.writestr(package_path, package)
        for name, data in {**files, **(extra_files or {})}.items():
            epub.writestr(name, data)
    return path


def without_file(epub: Path, name: str) -> Path:
    """Rewrite an EPUB without one of its files (to make broken books), and return its path."""
    with zipfile.ZipFile(epub) as original:
        contents = [(info, original.read(info)) for info in original.infolist()]
    with zipfile.ZipFile(epub, "w") as rewritten:
        for info, data in contents:
            if info.filename != name:
                rewritten.writestr(info, data)
    return epub


def replace_in_file(epub: Path, name: str, old: str, new: str) -> Path:
    """Rewrite an EPUB with ``old`` replaced by ``new`` in one of its text files."""
    with zipfile.ZipFile(epub) as original:
        contents = [(info, original.read(info)) for info in original.infolist()]
    with zipfile.ZipFile(epub, "w") as rewritten:
        for info, data in contents:
            if info.filename == name:
                text = data.decode("utf-8")
                if old not in text:
                    raise ValueError(f"{old!r} isn't in {name}")
                data = text.replace(old, new).encode("utf-8")
            rewritten.writestr(info, data)
    return epub


def _nav_document(toc: Sequence[NavPoint]) -> str:
    def items(points: Sequence[NavPoint]) -> str:
        return "".join(
            f"<li><a href={quoteattr(point.href)}>{escape(point.label)}</a>"
            + (f"<ol>{items(point.children)}</ol>" if point.children else "")
            + "</li>"
            for point in points
        )

    # A landmarks list comes first, so a reader that grabs the wrong <nav> gets wrong titles.
    landmarks = (
        f'<nav epub:type="landmarks"><ol><li><a epub:type="bodymatter" '
        f"href={quoteattr(toc[0].href)}>Start of Content</a></li></ol></nav>"
        if toc
        else ""
    )
    body = f'{landmarks}<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{items(toc)}</ol></nav>'
    return xhtml(body, title="Contents")


def _ncx_document(toc: Sequence[NavPoint]) -> str:
    counter = 0

    def nav_points(points: Sequence[NavPoint]) -> str:
        nonlocal counter
        parts = []
        for point in points:
            counter += 1
            parts.append(
                f'<navPoint id="point{counter}" playOrder="{counter}">'
                f"<navLabel><text>{escape(point.label)}</text></navLabel>"
                f"<content src={quoteattr(point.href)}/>"
                f"{nav_points(point.children)}</navPoint>"
            )
        return "".join(parts)

    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:00000000-0000-4000-8000-000000000000"/></head>
  <docTitle><text>Book title, not a chapter</text></docTitle>
  <navMap>{nav_points(toc)}</navMap>
</ncx>
"""
