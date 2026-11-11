"""The graphics card failing under the premium voice (Ming, issue #11): Kokoro takes over (the
default), Ming carries on on the CPU, or the book stops and resumes later. Whichever it is,
every sentence is spoken: the pieces Ming made are kept, each other piece is voiced exactly
once, in order, and a book of mixed voices (44.1 and 24 kHz) is packaged as one.

All against the fake audio.cpp server, which can fail one model on the card only, or lose the
card altogether."""

import json
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg  # type: ignore[import-untyped]
import pytest
from epub_factory import NavPoint, Page, build_epub
from fake_engine import write_fake_server

from tomecast.audio import wav_info
from tomecast.book import Book
from tomecast.chunking import Chunk
from tomecast.convert import (
    ConversionCancelled,
    ConversionControl,
    ConversionProgress,
    MissingAudioError,
    convert_book,
    plan_chunks,
)
from tomecast.engines import ming
from tomecast.engines.audiocpp import CPU_FALLBACK_NOTE, RAN_OUT_OF_MEMORY_NOTE, EngineError
from tomecast.engines.ming import AutoMing, BookPosition, CardStopped, switch_note
from tomecast.readers import read_book

_DEVICES = json.dumps(
    {
        "schema_version": 1,
        "devices": [
            {"backend": "vulkan", "index": 0, "name": "Vulkan0", "type": "gpu",
             "description": "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",
             "memory": {"free_bytes": 7_000_000_000, "total_bytes": 8_000_000_000}},
        ],
    }
)  # fmt: skip


