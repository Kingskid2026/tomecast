import array
import math
import subprocess
import threading
import time
import wave
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]
import pytest

from tomecast.audio import write_silence
from tomecast.book import Book, Chapter, Cover
from tomecast.chunking import normalize_whitespace, verify_coverage
from tomecast.convert import (
    ConversionCancelled,
    ConversionControl,
    ConversionProgress,
    MissingAudioError,
    audio_problem,
    convert_book,
    plan_chunks,
)

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
    # once at the start, after every chunk, and once more for packaging
    assert seen == [(i, total) for i in range(total + 1)] + [(total, total)]


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


@pytest.mark.parametrize("speed", [1.0, 1.25, 1.5])
def test_audio_problem_allows_fast_audio_at_a_fast_speed(tmp_path: Path, speed: float) -> None:
    wav = tmp_path / "x.wav"
    text = "x" * 60
    _write_speech_like(wav, text, chars_per_second=40.0 * speed * 0.95)  # a little under the limit
    assert audio_problem(wav, text, speed) == ""
    if speed > 1.0:  # the same audio is too short for normal speed
        assert "cut off" in audio_problem(wav, text)


@pytest.mark.parametrize("speed", [0.5, 1.0, 1.5, 3.0])
def test_audio_problem_still_catches_truncated_audio_at_every_speed(
    tmp_path: Path, speed: float
) -> None:
    wav = tmp_path / "x.wav"
    text = "A sentence long enough that five characters of audio cannot be all of it."
    _write_speech_like(wav, text[:5])
    assert "cut off" in audio_problem(wav, text, speed)


def test_slow_speeds_do_not_weaken_the_audio_check(tmp_path: Path) -> None:
    wav = tmp_path / "x.wav"
    text = "x" * 60
    _write_speech_like(wav, text, chars_per_second=60.0)  # 1.0 s: too short for 60 characters
    assert audio_problem(wav, text, 0.5) == audio_problem(wav, text, 1.0) != ""


class FastEngine(FakeEngine):
    """Voices at 1.5x and asks for a 2x tempo, as KokoroServer does at speed 3.0."""

    voice_speed = 1.5
    tempo = 2.0

    def synthesize(self, text: str, output: Path) -> None:
        self.texts.append(text)
        _write_speech_like(output, text, chars_per_second=CHARS_PER_SECOND * 3.0)  # 45 > 40


def test_a_fast_speed_loses_no_text_and_halves_the_book(tmp_path: Path) -> None:
    book = _book()
    unstretched = FastEngine()
    unstretched.tempo = 1.0
    normal = convert_book(book, unstretched, tmp_path / "a.m4b", workdir=tmp_path / "wa")
    fast_engine = FastEngine()
    fast = convert_book(book, fast_engine, tmp_path / "b.m4b", workdir=tmp_path / "wb")

    spoken = "".join("".join(t.split()) for t in fast_engine.texts)
    source = "".join(
        "".join(p.split()) for chapter in book.chapters for p in chapter.spoken_paragraphs()
    )
    assert spoken == source  # every character still reached the voice (and passed its check)
    assert [c.title for c in fast.chapters] == ["Chapter One", "Chapter Two"]
    assert _chapter_titles(tmp_path / "b.m4b") == ["Chapter One", "Chapter Two"]
    # The tempo halves everything, silences included; chapter 2 starts at the halved time.
    assert fast.chapters[-1].end_seconds == pytest.approx(normal.chapters[-1].end_seconds / 2)
    assert fast.chapters[1].start_seconds == pytest.approx(normal.chapters[1].start_seconds / 2)


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


