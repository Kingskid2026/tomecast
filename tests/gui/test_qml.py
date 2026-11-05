"""The QML window, driven by the demo backend: every screen, both themes, no QML warnings."""

from __future__ import annotations

import os

# Headless, whatever the desktop session sets (conftest uses setdefault, which a session's
# QT_QPA_PLATFORM=wayland would win over). This runs at collection, before any QGuiApplication.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"

from collections.abc import Iterator
from pathlib import Path

import demo_backend
import pytest
from demo_backend import SCREENS, DemoBackend
from PySide6.QtCore import (
    Q_ARG,
    QMessageLogContext,
    QMetaObject,
    QPointF,
    Qt,
    QtMsgType,
    QUrl,
    qInstallMessageHandler,
)
from PySide6.QtGui import QAccessible, QColor, QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

GUI_DIR = Path(__file__).resolve().parents[2] / "src" / "tomecast" / "gui"
MAIN_QML = GUI_DIR / "qml" / "Main.qml"
FONTS_DIR = GUI_DIR / "fonts"
THEMES = ("dark", "light")
# Platform chatter that isn't about our QML.
IGNORED = ("propagateSizeHints",)


class Messages:
    """Qt warnings and QML errors seen while a window is open."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def handle(self, mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
        if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
            return
        if not any(text in message for text in IGNORED):
            self.problems.append(message)

    def add_qml(self, errors: list[QQmlError]) -> None:
        self.problems.extend(error.toString() for error in errors)


class Window:
    """Main.qml loaded with the demo backend, plus helpers to look inside it."""

    def __init__(self, qtbot: QtBot, window: QQuickWindow, backend: DemoBackend) -> None:
        self.qtbot = qtbot
        self.window = window
        self.backend = backend

    def find(self, name: str) -> QQuickItem | None:
        """The visible-tree item with this objectName (works inside Loaders and delegates)."""
        stack = [self.window.contentItem()]
        while stack:
            item = stack.pop()
            if item.objectName() == name:
                return item
            stack.extend(item.childItems())
        return None

    def get(self, name: str) -> QQuickItem:
        item = self.find(name)
        assert item is not None, f"no item named {name!r}"
        return item

    def text(self, name: str) -> str:
        return str(self.get(name).property("text"))

    def show(self, screen: str, theme: str | None = None) -> QQuickItem:
        self.backend.show(screen, theme)
        self.qtbot.waitUntil(lambda: self.find(f"{screen}Screen") is not None, timeout=3000)
        page = self.get(f"{screen}Screen")
        self.qtbot.waitUntil(lambda: bool(page.isVisible()), timeout=3000)
        self.settle()
        return page

    def settle(self) -> None:
        """Let bindings, layout polish (done per frame) and image loading finish."""
        self.qtbot.wait(40)
        self.qtbot.waitUntil(self._images_loaded, timeout=20000)
        self.qtbot.wait(20)

    def _images_loaded(self) -> bool:
        return not any(
            item.isVisible() and item.property("imageLoading") is True
            for item in self.window.contentItem().findChildren(QQuickItem)
        )

    def scroll_into_view(self, item: QQuickItem) -> None:
        """Scroll the page holding ``item`` so it's on screen, as a person would."""
        node = item.parentItem()
        while node is not None:
            meta = node.metaObject()
            if meta.indexOfMethod("ensureVisible(QVariant)") != -1:
                QMetaObject.invokeMethod(node, "ensureVisible", Q_ARG("QVariant", item))
                self.settle()
                return
            node = node.parentItem()

    def click(self, name_or_item: str | QQuickItem, at: QPointF | None = None) -> None:
        item = self.get(name_or_item) if isinstance(name_or_item, str) else name_or_item
        self.scroll_into_view(item)
        local = at if at is not None else QPointF(item.width() / 2, item.height() / 2)
        point = item.mapToScene(local).toPoint()
        QTest.mouseClick(
            self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point
        )
        self.settle()


@pytest.fixture(scope="session")
def fonts(qapp: QGuiApplication) -> int:
    return sum(
        1
        for path in sorted(FONTS_DIR.glob("*.ttf"))
        if QFontDatabase.addApplicationFont(str(path)) != -1
    )


@pytest.fixture(scope="session")
def backend(qapp: QGuiApplication) -> DemoBackend:
    # Before any engine exists (see demo_backend.register).
    demo_backend.register_dot_grid()
    return demo_backend.register(None)


@pytest.fixture
def messages() -> Iterator[Messages]:
    collected = Messages()
    previous = qInstallMessageHandler(collected.handle)
    yield collected
    qInstallMessageHandler(previous)


class Sink:
    """Forwards the engine's QML errors to the running test's Messages."""

    def __init__(self) -> None:
        self.target = Messages()

    def add_qml(self, errors: list[QQmlError]) -> None:
        self.target.add_qml(errors)


