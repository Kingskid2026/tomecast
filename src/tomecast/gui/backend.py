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
import sys
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

from tomecast import voices
from tomecast.book import Book
from tomecast.chunking import split_sentences
from tomecast.cleanup import CleanupOptions, clean
from tomecast.downloads import Asset, build_in_use, data_dir, needs_download
from tomecast.engines import ming
from tomecast.engines.audiocpp import (
    DEFAULT_VOICE,
    VOICES,
    check_system,
    stop_all_engines,
)
from tomecast.gui.fixes import BadFix, FixLists, FixNotSaved, Scope
from tomecast.gui.models import DEFAULT_SPEEDUP, ChapterModel
from tomecast.gui.worker import (
    BookRead,
    ConversionWorker,
    ConvertJob,
    ConvertResult,
    ConvertUpdate,
    DeviceScanner,
    EngineFactory,
    PreviewJob,
    ReadJob,
    ScanJob,
    SelfCheckFor,
    SpeedProbe,
    TimedControl,
    kokoro_engine,
    megabytes,
    preview_file,
    probe_speeds,
    scan_devices,
)
from tomecast.hardware import short_name, vulkan_possible
from tomecast.machine import recommend, scan_machine
from tomecast.names import Name
from tomecast.packaging import portable_name
from tomecast.pronunciations import book_list_path
from tomecast.readers import FORMATS, SUFFIXES
from tomecast.speed import Speeds
from tomecast.textprep import TextPrep

SCREENS = ("drop", "chapters", "voice", "names", "converting", "done")
THEME_SETTINGS = ("system", "dark", "light")
OUTPUT_FORMATS = ("m4b", "mp3")
MIN_SPEED, MAX_SPEED, SPEED_STEP = 0.5, 3.0, 0.25  # 11 meter dots; 1.00x is the third
PLAYERS = ("pw-play", "paplay", "aplay")  # the first one found plays previews

FORMATS_LABEL = " · ".join(FORMATS)  # only what Tomecast can read today
# Formats the window lists but can't read yet (none at the moment), by extension.
COMING_SOON: dict[str, str] = {}
_FORMAT_LIST = f"{', '.join(FORMATS[:-1])} and {FORMATS[-1]}"
STOPPED_MESSAGE = "Stopped. Start again to continue where you left off."
UNKNOWN_TIME = "--:--:--"
# How long closing the window waits for the worker, twice over (packaging aside).
SHUTDOWN_WAIT_MS = 7_500
DOWNLOAD_NOTE = "STARTING DOWNLOADS THE VOICE ONCE · {mb} MB"
# When everything missing is a newer build of an engine that's installed (Start checks it
# before using it, and keeps the old one if it doesn't work here).
UPDATE_NOTE = "STARTING DOWNLOADS A VOICE ENGINE UPDATE · {mb} MB"
# The premium voice's speed until a book has measured it here: Ming through Vulkan on the
# maintainer's NVIDIA laptop card (local/ming-test/report.md). Kokoro's default is the CPU's.
DEFAULT_PREMIUM_SPEEDUP = 1.8
# When the premium voice is among the missing files (it's opt-in: Start with a premium voice
# chosen is the asking).
PREMIUM_DOWNLOAD_NOTE = (
    "STARTING DOWNLOADS THE PREMIUM VOICE ONCE · {mb} MB · NEEDS A GRAPHICS CARD"
)
# The "Use graphics card" note, with and without a measured speed-up (the card counts as
# faster from 1.05 times the CPU's speed).
GPU_NOTE_ON = "FASTER · USES THE DRIVER ALREADY INSTALLED"
GPU_NOTE_OFF = "USING THE CPU · THE GRAPHICS CARD IS USUALLY SEVERAL TIMES FASTER"
GPU_NOTE_ON_MEASURED = "ABOUT {ratio}\u00d7 FASTER HERE · USES THE DRIVER ALREADY INSTALLED"
GPU_NOTE_OFF_MEASURED = "USING THE CPU · THE GRAPHICS CARD IS ABOUT {ratio}\u00d7 FASTER HERE"
GPU_NOTE_ON_NOT_FASTER = "NO FASTER THAN THE CPU HERE · USES THE DRIVER ALREADY INSTALLED"
GPU_NOTE_OFF_NOT_FASTER = "USING THE CPU · THE GRAPHICS CARD WAS NO FASTER HERE"
NAME_METER_DOTS = 11  # the Names screen's count meter

