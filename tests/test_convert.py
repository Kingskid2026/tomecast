import array
import math
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]
import pytest

from tomecast.audio import write_silence
from tomecast.book import Book, Chapter, Cover
from tomecast.chunking import normalize_whitespace
from tomecast.convert import MissingAudioError, audio_problem, convert_book

SAMPLE_RATE = 24000
CHARS_PER_SECOND = 15.0


def _write_speech_like(path: Path, text: str, chars_per_second: float = CHARS_PER_SECOND) -> None:
    """Stand-in for real speech: a tone whose length depends on the text, like a voice."""
    frames = round(len(text) / chars_per_second * SAMPLE_RATE)
    samples = array.array(
        "h", (round(8000 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)) for i in range(frames))
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())


class FakeEngine:
    """Records every text it's asked to voice."""

    label = "Fake voice"
    max_chars = 60
    sample_rate = SAMPLE_RATE

    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str, output: Path) -> None:
        self.texts.append(text)
        _write_speech_like(output, text)


class SilentEngine(FakeEngine):
    def synthesize(self, text: str, output: Path) -> None:
        self.texts.append(text)
        write_silence(output, seconds=2.0, sample_rate=SAMPLE_RATE)


class TruncatingEngine(FakeEngine):
    """Like a model that stops early: long text gets far too little audio."""

    def synthesize(self, text: str, output: Path) -> None:
        self.texts.append(text)
        _write_speech_like(output, text[:5])


class FlakyEngine(FakeEngine):
    """Fails on the first attempt at every chunk, then works."""

    def __init__(self) -> None:
        super().__init__()
        self.failed: set[str] = set()

    def synthesize(self, text: str, output: Path) -> None:
        if text not in self.failed:
            self.failed.add(text)
            raise RuntimeError("engine crashed")
        super().synthesize(text, output)


def _book() -> Book:
    return Book(
        title="A Test Book",
        authors=("Ada Writer",),
        language="en",
        chapters=(
            Chapter("Chapter One", ("It was a dark night. The wind howled.", "Then it stopped")),
            Chapter(
                "Chapter Two",
                (
                    "A much longer sentence that goes on and on, past the engine's limit, "
                    "so it has to be split into several parts before anyone voices it.",
                    "The last page ends mid-sentence and",
                ),
            ),
        ),
    )


def _chapter_titles(m4b: Path) -> list[str]:
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(m4b), "-f", "ffmetadata", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.removeprefix("title=") for line in result.stdout.splitlines()
            if line.startswith("title=")][1:]  # fmt: skip


def test_every_sentence_is_voiced_and_packaged(tmp_path: Path) -> None:
    book = _book()
    engine = FakeEngine()
    output = tmp_path / "book.m4b"

    result = convert_book(book, engine, output, workdir=tmp_path / "work")

    spoken = "".join("".join(t.split()) for t in engine.texts)
    source = "".join(
        "".join(p.split()) for chapter in book.chapters for p in chapter.spoken_paragraphs()
    )
    assert spoken == source  # every character, in order, was sent to the voice
    assert all(len(t) <= engine.max_chars for t in engine.texts)
    assert engine.texts[-1] == "The last page ends mid-sentence and"
    assert output.is_file()
    assert [c.title for c in result.chapters] == ["Chapter One", "Chapter Two"]
    assert result.reused_chunks == 0
    assert result.warnings == ()
    assert _chapter_titles(output) == ["Chapter One", "Chapter Two"]


def test_progress_is_reported_for_every_chunk(tmp_path: Path) -> None:
    seen: list[tuple[int, int]] = []
    convert_book(
        _book(),
        FakeEngine(),
        tmp_path / "book.m4b",
        workdir=tmp_path / "work",
        progress=lambda p: seen.append((p.chunks_done, p.chunks_total)),
    )
    total = seen[0][1]
    assert seen == [(i, total) for i in range(1, total + 1)]


def test_silent_audio_stops_the_conversion(tmp_path: Path) -> None:
    with pytest.raises(MissingAudioError, match="silent"):
        convert_book(_book(), SilentEngine(), tmp_path / "book.m4b", workdir=tmp_path / "w")
    assert not (tmp_path / "book.m4b").exists()