@pytest.fixture(scope="session")
def session_window(
    qapp: QGuiApplication, fonts: int, backend: DemoBackend
) -> Iterator[tuple[QQuickWindow, Sink]]:
    """One engine and window for the whole run, like the app.

    A second engine in the same process doesn't see the singleton Backend's change signals
    (PySide6 6.12), so tests share this window and reset the backend instead.
    """
    sink = Sink()
    previous = qInstallMessageHandler(sink.target.handle)
    backend.reset()
    backend.set_reduce_motion(True)
    engine = QQmlApplicationEngine()
    engine.warnings.connect(sink.add_qml)
    provider, _kind = demo_backend.make_image_provider(None)
    engine.addImageProvider("dots", provider)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    qInstallMessageHandler(previous)
    roots = engine.rootObjects()
    if not roots or not isinstance(roots[0], QQuickWindow) or sink.target.problems:
        pytest.fail(f"Main.qml didn't load cleanly: {sink.target.problems}")
    window = roots[0]
    assert isinstance(window, QQuickWindow)
    window.show()
    QTest.qWaitForWindowExposed(window)
    yield window, sink
    window.close()
    del engine
    QGuiApplication.processEvents()


@pytest.fixture
def app_window(
    qtbot: QtBot,
    session_window: tuple[QQuickWindow, Sink],
    backend: DemoBackend,
    messages: Messages,
) -> Iterator[Window]:
    window, sink = session_window
    sink.target = messages
    backend.reset()
    backend.set_reduce_motion(True)
    window.resize(1200, 780)
    helper = Window(qtbot, window, backend)
    qtbot.waitUntil(lambda: helper.find("dropScreen") is not None, timeout=3000)
    helper.settle()
    yield helper
    helper.settle()
    sink.target = Messages()


def assert_clean(messages: Messages) -> None:
    assert messages.problems == [], "\n".join(messages.problems)


# -- loading --------------------------------------------------------------------------------------


def test_every_screen_in_both_themes_loads_without_warnings(
    app_window: Window, messages: Messages
) -> None:
    window = app_window.window
    assert window.objectName() == "mainWindow"
    assert (window.width(), window.height()) == (1200, 780)
    assert (window.minimumWidth(), window.minimumHeight()) == (400, 560)
    for theme in THEMES:
        for screen in SCREENS:
            page = app_window.show(screen, theme)
            assert page.isVisible()
            others = [s for s in SCREENS if s != screen]
            assert all(app_window.find(f"{other}Screen") is None for other in others)
    assert_clean(messages)


def test_theme_switches_the_palette(app_window: Window, messages: Messages) -> None:
    window = app_window.window
    assert window.property("color") == QColor("#0B0B0C")
    app_window.backend.setThemeSetting("light")
    app_window.settle()
    assert window.property("color") == QColor("#F2F2F2")
    app_window.backend.setThemeSetting("dark")
    app_window.settle()
    assert window.property("color") == QColor("#0B0B0C")
    assert_clean(messages)


# -- the screens' texts ---------------------------------------------------------------------------


def test_drop_screen_texts(app_window: Window, messages: Messages) -> None:
    app_window.show("drop")
    assert app_window.text("dropTitle") == "DROP A BOOK HERE"
    assert app_window.text("formatsLabel") == "EPUB · PDF · DOCX · FB2 · HTML · TXT"
    assert app_window.text("hardwareLabel") == "CPU · ABOUT 4.7× REAL TIME"
    assert app_window.find("artCredit") is None  # no art credit under the Cantoria
    assert app_window.text("chooseBookButton") == "Choose a book"
    assert app_window.text("noBookLabel") == "NO BOOK YET"
    assert app_window.get("artImage").property("source") == QUrl("image://dots/cantoria/dark")
    assert not app_window.get("errorBanner").isVisible()
    assert_clean(messages)


def test_chapters_screen_lists_every_section(app_window: Window, messages: Messages) -> None:
    app_window.show("chapters")
    assert app_window.text("screenTitle") == "CHAPTERS"
    assert app_window.text("bookTitle") == "Alice's Adventures in Wonderland"
    assert app_window.text("bookAuthors") == "Lewis Carroll"
    assert app_window.get("chapterList").property("count") == 15
    assert app_window.text("summary_sections") == "12 OF 15 SECTIONS"
    assert app_window.text("summary_words") == "26,371 WORDS"
    assert app_window.text("summary_audio") == "ABOUT 2 H 47 MIN OF AUDIO"
    assert app_window.text("summary_make") == "ABOUT 36 MIN TO MAKE HERE"
    first_chapter = app_window.get("chapterRow3")
    titles = [i for i in first_chapter.findChildren(QQuickItem) if i.objectName() == "chapterTitle"]
    assert [t.property("text") for t in titles] == ["Down the Rabbit-Hole"]
    assert app_window.get("skipFrontMatterSwitch").property("checked") is True
    assert_clean(messages)


def test_voice_screen_texts(app_window: Window, messages: Messages) -> None:
    app_window.show("voice")
    assert app_window.text("screenTitle") == "VOICE"
    assert app_window.text("previewVoiceLabel") == "PREVIEW · HEART · AMERICAN · FEMALE"
    assert app_window.text("speedValue") == "1.00×"
    assert app_window.get("voice_af_heart").property("chosen") is True
    assert app_window.get("voice_bm_george").property("chosen") is False
    assert app_window.text("previewText").startswith("Alice was beginning to get very tired")
    assert app_window.get("speedMeter").property("current") == 2  # 1.00x is the third dot
    assert app_window.text("speedMinLabel") == "0.5×"
    assert app_window.text("speedMaxLabel") == "3×"
    assert_clean(messages)