@pytest.fixture(autouse=True)
def _premium_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests use the premium voice, a preview for now: opted into."""
    monkeypatch.setenv("TOMECAST_PREMIUM_PREVIEW", "1")


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_DEVICES", _DEVICES)
    return write_fake_server(tmp_path / "audiocpp_server")


@pytest.fixture
def book(tmp_path: Path) -> Book:
    """Three chapters of a few sentences each: one piece per sentence, with titles."""
    sentences = [
        "Alice sat by the river. The sun was warm. Her sister read a book.",
        "A white rabbit ran past. It looked at a watch. It said it was late.",
        "Alice ran after it. She fell down a deep hole. She fell for a long time.",
    ]
    pages = [Page(f"c{i}.xhtml", f"<p>{text}</p>") for i, text in enumerate(sentences, 1)]
    toc = [NavPoint(f"Chapter {i}", f"c{i}.xhtml") for i in range(1, 4)]
    return read_book(build_epub(tmp_path / "alice.epub", pages, toc=toc, title="Alice"))


def _pieces(book: Book) -> list[Chunk]:
    """The pieces the book is voiced in, in order (the titles included)."""
    return [c for chunks in plan_chunks(book, ming.MAX_CHARS) for c in chunks if c.speakable]


def _events(tmp_path: Path, kind: str = "voiced") -> list[dict[str, object]]:
    path = tmp_path / "fake.log"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    return [event for event in events if event["event"] == kind]


def _voiced(tmp_path: Path) -> list[tuple[str, str, str]]:
    """(model, backend, text) of each piece voiced, in order."""
    return [
        (str(e["model"]), str(e["backend"]), str(e["body"]["input"]))  # type: ignore[index]
        for e in _events(tmp_path)
    ]


def _engine(tmp_path: Path, script: Path, **kwargs: object) -> AutoMing:
    return AutoMing(tmp_path / "data", executable=script, startup_timeout=20, **kwargs)  # type: ignore[arg-type]


def _convert(
    book: Book, engine: AutoMing, output: Path, workdir: Path, **kwargs: object
) -> tuple[list[ConversionProgress], object]:
    """Convert as the command line and the window do: the engine knows where the book is."""
    position = BookPosition(_pieces_all(book))
    engine.where = position.where
    seen: list[ConversionProgress] = []

    def progress(report: ConversionProgress) -> None:
        position.update(report.chunks_done)
        seen.append(report)

    with engine:
        result = convert_book(book, engine, output, workdir=workdir, progress=progress, **kwargs)  # type: ignore[arg-type]
    return seen, result


def _pieces_all(book: Book) -> list[Chunk]:
    """Every piece convert_book goes through, speakable or not (progress counts them all)."""
    return [c for chunks in plan_chunks(book, ming.MAX_CHARS) for c in chunks]


def _sample_rate(path: Path) -> int:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True).stderr
    found = re.search(rb"Audio: .*?, (\d+) Hz", out)
    assert found, out
    return int(found.group(1))


def _duration(path: Path) -> float:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True).stderr
    found = re.search(rb"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    assert found, out
    hours, minutes, seconds = found.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# --- Kokoro takes over (the default) ------------------------------------------------------------


def test_kokoro_takes_over_when_the_card_runs_out_of_memory_mid_book(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    pieces = [c.text for c in _pieces(book)]
    premium = 6  # Ming voices the first six pieces; the card is full from the seventh
    monkeypatch.setenv("FAKE_ONLY_MODEL", "ming")
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", str(premium))
    engine = _engine(tmp_path, fake_server, speed=1.5)
    workdir = tmp_path / "work"

    _seen, result = _convert(book, engine, tmp_path / "alice.m4b", workdir)

    voiced = _voiced(tmp_path)
    # Every piece exactly once, in order: Ming's six, then Kokoro for each of the rest.
    assert [text for _, _, text in voiced] == pieces
    assert [model for model, _, _ in voiced] == ["ming"] * premium + ["kokoro"] * (
        len(pieces) - premium
    )
    # The one Ming request the card refused was voiced again by Kokoro, not skipped.
    (refused,) = _events(tmp_path, "out of memory")
    assert refused["body"]["input"] == pieces[premium]  # type: ignore[index]
    # Kokoro runs where it would: here the card still works for it.
    assert {backend for model, backend, _ in voiced if model == "kokoro"} == {"vulkan"}
    kokoro_bodies = [e["body"] for e in _events(tmp_path) if e["model"] == "kokoro"]
    # The closest Kokoro voice, at voice speed 1.0: the user's 1.5x is the stretch, as for Ming.
    assert {(b["voice"], b["speed"]) for b in kokoro_bodies} == {("bm_george", 1.0)}  # type: ignore[index]
    assert (engine.voice_speed, engine.tempo) == (1.0, 1.5)

    # Said once, naming the voice and the chapters in the premium voice. Six pieces are
    # chapter 1 (title and three sentences) and the first two of chapter 2.
    assert engine.switched_to == "bm_george"
    assert engine.warning == (
        "The graphics card stopped working, so the rest of this book uses Kokoro's George "
        "(similar British voice). Chapter 1 and the start of chapter 2 are in the premium voice."
    )
    assert engine.fell_back and engine.out_of_memory  # proves nothing, never blamed on it
    assert engine.label == "Ming (UK_George) and Kokoro (bm_george)"

    # Mixed voices, packaged as one: the pieces stay at their own rates in the work folder,
    # and the book is one 44.1 kHz file as long as all of them.
    rates = sorted(wav_info(wav).sample_rate for wav in (workdir / "chunks").glob("*.wav"))
    assert rates == [24_000] * (len(pieces) - premium) + [44_100] * premium
    assert _sample_rate(result.output) == 44_100  # type: ignore[attr-defined]
    length = result.chapters[-1].end_seconds  # type: ignore[attr-defined]
    assert _duration(result.output) == pytest.approx(length, abs=0.1)  # type: ignore[attr-defined]


def test_kokoro_takes_over_on_the_cpu_when_the_card_is_lost(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    pieces = [c.text for c in _pieces(book)]
    monkeypatch.setenv("FAKE_CARD_LOST_AFTER", "3")  # the device is lost, for everything
    engine = _engine(tmp_path, fake_server)

    _convert(book, engine, tmp_path / "alice.m4b", tmp_path / "work")

    voiced = _voiced(tmp_path)
    assert [text for _, _, text in voiced] == pieces  # each once, in order
    assert voiced[:3] == [("ming", "vulkan", text) for text in pieces[:3]]
    assert voiced[3:] == [("kokoro", "cpu", text) for text in pieces[3:]]
    assert engine.warning is not None
    assert "Kokoro's George" in engine.warning
    assert "keeps crashing" not in engine.warning
    assert engine.fell_back and engine.out_of_memory


def test_kokoro_takes_over_from_the_start_if_the_premium_voice_wont_run_on_the_card(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_ONLY_MODEL", "ming")
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    engine = _engine(tmp_path, fake_server, voice="US_Emma")

    _convert(book, engine, tmp_path / "alice.m4b", tmp_path / "work")

    voiced = _voiced(tmp_path)
    assert [text for _, _, text in voiced] == [c.text for c in _pieces(book)]
    assert {(model, backend) for model, backend, _ in voiced} == {("kokoro", "vulkan")}
    assert engine.warning == (
        "The graphics card didn't work, so this book uses Kokoro's Heart (similar American "
        "voice) instead of the premium voice."
    )


@pytest.mark.parametrize(
    ("premium", "kokoro"),
    [
        ("UK_George", "bm_george"),
        ("UK_Charlotte", "bf_emma"),
        ("US_Emma", "af_heart"),
        ("US_James", "am_michael"),
    ],
)
def test_each_premium_voice_has_a_close_kokoro_voice(premium: str, kokoro: str) -> None:
    voice, name, accent = ming.KOKORO_MATCH[premium]
    assert voice == kokoro
    # Same accent and gender: the prefix says both (b = British, a = American; f/m).
    assert voice[0] == ("b" if premium.startswith("UK") else "a")
    assert accent == ("British" if premium.startswith("UK") else "American")
    assert name.lower() == voice.split("_")[1]
    assert set(ming.KOKORO_MATCH) == set(ming.VOICES)


# --- What isn't the card failing ----------------------------------------------------------------


def test_words_left_out_fail_the_piece_and_kokoro_doesnt_take_over(
    tmp_path: Path, fake_server: Path, book: Book
) -> None:
    engine = _engine(tmp_path, fake_server, verify=lambda wav, text: "skipped a word")
    with pytest.raises(MissingAudioError, match="left out words"):
        _convert(book, engine, tmp_path / "alice.m4b", tmp_path / "work")
    assert engine.switched_to is None
    assert {model for model, _, _ in _voiced(tmp_path)} == {"ming"}


def test_an_error_the_engine_answers_with_isnt_the_card_failing(
    tmp_path: Path, fake_server: Path
) -> None:
    with _engine(tmp_path, fake_server) as engine, pytest.raises(EngineError, match="broke"):
        engine.synthesize("It EXPLODEs.", tmp_path / "out.wav")
    assert engine.switched_to is None


# --- Ming on the CPU ("cpu") --------------------------------------------------------------------


def test_with_cpu_the_premium_voice_carries_on_on_the_cpu(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    pieces = [c.text for c in _pieces(book)]
    monkeypatch.setenv("FAKE_ONLY_MODEL", "ming")
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "4")
    engine = _engine(tmp_path, fake_server, if_card_fails="cpu")

    _convert(book, engine, tmp_path / "alice.m4b", tmp_path / "work")

    voiced = _voiced(tmp_path)
    assert [text for _, _, text in voiced] == pieces
    assert [(m, b) for m, b, _ in voiced] == [("ming", "vulkan")] * 4 + [("ming", "cpu")] * (
        len(pieces) - 4
    )
    assert engine.warning == RAN_OUT_OF_MEMORY_NOTE
    assert engine.switched_to is None


def test_with_cpu_a_lost_card_also_carries_on_on_the_cpu(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CARD_LOST_AFTER", "2")
    engine = _engine(tmp_path, fake_server, if_card_fails="cpu")
    _convert(book, engine, tmp_path / "alice.m4b", tmp_path / "work")
    voiced = _voiced(tmp_path)
    assert {(m, b) for m, b, _ in voiced[2:]} == {("ming", "cpu")}
    assert engine.warning == ming.CPU_NOTE


def test_with_cpu_a_card_that_wont_start_means_the_cpu(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with _engine(tmp_path, fake_server, if_card_fails="cpu") as engine:
        assert (engine.backend, engine.warning) == ("cpu", CPU_FALLBACK_NOTE)


# --- Stopping, and resuming on a working card ("stop") ------------------------------------------


def test_with_stop_the_book_stops_and_resumes_on_a_working_card(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    pieces = [c.text for c in _pieces(book)]
    monkeypatch.setenv("FAKE_ONLY_MODEL", "ming")
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "5")
    workdir = tmp_path / "work"
    output = tmp_path / "alice.m4b"

    with pytest.raises(CardStopped, match="stopped working"):
        _convert(book, _engine(tmp_path, fake_server, if_card_fails="stop"), output, workdir)
    assert not output.exists()
    assert len(list((workdir / "chunks").glob("*.wav"))) == 5  # what was made is kept
    assert [text for _, _, text in _voiced(tmp_path)] == pieces[:5]

    monkeypatch.delenv("FAKE_OOM_VULKAN_AFTER")  # the card works again
    _seen, result = _convert(
        book, _engine(tmp_path, fake_server, if_card_fails="stop"), output, workdir
    )

    assert result.reused_chunks == 5  # type: ignore[attr-defined]
    # Across both runs, every piece voiced exactly once, in order, all by Ming.
    assert _voiced(tmp_path) == [("ming", "vulkan", text) for text in pieces]
    assert _sample_rate(output) == 44_100


def test_a_book_kokoro_took_over_resumes_in_the_same_work_folder(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped after Kokoro took over, then made on a working card: nothing is voiced twice,
    the Ming and Kokoro pieces made so far are both kept, and the rest is Ming's."""
    pieces = [c.text for c in _pieces(book)]
    monkeypatch.setenv("FAKE_ONLY_MODEL", "ming")
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "4")
    workdir = tmp_path / "work"
    output = tmp_path / "alice.m4b"
    control = ConversionControl()
    engine = _engine(tmp_path, fake_server)
    position = BookPosition(_pieces_all(book))
    engine.where = position.where

    def stop_two_pieces_after_the_switch(report: ConversionProgress) -> None:
        position.update(report.chunks_done)
        if engine.switched_to and len(_events(tmp_path)) >= 6:
            control.cancel()

    with engine, pytest.raises(ConversionCancelled):
        convert_book(
            book, engine, output, workdir=workdir, control=control,
            progress=stop_two_pieces_after_the_switch,
        )  # fmt: skip
    first = _voiced(tmp_path)
    assert [m for m, _, _ in first] == ["ming"] * 4 + ["kokoro"] * 2

    monkeypatch.delenv("FAKE_OOM_VULKAN_AFTER")
    _seen, result = _convert(book, _engine(tmp_path, fake_server), output, workdir)

    assert result.reused_chunks == 6  # type: ignore[attr-defined]
    voiced = _voiced(tmp_path)
    assert [text for _, _, text in voiced] == pieces  # each once, in order, across both runs
    assert [m for m, _, _ in voiced] == ["ming"] * 4 + ["kokoro"] * 2 + ["ming"] * (len(pieces) - 6)
    assert _sample_rate(output) == 44_100


