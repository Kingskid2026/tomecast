"""Turn a :class:`~tomecast.book.Book` into an audiobook file.

This is the second half of the "every sentence gets spoken" guarantee (the first is
:mod:`tomecast.chunking`): every chunk must come back from the voice engine with real audio of
a believable length, or it's retried, and if it still fails, conversion stops with an error
naming the exact text. A book is never packaged with a hole in it.

Chunk audio is kept in a work folder under a name that includes a hash of the chunk's text,
so an interrupted conversion can be resumed: audio that still matches its text and passes
the checks is reused, and anything else is voiced again.
"""

from __future__ import annotations

import hashlib
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from tomecast.audio import peak_level, wav_info, write_silence
from tomecast.book import Book
from tomecast.chunking import Boundary, Chunk, chunk_paragraphs
from tomecast.packaging import (
    AudiobookInfo,
    ChapterAudio,
    PackagedChapter,
    cover_is_usable,
    write_m4b,
)

# Seconds of silence after each kind of chunk.
GAP_AFTER = {Boundary.PART: 0.1, Boundary.SENTENCE: 0.3, Boundary.PARAGRAPH: 0.6}
GAP_AFTER_TITLE = 1.0
GAP_AFTER_CHAPTER = 1.5
# Text with no letters or digits (a ``* * *`` scene break) becomes this much silence.
SCENE_BREAK_SECONDS = 0.8

# Even very fast narration stays well under this, so shorter audio means text was cut off.
MAX_CHARS_PER_SECOND = 40.0
# Audio quieter than this (as a fraction of full scale) counts as silence.
SILENCE_LEVEL = 0.01


class VoiceEngine(Protocol):
    """Anything that can turn a short piece of text into a mono 16-bit WAV file."""

    @property
    def label(self) -> str:
        """How the narrator is credited in the audiobook, e.g. ``Kokoro (af_heart)``."""
        ...

    @property
    def max_chars(self) -> int:
        """The longest text the engine reliably voices in one call."""
        ...

    @property
    def sample_rate(self) -> int:
        """The sample rate of the WAV files the engine writes."""
        ...

    def synthesize(self, text: str, output: Path) -> None:
        """Write ``text`` as speech to ``output`` (mono, 16-bit PCM WAV)."""
        ...


class MissingAudioError(RuntimeError):
    """Raised when a chunk still has no usable audio after retrying."""


class ConversionCancelled(Exception):
    """Raised inside :func:`convert_book` when its :class:`ConversionControl` is cancelled.

    Everything voiced so far stays in the work folder, so the conversion can be resumed.
    """


class ConversionControl:
    """Pause, resume and cancel a running conversion from another thread (the window).

    :func:`convert_book` checks it before each chunk: while paused it waits, and once
    cancelled it raises :class:`ConversionCancelled`. A chunk already being voiced finishes.
    """

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._cancelled = threading.Event()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._running.set()  # wake a paused conversion so it can stop

    def checkpoint(self) -> None:
        """Wait while paused; raise :class:`ConversionCancelled` once cancelled."""
        self._running.wait()
        if self._cancelled.is_set():
            raise ConversionCancelled


@dataclass(frozen=True, slots=True)
class ConversionProgress:
    """Passed to the progress callback: once at the start, after every chunk, and once
    more when packaging begins."""

    chunks_done: int
    chunks_total: int
    chapter_index: int
    chapter_count: int
    now_speaking: str = ""  # the text being voiced next ("" once all of it is voiced)
    audio_seconds: float = 0.0  # length of the audio made so far
    stage: Literal["voicing", "packaging"] = "voicing"


ProgressCallback = Callable[[ConversionProgress], None]


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """What :func:`convert_book` made: the chapters, and anything the user should know."""

    chapters: list[PackagedChapter]
    reused_chunks: int
    warnings: tuple[str, ...]


def plan_chunks(book: Book, max_chars: int) -> list[list[Chunk]]:
    """Chunk every chapter of ``book`` (title first), one list per chapter."""
    return [
        chunk_paragraphs(index, chapter.spoken_paragraphs(), max_chars)
        for index, chapter in enumerate(book.chapters)
    ]