def test_converting_screen_texts(app_window: Window, messages: Messages) -> None:
    app_window.show("converting")
    assert app_window.text("screenTitle") == "CONVERTING"
    assert app_window.text("chapterLabel") == "A CAUCUS-RACE AND A LONG TALE"
    assert app_window.text("timeLeft") == "00:30:32"
    assert app_window.text("progressLabel") == "225 / 1,610 PIECES · 14%"
    assert app_window.text("convertingStatus") == (
        "CPU · ABOUT 4.7× REAL TIME · VOICE HEART · 1.00×"
    )
    assert app_window.text("nowSpeaking").startswith("They were indeed a queer-looking party")
    grid = app_window.get("sentenceGrid")
    assert (grid.property("total"), grid.property("done")) == (1610, 225)
    assert grid.property("complete") is False
    assert app_window.get("timeLeftReadout").isVisible()
    assert not app_window.get("downloadLabel").isVisible()
    assert_clean(messages)


def test_done_screen_texts(app_window: Window, messages: Messages) -> None:
    app_window.show("done")
    assert app_window.text("screenTitle") == "DONE"
    assert app_window.text("audioLength") == "2:47:00"
    assert app_window.text("doneSummary") == (
        "OF AUDIO · 12 CHAPTERS · ABOUT 79 MB · MADE IN ABOUT 36 MIN"
    )
    assert app_window.text("checkedLabel") == "EVERY SENTENCE CHECKED · 1,610 OF 1,610"
    assert app_window.text("finishedLabel") == "FINISHED · ALICE'S ADVENTURES IN WONDERLAND"
    assert app_window.text("outputPath") == "~/Books/Alice’s Adventures in Wonderland.m4b"
    grid = app_window.get("completeGrid")
    assert grid.property("complete") is True
    assert grid.property("total") == 1610
    assert not app_window.get("doneWarnings").isVisible()
    app_window.backend.set_warnings(["The cover was damaged, so it was left out."])
    app_window.settle()
    assert app_window.get("doneWarnings").isVisible()
    assert_clean(messages)


# -- interaction ----------------------------------------------------------------------------------


def test_choosing_chapters_updates_the_summary(app_window: Window, messages: Messages) -> None:
    app_window.show("chapters")
    app_window.click("chapterRow3")
    assert ("setChapterSelected", (3, False)) in app_window.backend.calls
    assert app_window.text("summary_sections") == "11 OF 15 SECTIONS"
    assert app_window.text("summary_words") == "24,190 WORDS"

    app_window.click("skipFrontMatterSwitch")
    assert ("setSkipFrontMatter", (False,)) in app_window.backend.calls
    assert app_window.get("skipFrontMatterSwitch").property("checked") is False
    assert app_window.text("summary_sections") == "14 OF 15 SECTIONS"

    app_window.click("chooseVoiceButton")
    app_window.qtbot.waitUntil(lambda: app_window.find("voiceScreen") is not None)
    assert_clean(messages)


def test_voice_and_speed_controls(app_window: Window, messages: Messages) -> None:
    app_window.show("voice")
    app_window.click("voice_bf_emma")
    assert app_window.backend.property("voice") == "bf_emma"
    assert app_window.text("previewVoiceLabel") == "PREVIEW · EMMA · BRITISH · FEMALE"

    meter = app_window.get("speedMeter")
    # Eleven 28 px targets spread across the meter: click the eighth (speed 2.25).
    step = (meter.width() - 28) / 10
    app_window.click(meter, QPointF(14 + 7 * step, meter.height() / 2))
    assert app_window.backend.property("speed") == pytest.approx(2.25)
    assert app_window.text("speedValue") == "2.25×"

    preview = app_window.get("previewButton")
    assert preview.property("glyph") == "play"
    app_window.click(preview)
    assert app_window.backend.property("previewPlaying") is True
    assert preview.property("glyph") == "pause"
    assert preview.property("text") == "Pause preview"

    app_window.click("startConvertingButton")
    app_window.qtbot.waitUntil(lambda: app_window.find("convertingScreen") is not None)
    assert_clean(messages)


def test_speed_meter_follows_the_keyboard(app_window: Window, messages: Messages) -> None:
    app_window.show("voice")
    meter = app_window.get("speedMeter")
    meter.forceActiveFocus()
    QTest.keyClick(app_window.window, Qt.Key.Key_Right)
    assert app_window.backend.property("speed") == pytest.approx(1.25)
    QTest.keyClick(app_window.window, Qt.Key.Key_Home)
    assert app_window.backend.property("speed") == pytest.approx(0.5)
    QTest.keyClick(app_window.window, Qt.Key.Key_End)
    assert app_window.backend.property("speed") == pytest.approx(3.0)
    assert_clean(messages)


