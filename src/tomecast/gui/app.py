"""Start the desktop window: the Qt application, fonts, Python types for QML, and Main.qml."""

from __future__ import annotations

import argparse
import contextlib
import signal
import socket
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from PySide6.QtCore import QSocketNotifier, QUrl
from PySide6.QtGui import QFontDatabase, QGuiApplication, QIcon
from PySide6.QtQml import (
    QQmlApplicationEngine,
    QQmlImageProviderBase,
    qmlRegisterSingletonInstance,
)

from tomecast.gui.backend import Backend

_HERE = Path(__file__).resolve().parent
QML_MAIN = _HERE / "qml" / "Main.qml"
FONTS = _HERE / "fonts"
# The 256 px PNG: the window icon, for window managers that don't use the desktop entry.
WINDOW_ICON = _HERE / "icons" / "png" / "tomecast-256.png"


def load_fonts(folder: Path = FONTS) -> int:
    """Load every bundled .ttf (a missing or empty folder is fine). Returns how many loaded."""
    loaded = 0
    for font in sorted(folder.glob("*.ttf")) if folder.is_dir() else ():
        if QFontDatabase.addApplicationFont(str(font)) != -1:
            loaded += 1
    return loaded


def _register_dot_grid() -> None:
    try:
        from tomecast.gui.dotgrid import register_types
    except ImportError:
        return
    register_types()


CoverSink = Callable[[bytes | None], None]


def _image_provider() -> tuple[QQmlImageProviderBase | None, CoverSink | None]:
    """The ``dots`` image provider and its ``set_cover``, or ``(None, None)`` if missing."""
    try:
        from tomecast.gui.images import DotImageProvider
    except ImportError:
        return None, None
    provider = DotImageProvider()
    return provider, provider.set_cover


@contextlib.contextmanager
def quit_on_signals(app: QGuiApplication) -> Iterator[None]:
    """Quit cleanly on SIGTERM (logout, ``kill``) and Ctrl-C while the window is open.

    Python only runs signal handlers between bytecodes, and Qt's event loop keeps it inside C++,
    so without this the signals would be ignored until the system gave up and killed Tomecast
    (leaving no chance to stop the voice engine tidily). Python's C-level handler writes a byte
    to a socket that Qt watches; reading that byte runs Python again, and so the handler.
    """
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read)

    def drain() -> None:
        with contextlib.suppress(OSError):
            while reader.recv(256):
                pass

    notifier.activated.connect(drain)
    previous_fd = signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
    previous = {
        sig: signal.signal(sig, lambda *_: app.quit()) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        signal.set_wakeup_fd(previous_fd)
        notifier.setEnabled(False)
        reader.close()
        writer.close()


def run(argv: Sequence[str] | None = None) -> int:
    """Open the window (optionally with a book) and return the exit code."""
    parser = argparse.ArgumentParser(prog="tomecast gui", description="Open Tomecast's window.")
    parser.add_argument("book", nargs="?", type=Path, help="an ebook to open")
    args, qt_args = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))

    app = QGuiApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Tomecast")
    app.setOrganizationName("Tomecast")
    app.setDesktopFileName("tomecast")  # the Wayland app-id, which finds the launcher icon
    app.setWindowIcon(QIcon(str(WINDOW_ICON)))
    load_fonts()

    provider, cover_sink = _image_provider()
    # The backend is created before the QML engine and outlives it.
    backend = Backend(cover_sink=cover_sink)
    app.aboutToQuit.connect(backend.shutdown)
    # The stubs say bytes for the name, but PySide6 only accepts str here.
    qmlRegisterSingletonInstance(Backend, "TomecastNative", 1, 0, "Backend", backend)  # type: ignore[arg-type]
    _register_dot_grid()

    engine = QQmlApplicationEngine()
    if provider is not None:
        engine.addImageProvider("dots", provider)
    engine.load(QUrl.fromLocalFile(str(QML_MAIN)))
    if not engine.rootObjects():
        print(f"tomecast: error: couldn't load the window from {QML_MAIN}", file=sys.stderr)
        del engine
        backend.shutdown()
        return 1
    if args.book is not None:
        backend.openBook(QUrl.fromLocalFile(str(args.book.expanduser().resolve())))
    with quit_on_signals(app):
        code = app.exec()
    del engine  # the QML engine goes first, while the backend it uses still exists
    backend.shutdown()
    return code
