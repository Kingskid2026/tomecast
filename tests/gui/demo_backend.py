"""A stand-in ``Backend`` for the QML window: Alice in Wonderland's real numbers, no engine.

It implements the whole Python-QML contract in ``docs/gui.md`` with static data, and its slots
change state plausibly (moving between screens, choosing chapters, voice and speed, pausing),
so the QML can be tested and screenshotted without a book, a voice model or a display.
It deliberately doesn't import ``tomecast.gui.backend`` or ``tomecast.gui.models``.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSize,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QImage
from PySide6.QtQml import QQmlEngine, qmlRegisterSingletonInstance, qmlRegisterType
from PySide6.QtQuick import QQuickImageProvider

QML_URI = "TomecastNative"
DATA_PATH = Path(__file__).resolve().parent / "data" / "alice_demo.json"
SCREENS = ("drop", "chapters", "voice", "converting", "done")
STAGES = ("downloading", "starting", "voicing", "packaging")
METER_DOTS = 17

VOICE_CATALOG = (
    (
        "AMERICAN · FEMALE",
        "af",
        (
            "heart",
            "bella",
            "nicole",
            "sarah",
            "sky",
            "nova",
            "river",
            "jessica",
            "kore",
            "aoede",
            "alloy",
        ),
    ),
    (
        "AMERICAN · MALE",
        "am",
        ("michael", "adam", "eric", "liam", "onyx", "puck", "echo", "fenrir", "santa"),
    ),
    ("BRITISH · FEMALE", "bf", ("emma", "isabella", "alice", "lily")),
    ("BRITISH · MALE", "bm", ("george", "lewis", "daniel", "fable")),
)

_CHAPTER_PREFIX = re.compile(r"^CHAPTER\s+[IVXLC\d]+\.\s*", re.IGNORECASE)
_ModelIndex = QModelIndex | QPersistentModelIndex


def load_demo_data(path: Path = DATA_PATH) -> dict[str, Any]:
    """The Alice numbers measured from the real book (see ``tests/gui/data``)."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _display_title(raw: str) -> str:
    """'CHAPTER III. A Caucus-Race…' -> 'A Caucus-Race…'; all-caps titles in title case."""
    title = _CHAPTER_PREFIX.sub("", raw).strip()
    return title.title() if title.isupper() else title


@dataclass
class Section:
    """One row of the chapter list."""

    number: str
    title: str
    words: int
    minutes: float
    front_matter: bool
    chunks: int
    selected: bool = True


