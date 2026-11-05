"""Render every screen of the desktop window, in both themes, to PNG files, headless.

    uv run python scripts/gui_screenshots.py [--out local/screenshots] [--cover IMAGE]
                                             [--width 1200 520]

Uses the demo backend in ``tests/gui/demo_backend.py`` (Alice in Wonderland's real numbers),
the bundled fonts, the real ``DotGrid`` and the ``dots`` image provider when they exist, and
stand-ins when they don't. Each screen is drawn at each width (default 1200 and 520; any width
from 400 up) at its own height (the taller of the mockup's height and what the content needs) to
``<out>/<screen>-<theme>-<width>.png``.
Any QML warning or error fails the run, after every screen has been tried.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType

# Always headless, and at scale 1 unless --scale asks otherwise (1.6 shows what a fractionally
# scaled screen sees), even inside a desktop session that sets these. Qt takes the scale from
# the environment, so --scale is read here, before Qt starts.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
os.environ["QT_SCALE_FACTOR"] = next(
    (sys.argv[i + 2] for i, arg in enumerate(sys.argv[1:-1]) if arg == "--scale"), "1"
)
os.environ.pop("QT_SCREEN_SCALE_FACTORS", None)

from PySide6.QtCore import (
    QMessageLogContext,
    QObject,
    QtMsgType,
    QUrl,
    qInstallMessageHandler,
)
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuick import QQuickItem, QQuickWindow

ROOT = Path(__file__).resolve().parent.parent
GUI_DIR = ROOT / "src" / "tomecast" / "gui"
MAIN_QML = GUI_DIR / "qml" / "Main.qml"
FONTS_DIR = GUI_DIR / "fonts"
DEMO_BACKEND = ROOT / "tests" / "gui" / "demo_backend.py"
DEFAULT_COVER = ROOT / "local" / "mockups" / "assets" / "alice-cover-src.png"
DEFAULT_OUT = ROOT / "local" / "screenshots"
SCREENS = ("drop", "chapters", "voice", "converting", "done")
THEMES = ("dark", "light")
WIDTHS = (1200, 520)
MIN_WIDTH = 400

# Platform chatter that isn't about our QML.
_IGNORED = ("propagateSizeHints",)


class Problems:
    """Every warning, critical or fatal message Qt reports while rendering."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def handle(self, mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
        if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
            return
        if any(text in message for text in _IGNORED):
            return
        where = f"{context.file}:{context.line}: " if context.file else ""
        self.add(f"{where}{message}")

    def add_qml(self, errors: list[QQmlError]) -> None:
        for error in errors:
            self.add(error.toString())

    def add(self, message: str) -> None:
        if message not in self.messages:
            self.messages.append(message)
            print(f"QML problem: {message}", file=sys.stderr)


def load_demo_backend() -> ModuleType:
    """Import ``tests/gui/demo_backend.py`` (``tests`` isn't a package)."""
    spec = importlib.util.spec_from_file_location("demo_backend", DEMO_BACKEND)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Can't load the demo backend from {DEMO_BACKEND}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_backend"] = module
    spec.loader.exec_module(module)
    return module


def load_fonts() -> int:
    """Add the bundled fonts; warn and fall back to the system's fonts if there are none."""
    files = sorted(FONTS_DIR.glob("*.ttf")) if FONTS_DIR.is_dir() else []
    loaded = sum(1 for path in files if QFontDatabase.addApplicationFont(str(path)) != -1)
    if loaded == 0:
        print(
            f"warning: no fonts loaded from {FONTS_DIR}; using the system's default fonts",
            file=sys.stderr,
        )
    elif loaded < len(files):
        print(
            f"warning: only {loaded} of {len(files)} fonts in {FONTS_DIR} loaded", file=sys.stderr
        )
    return loaded


def settle(app: QGuiApplication, seconds: float = 0.05) -> None:
    """Let bindings, layouts and pending events run."""
    deadline = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)


