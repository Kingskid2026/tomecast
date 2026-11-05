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
    QMessageLogContext,
    QPointF,
    Qt,
    QtMsgType,
    QUrl,
    qInstallMessageHandler,
)
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication
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

    def click(self, name_or_item: str | QQuickItem, at: QPointF | None = None) -> None:
        item = self.get(name_or_item) if isinstance(name_or_item, str) else name_or_item
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
    assert app_window.text("artCredit") == "LUCA DELLA ROBBIA · CANTORIA · 1431–38"
    assert app_window.text("chooseBookButton") == "Choose a book"
    assert app_window.text("noBookLabel") == "[ 00 ] NO BOOK YET"
    assert app_window.get("artImage").property("source") == QUrl("image://dots/cantoria/dark")
    assert not app_window.get("errorBanner").isVisible()
    assert_clean(messages)


def test_chapters_screen_lists_every_section(app_window: Window, messages: Messages) -> None:
    app_window.show("chapters")
    assert app_window.text("screenTitle") == "[ 02 ] CHAPTERS"
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
    assert app_window.text("screenTitle") == "[ 03 ] VOICE"
    assert app_window.text("previewVoiceLabel") == "PREVIEW · HEART · AMERICAN · FEMALE"
    assert app_window.text("speedValue") == "1.00×"
    assert app_window.get("voice_af_heart").property("chosen") is True
    assert app_window.get("voice_bm_george").property("chosen") is False
    assert app_window.text("previewText").startswith("Alice was beginning to get very tired")
    assert app_window.get("speedMeter").property("current") == 5
    assert_clean(messages)


def test_converting_screen_texts(app_window: Window, messages: Messages) -> None:
    app_window.show("converting")
    assert app_window.text("screenTitle") == "[ 04 ] CONVERTING"
    assert app_window.text("chapterLabel") == "[ 03 ] A CAUCUS-RACE AND A LONG TALE"
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
    assert app_window.text("screenTitle") == "[ 05 ] DONE"
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
    # Eleven 28 px targets spread across the meter: click the eighth (speed 1.10).
    step = (meter.width() - 28) / 10
    app_window.click(meter, QPointF(14 + 7 * step, meter.height() / 2))
    assert app_window.backend.property("speed") == pytest.approx(1.10)
    assert app_window.text("speedValue") == "1.10×"

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
    assert app_window.backend.property("speed") == pytest.approx(1.05)
    QTest.keyClick(app_window.window, Qt.Key.Key_Home)
    assert app_window.backend.property("speed") == pytest.approx(0.75)
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


@pytest.mark.parametrize("width", [400, 639, 640, 900])
@pytest.mark.parametrize("screen", SCREENS)
def test_small_windows_reflow(
    app_window: Window, messages: Messages, screen: str, width: int
) -> None:
    app_window.window.resize(width, 560)
    page = app_window.show(screen)
    app_window.settle()
    assert page.property("compact") is (width < 640)
    # Nothing is laid out wider than the window.
    for item in page.findChildren(QQuickItem):
        if item.isVisible() and item.objectName():
            left = item.mapToScene(QPointF(0, 0)).x()
            right = left + item.width()
            assert left >= -0.5, f"{item.objectName()} starts off the left edge"
            assert right <= width + 0.5, f"{item.objectName()} sticks out ({right} > {width})"
    assert_clean(messages)


