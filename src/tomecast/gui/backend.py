"""Backend: the one QObject the QML window talks to, registered as the singleton ``Backend``.

Every property, signal and slot here is part of the Python ↔ QML contract in docs/gui.md.
Slow work (reading a book, voicing a preview, converting) runs on the
:class:`~tomecast.gui.worker.ConversionWorker` thread and comes back through queued signals.
"""

from __future__ import annotations

import itertools
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import (
    Property,
    QObject,
    QProcess,
    QSettings,
    Qt,
    QThread,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QGuiApplication

from tomecast.book import Book
from tomecast.chunking import split_sentences
from tomecast.downloads import Asset, data_dir, is_installed
from tomecast.engines.audiocpp import (
    DEFAULT_VOICE,
    VOICES,
    check_system,
    default_threads,
    required_assets,
)
from tomecast.gui.models import DEFAULT_SPEEDUP, ChapterModel
from tomecast.gui.worker import (
    ConversionWorker,
    ConvertJob,
    ConvertResult,
    ConvertUpdate,
    DeviceScanner,
    EngineFactory,
    PreviewJob,
    ReadJob,
    ScanJob,
    TimedControl,
    kokoro_engine,
    megabytes,
    preview_file,
    scan_gpu,
)
from tomecast.hardware import short_name, vulkan_possible
from tomecast.packaging import portable_name
from tomecast.readers import FORMATS, SUFFIXES

SCREENS = ("drop", "chapters", "voice", "converting", "done")
THEME_SETTINGS = ("system", "dark", "light")
OUTPUT_FORMATS = ("m4b", "mp3")
MIN_SPEED, MAX_SPEED, SPEED_STEP = 0.5, 3.0, 0.25  # 11 meter dots; 1.00x is the third
PLAYERS = ("pw-play", "paplay", "aplay")  # the first one found plays previews

FORMATS_LABEL = " · ".join(FORMATS)  # only what Tomecast can read today
# Formats the window lists but can't read yet.
COMING_SOON = {".pdf": "PDF"}
_FORMAT_LIST = f"{', '.join(FORMATS[:-1])} and {FORMATS[-1]}"
STOPPED_MESSAGE = "Stopped. Start again to continue where you left off."
UNKNOWN_TIME = "--:--:--"
DOWNLOAD_NOTE = "STARTING DOWNLOADS THE VOICE ONCE · {mb} MB"

# Voice menu groups, and the order of voices in each (as in the maintainer's mockup).
VOICE_GROUPS = (
    ("af", "AMERICAN · FEMALE"),
    ("am", "AMERICAN · MALE"),
    ("bf", "BRITISH · FEMALE"),
    ("bm", "BRITISH · MALE"),
)
_VOICE_ORDER = (
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_nova", "af_river",
    "af_jessica", "af_kore", "af_aoede", "af_alloy",
    "am_michael", "am_adam", "am_eric", "am_liam", "am_onyx", "am_puck", "am_echo", "am_fenrir",
    "am_santa",
    "bf_emma", "bf_isabella", "bf_alice", "bf_lily",
    "bm_george", "bm_lewis", "bm_daniel", "bm_fable",
)  # fmt: skip


def _property[T](kind: Any, getter: Callable[[Any], T], notify: Signal) -> T:
    """A read-only Qt property. Typed as its value, so Python callers (and tests) see a str,
    bool… rather than a ``Property``."""
    return cast(T, Property(kind, getter, notify=notify))


def _field(kind: Any, name: str, notify: Signal) -> Any:
    """A read-only Qt property backed by the attribute ``_<name>``."""
    return Property(kind, lambda self: getattr(self, f"_{name}"), notify=notify)


def voice_groups() -> list[dict[str, Any]]:
    """The voice menu: American and British English, grouped by accent and gender."""
    groups: list[dict[str, Any]] = []
    for prefix, label in VOICE_GROUPS:
        ids = [voice for voice in VOICES if voice.startswith(f"{prefix}_")]
        ids.sort(key=lambda v: (_VOICE_ORDER.index(v) if v in _VOICE_ORDER else 99, v))
        voices = [{"id": voice, "name": voice.split("_", 1)[1].capitalize()} for voice in ids]
        groups.append({"label": label, "voices": voices})
    return groups


def _as_bool(value: object, *, default: bool) -> bool:
    """A saved setting as a bool (an INI file stores it as the text "true" or "false")."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text == "true" else False if text == "false" else default


def voice_label(voice: str) -> str:
    """``"HEART · AMERICAN · FEMALE"``."""
    prefix, _, name = voice.partition("_")
    group = dict(VOICE_GROUPS).get(prefix)
    return f"{name.upper()} · {group}" if group else name.upper()


def safe_file_name(title: str) -> str:
    """``title`` as a file or folder name that works on every common file system, including
    the FAT32 and exFAT of USB sticks (see :func:`tomecast.packaging.portable_name`)."""
    return portable_name(title)


def unique_output(folder: Path, title: str, output_format: str = "m4b") -> Path:
    """``<folder>/<title>.m4b`` (or the folder ``<folder>/<title>`` for ``"mp3"``), with
    " (2)", " (3)"… added so nothing is ever overwritten."""
    name = safe_file_name(title) or "Audiobook"
    suffix = ".m4b" if output_format == "m4b" else ""
    candidate = folder / f"{name}{suffix}"
    for number in itertools.count(2):
        if not (candidate.exists() or candidate.is_symlink()):
            return candidate
        candidate = folder / f"{name} ({number}){suffix}"
    raise AssertionError("unreachable")  # pragma: no cover


def find_player() -> str | None:
    """The system audio player for previews: pw-play, then paplay, then aplay."""
    return next((path for name in PLAYERS if (path := shutil.which(name))), None)


def system_reduce_motion() -> bool:
    """True if ``TOMECAST_REDUCE_MOTION=1`` or GNOME has animations turned off."""
    if os.environ.get("TOMECAST_REDUCE_MOTION") == "1":
        return True
    gsettings = shutil.which("gsettings")
    if gsettings is None:
        return False
    try:
        result = subprocess.run(
            [gsettings, "get", "org.gnome.desktop.interface", "enable-animations"],
            capture_output=True, text=True, timeout=1, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "false"


def _clock(seconds: float) -> str:
    """``"00:30:32"``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _length(seconds: float) -> str:
    """``"2:47:00"``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _took(seconds: float) -> str:
    """``"36 MIN"``, ``"1 H 05 MIN"`` or ``"40 SEC"``."""
    if seconds < 59.5:
        return f"{max(1, round(seconds))} SEC"
    minutes = round(seconds / 60)
    hours, rest = divmod(minutes, 60)
    return f"{hours} H {rest:02d} MIN" if hours else f"{rest} MIN"


def _local_path(url: QUrl | str) -> Path | None:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile()) if url.isLocalFile() else None
    if url.startswith("file:"):
        return _local_path(QUrl(url))
    return Path(url).expanduser() if url else None


class Backend(QObject):
    """The window's state and actions. See docs/gui.md for the contract."""

    # --- Signals (one per property) ------------------------------------------------------

    screenChanged = Signal()
    themeSettingChanged = Signal()
    themeChanged = Signal()
    reduceMotionChanged = Signal()
    hardwareLabelChanged = Signal()
    gpuAvailableChanged = Signal()
    useGpuChanged = Signal()
    gpuNameChanged = Signal()
    errorMessageChanged = Signal()

    bookLoadedChanged = Signal()
    loadingChanged = Signal()
    bookTitleChanged = Signal()
    bookAuthorsChanged = Signal()
    coverSourceChanged = Signal()
    artSourceChanged = Signal()
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

    downloadNoteChanged = Signal()
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
    outputFormatChanged = Signal()
    audioLengthChanged = Signal()
    doneSummaryChanged = Signal()
    warningsChanged = Signal()

    # To the worker (queued: the worker lives on its own thread).
    _jobRequested = Signal(object)
    _shutdownRequested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        engine_factory: EngineFactory = kokoro_engine,
        assets: Sequence[Asset] | None = None,
        data_root: Path | None = None,
        player: str | Literal["auto"] | None = "auto",
        settings: QSettings | None = None,
        system_check: Callable[[], None] = check_system,
        cover_sink: Callable[[bytes | None], None] | None = None,
        gpu_check: Callable[[], bool] = vulkan_possible,
        device_scanner: DeviceScanner = scan_gpu,
    ) -> None:
        super().__init__(parent)
        self._settings = settings if settings is not None else QSettings()
        self._data_root = data_root if data_root is not None else data_dir()
        self._player = find_player() if player == "auto" else player
        self._cover_sink = cover_sink
        self._jobs = itertools.count(1)
        self._read_job: int | None = None
        self._read_path: Path | None = None
        self._preview_job: int | None = None
        self._convert_job: int | None = None
        self._control: TimedControl | None = None
        self._chapter_labels: list[str] = []
        self._speedup: float | None = None  # measured in this session
        self._book: Book | None = None
        self._book_path: Path | None = None
        self._cover_count = 0
        self._has_cover = False
        self._playback: QProcess | None = None
        self._preview_cache: Path | None = None
        self._shut_down = False
        fixed_assets = None if assets is None else tuple(assets)
        # The files a run needs depend on whether it uses the graphics card.
        self._assets_for: Callable[[bool], Sequence[Asset]] = (
            required_assets if fixed_assets is None else (lambda use_gpu: fixed_assets)
        )
        self._verified: dict[Path, tuple[int, int]] = {}  # file -> (size, mtime_ns) once hashed

        # App
        self._screen = "drop"
        saved_theme = str(self._settings.value("themeSetting", "system"))
        self._themeSetting = saved_theme if saved_theme in THEME_SETTINGS else "system"
        self._theme = self._effective_theme()
        self._reduceMotion = system_reduce_motion()
        threads = default_threads()
        self._cpuLabel = f"CPU · {threads} THREAD{'' if threads == 1 else 'S'}"
        self._hardwareLabel = self._cpuLabel
        self._gpuAvailable = gpu_check()
        self._useGpu = _as_bool(self._settings.value("useGpu", True), default=True)
        self._gpuName = ""
        self._speedup_for = ""  # the hardware label the measured speed belongs to
        self._errorMessage = ""
        # Book
        self._bookLoaded = False
        self._loading = False
        self._bookTitle = ""
        self._bookAuthors = ""
        self._skipFrontMatter = True
        self._chapters = ChapterModel(self)
        self._chapters.selectionChanged.connect(self._on_selection_changed)
        # Voice
        self._voiceGroups = voice_groups()
        saved_voice = str(self._settings.value("voice", DEFAULT_VOICE))
        self._voice = saved_voice if saved_voice in self._voice_ids() else DEFAULT_VOICE
        self._speed = self._valid_speed(self._settings.value("speed", 1.0))
        saved_format = str(self._settings.value("outputFormat", "m4b"))
        self._outputFormat = saved_format if saved_format in OUTPUT_FORMATS else "m4b"
        self._convert_format = self._outputFormat  # the format of the run in progress
        self._previewText = ""
        self._previewPlaying = False
        self._downloadNote = ""
        # Converting
        self._stage = "starting"
        self._downloadLabel = ""
        self._chunksDone = 0
        self._chunksTotal = 0
        self._chapterLabel = ""
        self._timeLeft = UNKNOWN_TIME
        self._progressLabel = self._progress_text(0, 0)
        self._speedLabel = ""
        self._nowSpeaking = ""
        self._paused = False
        # Done
        self._outputPath = ""
        self._audioLength = ""
        self._doneSummary = ""
        self._warnings: list[str] = []

        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(self._update_theme)

        # The one worker thread, started once and kept until shutdown().
        self._thread = QThread()
        self._thread.setObjectName("tomecast-worker")
        self._worker = ConversionWorker(
            engine_factory=engine_factory,
            assets=self._assets_for,
            data_root=self._data_root,
            system_check=system_check,
            device_scanner=device_scanner,
        )
        self._worker.moveToThread(self._thread)
        self._jobRequested.connect(self._worker.run)
        self._shutdownRequested.connect(self._worker.shutdown)
        self._worker.bookRead.connect(self._on_book_read)
        self._worker.previewReady.connect(self._on_preview_ready)
        self._worker.stageChanged.connect(self._on_stage)
        self._worker.downloadProgress.connect(self._on_download_progress)
        self._worker.progressed.connect(self._on_progress)
        self._worker.converted.connect(self._on_converted)
        self._worker.failed.connect(self._on_failed)
        self._worker.deviceChosen.connect(self._on_device)
        self._thread.start()
        if self._gpuAvailable:
            self._jobRequested.emit(ScanJob(next(self._jobs)))  # learn the card's name

    # --- Properties: app -------------------------------------------------------------------

    screen: str = _field(str, "screen", screenChanged)
    themeSetting: str = _field(str, "themeSetting", themeSettingChanged)
    theme: str = _field(str, "theme", themeChanged)
    reduceMotion: bool = _field(bool, "reduceMotion", reduceMotionChanged)
    hardwareLabel: str = _field(str, "hardwareLabel", hardwareLabelChanged)
    gpuAvailable: bool = _field(bool, "gpuAvailable", gpuAvailableChanged)
    useGpu: bool = _field(bool, "useGpu", useGpuChanged)
    gpuName: str = _field(str, "gpuName", gpuNameChanged)
    errorMessage: str = _field(str, "errorMessage", errorMessageChanged)

    # --- Properties: book ------------------------------------------------------------------

    bookLoaded: bool = _field(bool, "bookLoaded", bookLoadedChanged)
    loading: bool = _field(bool, "loading", loadingChanged)
    bookTitle: str = _field(str, "bookTitle", bookTitleChanged)
    bookAuthors: str = _field(str, "bookAuthors", bookAuthorsChanged)

    def _get_cover_source(self) -> str:
        if not self._has_cover:
            return ""
        return f"image://dots/cover/{self._theme}?{self._cover_count}"

    coverSource = _property(str, _get_cover_source, coverSourceChanged)

    def _get_art_source(self) -> str:
        return f"image://dots/cantoria/{self._theme}"

    artSource = _property(str, _get_art_source, artSourceChanged)

    def _get_formats_label(self) -> str:
        return FORMATS_LABEL

    formatsLabel = _property(str, _get_formats_label, formatsLabelChanged)

    def _get_chapters(self) -> ChapterModel:
        return self._chapters

    chapters = _property(QObject, _get_chapters, chaptersChanged)
    skipFrontMatter: bool = _field(bool, "skipFrontMatter", skipFrontMatterChanged)

    def _get_selection_summary(self) -> dict[str, str]:
        return self._chapters.summary(self._speedup or DEFAULT_SPEEDUP, self._speed)

    selectionSummary = _property(dict, _get_selection_summary, selectionSummaryChanged)

    # --- Properties: voice -----------------------------------------------------------------

    voiceGroups: list[dict[str, Any]] = _field(list, "voiceGroups", voiceGroupsChanged)
    voice: str = _field(str, "voice", voiceChanged)

    def _get_voice_label(self) -> str:
        return voice_label(self._voice)

    voiceLabel = _property(str, _get_voice_label, voiceLabelChanged)
    speed: float = _field(float, "speed", speedChanged)
    outputFormat: str = _field(str, "outputFormat", outputFormatChanged)
    # The meter's range, so QML never hard-codes it. Constants: no notify signal.
    minSpeed = cast(float, Property(float, lambda self: MIN_SPEED, constant=True))
    maxSpeed = cast(float, Property(float, lambda self: MAX_SPEED, constant=True))
    speedStep = cast(float, Property(float, lambda self: SPEED_STEP, constant=True))
    previewText: str = _field(str, "previewText", previewTextChanged)
    previewPlaying: bool = _field(bool, "previewPlaying", previewPlayingChanged)
    downloadNote: str = _field(str, "downloadNote", downloadNoteChanged)

    def _get_preview_available(self) -> bool:
        return self._player is not None

    previewAvailable = _property(bool, _get_preview_available, previewAvailableChanged)

    # --- Properties: converting ------------------------------------------------------------

    stage: str = _field(str, "stage", stageChanged)
    downloadLabel: str = _field(str, "downloadLabel", downloadLabelChanged)
    chunksDone: int = _field(int, "chunksDone", chunksDoneChanged)
    chunksTotal: int = _field(int, "chunksTotal", chunksTotalChanged)
    chapterLabel: str = _field(str, "chapterLabel", chapterLabelChanged)
    timeLeft: str = _field(str, "timeLeft", timeLeftChanged)
    progressLabel: str = _field(str, "progressLabel", progressLabelChanged)
    speedLabel: str = _field(str, "speedLabel", speedLabelChanged)
    nowSpeaking: str = _field(str, "nowSpeaking", nowSpeakingChanged)
    paused: bool = _field(bool, "paused", pausedChanged)

    # --- Properties: done ------------------------------------------------------------------

    outputPath: str = _field(str, "outputPath", outputPathChanged)
    audioLength: str = _field(str, "audioLength", audioLengthChanged)
    doneSummary: str = _field(str, "doneSummary", doneSummaryChanged)
    warnings: list[str] = _field(list, "warnings", warningsChanged)

    # --- Slots: app ------------------------------------------------------------------------

    @Slot(str)
    def setThemeSetting(self, name: str) -> None:
        if name not in THEME_SETTINGS:
            return
        self._update("themeSetting", name)
        self._settings.setValue("themeSetting", name)
        self._settings.sync()
        self._update_theme()

    @Slot(bool)
    def setUseGpu(self, on: bool) -> None:
        """Use the graphics card (through the driver already installed) or the CPU. Saved."""
        if on == self._useGpu:
            return
        self._stop_preview()  # a preview made on the other hardware shouldn't keep playing
        self._update("useGpu", on)
        self._settings.setValue("useGpu", on)
        self._settings.sync()
        self._speedup = None  # the measured speed belonged to the other hardware
        self.selectionSummaryChanged.emit()
        self._refresh_hardware_label()
        self._refresh_download_note()

    @Slot(str)
    def goTo(self, screen: str) -> None:
        """Move between the screens a back (or next) button may reach from here."""
        current = self._screen
        if screen == current or current == "converting":
            return
        if screen == "drop":
            self._show("drop")
        elif screen == "chapters" and self._bookLoaded:
            self._show("chapters")
        elif screen == "voice" and self._bookLoaded and current in ("chapters", "done"):
            if not self._chapters.selected_rows():
                self._update("errorMessage", "Choose at least one section to convert.")
                return
            self._show("voice")

    @Slot(result=str)
    def chooseBookFolder(self) -> str:
        """The folder the file dialog should open in, as a ``file://`` URL."""
        saved = str(self._settings.value("lastBookFolder", "") or "")
        folder = Path(saved) if saved and Path(saved).is_dir() else Path.home()
        return QUrl.fromLocalFile(str(folder)).toString()

    @Slot()
    def dismissError(self) -> None:
        self._update("errorMessage", "")

    # --- Slots: book -----------------------------------------------------------------------

    @Slot(QUrl)
    def openBook(self, url: QUrl | str) -> None:
        """Read a book (a ``file://`` URL or a path) on the worker, then show its chapters."""
        if self._screen == "converting" or self._shut_down:
            return
        path = _local_path(url)
        if path is None:
            self._update("errorMessage", "Tomecast can only open books saved on this computer.")
            return
        suffix = path.suffix.lower()
        if suffix in COMING_SOON:
            name = COMING_SOON[suffix]
            self._update(
                "errorMessage",
                f"{name} support is coming soon. For now, Tomecast reads {_FORMAT_LIST} books.",
            )
            return
        if suffix not in SUFFIXES:
            kind = f"{suffix} files" if suffix else "files without an extension"
            self._update(
                "errorMessage", f"Tomecast can't read {kind}. It reads {_FORMAT_LIST} books."
            )
            return
        if not path.is_file():
            self._update("errorMessage", f"Couldn't find {path.name}.")
            return
        self._settings.setValue("lastBookFolder", str(path.parent))
        self._settings.sync()
        self._update("errorMessage", "")
        self._update("loading", True)
        self._read_job = next(self._jobs)
        self._read_path = path
        self._jobRequested.emit(ReadJob(self._read_job, path))

    @Slot(int, bool)
    def setChapterSelected(self, row: int, selected: bool) -> None:
        self._chapters.set_selected(row, selected)

    @Slot(bool)
    def setSkipFrontMatter(self, on: bool) -> None:
        self._update("skipFrontMatter", on)
        self._chapters.set_front_matter_selected(not on)

    # --- Slots: voice ----------------------------------------------------------------------

    @Slot(str)
    def setVoice(self, voice: str) -> None:
        if voice not in self._voice_ids() or voice == self._voice:
            return
        self._stop_preview()
        self._update("voice", voice)
        self.voiceLabelChanged.emit()
        self._settings.setValue("voice", voice)
        self._settings.sync()

    @Slot(float)
    def setSpeed(self, value: float) -> None:
        speed = self._valid_speed(value)
        if speed == self._speed:
            return
        self._stop_preview()
        self._update("speed", speed)
        self.selectionSummaryChanged.emit()  # the estimates follow the speed
        self._settings.setValue("speed", speed)
        self._settings.sync()

    @Slot(str)
    def setOutputFormat(self, name: str) -> None:
        """``"m4b"`` (one audiobook file) or ``"mp3"`` (a folder, one file per chapter)."""
        if name not in OUTPUT_FORMATS or name == self._outputFormat:
            return
        self._update("outputFormat", name)
        self._settings.setValue("outputFormat", name)
        self._settings.sync()

    @Slot()
    def togglePreview(self) -> None:
        """Voice ``previewText`` with the chosen voice and speed and play it; again to stop."""
        if self._previewPlaying:
            self._stop_preview()
            return
        if self._player is None or not self._previewText or self._shut_down:
            return
        self._update("previewPlaying", True)
        if self._preview_cache is None:
            self._preview_cache = Path(tempfile.mkdtemp(prefix="tomecast-preview-"))
        wav = preview_file(self._preview_cache, self._voice, self._speed, self._previewText)
        if wav.is_file():
            self._play(wav)
            return
        self._preview_job = next(self._jobs)
        job = PreviewJob(
            self._preview_job, self._previewText, self._voice, self._speed, wav, self._using_gpu
        )
        self._jobRequested.emit(job)

    @Slot()
    def startConversion(self) -> None:
        """Write ``<book folder>/<book title>.m4b`` (or, for MP3, the folder ``<book title>``),
        never overwriting, on the worker."""
        if (
            self._screen == "converting"
            or self._loading  # a book is being read: the result would replace this one
            or self._book is None
            or self._book_path is None
        ):
            return
        rows = self._chapters.selected_rows()
        if not rows:
            self._update("errorMessage", "Choose at least one section to convert.")
            return
        folder = self._book_path.parent
        if not os.access(folder, os.W_OK):
            self._update(
                "errorMessage",
                f"Tomecast can't save the audiobook in {folder}. Move the book to a folder you "
                "can write to, and open it again.",
            )
            return
        self._stop_preview()
        sections = self._chapters.sections
        # The whole book plus the ticked indices, so chunk IDs (and the resume folder) are the
        # same whichever sections are ticked, and match a run from the command line.
        book = self._book
        output_format: Literal["m4b", "mp3"] = "mp3" if self._outputFormat == "mp3" else "m4b"
        output = unique_output(folder, self._book.title or self._book_path.stem, output_format)
        self._convert_format = output_format
        self._chapter_labels = [sections[r].label for r in rows]
        self._control = TimedControl()
        self._convert_job = next(self._jobs)
        self._reset_progress()
        self._update("chapterLabel", self._chapter_labels[0])
        self._update("errorMessage", "")
        self._show("converting")
        self._jobRequested.emit(
            ConvertJob(
                self._convert_job, book, tuple(rows), self._book_path, output, self._voice,
                self._speed, self._control, self._using_gpu, output_format,
            )
        )  # fmt: skip

    # --- Slots: converting -----------------------------------------------------------------

    @Slot()
    def pauseConversion(self) -> None:
        """Pause before the next piece. Only while voicing: downloading and packaging can't
        be paused."""
        if self._control is None or self._stage != "voicing":
            return
        if self._control.pause():
            self._update("paused", True)

    @Slot()
    def resumeConversion(self) -> None:
        if self._control is not None:
            self._control.resume()
            self._update("paused", False)

    @Slot()
    def cancelConversion(self) -> None:
        """Stop after the current sentence. The work is kept, so starting again resumes.
        Once packaging has begun the book is written regardless, so this does nothing."""
        if self._control is None or self._stage == "packaging":
            return
        if not self._control.cancel():  # packaging began a moment ago
            return
        self._end_conversion()
        self._update("errorMessage", STOPPED_MESSAGE)
        self._show("voice")

    # --- Slots: done -----------------------------------------------------------------------

    @Slot()
    def openOutputFolder(self) -> None:
        if self._outputPath:
            output = Path(self._outputPath)
            # An MP3 run's output is the folder holding the files: open that one.
            folder = output if output.is_dir() else output.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    @Slot()
    def convertAnother(self) -> None:
        """Back to the drop screen with no book, keeping the voice and speed."""
        if self._screen == "converting":
            return
        self._stop_preview()
        self._book = None
        self._book_path = None
        self._read_job = None
        self._chapters.clear()
        self._set_cover(None)
        for name, value in (
            ("bookLoaded", False), ("loading", False), ("bookTitle", ""), ("bookAuthors", ""),
            ("outputPath", ""), ("audioLength", ""), ("doneSummary", ""), ("warnings", []),
            ("errorMessage", ""),
        ):  # fmt: skip
            self._update(name, value)
        self._show("drop")

    # --- Shutting down ---------------------------------------------------------------------

    def shutdown(self) -> None:
        """Cancel any running job, stop the engine and join the worker thread."""
        if self._shut_down:
            return
        self._shut_down = True
        self._worker.stop()
        if self._control is not None:
            self._control.cancel()
        self._stop_preview()
        self._shutdownRequested.emit()
        self._thread.wait()
        if self._preview_cache is not None:
            shutil.rmtree(self._preview_cache, ignore_errors=True)
            self._preview_cache = None

    @property
    def worker_thread(self) -> QThread:
        """The worker's thread (for tests and shutdown checks)."""
        return self._thread

    # --- Worker results --------------------------------------------------------------------

    @Slot(int, object)
    def _on_book_read(self, job: int, book: Book) -> None:
        if job != self._read_job:
            return
        self._read_job = None
        if self._screen == "converting":
            # A book finished reading while another is being converted. Drop it: showing
            # it would strand the conversion that is running.
            self._update("loading", False)
            return
        self._book = book
        self._book_path = self._read_path
        self._chapters.set_book(book, skip_front_matter=self._skipFrontMatter)
        self._update("bookTitle", book.title)
        self._update("bookAuthors", ", ".join(book.authors))
        self._set_cover(book.cover.data if book.cover else None)
        self._update("bookLoaded", True)
        self._update("loading", False)
        self._show("chapters")

    @Slot(str)
    def _on_device(self, description: str) -> None:
        """The worker found out (or started the engine on) which graphics card is in use."""
        self._update("gpuName", description)
        self._refresh_hardware_label()

    @Slot(int, str)
    def _on_preview_ready(self, job: int, path: str) -> None:
        if job != self._preview_job:
            return
        self._preview_job = None
        if self._previewPlaying:
            self._play(Path(path))

    @Slot(int, str)
    def _on_stage(self, job: int, stage: str) -> None:
        if job == self._convert_job:
            self._update("stage", stage)
            if stage != "downloading":
                self._refresh_download_note()  # the downloads (if any) just finished

    @Slot(int, str)
    def _on_download_progress(self, job: int, label: str) -> None:
        if job == self._convert_job:
            self._update("downloadLabel", label)

    @Slot(int, object)
    def _on_progress(self, job: int, update: ConvertUpdate) -> None:
        if job != self._convert_job:
            return
        self._update("stage", update.stage)
        self._update("chunksTotal", update.chunks_total)
        self._update("chunksDone", update.chunks_done)
        self._update("progressLabel", self._progress_text(update.chunks_done, update.chunks_total))
        if 0 <= update.chapter_index < len(self._chapter_labels):
            self._update("chapterLabel", self._chapter_labels[update.chapter_index])
        self._update("nowSpeaking", update.now_speaking)
        seconds_left = update.seconds_left
        self._update("timeLeft", UNKNOWN_TIME if seconds_left is None else _clock(seconds_left))
        if update.speedup is not None:
            self._update("speedLabel", f"{update.speedup:.1f}\u00d7 REAL TIME")
            self._measured(update.speedup)

    @Slot(int, object)
    def _on_converted(self, job: int, result: ConvertResult) -> None:
        if job != self._convert_job:
            return
        self._end_conversion()
        if result.speedup is not None:
            self._measured(result.speedup)
        noun = "MP3 FILE" if self._convert_format == "mp3" else "CHAPTER"
        chapters = f"{result.chapters} {noun}{'' if result.chapters == 1 else 'S'}"
        self._update("outputPath", str(result.output))  # the name actually used
        self._update("audioLength", _length(result.audio_seconds))
        self._update(
            "doneSummary",
            f"OF AUDIO · {chapters} · {megabytes(result.size_bytes)} · "
            f"MADE IN {_took(result.seconds_taken)}",
        )
        self._update("warnings", list(result.warnings))
        self._update("chunksDone", self._chunksTotal)
        self._show("done")

    @Slot(int, str)
    def _on_failed(self, job: int, message: str) -> None:
        if job == self._read_job:
            self._read_job = None
            self._update("loading", False)
        elif job == self._preview_job:
            self._preview_job = None
            self._update("previewPlaying", False)
        elif job == self._convert_job:
            self._end_conversion()
            self._show("voice")
        else:
            return  # a job the window no longer waits for
        self._update("errorMessage", message)

    # --- Helpers ---------------------------------------------------------------------------

    def _update(self, name: str, value: object) -> None:
        """Set the property ``name`` and emit ``<name>Changed`` if its value changed."""
        attribute = f"_{name}"
        if getattr(self, attribute) != value:
            setattr(self, attribute, value)
            getattr(self, f"{name}Changed").emit()

    def _show(self, screen: str) -> None:
        if screen != "voice":
            self._stop_preview()
        else:
            self._refresh_download_note()
        self._update("screen", screen)

    def _refresh_download_note(self) -> None:
        """Say what Start will download: nothing when every required file is installed."""
        missing = [a for a in self._assets_for(self._using_gpu) if not self._installed(a)]
        if not missing:
            self._update("downloadNote", "")
            return
        mb = math.ceil(sum(asset.size for asset in missing) / 1_000_000)
        self._update("downloadNote", DOWNLOAD_NOTE.format(mb=f"{mb:,}"))

    def _installed(self, asset: Asset) -> bool:
        """``is_installed``, but a plain file that was already hashed isn't hashed again until
        its size or modification time changes (the voice model is large)."""
        path = self._data_root / asset.path
        try:
            stat = path.stat()
        except OSError:
            self._verified.pop(path, None)
            return is_installed(asset, self._data_root)
        if asset.archive:
            return is_installed(asset, self._data_root)  # reads one small marker file
        stamp = (stat.st_size, stat.st_mtime_ns)
        if self._verified.get(path) == stamp:
            return True
        if is_installed(asset, self._data_root):
            self._verified[path] = stamp
            return True
        self._verified.pop(path, None)
        return False

    @Slot()
    def _update_theme(self) -> None:
        theme = self._effective_theme()
        if theme != self._theme:
            self._theme = theme
            self.themeChanged.emit()
            self.coverSourceChanged.emit()
            self.artSourceChanged.emit()

    def _effective_theme(self) -> str:
        if self._themeSetting in ("dark", "light"):
            return self._themeSetting
        scheme = QGuiApplication.styleHints().colorScheme()
        return "light" if scheme == Qt.ColorScheme.Light else "dark"

    @Slot()
    def _on_selection_changed(self) -> None:
        self.selectionSummaryChanged.emit()
        self._update("previewText", self._first_sentence())

    def _first_sentence(self) -> str:
        """The first sentence of the first selected chapter that has any text."""
        sections = self._chapters.sections
        rows = self._chapters.selected_rows() or range(len(sections))
        for row in rows:
            for paragraph in sections[row].chapter.paragraphs:
                for sentence in split_sentences(paragraph):
                    if any(character.isalnum() for character in sentence):
                        return sentence
        return ""

    def _set_cover(self, data: bytes | None) -> None:
        self._cover_count += 1
        self._has_cover = data is not None
        if self._cover_sink is not None:
            self._cover_sink(data)
        self.coverSourceChanged.emit()

    def _voice_ids(self) -> set[str]:
        return {voice["id"] for group in self._voiceGroups for voice in group["voices"]}

    @staticmethod
    def _valid_speed(value: object) -> float:
        try:
            speed = float(cast(Any, value))
        except (TypeError, ValueError):
            speed = 1.0
        speed = min(MAX_SPEED, max(MIN_SPEED, speed))
        return round(round(speed / SPEED_STEP) * SPEED_STEP, 2)

    @staticmethod
    def _progress_text(done: int, total: int) -> str:
        percent = 100 * done // total if total else 0
        return f"{done:,} / {total:,} PIECES · {percent}%"

    def _reset_progress(self) -> None:
        for name, value in (
            ("stage", "starting"), ("downloadLabel", ""), ("chunksDone", 0), ("chunksTotal", 0),
            ("timeLeft", UNKNOWN_TIME), ("progressLabel", self._progress_text(0, 0)),
            ("speedLabel", ""), ("nowSpeaking", ""), ("paused", False), ("outputPath", ""),
            ("audioLength", ""), ("doneSummary", ""), ("warnings", []),
        ):  # fmt: skip
            self._update(name, value)

    def _end_conversion(self) -> None:
        self._convert_job = None
        self._control = None
        self._update("paused", False)

    @property
    def _using_gpu(self) -> bool:
        """True when the graphics card is both there and switched on."""
        return self._gpuAvailable and self._useGpu

    def _device_label(self) -> str:
        """What is voicing, short: ``"NVIDIA RTX PRO 1000"`` or ``"CPU"``."""
        if self._using_gpu and self._gpuName:
            return short_name(self._gpuName)
        return "CPU"

    def _refresh_hardware_label(self) -> None:
        device = self._device_label()
        if self._speedup is not None and self._speedup_for == device:
            self._update(
                "hardwareLabel", f"{device} \u00b7 ABOUT {self._speedup:.1f}\u00d7 REAL TIME"
            )
        elif device == "CPU":
            self._update("hardwareLabel", self._cpuLabel)
        else:
            self._update("hardwareLabel", device)

    def _measured(self, speedup: float) -> None:
        self._speedup = speedup
        self._speedup_for = self._device_label()
        self._refresh_hardware_label()
        self.selectionSummaryChanged.emit()

    # --- Preview playback ------------------------------------------------------------------

    def _play(self, wav: Path) -> None:
        assert self._player is not None
        process = QProcess(self)
        process.finished.connect(self._on_playback_finished)
        process.errorOccurred.connect(self._on_playback_error)
        self._playback = process
        process.start(self._player, [str(wav)])

    @Slot()
    def _on_playback_finished(self) -> None:
        if self.sender() is self._playback:
            self._playback = None
            self._update("previewPlaying", False)

    @Slot(QProcess.ProcessError)
    def _on_playback_error(self, error: QProcess.ProcessError) -> None:
        if self.sender() is self._playback and error == QProcess.ProcessError.FailedToStart:
            self._playback = None
            self._update("previewPlaying", False)
            self._update("errorMessage", f"Couldn't play the preview with {self._player}.")

    def _stop_preview(self) -> None:
        self._preview_job = None
        playback, self._playback = self._playback, None
        if playback is not None and playback.state() != QProcess.ProcessState.NotRunning:
            playback.kill()
            playback.waitForFinished(1000)
        self._update("previewPlaying", False)