# Voice menu groups, and the order of voices in each (as in the maintainer's mockup).
VOICE_GROUPS = (
    ("af", "AMERICAN · FEMALE"),
    ("am", "AMERICAN · MALE"),
    ("bf", "BRITISH · FEMALE"),
    ("bm", "BRITISH · MALE"),
)
# The premium voices (Ming), shown after Kokoro's only when there's a graphics card. Their
# names say the accent; British first, since UK_George is the provisional default (#11).
PREMIUM_GROUPS = (
    ("UK", "PREMIUM · BRITISH"),
    ("US", "PREMIUM · AMERICAN"),
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


def voice_groups(premium: bool = False) -> list[dict[str, Any]]:
    """The voice menu: American and British English, grouped by accent and gender, then the
    premium voices (Ming) by accent when ``premium`` (a graphics card is there to use)."""
    groups: list[dict[str, Any]] = []
    for prefix, label in VOICE_GROUPS:
        ids = [voice for voice in VOICES if voice.startswith(f"{prefix}_")]
        ids.sort(key=lambda v: (_VOICE_ORDER.index(v) if v in _VOICE_ORDER else 99, v))
        names = [{"id": voice, "name": voice.split("_", 1)[1].capitalize()} for voice in ids]
        groups.append({"label": label, "voices": names})
    if premium:
        for prefix, label in PREMIUM_GROUPS:
            ids = [voice for voice in ming.VOICES if voice.startswith(f"{prefix}_")]
            names = [{"id": voice, "name": voice.split("_", 1)[1]} for voice in ids]
            groups.append({"label": label, "voices": names})
    return groups


def _as_bool(value: object, *, default: bool) -> bool:
    """A saved setting as a bool (an INI file stores it as the text "true" or "false")."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text == "true" else False if text == "false" else default


def voice_label(voice: str) -> str:
    """``"HEART · AMERICAN · FEMALE"``, or ``"GEORGE · PREMIUM · BRITISH"``."""
    prefix, _, name = voice.partition("_")
    group = dict(PREMIUM_GROUPS if ming.is_ming_voice(voice) else VOICE_GROUPS).get(prefix)
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


def cpu_label(threads: int, *, on_battery: bool = False, speedup: float | None = None) -> str:
    """The corner's hardware label for the CPU: ``"CPU · 8 THREADS"``, or ``"CPU · ABOUT 4.7x
    REAL TIME"`` once measured with that many threads, with ``"ON BATTERY"`` after ``"CPU"``
    while the computer runs on its battery (it voices with fewer threads then)."""
    parts = ["CPU", "ON BATTERY"] if on_battery else ["CPU"]
    if speedup is not None:
        parts.append(f"ABOUT {speedup:.1f}\u00d7 REAL TIME")
    else:
        parts.append(f"{threads} THREAD{'' if threads == 1 else 'S'}")
    return " \u00b7 ".join(parts)


def name_rows(names: Sequence[Name]) -> list[dict[str, Any]]:
    """The Names screen's rows: ``{word, count, countLabel, meterLit}``. The label is the
    count and a multiplication sign; the 11-dot meter is log-scaled to the most frequent
    name, with at least one dot lit."""
    top = math.log(names[0].count) if names and names[0].count > 1 else 0.0
    rows = []
    for name in names:
        share = math.log(name.count) / top if top > 0 else 1.0
        lit = max(1, min(NAME_METER_DOTS, math.floor(share * NAME_METER_DOTS + 0.5)))
        rows.append(
            {"word": name.word, "count": name.count, "countLabel": f"{name.count}\u00d7",
             "meterLit": lit}
        )  # fmt: skip
    return rows


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
    gpuNoteChanged = Signal()
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
    bookHasPagesChanged = Signal()
    skipPageNumbersChanged = Signal()
    footnotesAtEndChanged = Signal()
    selectionSummaryChanged = Signal()

    voiceGroupsChanged = Signal()
    voiceChanged = Signal()
    voiceLabelChanged = Signal()
    speedChanged = Signal()
    previewTextChanged = Signal()
    previewPlayingChanged = Signal()
    previewAvailableChanged = Signal()

    namesChanged = Signal()
    nameFixesChanged = Signal()
    fixesChanged = Signal()
    bookListNameChanged = Signal()
    playingNameChanged = Signal()
    tryPlayingChanged = Signal()

    downloadNoteChanged = Signal()
    premiumNoteChanged = Signal()
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
    _leaveCardRequested = Signal()

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
        device_scanner: DeviceScanner = scan_devices,
        speed_probe: SpeedProbe = probe_speeds,
        engine_check: SelfCheckFor | None = None,
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
        # Times real time per device ("CPU/<threads>" or the card's short name, see _speed_key):
        # from the speed test at start-up, then from what this session's conversions measured.
        self._speeds: dict[str, float] = {}
        self._raw_book: Book | None = None  # as read, before cleanup
        self._book: Book | None = None  # cleaned with the skip switches
        self._book_path: Path | None = None
        self._cover_count = 0
        self._has_cover = False
        self._playback: QProcess | None = None
        self._playing = ""  # what plays: "preview", "try", "name:<word>" or "" for nothing
        self._fix_lists: FixLists | None = None
        self._fix_error = ""  # the last banner message a fix caused
        self._notice = ""  # the banner's sentence while a book goes on (the card ran out of memory)
        self._preview_cache: Path | None = None
        self._shut_down = False
        fixed_assets = None if assets is None else tuple(assets)
        # The files a run needs depend on the voice (Kokoro or Ming) and whether it uses the
        # graphics card.
        self._assets_for: Callable[[str, bool], Sequence[Asset]] = (
            voices.required_assets
            if fixed_assets is None
            else (lambda voice, use_gpu: fixed_assets)
        )

        # App
        self._screen = "drop"
        saved_theme = str(self._settings.value("themeSetting", "system"))
        self._themeSetting = saved_theme if saved_theme in THEME_SETTINGS else "system"
        self._theme = self._effective_theme()
        self._reduceMotion = system_reduce_motion()
        # The CPU's thread count and the power state, as last read (see _rescan_machine).
        self._cpu_threads, self._on_battery = 0, False
        self._rescan_machine()
        self._hardwareLabel = cpu_label(self._cpu_threads, on_battery=self._on_battery)
        self._gpuAvailable = gpu_check()
        self._useGpu = _as_bool(self._settings.value("useGpu", True), default=True)
        self._gpuName = ""
        self._gpuNote = GPU_NOTE_ON if self._useGpu else GPU_NOTE_OFF
        self._errorMessage = ""
        # Book
        self._bookLoaded = False
        self._loading = False
        self._bookTitle = ""
        self._bookAuthors = ""
        self._skipFrontMatter = True
        self._bookHasPages = False
        self._skipPageNumbers = True
        self._footnotesAtEnd = False
        self._chapters = ChapterModel(self)
        self._chapters.selectionChanged.connect(self._on_selection_changed)
        # Voice
        self._voiceGroups = voice_groups(premium=self._premium_listed)
        saved_voice = str(self._settings.value("voice", DEFAULT_VOICE))
        # A saved premium voice counts only while it's listed (the preview opted into, and
        # the card in use); otherwise it's the default voice.
        self._voice = saved_voice if saved_voice in self._voice_ids() else DEFAULT_VOICE
        self._speed = self._valid_speed(self._settings.value("speed", 1.0))
        if ming.is_ming_voice(self._voice):  # a speed saved with Kokoro can be above 2x
            self._speed = ming.capped_speed(self._speed)
        # Which voice family the conversion in progress is measuring ("kokoro" or "ming"):
        # each has its own speeds (see _speed_key). Kokoro, once it takes over from Ming.
        self._voicing_family = "kokoro"
        saved_format = str(self._settings.value("outputFormat", "m4b"))
        self._outputFormat = saved_format if saved_format in OUTPUT_FORMATS else "m4b"
        self._convert_format = self._outputFormat  # the format of the run in progress
        self._previewText = ""
        self._previewPlaying = False
        self._downloadNote = ""
        self._premiumNote = ""
        self._refresh_premium_note()
        # Names
        self._names: list[dict[str, Any]] = []
        self._nameFixes: dict[str, str] = {}
        self._fixes: list[dict[str, str]] = []
        self._bookListName = ""
        self._playingName = ""
        self._tryPlaying = False
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
            speed_probe=speed_probe,
            **({"engine_check": engine_check} if engine_check is not None else {}),
        )
        self._worker.moveToThread(self._thread)
        self._jobRequested.connect(self._worker.run)
        self._shutdownRequested.connect(self._worker.shutdown)
        self._leaveCardRequested.connect(self._worker.leave_card)
        self._worker.bookRead.connect(self._on_book_read)
        self._worker.previewReady.connect(self._on_preview_ready)
        self._worker.stageChanged.connect(self._on_stage)
        self._worker.downloadProgress.connect(self._on_download_progress)
        self._worker.progressed.connect(self._on_progress)
        self._worker.converted.connect(self._on_converted)
        self._worker.failed.connect(self._on_failed)
        self._worker.noticed.connect(self._on_notice)
        self._worker.deviceChosen.connect(self._on_device)
        self._worker.speedsKnown.connect(self._on_speeds)
        self._thread.start()
        # The card's name, and how fast it and the CPU voice (measured once per engine build
        # and device, then remembered).
        # With "Use graphics card" off, it doesn't touch the card at all.
        self._jobRequested.emit(ScanJob(next(self._jobs), gpu=self._using_gpu))

    # --- Properties: app -------------------------------------------------------------------

    screen: str = _field(str, "screen", screenChanged)
    themeSetting: str = _field(str, "themeSetting", themeSettingChanged)
    theme: str = _field(str, "theme", themeChanged)
    reduceMotion: bool = _field(bool, "reduceMotion", reduceMotionChanged)
    hardwareLabel: str = _field(str, "hardwareLabel", hardwareLabelChanged)
    gpuAvailable: bool = _field(bool, "gpuAvailable", gpuAvailableChanged)
    useGpu: bool = _field(bool, "useGpu", useGpuChanged)
    gpuName: str = _field(str, "gpuName", gpuNameChanged)
    gpuNote: str = _field(str, "gpuNote", gpuNoteChanged)
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
    bookHasPages: bool = _field(bool, "bookHasPages", bookHasPagesChanged)
    skipPageNumbers: bool = _field(bool, "skipPageNumbers", skipPageNumbersChanged)
    footnotesAtEnd: bool = _field(bool, "footnotesAtEnd", footnotesAtEndChanged)

    def _get_selection_summary(self) -> dict[str, str]:
        family = voices.engine_of(self._voice)
        speedup = self._speeds.get(self._speed_key(self._device_label(), family))
        if speedup is None:
            speedup = DEFAULT_SPEEDUP if family == "kokoro" else DEFAULT_PREMIUM_SPEEDUP
        # The premium voice always speaks at 1.0 (all of its speed is the stretch).
        voice_speed = 1.0 if family == "ming" else None
        return self._chapters.summary(speedup, self._speed, voice_speed=voice_speed)

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
    premiumNote: str = _field(str, "premiumNote", premiumNoteChanged)

    def _get_preview_available(self) -> bool:
        return self._player is not None

    previewAvailable = _property(bool, _get_preview_available, previewAvailableChanged)

    # --- Properties: names -----------------------------------------------------------------

    names: list[dict[str, Any]] = _field(list, "names", namesChanged)
    nameFixes: dict[str, str] = _field(dict, "nameFixes", nameFixesChanged)
    fixes: list[dict[str, str]] = _field(list, "fixes", fixesChanged)
    bookListName: str = _field(str, "bookListName", bookListNameChanged)
    playingName: str = _field(str, "playingName", playingNameChanged)
    tryPlaying: bool = _field(bool, "tryPlaying", tryPlayingChanged)

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
        if not on:
            # With the switch off, nothing of Tomecast's stays on the card: the preview engine
            # kept running between previews would otherwise hold its memory until the next one.
            self._leaveCardRequested.emit()
        self._rescan_machine()
        self._update("useGpu", on)
        self._settings.setValue("useGpu", on)
        self._settings.sync()
        # The premium voices run only on the card, so they're listed only while it's in use;
        # turning it off while one is chosen goes back to the default voice.
        if not self._premium_listed and ming.is_ming_voice(self._voice):
            self._update("voice", DEFAULT_VOICE)
            self.voiceLabelChanged.emit()
            self._settings.setValue("voice", DEFAULT_VOICE)
        self._update("voiceGroups", voice_groups(premium=self._premium_listed))
        self.selectionSummaryChanged.emit()  # each device has its own speed
        self._refresh_hardware_label()
        self._refresh_download_note()
        if self._using_gpu and not self._card_known():
            # Switched on for the first time this session: learn the card's name and speed.
            self._jobRequested.emit(ScanJob(next(self._jobs), gpu=True))

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
        elif screen == "voice" and self._bookLoaded and current in ("chapters", "done", "names"):
            if not self._chapters.selected_rows():
                self._update("errorMessage", "Choose at least one section to convert.")
                return
            self._show("voice")
        elif screen == "names" and self._bookLoaded and current == "voice" and self._names:
            self._show("names")

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

    @Slot(bool)
    def setSkipPageNumbers(self, on: bool) -> None:
        """Leave out (or read) page numbers and running headers and footers, then refresh
        the chapter list. Only books with pages (PDF) have them."""
        if on != self._skipPageNumbers:
            self._update("skipPageNumbers", on)
            self._apply_cleanup(keep_selection=True)

    @Slot(bool)
    def setFootnotesAtEnd(self, on: bool) -> None:
        """Read footnotes at the end of their chapter (or leave them out), then refresh."""
        if on != self._footnotesAtEnd:
            self._update("footnotesAtEnd", on)
            self._apply_cleanup(keep_selection=True)

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
        self.setSpeed(self._speed)  # the premium voice goes up to 2x
        self._refresh_download_note()  # each voice has its own files

    @Slot(float)
    def setSpeed(self, value: float) -> None:
        speed = self._valid_speed(value)
        if ming.is_ming_voice(self._voice):
            speed = min(speed, ming.MAX_SPEED)
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
        self._toggle_play("preview", self._previewText)

    # --- Slots: names ----------------------------------------------------------------------

    @Slot(str, str)
    def setNameFix(self, word: str, say: str) -> None:
        """Save how to say ``word`` (a listed name); an empty ``say`` removes its fix. A new
        fix goes in the book's list; one that exists is changed in the list that has it."""
        if self._fix_lists is None or not any(row["word"] == word for row in self._names):
            return
        try:
            self._fix_lists.set_for_name(word, say)
        except BadFix as problem:
            self._fix_problem(str(problem))  # nothing changed
            return
        except FixNotSaved as problem:
            self._fix_problem(str(problem))
        else:
            self._fix_problem("")
        self._refresh_fixes()

    @Slot(str, str)
    def removeFix(self, scope: str, word: str) -> None:
        """Remove the fix for ``word`` from the book's list (``"book"``) or the global one
        (``"all"``)."""
        self._edit_fixes(lambda lists, where: lists.remove(where, word), scope)

    @Slot(str, str)
    def toggleFixScope(self, scope: str, word: str) -> None:
        """Move the fix for ``word`` from the list ``scope`` to the other one."""
        self._edit_fixes(lambda lists, where: lists.move(where, word), scope)

    @Slot(str)
    def playName(self, word: str) -> None:
        """Voice ``word`` with the fixes, in the chosen voice and speed; again to stop."""
        self._toggle_play(f"name:{word}", word)

    @Slot(str)
    def playText(self, text: str) -> None:
        """Voice any text with the fixes (TRY ANY WORD); again to stop."""
        self._toggle_play("try", text)

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
        # The lists are read again on the worker, unless a change couldn't be saved: then the
        # window's own copy is the only one with every fix.
        lists = self._fix_lists
        fixes = lists.combined() if lists is not None and lists.unsaved else None
        # The whole book plus the ticked indices, so chunk IDs (and the resume folder) are the
        # same whichever sections are ticked, and match a run from the command line.
        book = self._book
        output_format: Literal["m4b", "mp3"] = "mp3" if self._outputFormat == "mp3" else "m4b"
        output = unique_output(folder, self._book.title or self._book_path.stem, output_format)
        self._convert_format = output_format
        self._chapter_labels = [sections[r].label for r in rows]
        self._control = TimedControl()
        self._convert_job = next(self._jobs)
        self._voicing_family = voices.engine_of(self._voice)
        self._reset_progress()
        self._update("chapterLabel", self._chapter_labels[0])
        self._update("errorMessage", "")
        self._notice = ""
        self._show("converting")
        self._jobRequested.emit(
            ConvertJob(
                self._convert_job, book, tuple(rows), self._book_path, output, self._voice,
                self._speed, self._control, self._using_gpu, output_format, fixes,
                self._speeds.get(self._speed_key("CPU")),
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
        self._raw_book = None
        self._book = None
        self._book_path = None
        self._read_job = None
        self._chapters.clear()
        self._set_cover(None)
        self._fix_lists = None
        for name, value in (
            ("bookLoaded", False), ("loading", False), ("bookTitle", ""), ("bookAuthors", ""),
            ("bookHasPages", False), ("names", []), ("nameFixes", {}), ("fixes", []),
            ("bookListName", ""),
            ("outputPath", ""), ("audioLength", ""), ("doneSummary", ""), ("warnings", []),
            ("errorMessage", ""),
        ):  # fmt: skip
            self._update(name, value)
        self._show("drop")

    # --- Shutting down ---------------------------------------------------------------------

    def shutdown(self) -> None:
        """Cancel any running job, stop the engines and join the worker thread.

        While a book is being packaged it waits for that to finish (the audiobook is written
        regardless). Otherwise it doesn't wait long: every engine is stopped at once, and the
        worker's waits (another Tomecast's install, a self-check, the speed test, a download)
        all stop when asked, so the thread ends within :data:`SHUTDOWN_WAIT_MS`."""
        if self._shut_down:
            return
        self._shut_down = True
        self._worker.stop()
        # cancel() refuses (False) exactly when packaging has begun, deciding under the
        # control's own lock, so a commit can't slip in between looking and cancelling.
        packaging = self._control is not None and not self._control.cancel()
        self._stop_preview()
        self._shutdownRequested.emit()
        if packaging:
            self._thread.wait()
        else:
            stop_all_engines()  # a start-up or request in progress ends now
            if not self._thread.wait(SHUTDOWN_WAIT_MS):
                stop_all_engines()  # one that started meanwhile
                if not self._thread.wait(SHUTDOWN_WAIT_MS):
                    print(
                        "tomecast: the worker didn't stop in time; closing anyway.", file=sys.stderr
                    )
        if self._preview_cache is not None:
            shutil.rmtree(self._preview_cache, ignore_errors=True)
            self._preview_cache = None

    def save_settings(self) -> None:
        """Write the saved settings to disk now (before leaving without Qt's own clean-up)."""
        self._settings.sync()

    @property
    def worker_thread(self) -> QThread:
        """The worker's thread (for tests and shutdown checks)."""
        return self._thread

    # --- Worker results --------------------------------------------------------------------

    @Slot(int, object)
    def _on_book_read(self, job: int, read: BookRead) -> None:
        if job != self._read_job:
            return
        self._read_job = None
        if self._screen == "converting":
            # A book finished reading while another is being converted. Drop it: showing
            # it would strand the conversion that is running.
            self._update("loading", False)
            return
        book = read.book
        self._raw_book = book
        self._book_path = self._read_path
        self._fix_lists = read.fixes
        self._update("names", name_rows(read.names))
        self._refresh_fixes()
        list_name = book_list_path(self._book_path).name if self._book_path else ""
        self._update("bookListName", list_name)
        self._update("bookHasPages", book.has_pages)
        self._apply_cleanup(keep_selection=False)
        self._update("bookTitle", book.title)
        self._update("bookAuthors", ", ".join(book.authors))
        self._set_cover(book.cover.data if book.cover else None)
        self._update("bookLoaded", True)
        self._update("loading", False)
        self._show("chapters")

    @Slot(str, bool)
    def _on_device(self, description: str, asked: bool) -> None:
        """The worker found out (or started the engine on) which graphics card is in use.
        Only when the card was ``asked`` for does an empty description mean it isn't there or
        didn't work: an engine started on the CPU because the card is switched off says
        nothing about it, so its name stays, and with it the measured note."""
        self._rescan_machine()  # what the engine just read too
        if asked:
            self._update("gpuName", description)
        self._refresh_hardware_label()
        self.selectionSummaryChanged.emit()

    @Slot(object)
    def _on_speeds(self, speeds: Speeds) -> None:
        """The start-up scan's speeds. A speed this session measured converting is better,
        so it isn't replaced."""
        # The CPU was measured with the thread count of a moment ago (the probe reads it
        # itself), so it's kept under the count read now.
        self._rescan_machine()
        if speeds.cpu is not None:
            self._speeds.setdefault(self._speed_key("CPU"), speeds.cpu)
        if speeds.gpu is not None and speeds.gpu_description:
            self._speeds.setdefault(short_name(speeds.gpu_description), speeds.gpu)
            if not self._gpuName and self._gpuAvailable:
                self._update("gpuName", speeds.gpu_description)
        self._refresh_hardware_label()
        self.selectionSummaryChanged.emit()

    @Slot(int, str)
    def _on_preview_ready(self, job: int, path: str) -> None:
        if job != self._preview_job:
            return
        self._preview_job = None
        if self._playing:
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
        if self._notice and self._errorMessage == self._notice:
            self._update("errorMessage", "")  # the done screen's notes say it now
        self._notice = ""
        self._show("done")

    @Slot(int, str, int)
    def _on_notice(self, job: int, message: str, threads: int) -> None:
        """The graphics card ran out of memory and the book goes on on the CPU, with
        ``threads``: say so in the banner while it's being made (the done screen lists it with
        the other notes), and show the CPU. The thread count is the engine's own, not read
        again, so the speed this run measures is kept under the count that made it."""
        if job != self._convert_job:
            return
        self._cpu_threads = threads
        self._update("gpuName", "")
        # A notice in a premium-voice book means Kokoro took over (the window's default when
        # the card fails): what's measured from here is Kokoro's speed.
        self._voicing_family = "kokoro"
        self._refresh_hardware_label()
        self.selectionSummaryChanged.emit()
        if message:
            self._notice = message
            self._update("errorMessage", message)

    @Slot(int, str)
    def _on_failed(self, job: int, message: str) -> None:
        if job == self._read_job:
            self._read_job = None
            self._update("loading", False)
        elif job == self._preview_job:
            self._preview_job = None
            self._set_playing("")
        elif job == self._convert_job:
            self._end_conversion()
            self._show("voice")
        else:
            return  # a job the window no longer waits for
        self._update("errorMessage", message)

    # --- Helpers ---------------------------------------------------------------------------

    def _cleanup_options(self) -> CleanupOptions:
        """The cleanup the switches ask for. Front and back matter is found but not removed:
        "Skip front matter" unticks those rows instead, so they can be ticked again."""
        return CleanupOptions(
            page_numbers=self._skipPageNumbers,
            footnotes="end" if self._footnotesAtEnd else "drop",
            skip=frozenset(),
        )

    def _apply_cleanup(self, *, keep_selection: bool) -> None:
        """Clean the book as read with the current switches and refresh the chapter list."""
        if self._raw_book is None:
            return
        result = clean(self._raw_book, self._cleanup_options())
        self._book = result.book
        self._chapters.set_book(
            result.book,
            skip_front_matter=self._skipFrontMatter,
            matter=result.matter,
            keep_selection=keep_selection,
        )

    def _update(self, name: str, value: object) -> None:
        """Set the property ``name`` and emit ``<name>Changed`` if its value changed."""
        attribute = f"_{name}"
        if getattr(self, attribute) != value:
            setattr(self, attribute, value)
            getattr(self, f"{name}Changed").emit()

    def _show(self, screen: str) -> None:
        if self._rescan_machine():  # the charger was plugged in or pulled
            self._refresh_hardware_label()
            self.selectionSummaryChanged.emit()
        if screen != self._screen:
            self._stop_preview()  # whatever played belonged to the screen being left
        if screen in ("voice", "names"):
            self._refresh_download_note()
        self._update("screen", screen)

    def _refresh_premium_note(self) -> None:
        """The muted line under the premium voices (``""`` while they aren't listed): what
        choosing one downloads, if anything, that they need the graphics card, and that
        they're a preview while no spoken-text check is plugged in."""
        if not self._premium_listed:
            self._update("premiumNote", "")
            return
        parts = ["PREMIUM VOICES"]
        missing = [
            asset
            for asset in self._assets_for(ming.DEFAULT_VOICE, True)
            if self._needs_download(asset)
        ]
        if missing:
            size = sum(asset.size for asset in missing) / 1e9
            parts.append(f"{size:.1f} GB DOWNLOAD")
        parts.append("NEED A GRAPHICS CARD")
        if ming.preview_note():
            parts.append("PREVIEW")
        self._update("premiumNote", " · ".join(parts))

    def _refresh_download_note(self) -> None:
        """Say what Start will download: nothing when every required file is installed."""
        assets = self._assets_for(self._voice, self._using_gpu)
        missing = [a for a in assets if self._needs_download(a)]
        if not missing:
            self._update("downloadNote", "")
            self._refresh_premium_note()
            return
        mb = math.ceil(sum(asset.size for asset in missing) / 1_000_000)
        updates = all(a.archive and build_in_use(a, self._data_root) is not None for a in missing)
        note = UPDATE_NOTE if updates else DOWNLOAD_NOTE
        if any(asset.opt_in for asset in missing):
            note = PREMIUM_DOWNLOAD_NOTE
        self._update("downloadNote", note.format(mb=f"{mb:,}"))
        self._refresh_premium_note()

    def _needs_download(self, asset: Asset) -> bool:
        """``needs_download`` without hashing anything: this runs on the GUI thread, and a
        model can be 1.7 GB. A file already verified has a marker; one that hasn't been yet
        counts if it has the pinned size, and the worker verifies it before converting."""
        return needs_download(asset, self._data_root, verify=False)

    def _card_known(self) -> bool:
        """The card's name and its speed are both known."""
        return bool(self._gpuName) and short_name(self._gpuName) in self._speeds

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

    @property
    def _premium_listed(self) -> bool:
        """The premium voices are listed: the graphics card is in use, and they're offered
        (until a spoken-text check is plugged in, only with TOMECAST_PREMIUM_PREVIEW=1)."""
        return self._using_gpu and ming.preview_enabled()

    def _device_label(self) -> str:
        """What is voicing, short: ``"NVIDIA RTX PRO 1000"`` or ``"CPU"``."""
        if self._using_gpu and self._gpuName:
            return short_name(self._gpuName)
        return "CPU"

    def _speed_key(self, device: str, family: str = "kokoro") -> str:
        """Where a device's speed is kept: the card's short name, or ``"CPU/<threads>"``, since
        the CPU's speed depends on how many threads it voices with (fewer on battery). The
        premium voice's speeds are kept apart, with ``/ming`` added (as in speeds.json): it's
        several times slower than Kokoro on the same card. The hardware label and the "Use
        graphics card" note show Kokoro's, the hardware's own speed test."""
        key = f"CPU/{self._cpu_threads}" if device == "CPU" else device
        return f"{key}/ming" if family == "ming" else key

    def _rescan_machine(self) -> bool:
        """Read the thread count the voice would use now and whether the computer runs on
        battery (a few small file reads); True if either changed. Done when the screen changes,
        the switch flips, an engine starts and the speed test ends, never on progress updates,
        so a conversion's speed is kept under the thread count its engine started with."""
        machine = scan_machine()
        now = (recommend(machine).threads, machine.power.on_battery is True)
        changed = now != (self._cpu_threads, self._on_battery)
        self._cpu_threads, self._on_battery = now
        return changed

    def _refresh_hardware_label(self) -> None:
        device = self._device_label()
        speedup = self._speeds.get(self._speed_key(device))
        if device == "CPU":
            label = cpu_label(self._cpu_threads, on_battery=self._on_battery, speedup=speedup)
            self._update("hardwareLabel", label)
        elif speedup is not None:
            self._update("hardwareLabel", f"{device} \u00b7 ABOUT {speedup:.1f}\u00d7 REAL TIME")
        else:
            self._update("hardwareLabel", device)
        self._update("gpuNote", self._gpu_note())

    def _gpu_note(self) -> str:
        """The note under "Use graphics card": the card's measured speed-up over the CPU
        once both have been measured, else the general one."""
        cpu = self._speeds.get(self._speed_key("CPU"))
        card = self._speeds.get(short_name(self._gpuName)) if self._gpuName else None
        if not cpu or not card:
            return GPU_NOTE_ON if self._useGpu else GPU_NOTE_OFF
        ratio = card / cpu
        if ratio < 1.05:
            return GPU_NOTE_ON_NOT_FASTER if self._useGpu else GPU_NOTE_OFF_NOT_FASTER
        note = GPU_NOTE_ON_MEASURED if self._useGpu else GPU_NOTE_OFF_MEASURED
        return note.format(ratio=f"{ratio:.1f}")

    def _measured(self, speedup: float) -> None:
        """A conversion measured the speed on what it ran on (the CPU if the card fell back),
        with the voice it was voicing (Kokoro once it took over from the premium voice)."""
        self._speeds[self._speed_key(self._device_label(), self._voicing_family)] = speedup
        self._refresh_hardware_label()
        self.selectionSummaryChanged.emit()

    # --- Names and fixes -------------------------------------------------------------------

    def _edit_fixes(self, change: Callable[[FixLists, Scope], None], scope: str) -> None:
        if self._fix_lists is None or scope not in ("book", "all"):
            return
        try:
            change(self._fix_lists, cast(Scope, scope))
        except FixNotSaved as problem:
            self._fix_problem(str(problem))
        else:
            self._fix_problem("")
        self._refresh_fixes()

    def _fix_problem(self, message: str) -> None:
        """Show what went wrong with a fix, or clear the banner a fix put up earlier."""
        if message:
            self._fix_error = message
            self._update("errorMessage", message)
        elif self._fix_error and self._errorMessage == self._fix_error:
            self._update("errorMessage", "")

    def _refresh_fixes(self) -> None:
        lists = self._fix_lists
        if lists is None:
            self._update("nameFixes", {})
            self._update("fixes", [])
            return
        found = {row["word"]: lists.fix_for(row["word"]) for row in self._names}
        self._update("nameFixes", {word: fix.say for word, fix in found.items() if fix})
        self._update(
            "fixes", [{"word": f.word, "say": f.say, "scope": f.scope} for f in lists.fixes()]
        )

    def _spoken(self, text: str) -> str:
        """``text`` as the book would say it: the fixes, then the language's rules (what
        `tomecast preview` does)."""
        fixes = self._fix_lists.combined() if self._fix_lists is not None else None
        language = self._book.language if self._book is not None else None
        return TextPrep.for_language(language, fixes).body(text)

    # --- Preview playback ------------------------------------------------------------------

    def _toggle_play(self, key: str, text: str) -> None:
        """Voice ``text`` (as the book would say it) and play it. Only one thing plays at a
        time: a second press of the same button stops it, another button switches."""
        if self._playing == key:
            self._stop_preview()
            return
        self._stop_preview()
        spoken = self._spoken(text.strip())
        if self._player is None or self._shut_down or not any(c.isalnum() for c in spoken):
            return
        self._set_playing(key)
        if self._preview_cache is None:
            self._preview_cache = Path(tempfile.mkdtemp(prefix="tomecast-preview-"))
        wav = preview_file(self._preview_cache, self._voice, self._speed, spoken)
        if wav.is_file():
            self._play(wav)
            return
        self._preview_job = next(self._jobs)
        job = PreviewJob(self._preview_job, spoken, self._voice, self._speed, wav, self._using_gpu)
        self._jobRequested.emit(job)

    def _set_playing(self, key: str) -> None:
        self._playing = key
        self._update("previewPlaying", key == "preview")
        self._update("tryPlaying", key == "try")
        self._update("playingName", key.removeprefix("name:") if key.startswith("name:") else "")

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
            self._set_playing("")

    @Slot(QProcess.ProcessError)
    def _on_playback_error(self, error: QProcess.ProcessError) -> None:
        if self.sender() is self._playback and error == QProcess.ProcessError.FailedToStart:
            self._playback = None
            self._set_playing("")
            self._update("errorMessage", f"Couldn't play the preview with {self._player}.")

    def _stop_preview(self) -> None:
        self._preview_job = None
        playback, self._playback = self._playback, None
        if playback is not None and playback.state() != QProcess.ProcessState.NotRunning:
            playback.kill()
            playback.waitForFinished(1000)
        self._set_playing("")