def test_corner_window_layouts_follow_the_compact_mockups(
    app_window: Window, messages: Messages
) -> None:
    app_window.window.resize(400, 600)
    app_window.show("drop")
    assert app_window.get("artImage").width() == 160
    assert app_window.get("chooseBookButton").height() == 40

    app_window.show("chapters")
    assert app_window.get("chaptersCover").width() == 72
    assert app_window.get("chooseVoiceButton").property("text") == "Next"
    chapter_list = app_window.get("chapterList")
    # The list scrolls by itself, so the page fits the window.
    assert chapter_list.property("interactive") is True
    assert chapter_list.property("contentHeight") > chapter_list.height()
    assert app_window.get("chaptersScreen").property("naturalHeight") <= 600
    assert app_window.get("chapterRow3").height() == 36
    assert app_window.text("summary_line1") == "12 OF 15 SECTIONS · ABOUT 2 H 47 MIN"
    assert app_window.text("summary_line2") == "ABOUT 36 MIN TO MAKE HERE"

    app_window.show("voice")
    menu, panel = app_window.get("voiceMenu"), app_window.get("previewPanel")
    assert panel.mapToScene(QPointF(0, 0)).y() > menu.mapToScene(QPointF(0, 0)).y()
    assert app_window.get("previewButton").width() == 44
    assert app_window.get("voiceScreen").property("naturalHeight") <= 600

    app_window.show("converting")
    cover, grid = app_window.get("convertingCover"), app_window.get("sentenceGrid")
    assert cover.width() == 120
    assert grid.mapToScene(QPointF(0, 0)).y() > cover.mapToScene(QPointF(0, 0)).y()
    assert (grid.property("dotSize"), grid.property("gap")) == (3, 2)
    assert grid.property("columns") == 72  # as in Corner-converting.dc.html
    assert 100 <= grid.height() <= 140
    assert app_window.get("timeLeft").height() == 38
    assert app_window.text("convertingStatus") == "4.7× REAL TIME"
    assert app_window.get("cancelButton").height() == 36

    app_window.show("done")
    assert app_window.get("doneCover").width() == 120
    assert app_window.text("checkedLabel") == "1,610 OF 1,610 CHECKED"
    assert app_window.get("openFolderButton").width() == 400 - 2 * 18

    app_window.window.resize(1200, 780)
    app_window.show("converting")
    grid = app_window.get("sentenceGrid")
    assert (grid.property("dotSize"), grid.property("columns")) == (5, 70)
    assert app_window.get("timeLeft").height() == 64
    assert app_window.get("convertingCover").width() == 200
    assert_clean(messages)


def test_speed_text_hides_until_measured(app_window: Window, messages: Messages) -> None:
    app_window.window.resize(400, 600)
    app_window.show("converting")
    assert app_window.get("convertingStatus").isVisible()
    app_window.backend.set_speed_label("")
    app_window.settle()
    assert not app_window.get("convertingStatus").isVisible()
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
        "bookLoaded", "loading", "bookTitle", "bookAuthors", "coverSource", "artSource",
        "artCredit", "formatsLabel", "chapters", "skipFrontMatter", "selectionSummary",
        "voiceGroups", "voice", "voiceLabel", "speed", "previewText", "previewPlaying",
        "previewAvailable", "stage", "downloadLabel", "chunksDone", "chunksTotal",
        "chapterLabel", "timeLeft", "progressLabel", "speedLabel", "nowSpeaking", "paused",
        "outputPath", "audioLength", "doneSummary", "warnings",
    }  # fmt: skip
    meta = backend.metaObject()
    names = {meta.property(i).name() for i in range(meta.propertyCount())}
    assert contract <= names
    for name in contract:
        assert meta.property(meta.indexOfProperty(name)).hasNotifySignal(), name
    slots = {
        "setThemeSetting", "goTo", "chooseBookFolder", "dismissError", "openBook",
        "setChapterSelected", "setSkipFrontMatter", "setVoice", "setSpeed", "togglePreview",
        "startConversion", "pauseConversion", "resumeConversion", "cancelConversion",
        "openOutputFolder", "convertAnother",
    }  # fmt: skip
    methods = {bytes(meta.method(i).name().data()).decode() for i in range(meta.methodCount())}
    assert slots <= methods
    model = backend.chapter_model
    roles = {bytes(name.data()).decode() for name in model.roleNames().values()}
    assert roles == {
        "number", "title", "wordsLabel", "minutesLabel", "meterLit", "frontMatter", "selected",
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