def test_cut_off_audio_stops_the_conversion(tmp_path: Path) -> None:
    with pytest.raises(MissingAudioError, match="cut off"):
        convert_book(_book(), TruncatingEngine(), tmp_path / "book.m4b", workdir=tmp_path / "w")


def test_a_failed_chunk_is_retried(tmp_path: Path) -> None:
    engine = FlakyEngine()
    convert_book(_book(), engine, tmp_path / "book.m4b", workdir=tmp_path / "w", retries=1)
    assert (tmp_path / "book.m4b").is_file()


def test_retries_can_run_out(tmp_path: Path) -> None:
    with pytest.raises(MissingAudioError, match="engine crashed"):
        convert_book(_book(), FlakyEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w", retries=0)


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        ("missing", "no file"),
        ("garbage", "not a valid WAV"),
        ("empty", "empty"),
    ],
)
def test_audio_problem_explains_bad_files(tmp_path: Path, setup: str, problem: str) -> None:
    wav = tmp_path / "x.wav"
    if setup == "garbage":
        wav.write_bytes(b"not audio")
    elif setup == "empty":
        write_silence(wav, seconds=0, sample_rate=SAMPLE_RATE)
    assert problem in audio_problem(wav, "Hello.")


def test_audio_problem_accepts_good_audio(tmp_path: Path) -> None:
    wav = tmp_path / "x.wav"
    text = normalize_whitespace("A perfectly normal sentence.")
    _write_speech_like(wav, text)
    assert audio_problem(wav, text) == ""


class CountingEngine(FakeEngine):
    """Fails once it has voiced ``limit`` chunks, like a run that gets interrupted."""

    def __init__(self, limit: int | None = None) -> None:
        super().__init__()
        self.limit = limit

    def synthesize(self, text: str, output: Path) -> None:
        if self.limit is not None and len(self.texts) >= self.limit:
            raise RuntimeError("interrupted")
        super().synthesize(text, output)


def test_scene_breaks_become_silence_instead_of_failing(tmp_path: Path) -> None:
    book = Book("B", ("A",), "en", (Chapter("One", ("Before.", "* * *", "\u2042", "After.")),))
    engine = FakeEngine()
    convert_book(book, engine, tmp_path / "b.m4b", workdir=tmp_path / "w")
    assert engine.texts == ["One", "Before.", "After."]  # scene breaks aren't sent to the voice
    assert (tmp_path / "b.m4b").is_file()


def test_an_interrupted_conversion_resumes_without_redoing_work(tmp_path: Path) -> None:
    book = _book()
    workdir = tmp_path / "work"
    first = CountingEngine(limit=3)
    with pytest.raises(MissingAudioError, match="interrupted"):
        convert_book(book, first, tmp_path / "b.m4b", workdir=workdir, retries=0)
    assert not list(workdir.glob("chunks/*.partial"))  # no half-written audio is left behind

    second = CountingEngine()
    result = convert_book(book, second, tmp_path / "b.m4b", workdir=workdir)

    assert result.reused_chunks == 3
    assert len(first.texts) == 3
    spoken = "".join("".join(t.split()) for t in first.texts[:3] + second.texts)
    source = "".join("".join(p.split()) for c in book.chapters for p in c.spoken_paragraphs())
    assert spoken == source  # together, the two runs voiced every character exactly once


def test_changed_text_is_voiced_again_on_resume(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    convert_book(_book(), FakeEngine(), tmp_path / "b.m4b", workdir=workdir)
    edited = Book("B", ("A",), "en", (Chapter("Chapter One", ("It was a bright day.",)),))

    engine = FakeEngine()
    convert_book(edited, engine, tmp_path / "b2.m4b", workdir=workdir)

    assert engine.texts == ["It was a bright day."]  # the title was reused, the new text wasn't


def test_a_damaged_cover_is_dropped_with_a_warning(tmp_path: Path) -> None:
    damaged = Cover(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "image/png")
    book = Book("B", ("A",), "en", (Chapter("One", ("Hello.",)),), cover=damaged)

    result = convert_book(book, FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w")

    assert result.warnings == ("The book's cover image is damaged, so the audiobook has no cover.",)
    assert (tmp_path / "b.m4b").is_file()
