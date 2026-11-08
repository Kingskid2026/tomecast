"""ConversionWorker: reads books, voices previews and converts them on one long-lived thread.

The window moves one :class:`ConversionWorker` to a ``QThread`` that it starts once and keeps
for its whole life. Jobs arrive through a queued signal and run one after another; results go
back only through signals, so the window never touches the worker's objects.

The voice engine is started *and* stopped on that thread. That matters: the engine is told to
stop when the thread that started it exits (see ``_stop_with_parent`` in
:mod:`tomecast.engines.audiocpp`), so it must be started from a thread that outlives it.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from PySide6.QtCore import QObject, QThread, Signal, Slot

from tomecast.audio import concatenate_wavs, wav_info
from tomecast.book import Book
from tomecast.chunking import Chunk, CoverageError, chunk_paragraphs
from tomecast.convert import (
    ConversionCancelled,
    ConversionControl,
    ConversionProgress,
    MissingAudioError,
    VoiceEngine,
    audio_problem,
    convert_book,
    plan_chunks,
)
from tomecast.downloads import (
    Asset,
    DownloadError,
    ProgressCallback,
    SelfCheck,
    build_in_use,
    is_usable,
    prepare,
)
from tomecast.engines.audiocpp import (
    AUDIOCPP_VULKAN,
    EXECUTABLE,
    AutoKokoro,
    EngineError,
    self_check,
)
from tomecast.gui.fixes import FixLists, ListFile
from tomecast.hardware import list_devices, pick_device
from tomecast.names import Name, find_book_names
from tomecast.packaging import PackagingError, stretch_wav
from tomecast.pronunciations import (
    Pronunciations,
    book_list_path,
    global_list_path,
    load_for_book,
)
from tomecast.readers import InvalidEbookError, read_book
from tomecast.speed import Speeds, probe
from tomecast.textprep import TextPrep
from tomecast.workdirs import WorkFolderBusy, exclusive, work_folder

# Speed and time left are shown once this many chunks have been voiced in the current run.
MIN_VOICED_FOR_ESTIMATES = 5


class Engine(VoiceEngine, Protocol):
    """A voice engine the window can start, stop, and point at another voice or speed.

    audio.cpp takes the voice and speed with every request, so one running server can voice
    previews in any voice without reloading the model.
    """

    voice: str
    speed: float
    device_label: str  # after start(): what is in use, "CPU" or the card's short name
    gpu_description: str  # after start(): the card's full description, or "" on the CPU
    warning: str | None  # after start(): why the CPU is used when the card was wanted

    def start(self) -> None: ...

    def close(self) -> None: ...


# (data folder, voice, speed, use the graphics card if possible)
EngineFactory = Callable[[Path, str, float, bool], Engine]
# (use the graphics card) -> the files that choice needs installed
AssetsFor = Callable[[bool], Sequence[Asset]]
# (data folder) -> the graphics card's description if one would be used, else ""
DeviceScanner = Callable[[Path], str]
# (data folder, look at the graphics card too) -> the speeds measured (or remembered) there
SpeedProbe = Callable[[Path, bool], Speeds]
# (data folder, use the graphics card) -> the check a new engine build must pass
SelfCheckFor = Callable[[Path, bool], SelfCheck]


def kokoro_engine(root: Path, voice: str, speed: float, use_gpu: bool) -> Engine:
    """The real engine: Kokoro through audio.cpp, with its files under ``root``. It uses the
    graphics card when asked to and able, and falls back to the CPU."""
    return AutoKokoro(root, voice=voice, speed=speed, use_gpu=use_gpu)


def scan_gpu(root: Path) -> str:
    """The description of the graphics card that would be used, or ``""``. Only looks if a
    graphics-card engine build is installed; it never downloads."""
    build = build_in_use(AUDIOCPP_VULKAN, root)
    if build is None:
        return ""
    device = pick_device(list_devices(build / EXECUTABLE))
    return device.description if device else ""


def probe_speeds(root: Path, gpu: bool) -> Speeds:
    """The real speed probe: the CPU's speed, and the card's with ``gpu``, measured once per
    engine build and device (a few seconds each) and remembered after that."""
    return probe(root, gpu=gpu)


class TimedControl(ConversionControl):
    """A :class:`ConversionControl` that also counts the time the conversion spent paused,
    so speed and time-left estimates leave pauses out."""

    def __init__(self) -> None:
        super().__init__()
        self._paused_seconds = 0.0
        self._holding = threading.Event()

    @property
    def paused_seconds(self) -> float:
        return self._paused_seconds

    @property
    def holding(self) -> bool:
        """True while the conversion is waiting at a checkpoint because it's paused."""
        return self._holding.is_set()

    def checkpoint(self) -> None:
        if self.paused:
            self._holding.set()
        started = time.monotonic()
        try:
            super().checkpoint()
        finally:
            self._holding.clear()
            self._paused_seconds += time.monotonic() - started