def test_progress_reports_the_text_being_spoken_and_audio_made(tmp_path: Path) -> None:
    seen: list[ConversionProgress] = []
    convert_book(
        _book(), FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w", progress=seen.append
    )

    assert seen[0].chunks_done == 0
    assert seen[0].now_speaking == "Chapter One"  # the first chunk, about to be voiced
    assert seen[1].now_speaking == "It was a dark night."
    assert [p.audio_seconds for p in seen] == sorted(p.audio_seconds for p in seen)
    assert seen[-2].audio_seconds > 0
    assert seen[-1].stage == "packaging"
    assert seen[-1].now_speaking == ""


def test_a_conversion_can_be_cancelled_and_then_resumed(tmp_path: Path) -> None:
    control = ConversionControl()
    engine = FakeEngine()

    def cancel_after_two(p: ConversionProgress) -> None:
        if p.chunks_done == 2:
            control.cancel()

    with pytest.raises(ConversionCancelled):
        convert_book(_book(), engine, tmp_path / "b.m4b", workdir=tmp_path / "w",
                     progress=cancel_after_two, control=control)  # fmt: skip
    assert len(engine.texts) == 2
    assert not (tmp_path / "b.m4b").exists()

    result = convert_book(_book(), FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w")
    assert result.reused_chunks == 2


def test_a_paused_conversion_waits_until_resumed(tmp_path: Path) -> None:
    control = ConversionControl()
    control.pause()
    engine = FakeEngine()
    worker = threading.Thread(
        target=convert_book,
        args=(_book(), engine, tmp_path / "b.m4b"),
        kwargs={"workdir": tmp_path / "w", "control": control},
    )
    worker.start()
    time.sleep(0.3)
    assert engine.texts == []  # nothing voiced while paused
    control.resume()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert (tmp_path / "b.m4b").is_file()


# --- Cancelling at the last moment ---------------------------------------------------------


def test_cancelling_during_the_last_piece_stops_before_packaging(tmp_path: Path) -> None:
    control = ConversionControl()
    seen: list[ConversionProgress] = []

    def cancel_on_the_last_piece(p: ConversionProgress) -> None:
        seen.append(p)
        if p.stage == "voicing" and p.chunks_done == p.chunks_total:
            control.cancel()

    with pytest.raises(ConversionCancelled):
        convert_book(_book(), FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w",
                     progress=cancel_on_the_last_piece, control=control)  # fmt: skip

    assert not (tmp_path / "b.m4b").exists()  # nothing was written, so "Stopped" is true
    assert not list(tmp_path.glob(".*"))
    assert all(p.stage == "voicing" for p in seen)  # packaging never started

    # Everything was voiced and kept: starting again reuses all of it.
    again = convert_book(_book(), FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w")
    assert again.reused_chunks == seen[-1].chunks_total


def test_a_pause_on_the_last_piece_holds_before_packaging(tmp_path: Path) -> None:
    control = ConversionControl()
    stages: list[str] = []

    def pause_on_the_last_piece(p: ConversionProgress) -> None:
        stages.append(p.stage)
        if p.stage == "voicing" and p.chunks_done == p.chunks_total:
            control.pause()

    worker = threading.Thread(
        target=convert_book,
        args=(_book(), FakeEngine(), tmp_path / "b.m4b"),
        kwargs={"workdir": tmp_path / "w", "control": control, "progress": pause_on_the_last_piece},
    )
    worker.start()
    time.sleep(0.5)
    assert "packaging" not in stages and not (tmp_path / "b.m4b").exists()
    control.resume()
    worker.join(timeout=30)
    assert (tmp_path / "b.m4b").is_file()


def test_cancel_and_pause_do_nothing_once_packaging_has_begun(tmp_path: Path) -> None:
    control = ConversionControl()
    answers: list[tuple[bool, bool]] = []

    def try_to_stop(p: ConversionProgress) -> None:
        if p.stage == "packaging":
            answers.append((control.cancel(), control.pause()))

    convert_book(_book(), FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w",
                 progress=try_to_stop, control=control)  # fmt: skip

    assert answers == [(False, False)]  # refused, so nobody is told "Stopped"
    assert control.committed and not control.cancelled and not control.paused
    assert (tmp_path / "b.m4b").is_file()


# --- Never overwriting ----------------------------------------------------------------------


def test_overwrite_false_gives_way_to_a_file_that_appears_meanwhile(tmp_path: Path) -> None:
    target = tmp_path / "b.m4b"

    def someone_saves_a_file(p: ConversionProgress) -> None:
        if p.stage == "packaging":
            target.write_bytes(b"not ours")

    result = convert_book(_book(), FakeEngine(), target, workdir=tmp_path / "w",
                          progress=someone_saves_a_file, overwrite=False)  # fmt: skip

    assert result.output == tmp_path / "b (2).m4b"
    assert target.read_bytes() == b"not ours"
    assert _chapter_titles(result.output) == ["Chapter One", "Chapter Two"]


def test_overwrite_is_the_default_and_the_result_names_the_file(tmp_path: Path) -> None:
    target = tmp_path / "b.m4b"
    target.write_bytes(b"old")

    result = convert_book(_book(), FakeEngine(), target, workdir=tmp_path / "w")

    assert result.output == target
    assert _chapter_titles(target) == ["Chapter One", "Chapter Two"]


# --- Choosing chapters ----------------------------------------------------------------------


def _four_chapters() -> Book:
    return Book(
        title="Four",
        authors=("Ada Writer",),
        language="en",
        chapters=(
            Chapter("Contents", ("Part one. Part two. Part three.",)),
            Chapter("Preface", ("Why this book exists. It is short.",)),
            Chapter("The Start", ("It began. Then it went on, and on, and on past the limit.",
                                  "A second paragraph for the start.")),
            Chapter("The End", ("It ended. Everyone went home, tired but glad.",)),
        ),
    )  # fmt: skip


def _source_text(book: Book, indices: list[int]) -> str:
    return "".join(
        "".join(p.split()) for i in indices for p in book.chapters[i].spoken_paragraphs()
    )


def test_a_window_style_run_reuses_the_chunks_of_a_full_run(tmp_path: Path) -> None:
    book = _four_chapters()
    workdir = tmp_path / "w"
    convert_book(book, FakeEngine(), tmp_path / "full.m4b", workdir=workdir)
    files_after_full = sorted(p.name for p in (workdir / "chunks").iterdir())

    engine = FakeEngine()
    result = convert_book(book, engine, tmp_path / "some.m4b", workdir=workdir, chapters=[2, 3])

    assert engine.texts == []  # nothing voiced again: the IDs match the full run's
    assert result.reused_chunks == sum(
        len(chunks) for chunks in plan_chunks(book, FakeEngine.max_chars, [2, 3])
    )
    assert sorted(p.name for p in (workdir / "chunks").iterdir()) == files_after_full


def test_a_full_run_reuses_the_chunks_of_a_window_style_run(tmp_path: Path) -> None:
    book = _four_chapters()
    workdir = tmp_path / "w"
    first = FakeEngine()
    convert_book(book, first, tmp_path / "some.m4b", workdir=workdir, chapters=[2, 3])
    second = FakeEngine()
    convert_book(book, second, tmp_path / "full.m4b", workdir=workdir)

    assert "".join("".join(t.split()) for t in first.texts + second.texts) == _source_text(
        book, [2, 3, 0, 1]
    )  # the later chapters once, then only the earlier ones: nothing voiced twice
    assert not set(first.texts) & set(second.texts)


def test_changing_the_ticks_voices_only_what_is_new(tmp_path: Path) -> None:
    book = _four_chapters()
    workdir = tmp_path / "w"
    convert_book(book, FakeEngine(), tmp_path / "a.m4b", workdir=workdir, chapters=[1, 2, 3])

    engine = FakeEngine()
    convert_book(book, engine, tmp_path / "b.m4b", workdir=workdir, chapters=[0, 2, 3])

    assert "".join("".join(t.split()) for t in engine.texts) == _source_text(book, [0])


@pytest.mark.parametrize("selection", [[0], [2, 3], [1, 3], [3, 1, 3], [0, 1, 2, 3]])
def test_every_sentence_of_every_selected_chapter_is_voiced(
    tmp_path: Path, selection: list[int]
) -> None:
    book = _four_chapters()
    engine = FakeEngine()
    output = tmp_path / "b.m4b"

    result = convert_book(book, engine, output, workdir=tmp_path / "w", chapters=selection)

    chosen = sorted(set(selection))
    assert "".join("".join(t.split()) for t in engine.texts) == _source_text(book, chosen)
    for chunks, index in zip(plan_chunks(book, engine.max_chars, selection), chosen, strict=True):
        verify_coverage(book.chapters[index].spoken_paragraphs(), chunks)
        assert {chunk.chapter for chunk in chunks} == {index}  # IDs keep the book's numbering
    titles = [book.chapters[i].title for i in chosen]
    assert [c.title for c in result.chapters] == titles  # unselected chapters have no marker
    assert _chapter_titles(output) == titles  # …in the file, in book order


def test_unselected_chapters_make_no_audio(tmp_path: Path) -> None:
    book = _four_chapters()
    workdir = tmp_path / "w"
    engine = FakeEngine()

    convert_book(book, engine, tmp_path / "b.m4b", workdir=workdir, chapters=[2])

    names = [p.name for p in (workdir / "chunks").iterdir()]
    assert names and all(name.startswith("c002-") for name in names)
    skipped = {"Contents", "Preface", "The End"}
    assert not skipped & {t for t in engine.texts}


def test_progress_counts_only_the_selected_chapters(tmp_path: Path) -> None:
    seen: list[ConversionProgress] = []
    book = _four_chapters()

    convert_book(book, FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w",
                 progress=seen.append, chapters=[1, 3])  # fmt: skip

    expected = sum(len(c) for c in plan_chunks(book, FakeEngine.max_chars, [1, 3]))
    assert seen[0].chunks_total == expected == seen[-1].chunks_done
    assert {p.chapter_count for p in seen} == {2}
    assert {p.chapter_index for p in seen} == {0, 1}  # positions among the chosen, not the book's


def test_a_chapter_that_isnt_in_the_book_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no such chapter index 9"):
        convert_book(_four_chapters(), FakeEngine(), tmp_path / "b.m4b", workdir=tmp_path / "w",
                     chapters=[1, 9])  # fmt: skip


# --- MP3 output -----------------------------------------------------------------------------


def test_mp3_format_writes_a_folder_with_every_sentence(tmp_path: Path) -> None:
    book = _book()
    engine = FakeEngine()
    target = tmp_path / "A Test Book"

    result = convert_book(book, engine, target, workdir=tmp_path / "work", format="mp3")

    assert result.output == target
    assert sorted(p.name for p in target.iterdir()) == [
        "01 - Chapter One.mp3",
        "02 - Chapter Two.mp3",
    ]
    assert [c.title for c in result.chapters] == ["Chapter One", "Chapter Two"]
    spoken = "".join("".join(t.split()) for t in engine.texts)
    source = "".join(
        "".join(p.split()) for chapter in book.chapters for p in chapter.spoken_paragraphs()
    )
    assert spoken == source


def test_mp3_format_never_overwrites_a_folder_that_appears_meanwhile(tmp_path: Path) -> None:
    target = tmp_path / "Book"

    def folder_appears(p: ConversionProgress) -> None:
        if p.stage == "packaging":
            target.mkdir()

    result = convert_book(_book(), FakeEngine(), target, workdir=tmp_path / "w",
                          progress=folder_appears, overwrite=False, format="mp3")  # fmt: skip

    assert result.output == tmp_path / "Book (2)"
    assert list(target.iterdir()) == []
