"""``DotImageProvider``: serves ``image://dots/<name>/<theme>`` to QML as Night Studio dots."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtQuick import QQuickImageProvider

from tomecast.gui.dots import render_dots

ART_PATH = Path(__file__).resolve().parent / "assets" / "cantoria.jpg"
DEFAULT_WIDTH = 240
_NAMES = ("cover", "cantoria")
_THEMES = ("dark", "light")
MAX_COVER_ASPECT = 4  # covers taller than 1:4 or wider than 4:1 count as no cover


def _screen_scale() -> float:
    """The app's screen scale (1.0, 1.5, 2.0...), or 1.0 before the app exists."""
    app = QGuiApplication.instance()
    if isinstance(app, QGuiApplication):
        return max(1.0, app.devicePixelRatio())
    return 1.0


def _transparent() -> QImage:
    image = QImage(1, 1, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    return image


def _reasonable_shape(image: QImage) -> bool:
    """False for a sliver of an image (a 1x4000 'cover'), which would render enormous."""
    width, height = image.width(), image.height()
    return width * MAX_COVER_ASPECT >= height and height * MAX_COVER_ASPECT >= width


class DotImageProvider(QQuickImageProvider):
    """Image provider for ``cover/<theme>`` and ``cantoria/<theme>`` (ignores a ``?n`` suffix)."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._lock = threading.Lock()
        self._cover: QImage | None = None
        self._art: QImage | None = None
        self._cache: dict[tuple[str, str, int, float], QImage] = {}

    def set_cover(self, data: bytes | None) -> None:
        """Remember the book cover (image file bytes), or forget it with ``None``."""
        image = QImage.fromData(data) if data else QImage()
        if image.isNull() or not _reasonable_shape(image):
            image = QImage()
        with self._lock:
            self._cover = None if image.isNull() else image
            self._cache = {k: v for k, v in self._cache.items() if k[0] != "cover"}

    def requestImage(self, id: str, size: QSize, requestedSize: QSize) -> QImage:
        parts = id.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] not in _NAMES or parts[1] not in _THEMES:
            return _transparent()
        name, theme = parts
        # Qt asks in screen pixels (an Image's sourceSize times the screen's scale), so the dots
        # are drawn for the real screen: crisp, and round even on a 1x screen.
        dpr = _screen_scale()
        if requestedSize.width() > 0:
            width = max(1, round(requestedSize.width() / dpr))
        else:
            width = DEFAULT_WIDTH
        key = (name, theme, width, dpr)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            source = self._source(name)
        if source is None:
            return _transparent()
        # Render outside the lock, so a slow render never blocks set_cover on the GUI thread.
        image = render_dots(source, width, dark=theme == "dark", device_pixel_ratio=dpr)
        with self._lock:
            if self._source(name) is source:  # not replaced while rendering
                self._cache[key] = image
        return image

    def _source(self, name: str) -> QImage | None:
        # Called with the lock held.
        if name == "cover":
            return self._cover
        if self._art is None:
            art = QImage(str(ART_PATH))
            if art.isNull():
                return None
            self._art = art
        return self._art