def test_pause_and_resume(app_window: Window, messages: Messages) -> None:
    app_window.show("converting")
    button = app_window.get("pauseButton")
    assert button.property("text") == "Pause converting"
    app_window.click(button)
    assert app_window.backend.property("paused") is True
    assert button.property("text") == "Resume converting"
    assert app_window.text("pauseNote") == "Paused. Everything voiced so far is kept."
    app_window.click(button)
    assert app_window.backend.property("paused") is False
    assert app_window.text("pauseNote") == "Pause any time. Everything voiced so far is kept."
    assert_clean(messages)


def test_downloading_shows_the_download_instead_of_time_left(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("converting")
    app_window.backend.set_stage("downloading")
    app_window.settle()
    assert not app_window.get("timeLeftReadout").isVisible()
    assert app_window.get("downloadLabel").isVisible()
    assert app_window.text("downloadLabel") == (
        "DOWNLOADING ONCE · KOKORO VOICE MODEL (82M, 8-BIT) · 45% OF 190 MB"
    )
    assert_clean(messages)


def test_error_banner_shows_and_dismisses(app_window: Window, messages: Messages) -> None:
    app_window.show("drop")
    app_window.backend.set_error("That file isn't a book Tomecast can read.")
    app_window.settle()
    assert app_window.get("errorBanner").isVisible()
    assert app_window.text("errorText") == "That file isn't a book Tomecast can read."
    app_window.click("dismissErrorButton")
    assert app_window.backend.property("errorMessage") == ""
    assert not app_window.get("errorBanner").isVisible()
    assert_clean(messages)


def test_done_buttons(app_window: Window, messages: Messages) -> None:
    app_window.show("done")
    app_window.click("openFolderButton")
    assert ("openOutputFolder", ()) in app_window.backend.calls
    app_window.click("convertAnotherButton")
    app_window.qtbot.waitUntil(lambda: app_window.find("dropScreen") is not None)
    assert_clean(messages)


def test_screens_cross_fade_unless_motion_is_reduced(
    app_window: Window, messages: Messages
) -> None:
    app_window.backend.set_reduce_motion(False)
    app_window.backend.goTo("chapters")
    QGuiApplication.processEvents()
    # The new screen loads at once and fades in over the old one, which then unloads.
    assert app_window.find("dropScreen") is not None
    assert app_window.find("chaptersScreen") is not None
    app_window.qtbot.waitUntil(lambda: app_window.find("dropScreen") is None, timeout=2000)
    app_window.backend.set_reduce_motion(True)
    app_window.backend.goTo("voice")
    app_window.settle()
    assert app_window.find("chaptersScreen") is None
    assert_clean(messages)


def test_keyboard_focus_reaches_the_buttons(app_window: Window, messages: Messages) -> None:
    app_window.show("drop")
    window = app_window.window
    window.requestActivate()
    QTest.keyClick(window, Qt.Key.Key_Tab)
    app_window.settle()
    focused = window.activeFocusItem()
    assert focused is not None
    assert focused.objectName() == "chooseBookButton"
    assert focused.property("visualFocus") is True
    assert_clean(messages)


WIDTHS = [400, 520, 680, 681, 900, 1200]


def sticks_out(page: QQuickItem, width: int) -> list[str]:
    """Named visible items laid out wider than the window."""
    found = []
    for item in descendants(page):
        if item.isVisible() and item.objectName():
            left = item.mapToScene(QPointF(0, 0)).x()
            right = left + item.width()
            if left < -0.5 or right > width + 0.5:
                found.append(f"{item.objectName()} spans {left:.1f}..{right:.1f}")
    return found


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("screen", SCREENS)
def test_every_screen_reflows_without_overflow(
    app_window: Window, messages: Messages, screen: str, width: int
) -> None:
    app_window.window.resize(width, 560)
    page = app_window.show(screen)
    app_window.settle()
    assert page.property("narrow") is (width <= 680)
    assert page.property("contentWidth") == width  # nothing scrolls sideways
    assert sticks_out(page, width) == []
    # Page padding: 40 wide, 20 narrow.
    assert page.property("sidePadding") == (20 if width <= 680 else 40)
    assert_clean(messages)


@pytest.mark.parametrize("width", [1200, 520])
def test_chapter_rows_drop_words_and_meter_when_narrow(
    app_window: Window, messages: Messages, width: int
) -> None:
    app_window.window.resize(width, 780)
    app_window.show("chapters")
    row = app_window.get("chapterRow3")
    names = {i.objectName(): i for i in descendants(row) if i.objectName()}
    assert "chapterNumber" not in names  # no number column
    wide = width > 680
    assert names["chapterWords"].isVisible() is wide
    assert names["chapterMeter"].isVisible() is wide
    assert names["chapterTitle"].isVisible()
    assert names["chapterMinutes"].isVisible()
    assert row.height() >= 49
    assert_clean(messages)


def test_sections_stack_when_narrow(app_window: Window, messages: Messages) -> None:
    def y(name: str) -> float:
        return app_window.get(name).mapToScene(QPointF(0, 0)).y()

    def x(name: str) -> float:
        return app_window.get(name).mapToScene(QPointF(0, 0)).x()

    app_window.window.resize(1200, 780)
    app_window.show("chapters")
    assert y("bookTitle") > y("chaptersCover")
    assert x("chapterList") > x("chaptersCover") + 240  # the table is beside the book
    app_window.window.resize(520, 780)
    app_window.settle()
    assert y("chapterList") > y("bookTitle")  # ...and under it
    assert x("chapterList") == x("chaptersCover")

    app_window.show("voice")
    assert y("previewPanel") > y("voiceMenu")
    app_window.window.resize(1200, 780)
    app_window.settle()
    assert y("previewPanel") == y("voiceMenu") or x("previewPanel") > x("voiceMenu")

    app_window.show("converting")
    app_window.window.resize(520, 780)
    app_window.settle()
    assert y("sentenceGrid") > y("convertingCover") + app_window.get("convertingCover").height()
    assert_clean(messages)


@pytest.mark.parametrize("width", [1200, 900, 520, 400])
def test_sentence_grid_columns_follow_the_width(
    app_window: Window, messages: Messages, width: int
) -> None:
    app_window.window.resize(width, 780)
    for screen, name in (("converting", "sentenceGrid"), ("done", "completeGrid")):
        app_window.show(screen)
        grid = app_window.get(name)
        room = grid.parentItem().width()
        assert (grid.property("dotSize"), grid.property("gap")) == (5, 3)
        assert grid.property("columns") == min(70, int(room // 8))
        assert grid.width() <= room
    if width == 1200:
        assert grid.property("columns") == 70
    if width == 520:
        assert grid.property("columns") == 60  # (520 - 2 x 20) / 8
    assert_clean(messages)


def descendants(item: QQuickItem) -> list[QQuickItem]:
    """Every live item below this one (findChildren can return delegates that are going away)."""
    found = []
    for child in item.childItems():
        found.append(child)
        found.extend(descendants(child))
    return found


def pixel_size(item: QQuickItem) -> int:
    texts = [i for i in descendants(item) if i.metaObject().className() == "QQuickText"]
    assert texts, "no text inside the display number"
    return int(texts[0].property("font").pixelSize())


@pytest.mark.parametrize("width", [1200, 520, 400])
def test_every_dot_outside_the_art_is_five_pixels(
    app_window: Window, messages: Messages, width: int
) -> None:
    app_window.window.resize(width, 780)
    app_window.show("chapters")
    meters = [
        i for i in descendants(app_window.get("chapterList"))
        if i.property("lit") is not None and i.property("dotSize") is not None
    ]  # fmt: skip
    if width > 680:
        assert len(meters) == 15
    for meter in meters:
        assert (meter.property("dotSize"), meter.property("dotSpacing")) == (5, 3)
        assert meter.property("count") == 17

    app_window.show("voice")
    wave = app_window.get("previewWave")
    assert (wave.property("dotSize"), wave.property("dotSpacing")) == (5, 3)
    assert wave.property("count") == 40
    assert wave.height() == 5  # one line: dots that don't fit are clipped, never wrapped
    speed = app_window.get("speedMeter")
    assert speed.property("dotSize") == 5
    assert speed.property("currentSize") is None  # the chosen speed is a colour, not a size
    assert speed.property("fitCellWidth") >= 24  # click and keyboard targets
    assert speed.width() <= app_window.get("previewPanel").width()

    app_window.show("converting")
    assert pixel_size(app_window.get("timeLeft")) == 67
    assert app_window.get("timeLeft").height() == 67
    app_window.show("done")
    assert pixel_size(app_window.get("audioLength")) == 67
    assert_clean(messages)


def test_dragging_a_book_lights_up_the_title(app_window: Window, messages: Messages) -> None:
    page = app_window.show("drop")
    title = app_window.get("dropTitle")
    assert title.property("color") == QColor("#F2F2F2")
    page.setProperty("dragActive", True)
    app_window.settle()
    assert title.property("color") == QColor("#7C8CFF")
    page.setProperty("dragActive", False)
    assert_clean(messages)


def test_register_again_returns_the_same_backend(backend: DemoBackend) -> None:
    # Once registered, later calls with an engine just reset the same backend.
    assert demo_backend.register(QQmlApplicationEngine()) is backend


def test_backend_property_list_matches_the_contract(backend: DemoBackend) -> None:
    contract = {
        "screen", "themeSetting", "theme", "reduceMotion", "hardwareLabel", "errorMessage",
        "gpuAvailable", "useGpu", "gpuName",
        "bookLoaded", "loading", "bookTitle", "bookAuthors", "coverSource", "artSource",
        "formatsLabel", "chapters", "skipFrontMatter", "selectionSummary",
        "voiceGroups", "voice", "voiceLabel", "speed", "outputFormat", "previewText",
        "previewPlaying",


        "bookHasPages", "skipPageNumbers",
        "footnotesAtEnd", "previewAvailable", "downloadNote", "stage", "downloadLabel",
        "chunksDone", "chunksTotal",
        "chapterLabel", "timeLeft", "progressLabel", "speedLabel", "nowSpeaking", "paused",
        "outputPath", "audioLength", "doneSummary", "warnings",
    }  # fmt: skip
    meta = backend.metaObject()
    names = {meta.property(i).name() for i in range(meta.propertyCount())}
    assert contract <= names
    assert {"minSpeed", "maxSpeed", "speedStep"} <= names  # constants: no notify signal
    for name in contract:
        assert meta.property(meta.indexOfProperty(name)).hasNotifySignal(), name
    slots = {
        "setThemeSetting", "goTo", "chooseBookFolder", "dismissError", "openBook",
        "setChapterSelected", "setSkipFrontMatter", "setVoice", "setSpeed", "setOutputFormat",
        "setUseGpu",


        "setSkipPageNumbers", "setFootnotesAtEnd",
        "togglePreview",
        "startConversion", "pauseConversion", "resumeConversion", "cancelConversion",
        "openOutputFolder", "convertAnother",
    }  # fmt: skip
    methods = {bytes(meta.method(i).name().data()).decode() for i in range(meta.methodCount())}
    assert slots <= methods
    model = backend.chapter_model
    roles = {bytes(name.data()).decode() for name in model.roleNames().values()}
    assert roles == {
        "title", "wordsLabel", "minutesLabel", "meterLit", "frontMatter", "selected",
        "matterLabel",
    }  # fmt: skip


def test_cancel_goes_back_to_voice_and_says_why(app_window: Window, messages: Messages) -> None:
    app_window.show("converting")
    app_window.click("cancelButton")
    app_window.qtbot.waitUntil(lambda: app_window.find("voiceScreen") is not None)
    assert app_window.get("errorBanner").isVisible()
    assert app_window.text("errorText") == "Stopped. Start again to continue where you left off."
    assert_clean(messages)


def test_convert_another_hides_the_cover(app_window: Window, messages: Messages) -> None:
    app_window.show("done")
    assert app_window.get("doneCover").isVisible()
    app_window.click("convertAnotherButton")
    app_window.qtbot.waitUntil(lambda: app_window.find("dropScreen") is not None)
    assert app_window.backend.property("coverSource") == ""
    app_window.backend.goTo("chapters")
    app_window.qtbot.waitUntil(lambda: app_window.find("chaptersScreen") is not None)
    assert not app_window.get("chaptersCover").isVisible()
    assert_clean(messages)


def test_next_is_disabled_with_nothing_ticked(app_window: Window, messages: Messages) -> None:
    app_window.show("chapters")
    for row in range(3, 15):
        app_window.backend.setChapterSelected(row, False)
    app_window.settle()
    assert app_window.text("summary_audio") == "NO AUDIO"
    assert app_window.text("summary_make") == "NOTHING TO MAKE"
    assert app_window.get("chooseVoiceButton").property("enabled") is False
    app_window.backend.setChapterSelected(5, True)
    app_window.settle()
    assert app_window.get("chooseVoiceButton").property("enabled") is True
    assert_clean(messages)


def test_preview_explains_when_there_is_no_audio_player(
    app_window: Window, messages: Messages
) -> None:
    app_window.backend.set_preview_available(False)
    app_window.show("voice")
    button = app_window.get("previewButton")
    assert button.property("enabled") is False
    assert "no audio player" in str(button.property("text"))
    assert app_window.get("previewUnavailable").isVisible()
    assert not app_window.get("previewWave").isVisible()
    assert_clean(messages)


def test_drop_screen_while_a_book_is_read(app_window: Window, messages: Messages) -> None:
    app_window.show("drop")
    app_window.backend.set_loading(True)
    app_window.settle()
    assert app_window.text("dropTitle") == "READING THE BOOK…"
    assert app_window.get("chooseBookButton").property("enabled") is False
    assert_clean(messages)


# --- Pause and cancel by stage, Start while loading, the download note --------------------------


def test_pause_is_only_for_voicing_and_cancel_not_for_packaging(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("converting")
    pause, cancel = app_window.get("pauseButton"), app_window.get("cancelButton")
    states = {}
    for stage in ("downloading", "starting", "voicing", "packaging"):
        app_window.backend.set_stage(stage)
        app_window.settle()
        states[stage] = (pause.property("enabled"), cancel.property("enabled"))
    assert states == {
        "downloading": (False, True),
        "starting": (False, True),
        "voicing": (True, True),
        "packaging": (False, False),
    }
    assert "can't be paused or cancelled" in app_window.text("pauseNote")
    assert_clean(messages)


def test_clicking_a_disabled_pause_or_cancel_does_nothing(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("converting")
    app_window.backend.set_stage("packaging")
    app_window.settle()
    app_window.backend.calls.clear()
    app_window.click("pauseButton")
    app_window.click("cancelButton")
    assert app_window.backend.calls == []
    assert app_window.backend.property("screen") == "converting"
    assert_clean(messages)


def test_a_paused_conversion_can_always_be_resumed(app_window: Window, messages: Messages) -> None:
    app_window.show("converting")
    app_window.click("pauseButton")
    assert app_window.backend.property("paused") is True
    assert app_window.get("pauseButton").property("enabled") is True
    assert_clean(messages)


def test_start_is_disabled_while_a_book_is_being_read(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("voice")
    start = app_window.get("startConvertingButton")
    assert start.property("enabled") is True
    app_window.backend.set_loading(True)
    app_window.settle()
    assert start.property("enabled") is False
    app_window.backend.set_loading(False)
    app_window.settle()
    assert start.property("enabled") is True
    assert_clean(messages)


@pytest.mark.parametrize("width", [1200, 520])
def test_output_format_choice(app_window: Window, messages: Messages, width: int) -> None:
    app_window.window.resize(width, 780)
    app_window.show("voice")
    m4b, mp3 = app_window.get("format_m4b"), app_window.get("format_mp3")
    assert (m4b.property("text"), mp3.property("text")) == (
        "M4B audiobook",
        "MP3 per chapter",
    )
    assert (m4b.property("chosen"), mp3.property("chosen")) == (True, False)
    # Under SPEED in the preview panel, like the panel's other settings, and inside the window.
    choice = app_window.get("outputFormatChoice")
    meter = app_window.get("speedMeter")
    assert choice.mapToScene(QPointF(0, 0)).y() > meter.mapToScene(QPointF(0, 0)).y()
    for pill in (m4b, mp3):
        left = pill.mapToScene(QPointF(0, 0)).x()
        assert left >= 0 and left + pill.width() <= width
        assert pill.height() == 44

    app_window.click("format_mp3")
    assert ("setOutputFormat", ("mp3",)) in app_window.backend.calls
    assert (m4b.property("chosen"), mp3.property("chosen")) == (False, True)
    app_window.click("format_m4b")
    assert app_window.backend.property("outputFormat") == "m4b"
    assert m4b.property("chosen") is True
    assert_clean(messages)


def test_voice_screen_shows_what_start_will_download(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("voice")
    note = app_window.get("downloadNote")
    assert not note.isVisible()  # everything is installed: no note
    app_window.backend.set_download_note("STARTING DOWNLOADS THE VOICE ONCE · 271 MB")
    app_window.settle()
    assert note.isVisible()
    assert app_window.text("downloadNote") == "STARTING DOWNLOADS THE VOICE ONCE · 271 MB"
    start = app_window.get("startConvertingButton")
    # Right above Start converting, not somewhere else on the page.
    assert note.mapToScene(QPointF(0, 0)).y() + note.height() <= start.mapToScene(QPointF(0, 0)).y()
    app_window.backend.set_download_note("")
    app_window.settle()
    assert not note.isVisible()
    assert_clean(messages)


@pytest.mark.parametrize("width", [1200, 560, 400])
def test_the_download_note_fits_at_every_width(
    app_window: Window, messages: Messages, width: int
) -> None:
    app_window.window.resize(width, 700)
    app_window.backend.set_download_note("STARTING DOWNLOADS THE VOICE ONCE · 271 MB")
    page = app_window.show("voice")
    app_window.settle()
    assert page.property("contentWidth") == width  # nothing scrolls sideways
    assert sticks_out(page, width) == []
    assert_clean(messages)


def test_an_untitled_section_has_a_readable_name(app_window: Window, messages: Messages) -> None:
    model = app_window.backend.chapter_model
    model.sections[3].title = "Untitled section"
    model.dataChanged.emit(model.index(3), model.index(3))
    app_window.show("chapters")
    assert app_window.text("chapterTitle") != ""  # the rows still render
    row = app_window.get("chapterRow3")
    QAccessible.setActive(True)
    interface = QAccessible.queryAccessibleInterface(row)
    assert interface is not None
    assert interface.text(QAccessible.Text.Name) == "Include untitled section"
    assert_clean(messages)


def test_the_graphics_card_row_is_only_there_when_a_driver_is(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("voice")
    assert not app_window.get("gpuChoice").isVisible()
    assert not app_window.get("useGpuSwitch").isVisible()

    app_window.backend.set_gpu(True, "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU")
    app_window.settle()
    assert app_window.get("gpuChoice").isVisible()
    assert app_window.text("useGpuSwitch") == "Use graphics card"
    assert app_window.get("useGpuSwitch").property("checked") is True
    assert app_window.text("gpuNote") == "FASTER · USES THE DRIVER ALREADY INSTALLED"
    assert_clean(messages)


def _qml_type(item: QQuickItem) -> str:
    """The QML component an item was made from, e.g. ``CheckRow_QMLTYPE_12``."""
    return str(item.metaObject().className())


def test_the_graphics_card_row_is_the_same_check_row_as_skip_front_matter(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("chapters")
    skip = app_window.get("skipFrontMatterSwitch")
    skip_type, skip_height = _qml_type(skip), skip.property("height")
    app_window.backend.set_gpu(True)
    app_window.show("voice")
    gpu = app_window.get("useGpuSwitch")
    assert skip_type.startswith("CheckRow_QMLTYPE")
    assert _qml_type(gpu) == skip_type  # the same QML component
    assert gpu.property("height") == skip_height  # a 44 px tick row
    assert_clean(messages)


def test_clicking_the_graphics_card_row_switches_it_and_the_note(
    app_window: Window, messages: Messages
) -> None:
    app_window.backend.set_gpu(True)
    app_window.show("voice")

    app_window.click("useGpuSwitch")
    assert ("setUseGpu", (False,)) in app_window.backend.calls
    assert app_window.get("useGpuSwitch").property("checked") is False
    assert (
        app_window.text("gpuNote")
        == "USING THE CPU · THE GRAPHICS CARD IS USUALLY SEVERAL TIMES FASTER"
    )

    app_window.click("useGpuSwitch")
    assert ("setUseGpu", (True,)) in app_window.backend.calls
    assert app_window.text("gpuNote") == "FASTER · USES THE DRIVER ALREADY INSTALLED"
    assert_clean(messages)


def test_the_graphics_card_row_is_keyboard_reachable_and_named(
    app_window: Window, messages: Messages
) -> None:
    app_window.backend.set_gpu(True)
    app_window.show("voice")
    row = app_window.get("useGpuSwitch")
    assert row.property("focusPolicy") == Qt.FocusPolicy.StrongFocus
    QAccessible.setActive(True)
    interface = QAccessible.queryAccessibleInterface(row)
    assert interface is not None
    assert interface.text(QAccessible.Text.Name) == "Use graphics card"
    assert_clean(messages)


@pytest.mark.parametrize("width", [1200, 680, 560, 400])
@pytest.mark.parametrize("use_gpu", [True, False])
def test_the_graphics_card_row_fits_at_every_width(
    app_window: Window, messages: Messages, width: int, use_gpu: bool
) -> None:
    app_window.window.resize(width, 700)
    app_window.backend.set_gpu(True)
    app_window.backend.setUseGpu(use_gpu)
    page = app_window.show("voice")
    app_window.settle()
    assert page.property("contentWidth") == width  # nothing scrolls sideways
    assert sticks_out(page, width) == []
    note = app_window.get("gpuNote")
    assert note.width() <= app_window.get("gpuChoice").width() + 1
    assert_clean(messages)


# -- the skip switches ----------------------------------------------------------------------------

SWITCHES = ("skipPageNumbersSwitch", "skipFrontMatterSwitch", "footnotesAtEndSwitch")


def _box(window: Window, name: str) -> tuple[float, float, float, float]:
    item = window.get(name)
    corner = item.mapToScene(QPointF(0, 0))
    return corner.x(), corner.y(), item.width(), item.height()


def test_skip_switches_say_what_they_do(app_window: Window, messages: Messages) -> None:
    app_window.show("chapters")
    assert [app_window.text(name) for name in SWITCHES] == [
        "Skip page numbers", "Skip front matter", "Read footnotes at chapter end",
    ]  # fmt: skip
    assert [app_window.get(name).property("checked") for name in SWITCHES] == [True, True, False]
    assert all(app_window.get(name).isVisible() for name in SWITCHES)
    assert_clean(messages)


def test_skip_page_numbers_is_only_for_books_with_pages(
    app_window: Window, messages: Messages
) -> None:
    app_window.show("chapters")
    app_window.backend.set_book_has_pages(False)
    app_window.settle()

    assert not app_window.get("skipPageNumbersSwitch").isVisible()
    assert app_window.get("skipFrontMatterSwitch").isVisible()
    assert app_window.get("footnotesAtEndSwitch").isVisible()
    # The switches that are left start where the first one did.
    assert _box(app_window, "skipFrontMatterSwitch")[0] == _box(app_window, "chapterList")[0]
    assert_clean(messages)


def test_skip_switches_call_the_backend(app_window: Window, messages: Messages) -> None:
    app_window.show("chapters")

    app_window.click("skipPageNumbersSwitch")
    app_window.click("footnotesAtEndSwitch")

    assert ("setSkipPageNumbers", (False,)) in app_window.backend.calls
    assert ("setFootnotesAtEnd", (True,)) in app_window.backend.calls
    assert app_window.get("skipPageNumbersSwitch").property("checked") is False
    assert app_window.get("footnotesAtEndSwitch").property("checked") is True
    assert_clean(messages)


@pytest.mark.parametrize("width", [1200, 520])
def test_skip_switches_wrap_as_in_the_mockup(
    app_window: Window, messages: Messages, width: int
) -> None:
    app_window.window.resize(width, 780)
    page = app_window.show("chapters")
    pages, front, notes = (_box(app_window, name) for name in SWITCHES)

    assert pages[1] == front[1]  # side by side, 24 px apart (gap: 10px 24px)
    assert front[0] - (pages[0] + pages[2]) == 24
    if width == 1200:
        assert notes[1] == front[1]
        assert notes[0] - (front[0] + front[2]) == 24
    else:
        assert notes[0] == pages[0]  # wrapped to a second row, 10 px down
        assert notes[1] - (pages[1] + pages[3]) == 10
    assert all(box[3] == 44 for box in (pages, front, notes))
    assert sticks_out(page, width) == []
    assert_clean(messages)