def wait_for_images(app: QGuiApplication, root: QObject, timeout: float = 20.0) -> None:
    """Wait until no visible DotImage is still loading (they render on a worker thread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        loading = [
            item
            for item in root.findChildren(QQuickItem)
            if item.isVisible() and item.property("imageLoading") is True
        ]
        if not loading:
            return
        settle(app, 0.05)
    print("warning: images were still loading when the screenshot was taken", file=sys.stderr)


def find_screen(window: QQuickWindow, screen: str) -> QQuickItem:
    # Items declared inside the ApplicationWindow are QObject children of the window itself.
    # An unloaded screen lingers (invisible, parentless) until Qt deletes it: take the live one.
    pages = [
        item
        for item in window.findChildren(QQuickItem, f"{screen}Screen")
        if item.isVisible() and item.parentItem() is not None
    ]
    page = pages[0] if pages else None
    if page is None:
        raise SystemExit(f"The {screen} screen didn't load (no item named {screen}Screen)")
    return page


def render(
    app: QGuiApplication,
    window: QQuickWindow,
    backend: QObject,
    out: Path,
    problems: Problems,
    width: int,
) -> list[Path]:
    show = getattr(backend, "show")  # noqa: B009  (the demo module is loaded dynamically)
    written: list[Path] = []
    for theme in THEMES:
        for screen in SCREENS:
            show(screen, theme)
            window.resize(width, window.height())
            settle(app)
            page = find_screen(window, screen)
            # Lay out at the mockup's height first, then grow until the content fits.
            floor = int(page.property("designHeight"))
            height = floor
            for _ in range(6):
                window.resize(width, height)
                settle(app, 0.25)  # lists lay out their rows over a few frames
                wait_for_images(app, window)
                settle(app, 0.1)
                window.grabWindow()  # a rendered frame runs the layout polish that's still due
                settle(app, 0.05)
                needed = max(floor, int(page.property("naturalHeight")))
                if needed == height:
                    break
                height = needed
            scale = os.environ["QT_SCALE_FACTOR"]
            suffix = "" if float(scale) == 1 else f"@{scale}x"
            path = out / f"{screen}-{theme}-{width}{suffix}.png"
            image = window.grabWindow()
            if image.isNull() or not image.save(str(path)):
                problems.add(f"couldn't save {path}")
                continue
            written.append(path)
            print(
                f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}"
                f"  {image.width()}x{image.height()}"
            )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="folder for the PNG files")
    parser.add_argument(
        "--cover",
        type=Path,
        default=DEFAULT_COVER,
        help="book cover for the demo (default: the Alice cover in local/mockups, if present)",
    )
    parser.add_argument(
        "--width",
        type=int,
        nargs="+",
        default=list(WIDTHS),
        help=f"window width(s) (default {' '.join(map(str, WIDTHS))}); file names end in -<width>",
    )
    parser.add_argument(
        "--scale",
        default="1",
        help="screen scale to render at, e.g. 1.6 (default 1); file names end in @<scale>x",
    )
    args = parser.parse_args(argv)
    if any(width < MIN_WIDTH for width in args.width):
        parser.error(f"the window's minimum width is {MIN_WIDTH}")

    problems = Problems()
    qInstallMessageHandler(problems.handle)
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("Tomecast")

    demo = load_demo_backend()
    load_fonts()
    if demo.register_dot_grid() != "real":
        print("warning: tomecast.gui.dotgrid is missing; using a stand-in DotGrid", file=sys.stderr)

    backend = demo.register(None)  # before the engine exists (see demo_backend.register)
    engine = QQmlApplicationEngine()
    engine.warnings.connect(problems.add_qml)
    backend.set_reduce_motion(True)  # no cross-fades between screenshots

    cover = args.cover.read_bytes() if args.cover.is_file() else None
    if cover is None:
        print(f"warning: no cover at {args.cover}; the demo book has no cover", file=sys.stderr)
    provider, kind = demo.make_image_provider(cover)
    if kind != "real":
        print(
            "warning: tomecast.gui.images is missing; using grey placeholder images",
            file=sys.stderr,
        )
    engine.addImageProvider("dots", provider)

    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    roots = engine.rootObjects()
    if not roots or not isinstance(roots[0], QQuickWindow):
        problems.add(f"{MAIN_QML} didn't load")
        return report(problems, [])
    window = roots[0]

    args.out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for width in args.width:
        written += render(app, window, backend, args.out, problems, width)
    window.close()
    return report(problems, written)


def report(problems: Problems, written: list[Path]) -> int:
    if problems.messages:
        print(f"\nFAILED: {len(problems.messages)} QML warning(s) or error(s):", file=sys.stderr)
        for message in problems.messages:
            print(f"  {message}", file=sys.stderr)
        return 1
    print(f"Wrote {len(written)} screenshots.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