def convert_book(
    book: Book,
    engine: VoiceEngine,
    output: Path,
    *,
    workdir: Path,
    progress: ProgressCallback | None = None,
    control: ConversionControl | None = None,
    retries: int = 1,
) -> ConversionResult:
    """Voice ``book`` with ``engine`` and write an M4B audiobook to ``output``.

    Chunk audio is written to ``workdir``. Running again with the same ``workdir`` after an
    interruption reuses the chunks that were already voiced. ``control`` lets another thread
    pause, resume or cancel the conversion between chunks.
    """
    warnings: list[str] = []
    cover = book.cover
    if cover is not None and not cover_is_usable(cover):
        warnings.append("The book's cover image is damaged, so the audiobook has no cover.")
        cover = None

    plan = plan_chunks(book, engine.max_chars)
    total = sum(len(chunks) for chunks in plan)
    chunk_dir = workdir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    queue = [chunk for chunks in plan for chunk in chunks]
    chapter_count = len(book.chapters)

    def report(done: int, chapter_index: int, seconds: float, stage: str = "voicing") -> None:
        if progress:
            upcoming = queue[done].text if done < total else ""
            progress(
                ConversionProgress(
                    done, total, chapter_index, chapter_count, upcoming, seconds,
                    "packaging" if stage == "packaging" else "voicing",
                )
            )  # fmt: skip

    chapters: list[ChapterAudio] = []
    done = reused = 0
    audio_seconds = 0.0
    report(0, next((i for i, chunks in enumerate(plan) if chunks), 0), 0.0)
    for chapter_index, (chapter, chunks) in enumerate(zip(book.chapters, plan, strict=True)):
        if not chunks:
            continue
        segments: list[Path] = []
        gaps: list[float] = []
        for position, chunk in enumerate(chunks):
            if control:
                control.checkpoint()
            wav = chunk_dir / _chunk_file_name(chunk)
            if _is_reusable(wav, chunk):
                reused += 1
            elif chunk.speakable:
                _voice_chunk(engine, chunk, wav, retries)
            else:
                partial = wav.with_name(wav.name + ".partial")
                write_silence(partial, SCENE_BREAK_SECONDS, engine.sample_rate)
                partial.replace(wav)
            segments.append(wav)
            is_last = position == len(chunks) - 1
            gaps.append(_gap_after(chunk, is_title=bool(chapter.title), is_last=is_last))
            done += 1
            audio_seconds += wav_info(wav).seconds
            report(done, chapter_index, audio_seconds)
        chapters.append(ChapterAudio(chapter.title, tuple(segments), tuple(gaps)))

    report(done, max(0, chapter_count - 1), audio_seconds, stage="packaging")

    info = AudiobookInfo(
        title=book.title,
        authors=book.authors,
        narrator=engine.label,
        language=book.language,
        cover=cover,
    )
    packaged = write_m4b(chapters, info, output, workdir=workdir)
    return ConversionResult(packaged, reused, tuple(warnings))


def _chunk_file_name(chunk: Chunk) -> str:
    """The chunk's ID plus a hash of its text, so stale audio is never reused for new text."""
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()[:12]
    return f"{chunk.id}-{digest}.wav"


def _is_reusable(wav: Path, chunk: Chunk) -> bool:
    """True if a previous run already made good audio for exactly this chunk."""
    if not wav.is_file():
        return False
    if not chunk.speakable:
        return True
    return not audio_problem(wav, chunk.text)


def _gap_after(chunk: Chunk, *, is_title: bool, is_last: bool) -> float:
    if is_last:
        return GAP_AFTER_CHAPTER
    if is_title and chunk.paragraph == 0 and chunk.boundary is Boundary.PARAGRAPH:
        return GAP_AFTER_TITLE
    return GAP_AFTER[chunk.boundary]


def _voice_chunk(engine: VoiceEngine, chunk: Chunk, wav: Path, retries: int) -> None:
    """Voice ``chunk`` into ``wav``. The file only appears once its audio passed the checks."""
    partial = wav.with_name(wav.name + ".partial")
    problem = ""
    for _attempt in range(retries + 1):
        partial.unlink(missing_ok=True)
        try:
            engine.synthesize(chunk.text, partial)
        except RuntimeError as error:
            problem = str(error)
            continue
        problem = audio_problem(partial, chunk.text)
        if not problem:
            partial.replace(wav)
            return
    partial.unlink(missing_ok=True)
    raise MissingAudioError(f"no usable audio for {chunk.id} ({chunk.text[:80]!r}): {problem}")


def audio_problem(wav: Path, text: str) -> str:
    """Describe what's wrong with ``wav`` as audio for ``text``, or return "" if it's fine."""
    if not wav.is_file():
        return "the engine wrote no file"
    try:
        info = wav_info(wav)
    except (wave.Error, EOFError, OSError) as error:
        return f"not a valid WAV file ({error})"
    if info.channels != 1 or info.sample_width != 2:
        return "expected mono 16-bit audio"
    if info.frames == 0:
        return "the audio is empty"
    shortest = len(text) / MAX_CHARS_PER_SECOND
    if info.seconds < shortest:
        return f"only {info.seconds:.2f}s of audio for {len(text)} characters (text was cut off?)"
    if peak_level(wav) < SILENCE_LEVEL:
        return "the audio is silent"
    return ""