# --- Jobs (window → worker) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadJob:
    job: int
    path: Path


@dataclass(frozen=True, slots=True)
class ScanJob:
    """Look for the graphics card (when its engine is installed) to show its name, and find
    out how fast this computer voices (remembered, or measured once with what's installed)."""

    job: int
    gpu: bool = False  # a graphics driver is there, so look at the card too


@dataclass(frozen=True, slots=True)
class PreviewJob:
    job: int
    text: str
    voice: str
    speed: float
    output: Path  # where the finished WAV goes (it's cached there)
    use_gpu: bool = False


@dataclass(frozen=True, slots=True)
class ConvertJob:
    job: int
    book: Book  # the whole book, so chunk IDs match a run from the command line
    chapters: tuple[int, ...]  # indices into book.chapters to convert
    book_path: Path  # the book file, which keys the resume folder
    output: Path  # the wanted name; the book may land at "name (2).m4b" if it's taken
    voice: str
    speed: float
    control: TimedControl
    use_gpu: bool = False
    format: Literal["m4b", "mp3"] = "m4b"  # "mp3": ``output`` is a folder of MP3 files
    # The pronunciation fixes to use. None: read the lists on the worker (load_for_book), as
    # the command line does. The window passes its own only when a change couldn't be saved.
    fixes: Pronunciations | None = None


Job = ReadJob | ScanJob | PreviewJob | ConvertJob


# --- Results (worker → window) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookRead:
    """A book as read, the names its Names screen lists, and its pronunciation lists."""

    book: Book
    names: tuple[Name, ...] = ()  # find_book_names(), most frequent first
    fixes: FixLists | None = None  # handed over to the window, which owns it from then on


@dataclass(frozen=True, slots=True)
class ConvertUpdate:
    """Progress, with the speed measured on the worker."""

    chunks_done: int
    chunks_total: int
    chapter_index: int  # into the converted book's chapters
    now_speaking: str
    stage: str  # "voicing" or "packaging"
    speedup: float | None  # audio made ÷ time taken, once measurable
    seconds_left: float | None  # once measurable


@dataclass(frozen=True, slots=True)
class ConvertResult:
    output: Path
    audio_seconds: float
    chapters: int
    size_bytes: int
    seconds_taken: float  # from the engine starting to the file being written, minus pauses
    warnings: tuple[str, ...]
    speedup: float | None


def _size_of(output: Path) -> int:
    """The size of the audiobook file, or of all the files in the MP3 folder."""
    if output.is_dir():
        return sum(p.stat().st_size for p in output.iterdir() if p.is_file())
    return output.stat().st_size