class DemoChapterModel(QAbstractListModel):
    """``ChapterModel``-compatible list: the roles documented in docs/gui.md."""

    NumberRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = Qt.ItemDataRole.UserRole + 2
    WordsLabelRole = Qt.ItemDataRole.UserRole + 3
    MinutesLabelRole = Qt.ItemDataRole.UserRole + 4
    MeterLitRole = Qt.ItemDataRole.UserRole + 5
    FrontMatterRole = Qt.ItemDataRole.UserRole + 6
    SelectedRole = Qt.ItemDataRole.UserRole + 7

    _ROLE_NAMES: ClassVar[dict[int, bytes]] = {
        NumberRole: b"number",
        TitleRole: b"title",
        WordsLabelRole: b"wordsLabel",
        MinutesLabelRole: b"minutesLabel",
        MeterLitRole: b"meterLit",
        FrontMatterRole: b"frontMatter",
        SelectedRole: b"selected",
    }

    selectionChanged = Signal()

    def __init__(self, sections: list[Section], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sections = sections

    @property
    def sections(self) -> list[Section]:
        return self._sections

    def rowCount(self, parent: _ModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._sections)

    def roleNames(self) -> dict[int, QByteArray]:
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._sections):
            return None
        section = self._sections[index.row()]
        if role in (self.TitleRole, Qt.ItemDataRole.DisplayRole):
            return section.title
        if role == self.NumberRole:
            return section.number
        if role == self.WordsLabelRole:
            return f"{section.words:,}"
        if role == self.MinutesLabelRole:
            if section.minutes < 1:
                return "<1 min"
            return f"{_half_up(section.minutes)} min"
        if role == self.MeterLitRole:
            return min(METER_DOTS, _half_up(section.minutes))
        if role == self.FrontMatterRole:
            return section.front_matter
        if role == self.SelectedRole:
            return section.selected
        return None

    def setData(self, index: _ModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != self.SelectedRole or not index.isValid():
            return False
        return self.set_selected(index.row(), bool(value))

    def flags(self, index: _ModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        return base | Qt.ItemFlag.ItemIsEditable if index.isValid() else base

    def set_selected(self, row: int, selected: bool) -> bool:
        if not 0 <= row < len(self._sections):
            return False
        section = self._sections[row]
        if section.selected == selected:
            return True
        section.selected = selected
        model_index = self.index(row, 0)
        self.dataChanged.emit(model_index, model_index, [self.SelectedRole])
        self.selectionChanged.emit()
        return True


def _notify(name: str) -> Callable[[DemoBackend], None]:
    def emit(backend: DemoBackend) -> None:
        getattr(backend, f"{name}Changed").emit()

    return emit


class DemoBackend(QObject):
    """The ``Backend`` singleton, with Alice in Wonderland and no engine behind it."""

    screenChanged = Signal()
    themeSettingChanged = Signal()
    themeChanged = Signal()
    reduceMotionChanged = Signal()
    hardwareLabelChanged = Signal()
    errorMessageChanged = Signal()
    bookLoadedChanged = Signal()
    loadingChanged = Signal()
    bookTitleChanged = Signal()
    bookAuthorsChanged = Signal()
    coverSourceChanged = Signal()
    artSourceChanged = Signal()
    artCreditChanged = Signal()
    formatsLabelChanged = Signal()
    chaptersChanged = Signal()
    skipFrontMatterChanged = Signal()
    selectionSummaryChanged = Signal()
    voiceGroupsChanged = Signal()
    voiceChanged = Signal()
    voiceLabelChanged = Signal()
    speedChanged = Signal()
    previewTextChanged = Signal()
    previewPlayingChanged = Signal()
    previewAvailableChanged = Signal()
    stageChanged = Signal()
    downloadLabelChanged = Signal()
    chunksDoneChanged = Signal()
    chunksTotalChanged = Signal()
    chapterLabelChanged = Signal()
    timeLeftChanged = Signal()
    progressLabelChanged = Signal()
    speedLabelChanged = Signal()
    nowSpeakingChanged = Signal()
    pausedChanged = Signal()
    outputPathChanged = Signal()
    audioLengthChanged = Signal()
    doneSummaryChanged = Signal()
    warningsChanged = Signal()

    def __init__(self, parent: QObject | None = None, *, data: dict[str, Any] | None = None):
        super().__init__(parent)
        self._data = data if data is not None else load_demo_data()
        self._chapters = DemoChapterModel(self._make_sections(), self)
        self._chapters.selectionChanged.connect(self._on_selection_changed)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._values: dict[str, Any] = {}
        self._cover_generation = 1
        self.reset()

    # -- state ----------------------------------------------------------------------------------

    def _make_sections(self) -> list[Section]:
        sections: list[Section] = []
        chapter = 0
        for raw in self._data["sections"]:
            title = str(raw["title"])
            numbered = bool(_CHAPTER_PREFIX.match(title))
            front = not numbered and chapter == 0
            if numbered:
                chapter += 1
            number = f"[ {chapter:02d} ]" if numbered else "—"
            sections.append(
                Section(
                    number=number,
                    title=_display_title(title),
                    words=int(raw["words"]),
                    minutes=float(raw["minutes"]),
                    front_matter=front,
                    chunks=int(raw["chunks"]),
                    selected=not front,
                )
            )
        return sections

    def reset(self) -> None:
        """Back to a fresh window: the drop screen, dark theme, Alice ready to be opened."""
        data = self._data
        speedup = float(data["speedup"])
        for section in self._chapters.sections:
            section.selected = not section.front_matter
        self._chapters.beginResetModel()
        self._chapters.endResetModel()
        story = [s for s in self._chapters.sections if not s.front_matter]
        chunks_total = int(data["story_chunks"])
        chunks_done = 225
        third = story[2]
        self._cover_generation = 1
        values: dict[str, Any] = {
            "screen": "drop",
            "themeSetting": "dark",
            "theme": "dark",
            "reduceMotion": False,
            "hardwareLabel": f"CPU · ABOUT {speedup}× REAL TIME",
            "errorMessage": "",
            "bookLoaded": False,
            "loading": False,
            "bookTitle": str(data["title"]),
            "bookAuthors": ", ".join(data["authors"]),
            "coverSource": "",
            "artSource": "",
            "artCredit": "LUCA DELLA ROBBIA · CANTORIA · 1431–38",
            "formatsLabel": "EPUB · PDF · DOCX · FB2 · HTML · TXT",
            "skipFrontMatter": True,
            "selectionSummary": {},
            "voiceGroups": self._voice_groups(),
            "voice": "af_heart",
            "voiceLabel": "",
            "speed": 1.0,
            "previewText": str(data["ch1_first_sentences"][0]),
            "previewPlaying": False,
            "previewAvailable": True,
            "stage": "voicing",
            "downloadLabel": ("DOWNLOADING ONCE · KOKORO VOICE MODEL (82M, 8-BIT) · 45% OF 190 MB"),
            "chunksDone": chunks_done,
            "chunksTotal": chunks_total,
            "chapterLabel": f"{third.number} {third.title.upper()}",
            "timeLeft": "00:30:32",
            "progressLabel": (
                f"{chunks_done:,} / {chunks_total:,} PIECES · "
                f"{_half_up(100 * chunks_done / chunks_total)}%"
            ),
            "speedLabel": f"{speedup}× REAL TIME",
            "nowSpeaking": str(data["ch3_sentence"]),
            "paused": False,
            "outputPath": str(Path.home() / "Books" / "Alice’s Adventures in Wonderland.m4b"),
            "audioLength": "2:47:00",
            "doneSummary": "OF AUDIO · 12 CHAPTERS · ABOUT 79 MB · MADE IN ABOUT 36 MIN",
            "warnings": [],
        }
        values["voiceLabel"] = self._label_for(values["voice"])
        values["selectionSummary"] = self._summary()
        values["artSource"] = self._art_url("dark")
        changed = [k for k, v in values.items() if self._values.get(k) != v]
        self._values = values
        self.calls.clear()
        for key in changed:
            _notify(key)(self)

    def _set(self, name: str, value: Any) -> None:
        if self._values.get(name) == value:
            return
        self._values[name] = value
        _notify(name)(self)

    def _get(self, name: str) -> Any:
        return self._values[name]

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    @staticmethod
    def _voice_groups() -> list[dict[str, Any]]:
        return [
            {
                "label": label,
                "voices": [{"id": f"{prefix}_{name}", "name": name.title()} for name in names],
            }
            for label, prefix, names in VOICE_CATALOG
        ]

    @staticmethod
    def _label_for(voice_id: str) -> str:
        for label, prefix, names in VOICE_CATALOG:
            for name in names:
                if f"{prefix}_{name}" == voice_id:
                    return f"{name.upper()} · {label}"
        return voice_id.upper()

    def _cover_url(self, theme: str) -> str:
        return f"image://dots/cover/{theme}?{self._cover_generation}"

    @staticmethod
    def _art_url(theme: str) -> str:
        return f"image://dots/cantoria/{theme}"

    def _summary(self) -> dict[str, str]:
        sections = self._chapters.sections
        chosen = [s for s in sections if s.selected]
        words = sum(s.words for s in chosen)
        minutes = words / float(self._data["wpm"])
        make = _half_up(minutes / float(self._data["speedup"]))
        hours, mins = divmod(_half_up(minutes), 60)
        return {
            "sections": f"{len(chosen)} OF {len(sections)} SECTIONS",
            "words": f"{words:,} WORDS",
            "audio": f"ABOUT {hours} H {mins:02d} MIN OF AUDIO" if chosen else "NO AUDIO",
            "make": f"ABOUT {make} MIN TO MAKE HERE" if chosen else "NOTHING TO MAKE",
        }

    def _on_selection_changed(self) -> None:
        self._set("selectionSummary", self._summary())
        fronts = [s for s in self._chapters.sections if s.front_matter]
        self._set("skipFrontMatter", bool(fronts) and not any(s.selected for s in fronts))

    # -- helpers for tests and screenshots (Python only) -----------------------------------------

    def set_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        self._set("stage", stage)

    def set_error(self, message: str) -> None:
        self._set("errorMessage", message)

    def set_reduce_motion(self, on: bool) -> None:
        self._set("reduceMotion", on)

    def set_speed_label(self, label: str) -> None:
        self._set("speedLabel", label)

    def set_loading(self, on: bool) -> None:
        self._set("loading", on)

    def set_preview_available(self, available: bool) -> None:
        self._set("previewAvailable", available)

    def set_warnings(self, warnings: list[str]) -> None:
        self._set("warnings", list(warnings))

    def show(self, screen: str, theme: str | None = None) -> None:
        """Jump straight to any screen (and theme), opening Alice first if needed.

        Unlike ``goTo`` this ignores which moves the app allows, for tests and screenshots.
        """
        if screen not in SCREENS:
            raise ValueError(f"unknown screen {screen!r}")
        if theme is not None:
            self.setThemeSetting(theme)
        if screen != "drop" and not self._get("bookLoaded"):
            self._load_book()
        if screen != "voice":
            self._set("previewPlaying", False)
        self._set("screen", screen)

    def _load_book(self) -> None:
        self._cover_generation += 1
        self._set("coverSource", self._cover_url(self._get("theme")))
        self._set("loading", False)
        self._set("bookLoaded", True)

    # -- App ------------------------------------------------------------------------------------

    @Slot(str)
    def setThemeSetting(self, name: str) -> None:
        self._record("setThemeSetting", name)
        if name not in ("system", "dark", "light"):
            return
        self._set("themeSetting", name)
        theme = "dark" if name == "system" else name
        self._set("theme", theme)
        if self._get("coverSource"):
            self._set("coverSource", self._cover_url(theme))
        self._set("artSource", self._art_url(theme))

    @Slot(str)
    def goTo(self, screen: str) -> None:
        """Back buttons and Next: "drop" from anywhere but converting, "voice" with a section."""
        self._record("goTo", screen)
        current = self._get("screen")
        if screen not in SCREENS or current == "converting":
            return
        if screen == "voice" and not any(s.selected for s in self._chapters.sections):
            self._set("errorMessage", "Tick at least one section to make an audiobook.")
            return
        if screen != "voice":
            self._set("previewPlaying", False)
        self._set("screen", screen)

    @Slot(result=str)
    def chooseBookFolder(self) -> str:
        self._record("chooseBookFolder")
        return QUrl.fromLocalFile(str(Path.home())).toString()

    @Slot()
    def dismissError(self) -> None:
        self._record("dismissError")
        self._set("errorMessage", "")

    # -- Book -----------------------------------------------------------------------------------

    @Slot("QVariant")
    def openBook(self, url: QUrl | str) -> None:
        """A QUrl (file dialog, drop) or a string; any local file "opens" as Alice."""
        self._record("openBook", url)
        qurl = url if isinstance(url, QUrl) else QUrl(str(url))
        path = qurl.toLocalFile() if qurl.isLocalFile() else str(url)
        if not path:
            self._set("errorMessage", "That doesn't look like a file Tomecast can read.")
            return
        self._load_book()
        self._set("screen", "chapters")

    @Slot(int, bool)
    def setChapterSelected(self, row: int, selected: bool) -> None:
        self._record("setChapterSelected", row, selected)
        self._chapters.set_selected(row, selected)

    @Slot(bool)
    def setSkipFrontMatter(self, on: bool) -> None:
        self._record("setSkipFrontMatter", on)
        for row, section in enumerate(self._chapters.sections):
            if section.front_matter:
                self._chapters.set_selected(row, not on)
        self._set("skipFrontMatter", on)

    # -- Voice ----------------------------------------------------------------------------------

    @Slot(str)
    def setVoice(self, voice_id: str) -> None:
        self._record("setVoice", voice_id)
        self._set("voice", voice_id)
        self._set("voiceLabel", self._label_for(voice_id))

    @Slot(float)
    def setSpeed(self, value: float) -> None:
        self._record("setSpeed", value)
        steps = round((min(1.25, max(0.75, value)) - 0.75) / 0.05)
        self._set("speed", round(0.75 + steps * 0.05, 2))

    @Slot()
    def togglePreview(self) -> None:
        self._record("togglePreview")
        if self._get("previewAvailable"):
            self._set("previewPlaying", not self._get("previewPlaying"))

    @Slot()
    def startConversion(self) -> None:
        self._record("startConversion")
        self._set("previewPlaying", False)
        self._set("paused", False)
        self._set("stage", "voicing")
        self._set("screen", "converting")

    # -- Converting -----------------------------------------------------------------------------

    @Slot()
    def pauseConversion(self) -> None:
        self._record("pauseConversion")
        self._set("paused", True)

    @Slot()
    def resumeConversion(self) -> None:
        self._record("resumeConversion")
        self._set("paused", False)

    @Slot()
    def cancelConversion(self) -> None:
        self._record("cancelConversion")
        self._set("paused", False)
        self._set("screen", "voice")
        self._set("errorMessage", "Stopped. Start again to continue where you left off.")

    # -- Done -----------------------------------------------------------------------------------

    @Slot()
    def openOutputFolder(self) -> None:
        self._record("openOutputFolder")

    @Slot()
    def convertAnother(self) -> None:
        self._record("convertAnother")
        self._set("bookLoaded", False)
        self._set("coverSource", "")
        self._set("screen", "drop")

    # -- properties -----------------------------------------------------------------------------

    def _chapters_model(self) -> QObject:
        return self._chapters

    @property
    def chapter_model(self) -> DemoChapterModel:
        return self._chapters

    screen = Property(str, lambda self: self._get("screen"), notify=screenChanged)
    themeSetting = Property(str, lambda self: self._get("themeSetting"), notify=themeSettingChanged)
    theme = Property(str, lambda self: self._get("theme"), notify=themeChanged)
    reduceMotion = Property(
        bool, lambda self: self._get("reduceMotion"), notify=reduceMotionChanged
    )
    hardwareLabel = Property(
        str, lambda self: self._get("hardwareLabel"), notify=hardwareLabelChanged
    )
    errorMessage = Property(str, lambda self: self._get("errorMessage"), notify=errorMessageChanged)
    bookLoaded = Property(bool, lambda self: self._get("bookLoaded"), notify=bookLoadedChanged)
    loading = Property(bool, lambda self: self._get("loading"), notify=loadingChanged)
    bookTitle = Property(str, lambda self: self._get("bookTitle"), notify=bookTitleChanged)
    bookAuthors = Property(str, lambda self: self._get("bookAuthors"), notify=bookAuthorsChanged)
    coverSource = Property(str, lambda self: self._get("coverSource"), notify=coverSourceChanged)
    artSource = Property(str, lambda self: self._get("artSource"), notify=artSourceChanged)
    artCredit = Property(str, lambda self: self._get("artCredit"), notify=artCreditChanged)
    formatsLabel = Property(str, lambda self: self._get("formatsLabel"), notify=formatsLabelChanged)
    chapters = Property(QObject, _chapters_model, notify=chaptersChanged)
    skipFrontMatter = Property(
        bool, lambda self: self._get("skipFrontMatter"), notify=skipFrontMatterChanged
    )
    selectionSummary = Property(
        dict, lambda self: self._get("selectionSummary"), notify=selectionSummaryChanged
    )
    voiceGroups = Property(list, lambda self: self._get("voiceGroups"), notify=voiceGroupsChanged)
    voice = Property(str, lambda self: self._get("voice"), notify=voiceChanged)
    voiceLabel = Property(str, lambda self: self._get("voiceLabel"), notify=voiceLabelChanged)
    speed = Property(float, lambda self: self._get("speed"), notify=speedChanged)
    previewText = Property(str, lambda self: self._get("previewText"), notify=previewTextChanged)
    previewPlaying = Property(
        bool, lambda self: self._get("previewPlaying"), notify=previewPlayingChanged
    )
    previewAvailable = Property(
        bool, lambda self: self._get("previewAvailable"), notify=previewAvailableChanged
    )
    stage = Property(str, lambda self: self._get("stage"), notify=stageChanged)
    downloadLabel = Property(
        str, lambda self: self._get("downloadLabel"), notify=downloadLabelChanged
    )
    chunksDone = Property(int, lambda self: self._get("chunksDone"), notify=chunksDoneChanged)
    chunksTotal = Property(int, lambda self: self._get("chunksTotal"), notify=chunksTotalChanged)
    chapterLabel = Property(str, lambda self: self._get("chapterLabel"), notify=chapterLabelChanged)
    timeLeft = Property(str, lambda self: self._get("timeLeft"), notify=timeLeftChanged)
    progressLabel = Property(
        str, lambda self: self._get("progressLabel"), notify=progressLabelChanged
    )
    speedLabel = Property(str, lambda self: self._get("speedLabel"), notify=speedLabelChanged)
    nowSpeaking = Property(str, lambda self: self._get("nowSpeaking"), notify=nowSpeakingChanged)
    paused = Property(bool, lambda self: self._get("paused"), notify=pausedChanged)
    outputPath = Property(str, lambda self: self._get("outputPath"), notify=outputPathChanged)
    audioLength = Property(str, lambda self: self._get("audioLength"), notify=audioLengthChanged)
    doneSummary = Property(str, lambda self: self._get("doneSummary"), notify=doneSummaryChanged)
    warnings = Property(list, lambda self: self._get("warnings"), notify=warningsChanged)


# -- registration -------------------------------------------------------------------------------

_backend: DemoBackend | None = None


def register(engine: QQmlEngine | None = None) -> DemoBackend:
    """Register the demo as the ``Backend`` singleton under ``TomecastNative`` 1.0.

    Qt can't unregister a singleton instance, so there is one demo backend per process:
    later calls reset it and return the same object.

    The first call must come before any QML engine exists: with PySide6 6.12, registering a
    singleton instance after an engine was created breaks that engine's type checks ("Cannot
    assign object of type QQuickItem to list property data"). So pass ``None`` the first time;
    later calls may pass the engine they're preparing, which is then fine.
    """
    global _backend
    if _backend is None:
        if engine is not None:
            raise RuntimeError(
                "Register the demo Backend before creating the QML engine "
                "(PySide6 breaks an engine that existed before the registration)."
            )
        _backend = DemoBackend()
        QQmlEngine.setObjectOwnership(_backend, QQmlEngine.ObjectOwnership.CppOwnership)
        # PySide6's stub says the name is bytes; at runtime only str is accepted.
        qmlRegisterSingletonInstance(DemoBackend, QML_URI, 1, 0, "Backend", _backend)  # type: ignore[arg-type]
    else:
        _backend.reset()
    return _backend


# -- DotGrid and the dots image provider ---------------------------------------------------------

_STAND_IN_DOTGRID = """\
// Throwaway stand-in for tomecast.gui.dotgrid.DotGrid (screenshots before it existed).
import QtQuick

Canvas {
    property int total: 0
    property int done: 0
    property int columns: 70
    property real dotSize: 5
    property real gap: 3
    property color doneColor: "white"
    property color currentColor: "blue"
    property color waitColor: "grey"
    property bool complete: false
    readonly property string description: done + " of " + total + " pieces voiced"
    readonly property int rows: total > 0 ? Math.ceil(total / columns) : 0

    implicitWidth: Math.max(0, Math.min(columns, total) * (dotSize + gap) - gap)
    implicitHeight: Math.max(0, rows * (dotSize + gap) - gap)
    onTotalChanged: requestPaint()
    onDoneChanged: requestPaint()
    onColumnsChanged: requestPaint()
    onCompleteChanged: requestPaint()
    onDoneColorChanged: requestPaint()
    onWaitColorChanged: requestPaint()
    onPaint: {
        const ctx = getContext("2d")
        ctx.reset()
        const pitch = dotSize + gap
        for (let i = 0; i < total; ++i) {
            const lit = complete || i < done
            ctx.fillStyle = lit ? doneColor : (i === done ? currentColor : waitColor)
            ctx.beginPath()
            ctx.ellipse((i % columns) * pitch, Math.floor(i / columns) * pitch, dotSize, dotSize)
            ctx.fill()
        }
    }
}
"""

_stand_in_dir: tempfile.TemporaryDirectory[str] | None = None
_dotgrid_kind: str | None = None


def register_dot_grid() -> str:
    """Register ``DotGrid``: the real one if ``tomecast.gui.dotgrid`` exists, else a stand-in.

    Returns ``"real"`` or ``"stand-in"``.
    """
    global _stand_in_dir, _dotgrid_kind
    if _dotgrid_kind is not None:
        return _dotgrid_kind
    try:
        from tomecast.gui import dotgrid
    except ImportError:
        _stand_in_dir = tempfile.TemporaryDirectory(prefix="tomecast-dotgrid-")
        qml_file = Path(_stand_in_dir.name) / "DotGrid.qml"
        qml_file.write_text(_STAND_IN_DOTGRID, encoding="utf-8")
        url = QUrl.fromLocalFile(str(qml_file))
        # PySide6's stub says the names are bytes; at runtime only str is accepted.
        qmlRegisterType(url, QML_URI, 1, 0, "DotGrid")  # type: ignore[call-overload]
        _dotgrid_kind = "stand-in"
    else:
        dotgrid.register_types()
        _dotgrid_kind = "real"
    return _dotgrid_kind


class GreyImageProvider(QQuickImageProvider):
    """Plain grey placeholders for ``image://dots/...`` when the real provider is missing."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)

    def requestImage(self, id: str, size: QSize, requestedSize: QSize) -> QImage:
        width = requestedSize.width() if requestedSize.width() > 0 else 240
        ratio = 1.49 if id.startswith("cantoria") else 1.38
        image = QImage(width, round(width * ratio), QImage.Format.Format_RGB32)
        image.fill(QColor("#7E7E7E"))
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image


def make_image_provider(cover: bytes | None = None) -> tuple[QQuickImageProvider, str]:
    """The ``dots`` provider: the real ``DotImageProvider`` if it exists, else grey images.

    ``cover`` (image file bytes) is given to the real provider as the book cover.
    Returns the provider and ``"real"`` or ``"grey"``.
    """
    try:
        from tomecast.gui.images import DotImageProvider
    except ImportError:
        return GreyImageProvider(), "grey"
    provider = DotImageProvider()
    provider.set_cover(cover)
    return provider, "real"
