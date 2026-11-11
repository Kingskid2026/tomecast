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
from collections.abc import Callable, Sequence
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
    write_mp3s,
)
from tomecast.textprep import TextPrep

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

    :func:`convert_book` checks it before each chunk and once more just before packaging:
    while paused it waits, and once cancelled it raises :class:`ConversionCancelled`. A chunk
    already being voiced finishes. Packaging itself can't be stopped: once it has begun
    (:meth:`commit`), :meth:`cancel` and :meth:`pause` do nothing and say so.
    """

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._committed = False

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def committed(self) -> bool:
        """True once packaging has begun, which can no longer be paused or cancelled."""
        return self._committed

    def pause(self) -> bool:
        """Pause before the next chunk. Returns False (doing nothing) once packaging began."""
        with self._lock:
            if self._committed:
                return False
            self._running.clear()
            return True

    def resume(self) -> None:
        self._running.set()

    def cancel(self) -> bool:
        """Stop the conversion. Returns False (doing nothing) once packaging began, because
        the audiobook is then written regardless."""
        with self._lock:
            if self._committed:
                return False
            self._cancelled.set()
            self._running.set()  # wake a paused conversion so it can stop
            return True

    def commit(self) -> None:
        """Called by :func:`convert_book` right after its last checkpoint, as packaging
        begins: from here on the book is written, so cancelling is refused rather than
        reported as a stop that never happened. Raises if cancelled in the meantime."""
        with self._lock:
            if self._cancelled.is_set():
                raise ConversionCancelled
            self._committed = True

    def checkpoint(self) -> None:
        """Wait while paused; raise :class:`ConversionCancelled` once cancelled."""
        self._running.wait()
        if self._cancelled.is_set():
            raise ConversionCancelled


@dataclass(frozen=True, slots=True)
class ConversionProgress:
    """Passed to the progress callback: once at the start, after every chunk, and once
    more when packaging begins. ``chapter_index`` counts the chapters being voiced (the
    selected ones), not the whole book."""

    chunks_done: int
    chunks_total: int
    chapter_index: int
    chapter_count: int
    now_speaking: str = ""  # the text being voiced next ("" once all of it is voiced)
    audio_seconds: float = 0.0  # length of the audio made so far
    stage: Literal["voicing", "packaging"] = "voicing"
    # Of ``chunks_done``, how many an earlier run had already made (reused, so not voiced
    # now): a speed or time left measured on this run leaves them out.
    chunks_reused: int = 0


ProgressCallback = Callable[[ConversionProgress], None]


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """What :func:`convert_book` made: the chapters, and anything the user should know."""

    chapters: list[PackagedChapter]
    reused_chunks: int
    warnings: tuple[str, ...]
    output: Path  # the audiobook file, or the MP3 folder (not ``output`` if that name was taken)


def select_chapters(book: Book, chapters: Sequence[int] | None) -> list[int]:
    """The indices into ``book.chapters`` to voice, in book order (``None`` means all)."""
    if chapters is None:
        return list(range(len(book.chapters)))
    bad = [i for i in chapters if not 0 <= i < len(book.chapters)]
    if bad:
        raise ValueError(f"no such chapter index {bad[0]} (the book has {len(book.chapters)})")
    return sorted(set(chapters))


def plan_chunks(
    book: Book,
    max_chars: int,
    chapters: Sequence[int] | None = None,
    prep: TextPrep | None = None,
) -> list[list[Chunk]]:
    """Chunk the chapters of ``book`` (title first), one list per chapter.

    ``chapters`` picks which ones, by index into ``book.chapters`` (``None``: all). Chunk
    IDs always carry the chapter's index in the *whole* book, so the same chapter has the
    same chunk IDs (and so reusable audio) however the others are ticked.

    This is the one place text prep happens: every paragraph goes through ``prep`` (numbers,
    abbreviations, pronunciation fixes) just before chunking. Without a ``prep``, the book's
    own language rules are used and no pronunciation lists.
    """
    prep = prep or TextPrep.for_language(book.language)
    return [
        chunk_paragraphs(index, prep.chapter(book.chapters[index]), max_chars)
        for index in select_chapters(book, chapters)
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
    chapters: Sequence[int] | None = None,
    overwrite: bool = True,
    format: Literal["m4b", "mp3"] = "m4b",
    prep: TextPrep | None = None,
) -> ConversionResult:
    """Voice ``book`` with ``engine`` and write an M4B audiobook to ``output``.

    Chunk audio is written to ``workdir``. Running again with the same ``workdir`` after an
    interruption reuses the chunks that were already voiced. ``control`` lets another thread
    pause, resume or cancel the conversion between chunks (and just before packaging).

    ``chapters`` are the indices into ``book.chapters`` to voice (``None``: all). Chunk IDs
    keep each chapter's index in the whole book, so a run with some chapters unticked reuses
    the audio of a run with all of them, and the audiobook holds only the chosen chapters,
    in book order.

    ``prep`` is the text prep (see :func:`plan_chunks`); the default is the book's language
    rules without pronunciation lists.

    ``overwrite=False`` never replaces an existing file: if ``output`` is taken, even by a
    file that appeared while voicing, the book is written to "name (2).m4b" and so on. The
    path used is in the result.

    ``format="mp3"`` writes a folder of MP3 files, one per chapter, at ``output`` (which is
    then the folder's name) instead of an M4B file. Everything else is the same.
    """
    warnings: list[str] = []
    cover = book.cover
    if cover is not None and not cover_is_usable(cover):
        warnings.append("The book's cover image is damaged, so the audiobook has no cover.")
        cover = None

    voice_speed = getattr(engine, "voice_speed", 1.0)  # test fakes may not have one
    selected = select_chapters(book, chapters)
    plan = plan_chunks(book, engine.max_chars, selected, prep)
    total = sum(len(chunks) for chunks in plan)
    chunk_dir = workdir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    queue = [chunk for chunks in plan for chunk in chunks]
    chapter_count = len(selected)

    def report(done: int, chapter_index: int, seconds: float, stage: str = "voicing") -> None:
        if progress:
            upcoming = queue[done].text if done < total else ""
            progress(
                ConversionProgress(
                    done, total, chapter_index, chapter_count, upcoming, seconds,
                    "packaging" if stage == "packaging" else "voicing", reused,
                )
            )  # fmt: skip

    audio: list[ChapterAudio] = []
    done = reused = 0
    audio_seconds = 0.0
    report(0, next((i for i, chunks in enumerate(plan) if chunks), 0), 0.0)
    for chapter_index, (book_index, chunks) in enumerate(zip(selected, plan, strict=True)):
        chapter = book.chapters[book_index]
        if not chunks:
            continue
        segments: list[Path] = []
        gaps: list[float] = []
        for position, chunk in enumerate(chunks):
            if control:
                control.checkpoint()
            wav = chunk_dir / _chunk_file_name(chunk)
            if _is_reusable(wav, chunk, voice_speed):
                reused += 1
            elif chunk.speakable:
                _voice_chunk(engine, chunk, wav, retries, voice_speed)
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
        audio.append(ChapterAudio(chapter.title, tuple(segments), tuple(gaps)))

    # The last chance to stop: once packaging starts the book is written, so a cancel that
    # comes later is refused (see ConversionControl.commit) instead of being reported as a stop.
    if control:
        control.checkpoint()
        control.commit()
    report(done, max(0, chapter_count - 1), audio_seconds, stage="packaging")

    info = AudiobookInfo(
        title=book.title,
        authors=book.authors,
        narrator=engine.label,
        language=book.language,
        cover=cover,
    )
    tempo = getattr(engine, "tempo", 1.0)
    write = write_mp3s if format == "mp3" else write_m4b
    packaged = write(audio, info, output, workdir=workdir, overwrite=overwrite, tempo=tempo)
    return ConversionResult(packaged.chapters, reused, tuple(warnings), packaged.output)


def _chunk_file_name(chunk: Chunk) -> str:
    """The chunk's ID plus a hash of its text, so stale audio is never reused for new text."""
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()[:12]
    return f"{chunk.id}-{digest}.wav"


def _is_reusable(wav: Path, chunk: Chunk, speed: float = 1.0) -> bool:
    """True if a previous run already made good audio for exactly this chunk."""
    if not wav.is_file():
        return False
    if not chunk.speakable:
        return True
    return not audio_problem(wav, chunk.text, speed)


def _gap_after(chunk: Chunk, *, is_title: bool, is_last: bool) -> float:
    if is_last:
        return GAP_AFTER_CHAPTER
    if is_title and chunk.paragraph == 0 and chunk.boundary is Boundary.PARAGRAPH:
        return GAP_AFTER_TITLE
    return GAP_AFTER[chunk.boundary]


def _voice_chunk(
    engine: VoiceEngine, chunk: Chunk, wav: Path, retries: int, speed: float = 1.0
) -> None:
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
        problem = audio_problem(partial, chunk.text, speed)
        if not problem:
            partial.replace(wav)
            return
    partial.unlink(missing_ok=True)
    raise MissingAudioError(f"no usable audio for {chunk.id} ({chunk.text[:80]!r}): {problem}")


def audio_problem(wav: Path, text: str, speed: float = 1.0) -> str:
    """Describe what's wrong with ``wav`` as audio for ``text``, or return "" if it's fine.

    ``speed`` is how fast the voice was asked to speak: faster narration is allowed to be
    proportionally shorter. Slower than 1.0 gets no extra allowance, so the check never
    weakens below its strength at normal speed."""
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
    shortest = len(text) / MAX_CHARS_PER_SECOND / max(1.0, speed)
    if info.seconds < shortest:
        return f"only {info.seconds:.2f}s of audio for {len(text)} characters (text was cut off?)"
    if peak_level(wav) < SILENCE_LEVEL:
        return "the audio is silent"
    return ""