class _Meter:
    """Wraps the engine to measure how fast it voices in this run (reused chunks don't count)."""

    def __init__(self, engine: VoiceEngine, control: TimedControl, chunks: Sequence[Chunk]):
        self._engine = engine
        self._control = control
        self._chars_before = [0]
        for chunk in chunks:
            self._chars_before.append(self._chars_before[-1] + len(chunk.text))
        self.voiced = 0
        self._voiced_chars = 0
        self._audio_seconds = 0.0
        self._started: float | None = None
        self._paused_before = 0.0

    @property
    def label(self) -> str:
        return self._engine.label

    @property
    def max_chars(self) -> int:
        return self._engine.max_chars

    @property
    def sample_rate(self) -> int:
        return self._engine.sample_rate

    def synthesize(self, text: str, output: Path) -> None:
        if self._started is None:
            self._started = time.monotonic()
            self._paused_before = self._control.paused_seconds
        self._engine.synthesize(text, output)
        self.voiced += 1
        self._voiced_chars += len(text)
        with contextlib.suppress(wave.Error, EOFError, OSError):
            self._audio_seconds += wav_info(output).seconds

    def _elapsed(self) -> float:
        if self._started is None:
            return 0.0
        paused = self._control.paused_seconds - self._paused_before
        return time.monotonic() - self._started - paused

    def speedup(self) -> float | None:
        elapsed = self._elapsed()
        if self.voiced < MIN_VOICED_FOR_ESTIMATES or elapsed <= 0 or self._audio_seconds <= 0:
            return None
        return self._audio_seconds / elapsed

    def seconds_left(self, chunks_done: int) -> float | None:
        """The rest of the text's audio (estimated from the audio made per character so far),
        divided by the measured speed."""
        if self.speedup() is None or self._voiced_chars == 0:
            return None
        done = min(chunks_done, len(self._chars_before) - 1)
        remaining_chars = self._chars_before[-1] - self._chars_before[done]
        return remaining_chars * self._elapsed() / self._voiced_chars


def _as_sentence(message: str, *, capitalize: bool = True) -> str:
    """Error messages from the engine start in lower case; the banner shows sentences.

    Messages that start with a file name keep it as it is (``capitalize=False``).
    """
    message = message.strip()
    if capitalize and message and message[0].islower():
        message = message[0].upper() + message[1:]
    if message and message[-1] not in ".!?:)\n":
        message += "."
    return message


def megabytes(size: int) -> str:
    return "<1 MB" if size < 1_000_000 else f"{size / 1_000_000:,.0f} MB"


