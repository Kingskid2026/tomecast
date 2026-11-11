"""The premium voice's listen-back check (spoken_check, #65), wired into the conversion loop:
convert_book calls it for every chunk, never the engine. Against the fake audio.cpp server and
a fake recogniser that "hears" what was voiced, minus the words a test says it skipped."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from epub_factory import NavPoint, Page, build_epub
from fake_engine import write_fake_server

from tomecast import spoken_check
from tomecast.audio import wav_info
from tomecast.book import Book
from tomecast.convert import MissingAudioError, convert_book, plan_chunks
from tomecast.engines import ming
from tomecast.engines.audiocpp import EngineError, KokoroServer
from tomecast.engines.ming import MingServer, SpokenTextCheck, UncheckablePiece
from tomecast.readers import read_book
from tomecast.textprep import TextPrep

QUEEN = "Alice saw the Queen of Hearts walking slowly across the croquet ground."


@pytest.fixture(autouse=True)
def _premium_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOMECAST_PREMIUM_PREVIEW", "1")


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    return write_fake_server(tmp_path / "audiocpp_server")


def _voiced(tmp_path: Path) -> list[dict[str, object]]:
    events = [json.loads(line) for line in (tmp_path / "fake.log").read_text().splitlines()]
    return [event for event in events if event["event"] == "voiced"]


def _inputs(tmp_path: Path) -> list[str]:
    return [str(e["body"]["input"]) for e in _voiced(tmp_path)]  # type: ignore[index]


class FakeEars:
    """A recogniser that hears the request voiced last, through ``hear(model, text)``."""

    def __init__(self, log: Path, hear: Callable[[str, str], str]) -> None:
        self._log = log
        self._hear = hear
        self.heard = 0

    def transcribe(self, wav: Path) -> str:
        self.heard += 1
        last = [json.loads(line) for line in self._log.read_text().splitlines()][-1]
        assert last["event"] == "voiced"
        return self._hear(str(last["model"]), str(last["body"]["input"]))


def skips_the_queen_unless_short(model: str, text: str) -> str:
    """Ming leaves "of Hearts" out of any piece longer than 40 characters."""
    if model == "ming" and "Queen of Hearts" in text and len(text) > 40:
        return text.replace("of Hearts ", "")
    return text


def always_skips_the_queen(model: str, text: str) -> str:
    """Ming never says "Queen of Hearts", however short the piece; Kokoro does."""
    return text.replace("Queen of Hearts", "Queen") if model == "ming" else text


@pytest.fixture
def book(tmp_path: Path) -> Book:
    pages = [
        Page("c1.xhtml", "<p>It was a bright day. The garden was full of roses.</p>"),
        Page("c2.xhtml", f"<p>{QUEEN} Everyone bowed low.</p>"),
    ]
    toc = [NavPoint("Chapter 1", "c1.xhtml"), NavPoint("Chapter 2", "c2.xhtml")]
    return read_book(build_epub(tmp_path / "alice.epub", pages, toc=toc, title="Alice"))


def _check(
    tmp_path: Path,
    engine: MingServer,
    hear: Callable[[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    on_unfixable: ming.Unfixable = ming.DEFAULT_UNFIXABLE,
) -> tuple[SpokenTextCheck, FakeEars]:
    ears = FakeEars(tmp_path / "fake.log", hear)
    monkeypatch.setattr(ming, "TRANSCRIBER", lambda root: ears)
    check = ming.spoken_text_check(
        tmp_path / "data", engine, prep=TextPrep.for_language("en"), on_unfixable=on_unfixable
    )
    assert check is not None
    return check, ears


def _engine(tmp_path: Path, script: Path) -> MingServer:
    return MingServer(tmp_path / "data", executable=script, startup_timeout=20)


def test_a_chunk_that_skips_is_voiced_again_in_pieces_until_every_word_is_heard(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, fake_server)
    check, ears = _check(tmp_path, engine, skips_the_queen_unless_short, monkeypatch)
    with engine, check:
        result = convert_book(
            book, engine, tmp_path / "alice.m4b", workdir=tmp_path / "work", checked=check
        )
    assert result.output.is_file()

    voiced = _inputs(tmp_path)
    assert ears.heard == len(voiced)  # every voicing was listened to
    queen = [i for i, text in enumerate(voiced) if "Queen" in text or "croquet" in text]
    attempts = [voiced[i] for i in queen]
    # The whole chunk, again with a new seed, then (80 characters is still too long) pieces
    # of at most 40, which come out whole: nothing lost, nothing voiced twice.
    assert attempts[:2] == [QUEEN, QUEEN]
    pieces = attempts[2:]
    assert all(len(piece) <= 40 for piece in pieces)
    assert " ".join(pieces) == QUEEN
    seeds = [_voiced(tmp_path)[i]["body"]["options"]["seed"] for i in queen]  # type: ignore[index]
    assert len(set(seeds)) == len(seeds)  # no seed twice for one chunk
    assert engine.seed == 42  # put back after the check
    # Every other chunk once, and every chunk of the book reached the audio.
    others = [text for i, text in enumerate(voiced) if i not in queen]
    assert len(others) == len(set(others))
    for chunks in plan_chunks(book, ming.MAX_CHARS):
        for chunk in chunks:
            if chunk.speakable and chunk.text != QUEEN:
                assert chunk.text in others


def test_a_chunk_nothing_fixes_stops_the_book_by_default_and_isnt_retried_twice(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, fake_server)
    check, _ears = _check(tmp_path, engine, always_skips_the_queen, monkeypatch)
    assert check.on_unfixable == "stop"
    with engine, check, pytest.raises(spoken_check.WordsSkippedError) as raised:
        convert_book(book, engine, tmp_path / "alice.m4b", workdir=tmp_path / "work", checked=check)
    assert isinstance(raised.value, MissingAudioError)
    assert ming.not_the_builds_fault(raised.value)
    # The check ran its whole plan once: convert's own retry didn't run it all again.
    assert _inputs(tmp_path).count(QUEEN) == 2  # the whole chunk, and one new seed
    assert not (tmp_path / "alice.m4b").exists()
    made = [wav.name for wav in (tmp_path / "work" / "chunks").glob("*.wav")]
    assert made  # the chunks before it are kept, for a resume
    assert not any(name.endswith(".partial") for name in made)


def test_with_kokoro_the_closest_kokoro_voice_reads_a_chunk_nothing_fixes(
    tmp_path: Path, fake_server: Path, book: Book, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, fake_server)
    check, _ears = _check(
        tmp_path, engine, always_skips_the_queen, monkeypatch, on_unfixable="kokoro"
    )
    with engine, check:
        result = convert_book(
            book, engine, tmp_path / "alice.m4b", workdir=tmp_path / "work", checked=check
        )
        assert check.note == (
            "Kokoro's George (similar British voice) read 1 piece of text the premium voice "
            "kept leaving words out of."
        )
    assert result.output.is_file()
    kokoro = [e for e in _voiced(tmp_path) if e["model"] == "kokoro"]
    assert len(kokoro) == 1  # that one chunk, whole, once, on the CPU, and checked too
    body = kokoro[0]["body"]
    assert (body["input"], body["voice"], kokoro[0]["backend"]) == (QUEEN, "bm_george", "cpu")  # type: ignore[index]
    rates = {wav_info(w).sample_rate for w in (tmp_path / "work" / "chunks").glob("*.wav")}
    assert rates == {44_100, 24_000}  # the packager makes them one


def test_text_the_check_cant_read_is_refused_before_anything_is_voiced(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, fake_server)
    check, ears = _check(tmp_path, engine, always_skips_the_queen, monkeypatch)
    greek = "Ἐν ἀρχῇ ἦν ὁ λόγος, καὶ ὁ λόγος ἦν πρὸς τὸν θεόν."
    assert check.uncheckable(["It began.", greek]) != ""
    assert check.uncheckable(["It began.", "The word was λόγος."]) == ""
    with engine, check, pytest.raises(UncheckablePiece) as raised:
        check("c001-p0001-s01", greek, lambda text: tmp_path / "never.wav")
    assert ming.not_the_builds_fault(raised.value)
    assert isinstance(raised.value.__cause__, spoken_check.UncheckableTextError)
    assert ears.heard == 0


def test_once_kokoro_has_taken_over_chunks_are_voiced_once_and_not_checked(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path, fake_server)
    check, ears = _check(tmp_path, engine, always_skips_the_queen, monkeypatch)
    engine.switched_to = "bm_george"
    voiced: list[str] = []

    def voice(text: str) -> Path:
        voiced.append(text)
        return tmp_path / "x.wav"

    assert check("c001", QUEEN, voice) == [tmp_path / "x.wav"]
    assert (voiced, ears.heard) == ([QUEEN], 0)


def test_the_check_is_only_for_the_premium_voice_and_only_with_a_recogniser(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prep = TextPrep.for_language("en")
    premium = _engine(tmp_path, fake_server)
    kokoro = KokoroServer(tmp_path / "data", executable=fake_server)
    assert ming.spoken_text_check(tmp_path, premium, prep=prep) is None  # no recogniser yet
    monkeypatch.setattr(ming, "TRANSCRIBER", lambda root: FakeEars(tmp_path, lambda m, t: t))
    assert ming.spoken_text_check(tmp_path, kokoro, prep=prep) is None  # Kokoro doesn't skip
    assert ming.spoken_text_check(tmp_path, premium, prep=prep) is not None
    # Wired in: the preview gate and its notes go, and old unchecked pieces aren't reused.
    monkeypatch.delenv("TOMECAST_PREMIUM_PREVIEW")
    assert ming.preview_enabled()
    assert ming.preview_note() is None
    assert ming.work_key("UK_George").endswith("spoken-check=on")


def test_which_failures_never_count_against_the_build() -> None:
    comparison = spoken_check.compare("the Queen of Hearts", "the Queen")
    skipped = spoken_check.WordsSkippedError("c1", "the Queen of Hearts", "x", comparison, 4)
    uncheckable = spoken_check.UncheckableTextError("only Latin-script text so far")
    try:
        raise MissingAudioError("no usable audio for c1") from skipped
    except MissingAudioError as wrapped:
        assert ming.not_the_builds_fault(wrapped)  # kept as the cause by convert
    assert ming.not_the_builds_fault(skipped)
    assert ming.not_the_builds_fault(uncheckable)
    assert ming.not_the_builds_fault(UncheckablePiece("can't be checked"))
    assert not ming.not_the_builds_fault(EngineError("the voice engine keeps crashing"))
    assert not ming.not_the_builds_fault(MissingAudioError("the audio is silent"))
