"""Turn a :class:`~tomecast.book.Book` into an audiobook file.

This is the second half of the "every sentence gets spoken" guarantee (the first is
:mod:`tomecast.chunking`): every chunk must come back from the voice engine with real audio of
a believable length, or it's retried, and if it still fails, conversion stops with an error
naming the exact text. A book is never packaged with a hole in it.
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tomecast.audio import peak_level, wav_info
from tomecast.book import Book
from tomecast.chunking import Boundary, Chunk, chunk_paragraphs
from tomecast.packaging import AudiobookInfo, ChapterAudio, PackagedChapter, write_m4b

# Seconds of silence after each kind of chunk.
GAP_AFTER = {Boundary.PART: 0.1, Boundary.SENTENCE: 0.3, Boundary.PARAGRAPH: 0.6}
GAP_AFTER_TITLE = 1.0
GAP_AFTER_CHAPTER = 1.5

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

    def synthesize(self, text: str, output: Path) -> None:
        """Write ``text`` as speech to ``output`` (mono, 16-bit PCM WAV)."""
        ...


class MissingAudioError(RuntimeError):
    """Raised when a chunk still has no usable audio after retrying."""


@dataclass(frozen=True, slots=True)
class ConversionProgress:
    """Passed to the progress callback after each chunk is voiced."""

    chunks_done: int
    chunks_total: int
    chapter_index: int
    chapter_count: int


ProgressCallback = Callable[[ConversionProgress], None]


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
    retries: int = 1,
) -> list[PackagedChapter]:
    """Voice ``book`` with ``engine`` and write an M4B audiobook to ``output``.

    Chunk audio is written to ``workdir``, which should be an empty folder.
    """
    plan = plan_chunks(book, engine.max_chars)
    total = sum(len(chunks) for chunks in plan)
    chunk_dir = workdir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    chapters: list[ChapterAudio] = []
    done = 0
    for chapter_index, (chapter, chunks) in enumerate(zip(book.chapters, plan, strict=True)):
        if not chunks:
            continue
        segments: list[Path] = []
        gaps: list[float] = []
        for position, chunk in enumerate(chunks):
            wav = chunk_dir / f"{chunk.id}.wav"
            _voice_chunk(engine, chunk, wav, retries)
            segments.append(wav)
            is_last = position == len(chunks) - 1
            gaps.append(_gap_after(chunk, is_title=bool(chapter.title), is_last=is_last))
            done += 1
            if progress:
                progress(ConversionProgress(done, total, chapter_index, len(book.chapters)))
        chapters.append(ChapterAudio(chapter.title, tuple(segments), tuple(gaps)))

    info = AudiobookInfo(
        title=book.title,
        authors=book.authors,
        narrator=engine.label,
        language=book.language,
        cover=book.cover,
    )
    return write_m4b(chapters, info, output, workdir=workdir)


def _gap_after(chunk: Chunk, *, is_title: bool, is_last: bool) -> float:
    if is_last:
        return GAP_AFTER_CHAPTER
    if is_title and chunk.paragraph == 0 and chunk.boundary is Boundary.PARAGRAPH:
        return GAP_AFTER_TITLE
    return GAP_AFTER[chunk.boundary]


def _voice_chunk(engine: VoiceEngine, chunk: Chunk, wav: Path, retries: int) -> None:
    problem = ""
    for _attempt in range(retries + 1):
        wav.unlink(missing_ok=True)
        try:
            engine.synthesize(chunk.text, wav)
        except RuntimeError as error:
            problem = str(error)
            continue
        problem = audio_problem(wav, chunk.text)
        if not problem:
            return
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