# --- The note ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("done", "partway", "says"),
    [
        (0, True, "The start of chapter 1 is in the premium voice."),
        (1, False, "Chapter 1 is in the premium voice."),
        (1, True, "Chapter 1 and the start of chapter 2 are in the premium voice."),
        (3, False, "Chapters 1–3 are in the premium voice."),
        (3, True, "Chapters 1–3 and the start of chapter 4 are in the premium voice."),
    ],
)
def test_the_note_says_which_chapters_are_premium(done: int, partway: bool, says: str) -> None:
    note = switch_note("UK_Charlotte", chapters_done=done, partway=partway)
    assert note == (
        "The graphics card stopped working, so the rest of this book uses Kokoro's Emma "
        f"(similar British voice). {says}"
    )


def test_the_note_names_no_chapters_when_it_doesnt_know_them() -> None:
    assert switch_note("US_James", chapters_done=None) == (
        "The graphics card stopped working, so the rest of this book uses Kokoro's Michael "
        "(similar American voice)."
    )


def test_the_position_counts_chapters_as_the_audiobook_has_them(book: Book) -> None:
    chunks = _pieces_all(book)
    position = BookPosition(chunks)
    starts = [i for i, c in enumerate(chunks) if i == 0 or c.chapter != chunks[i - 1].chapter]
    position.update(0)
    assert position.where() == (0, False)
    position.update(starts[1])
    assert position.where() == (1, False)
    position.update(starts[1] + 1)
    assert position.where() == (1, True)
    position.update(len(chunks))
    assert position.where() == (3, False)