class ConversionWorker(QObject):
    """Runs jobs one at a time on its thread and reports through signals (all queued)."""

    bookRead = Signal(int, object)  # job, BookRead
    previewReady = Signal(int, str)  # job, path of the WAV to play
    stageChanged = Signal(int, str)  # job, "downloading" or "starting"
    downloadProgress = Signal(int, str)  # job, the label to show
    progressed = Signal(int, object)  # job, ConvertUpdate
    converted = Signal(int, object)  # job, ConvertResult
    cancelled = Signal(int)  # job
    failed = Signal(int, str)  # job, message for the error banner
    deviceChosen = Signal(str)  # the graphics card's description, or "" for the CPU
    speedsKnown = Signal(object)  # Speeds, from the start-up scan

    def __init__(
        self,
        *,
        engine_factory: EngineFactory,
        assets: AssetsFor,
        data_root: Path,
        system_check: Callable[[], None],
        device_scanner: DeviceScanner = scan_gpu,
        speed_probe: SpeedProbe = probe_speeds,
        engine_check: SelfCheckFor = self_check,
    ) -> None:
        super().__init__()
        self._engine_factory = engine_factory
        self._assets_for = assets
        self._root = data_root
        self._system_check = system_check
        self._device_scanner = device_scanner
        self._speed_probe = speed_probe
        self._self_check = engine_check
        self._stopping = threading.Event()
        self._assets_ready: dict[bool, bool] = {}  # per "use the graphics card": prepared
        self._assets_usable: dict[bool, bool] = {}  # some build of each is there (previews)
        self._preview_engine: Engine | None = None
        self._preview_gpu = False

    def stop(self) -> None:
        """Skip every job that hasn't started yet. Safe to call from any thread."""
        self._stopping.set()

    @Slot(object)
    def run(self, job: Job) -> None:
        """Run one job (called through a queued connection, so on the worker's thread)."""
        if self._stopping.is_set():
            return
        try:
            if isinstance(job, ReadJob):
                self._read(job)
            elif isinstance(job, ScanJob):
                self._scan(job)
            elif isinstance(job, PreviewJob):
                self._preview(job)
            else:
                self._convert(job)
        except Exception as error:  # the worker must outlive any bug in a job
            self.failed.emit(job.job, f"Something went wrong: {error}")

    @Slot()
    def shutdown(self) -> None:
        """Stop the preview engine and end the worker's thread (on that thread)."""
        self._close_preview_engine()
        thread = QThread.currentThread()
        if thread is not None:
            thread.quit()

    # --- Start-up scan --------------------------------------------------------------------

    def _scan(self, job: ScanJob) -> None:
        if job.gpu:
            self.deviceChosen.emit(self._device_scanner(self._root))
        self._close_preview_engine()  # one model in memory at a time
        self.speedsKnown.emit(self._speed_probe(self._root, job.gpu))

    # --- Reading a book ------------------------------------------------------------------

    def _read(self, job: ReadJob) -> None:
        try:
            book = read_book(job.path)
        except InvalidEbookError as error:
            self.failed.emit(job.job, _as_sentence(str(error), capitalize=False))
            return
        except OSError as error:
            self.failed.emit(job.job, f"Couldn't open {job.path.name}: {error.strerror or error}.")
            return
        # The names (as `tomecast names` lists them, without leaving out the ones that have a
        # fix, since the Names screen shows and edits those) and the two lists, read here so
        # the window never waits on the disk.
        fixes = FixLists(
            ListFile.read(book_list_path(job.path)), ListFile.read(global_list_path(self._root))
        )
        self.bookRead.emit(job.job, BookRead(book, tuple(find_book_names(book)), fixes))

    # --- Voice preview -------------------------------------------------------------------

    def _preview(self, job: PreviewJob) -> None:
        try:
            self._system_check()
            # Use the graphics card's engine for the preview only if it's installed already
            # (previews never download); otherwise the CPU engine will do.
            use_gpu = job.use_gpu and self._have_usable(True)
            if not use_gpu and not self._have_usable(False):
                self.failed.emit(
                    job.job,
                    "The voice model downloads once, when you start converting a book. "
                    "Previews work after that.",
                )
                return
            engine = self._preview_engine_for(job.voice, job.speed, use_gpu)
            self._voice_text(engine, job.text, job.output)
        except (
            EngineError,
            MissingAudioError,
            CoverageError,
            PackagingError,
            OSError,
            wave.Error,
        ) as error:
            self._close_preview_engine()  # start afresh next time
            self.failed.emit(job.job, _as_sentence(f"The preview failed: {error}"))
            return
        self.previewReady.emit(job.job, str(job.output))

    def _preview_engine_for(self, voice: str, speed: float, use_gpu: bool) -> Engine:
        if self._preview_engine is not None and use_gpu != self._preview_gpu:
            self._close_preview_engine()  # the choice changed: start on the other hardware
        if self._preview_engine is None:
            engine = self._engine_factory(self._root, voice, speed, use_gpu)
            engine.start()
            self._preview_engine = engine
            self._preview_gpu = use_gpu
            self.deviceChosen.emit(engine.gpu_description)
        self._preview_engine.voice = voice
        self._preview_engine.speed = speed
        return self._preview_engine

    def _voice_text(self, engine: VoiceEngine, text: str, output: Path) -> None:
        """Voice ``text`` in engine-sized chunks (never all at once) and join them."""
        chunks = [c for c in chunk_paragraphs(0, (text,), engine.max_chars) if c.speakable]
        if not chunks:
            raise MissingAudioError("there's no text to preview")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".preview-") as tmp:
            parts = []
            for index, chunk in enumerate(chunks):
                part = Path(tmp) / f"{index}.wav"
                engine.synthesize(chunk.text, part)
                if problem := audio_problem(part, chunk.text, getattr(engine, "voice_speed", 1.0)):
                    raise MissingAudioError(problem)
                parts.append(part)
            joined = Path(tmp) / "preview.wav"
            concatenate_wavs(parts, joined, gap_seconds=0.3)
            tempo = getattr(engine, "tempo", 1.0)
            if tempo != 1.0:  # the same time-stretch the audiobook gets
                stretched = Path(tmp) / "stretched.wav"
                stretch_wav(joined, stretched, tempo)
                joined = stretched
            joined.replace(output)

    def _close_preview_engine(self) -> None:
        if self._preview_engine is not None:
            engine, self._preview_engine = self._preview_engine, None
            with contextlib.suppress(Exception):
                engine.close()

    # --- Converting ----------------------------------------------------------------------

    def _convert(self, job: ConvertJob) -> None:
        workdir: Path | None = None
        try:
            self._close_preview_engine()  # one model in memory at a time
            self._system_check()
            engine = self._engine_factory(self._root, job.voice, job.speed, job.use_gpu)
            fixes, notes = (
                (job.fixes, [])
                if job.fixes is not None
                else load_for_book(self._root, job.book_path)
            )
            prep = TextPrep.for_language(job.book.language, fixes)
            plan = plan_chunks(job.book, engine.max_chars, job.chapters, prep)
            chunks = [chunk for chapter in plan for chunk in chapter]
            first_chapter = next((position for position, c in enumerate(plan) if c), 0)
            self.progressed.emit(
                job.job, ConvertUpdate(0, len(chunks), first_chapter, "", "starting", None, None)
            )
            install_notes = self._prepare(job)
            if job.control.cancelled:
                raise ConversionCancelled
            workdir = work_folder(
                self._root, job.book_path, job.voice, job.speed, use_gpu=job.use_gpu
            )
            with exclusive(workdir):
                self.stageChanged.emit(job.job, "starting")
                outcome = self._voice_book(job, engine, chunks, workdir, prep, notes, install_notes)
            shutil.rmtree(workdir, ignore_errors=True)
        except ConversionCancelled:
            self.cancelled.emit(job.job)
            return
        except (
            EngineError, MissingAudioError, PackagingError, CoverageError, DownloadError,
            WorkFolderBusy, OSError, wave.Error,
        ) as error:  # fmt: skip
            message = _as_sentence(str(error))
            if workdir is not None and any((workdir / "chunks").glob("*.wav")):
                message += " The audio made so far is kept, so starting again continues from there."
            self.failed.emit(job.job, message)
            return
        self.converted.emit(job.job, outcome)

    def _voice_book(
        self,
        job: ConvertJob,
        engine: Engine,
        chunks: Sequence[Chunk],
        workdir: Path,
        prep: TextPrep,
        notes: Sequence[str] = (),
        install_notes: Sequence[str] = (),
    ) -> ConvertResult:
        """Start the engine, convert, and stop the engine again (all on this thread).

        ``notes`` are warnings about pronunciation list lines that were skipped;
        ``install_notes`` say an engine update was kept back.
        """
        control = job.control
        try:
            engine.start()
            self.deviceChosen.emit(engine.gpu_description)
            started = time.monotonic()
            paused_before = control.paused_seconds
            meter = _Meter(engine, control, chunks)

            def report(progress: ConversionProgress) -> None:
                self._report(job.job, meter, progress)

            result = convert_book(
                job.book, meter, job.output, workdir=workdir, progress=report, control=control,
                chapters=job.chapters, overwrite=False, format=job.format, prep=prep,
            )  # fmt: skip
            taken = time.monotonic() - started - (control.paused_seconds - paused_before)
        finally:
            engine.close()
        return ConvertResult(
            output=result.output,  # not job.output if that name was taken meanwhile
            audio_seconds=result.chapters[-1].end_seconds if result.chapters else 0.0,
            chapters=len(result.chapters),
            size_bytes=_size_of(result.output),
            seconds_taken=max(0.0, taken),
            warnings=(
                *install_notes,
                *((engine.warning,) if engine.warning else ()),
                *result.warnings,
                *(_as_sentence(f"Pronunciation list: {note}") for note in notes),
            ),
            speedup=meter.speedup(),
        )

    def _report(self, job: int, meter: _Meter, progress: ConversionProgress) -> None:
        voicing = progress.stage == "voicing"
        self.progressed.emit(
            job,
            ConvertUpdate(
                chunks_done=progress.chunks_done,
                chunks_total=progress.chunks_total,
                chapter_index=progress.chapter_index,
                now_speaking=progress.now_speaking,
                stage=progress.stage,
                speedup=meter.speedup(),
                seconds_left=meter.seconds_left(progress.chunks_done) if voicing else None,
            ),
        )

    def _have_usable(self, use_gpu: bool) -> bool:
        """Some build of every file the choice needs is installed (previews never download,
        and use an older engine build while an update waits)."""
        if not self._assets_usable.get(use_gpu):
            self._assets_usable[use_gpu] = all(
                is_usable(asset, self._root) for asset in self._assets_for(use_gpu)
            )
        return self._assets_usable[use_gpu]

    def _prepare(self, job: ConvertJob) -> tuple[str, ...]:
        """Download what's missing and switch to new engine builds once they pass their
        self-check (:func:`tomecast.downloads.prepare`). Pressing Start counts as agreeing to
        the downloads, so the label says whether it's a one-time download or an update.
        Returns the notes to show on the done screen (an update kept back)."""
        if self._assets_ready.get(job.use_gpu):
            return ()
        kind = "DOWNLOADING ONCE"

        def consent(missing: Sequence[Asset], updates: bool) -> bool:
            nonlocal kind
            kind = "DOWNLOADING AN UPDATE" if updates else "DOWNLOADING ONCE"
            return True

        def on_download(asset: Asset, number: int, count: int) -> ProgressCallback:
            self.stageChanged.emit(job.job, "downloading")
            step = f" ({number} OF {count})" if count > 1 else ""
            show = self._download_reporter(job, f"{kind}{step} · {asset.name.upper()}")
            show(0, asset.size)
            return show

        def on_check() -> None:
            self.stageChanged.emit(job.job, "starting")

        prepared = prepare(
            self._assets_for(job.use_gpu),
            self._root,
            check=self._self_check(self._root, job.use_gpu),
            consent=consent,
            on_download=on_download,
            on_check=on_check,
        )
        self._assets_ready[job.use_gpu] = True
        self._assets_usable[job.use_gpu] = True
        return prepared.notes

    def _download_reporter(self, job: ConvertJob, label: str) -> Callable[[int, int], None]:
        shown = -1

        def show(done: int, total: int) -> None:
            nonlocal shown
            if job.control.cancelled:
                raise ConversionCancelled  # stops the download; nothing half-done is kept
            percent = min(100, 100 * done // total) if total else 100
            if percent != shown:
                shown = percent
                self.downloadProgress.emit(job.job, f"{label} · {percent}% OF {megabytes(total)}")

        return show


def preview_file(cache: Path, voice: str, speed: float, text: str) -> Path:
    """Where the preview for this voice, speed and text is cached."""
    key = hashlib.sha256(f"{voice}|{speed:.2f}|{text}".encode()).hexdigest()[:24]
    return cache / f"{key}.wav"
