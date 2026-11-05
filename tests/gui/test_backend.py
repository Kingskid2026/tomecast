"""The window's Backend, driven directly (no QML), with a fake voice engine."""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import shutil
import sys
import threading
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QUrl
from pytestqt.qtbot import QtBot

from tomecast.book import Book, Chapter
from tomecast.convert import plan_chunks
from tomecast.downloads import Asset, is_installed
from tomecast.engines.audiocpp import EngineError
from tomecast.gui import backend as backend_module
from tomecast.gui.backend import STOPPED_MESSAGE, Backend, safe_file_name, unique_output
from tomecast.gui.models import ChapterModel, display_title, is_numbered_chapter, summarize
from tomecast.gui.worker import TimedControl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # for epub_factory
from epub_factory import NavPoint, Page, build_epub, tiny_png

SAMPLE_RATE = 24000
CHARS_PER_SECOND = 15.0
CONVERSION_TIMEOUT = 30_000  # ms; a real (tiny) M4B is packaged with ffmpeg
DEMO = json.loads((Path(__file__).parent / "data" / "alice_demo.json").read_text())


# --- A fake voice engine ----------------------------------------------------------------------


def _speech_like(path: Path, text: str) -> None:
    """Stand-in for real speech: a tone whose length depends on the text, like a voice."""
    frames = round(len(text) / CHARS_PER_SECOND * SAMPLE_RATE)
    samples = array.array(
        "h", (round(8000 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)) for i in range(frames))
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(samples.tobytes())


class Engines:
    """Makes fake engines and records what they all did. Tests set the knobs."""

    def __init__(self) -> None:
        self.made: list[FakeEngine] = []
        self.texts: list[str] = []  # every text voiced, by any engine, in order
        self.block_at: int | None = None  # the Nth synthesize call waits for ``release``
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.fail_from: int | None = None  # synthesize fails from the Nth call on
        self.start_error: str | None = None

    def __call__(self, root: Path, voice: str, speed: float) -> FakeEngine:
        engine = FakeEngine(self, voice, speed)
        self.made.append(engine)
        return engine


class FakeEngine:
    label = "Fake voice"
    max_chars = 60
    sample_rate = SAMPLE_RATE

    def __init__(self, engines: Engines, voice: str, speed: float) -> None:
        self._engines = engines
        self.voice = voice
        self.speed = speed
        self.started = False
        self.closed = False
        self.thread_id: int | None = None  # the OS thread that started it

    def start(self) -> None:
        self.thread_id = threading.get_ident()
        if self._engines.start_error:
            raise EngineError(self._engines.start_error)
        self.started = True

    def close(self) -> None:
        assert threading.get_ident() == self.thread_id  # stopped on the thread that started it
        self.closed = True

    def synthesize(self, text: str, output: Path) -> None:
        engines = self._engines
        engines.texts.append(text)
        count = len(engines.texts)
        if engines.block_at == count:
            engines.blocked.set()
            engines.release.wait(timeout=10)
        if engines.fail_from is not None and count >= engines.fail_from:
            raise RuntimeError("engine crashed")
        _speech_like(output, text)


# --- Books -----------------------------------------------------------------------------------


def _words(count: int, start: str = "Word") -> str:
    """``count`` words of text, in sentences of ten words."""
    words = ["word"] * count
    sentences = [" ".join(["Word", *words[i + 1 : i + 10]]) + "." for i in range(0, count, 10)]
    return start + " ".join(sentences).removeprefix("Word")


def _book_with_front_matter(folder: Path) -> Path:
    pages = [
        Page("title.xhtml", "<p>Test Book</p><p>by Ada Writer</p>"),
        Page("contents.xhtml", "<p>Preface</p><p>The Start</p><p>The End</p>"),
        Page("preface.xhtml", f"<p>{_words(120, 'Preface')}</p>"),
        Page("c1.xhtml", f"<p>{_words(474, 'Start')}</p>"),
        Page("c2.xhtml", f"<p>{_words(80, 'End')}</p>"),
    ]
    toc = [
        NavPoint("Test Book", "title.xhtml"),
        NavPoint("Contents", "contents.xhtml"),
        NavPoint("Preface", "preface.xhtml"),
        NavPoint("Chapter I. The Start", "c1.xhtml"),
        NavPoint("Chapter II. The End", "c2.xhtml"),
    ]
    return build_epub(folder / "test book.epub", pages, toc=toc, cover=tiny_png())


def _short_book(folder: Path) -> Path:
    """Two short chapters: quick to convert, with enough pieces to measure the speed."""
    pages = [
        Page("c1.xhtml", "<p>It was a dark night. The wind howled. The rain fell.</p>"
                         "<p>Then it stopped, and the stars came out one by one.</p>"),
        Page("c2.xhtml", "<p>Morning came. The birds sang. Everyone woke up at last.</p>"),
    ]  # fmt: skip
    toc = [NavPoint("Chapter 1: The Night", "c1.xhtml"), NavPoint("Chapter 2", "c2.xhtml")]
    return build_epub(folder / "short.epub", pages, toc=toc, title="Short Book")


def _url(path: Path) -> QUrl:
    return QUrl.fromLocalFile(str(path))


def _speakable_chunks(book: Book) -> int:
    return sum(1 for chunks in plan_chunks(book, FakeEngine.max_chars)
               for chunk in chunks if chunk.speakable)  # fmt: skip


# --- Fixtures --------------------------------------------------------------------------------


MakeBackend = Callable[..., Backend]


@pytest.fixture
def make_backend(qtbot: QtBot, tmp_path: Path) -> Iterator[MakeBackend]:
    """Build Backends with a fake engine, no downloads, a temp data folder and settings file."""
    made: list[Backend] = []

    def make(
        engines: Engines | None = None,
        *,
        assets: tuple[Asset, ...] = (),
        player: str | None = None,
        system_check: Callable[[], None] = lambda: None,
    ) -> Backend:
        backend = Backend(
            engine_factory=engines if engines is not None else Engines(),
            assets=assets,
            data_root=tmp_path / "data",
            player=player,
            settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
            system_check=system_check,
        )
        made.append(backend)
        return backend

    yield make
    for backend in made:
        backend.shutdown()


def _open(qtbot: QtBot, backend: Backend, book: Path) -> None:
    backend.openBook(_url(book))
    qtbot.waitUntil(lambda: backend.screen == "chapters" or bool(backend.errorMessage))
    assert backend.errorMessage == ""


def _rows(model: ChapterModel) -> list[dict[str, object]]:
    roles = {bytes(name.data()).decode(): role for role, name in model.roleNames().items()}
    return [
        {name: model.data(model.index(row), role) for name, role in roles.items()}
        for row in range(model.rowCount())
    ]


def _convert(qtbot: QtBot, backend: Backend) -> None:
    backend.goTo("voice")
    backend.startConversion()
    assert backend.screen == "converting"
    qtbot.waitUntil(lambda: backend.screen != "converting", timeout=CONVERSION_TIMEOUT)


def _control(backend: Backend) -> TimedControl:
    control = backend._control
    assert control is not None
    return control


# --- Opening a book --------------------------------------------------------------------------


def test_opening_a_book_lists_its_chapters(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    assert backend.screen == "drop"
    assert backend.bookLoaded is False

    backend.openBook(_url(_book_with_front_matter(tmp_path)))
    assert backend.loading is True
    qtbot.waitUntil(lambda: backend.screen == "chapters")

    assert backend.loading is False
    assert backend.bookLoaded is True
    assert backend.bookTitle == "Test Book"
    assert backend.bookAuthors == "Test Author"
    assert backend.coverSource.startswith("image://dots/cover/dark?")
    assert backend.artSource == "image://dots/cantoria/dark"
    assert backend.artCredit == "LUCA DELLA ROBBIA · CANTORIA · 1431\u201338"
    assert backend.formatsLabel == "EPUB · PDF · DOCX · FB2 · HTML · TXT"
    rows = _rows(backend.chapters)
    assert [row["number"] for row in rows] == ["\u2014", "\u2014", "[ 01 ]", "[ 02 ]", "[ 03 ]"]
    assert [row["title"] for row in rows] == [
        "Test Book", "Contents", "Preface", "The Start", "The End"
    ]  # fmt: skip
    assert [row["wordsLabel"] for row in rows] == ["5", "5", "120", "474", "80"]
    assert [row["minutesLabel"] for row in rows] == ["<1 min", "<1 min", "<1 min", "3 min",
                                                     "<1 min"]  # fmt: skip
    assert [row["meterLit"] for row in rows] == [0, 0, 1, 3, 1]
    assert [row["selected"] for row in rows] == [False, False, True, True, True]
    assert backend.selectionSummary == {
        "sections": "3 OF 5 SECTIONS",
        "words": "674 WORDS",
        "audio": "ABOUT 4 MIN OF AUDIO",
        "make": "ABOUT 1 MIN TO MAKE HERE",
    }
    assert backend.previewText == "Preface word word word word word word word word word."


def test_a_new_cover_gets_a_new_image_url(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    covers: list[bytes | None] = []
    backend = make_backend()
    backend._cover_sink = covers.append
    book = _book_with_front_matter(tmp_path)

    _open(qtbot, backend, book)
    first = backend.coverSource
    backend.goTo("drop")
    _open(qtbot, backend, book)

    assert covers == [tiny_png(), tiny_png()]
    assert backend.coverSource != first
    backend.convertAnother()
    assert covers[-1] is None
    assert backend.coverSource == ""


def test_unticking_chapters_updates_the_summary_and_preview(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _book_with_front_matter(tmp_path))
    summaries: list[dict[str, str]] = []
    backend.selectionSummaryChanged.connect(lambda: summaries.append(backend.selectionSummary))

    backend.setChapterSelected(2, False)  # the preface
    model = backend.chapters
    model.setData(model.index(4), False, ChapterModel.SelectedRole)  # as a QML delegate does

    assert summaries[-1] == {
        "sections": "1 OF 5 SECTIONS",
        "words": "474 WORDS",
        "audio": "ABOUT 3 MIN OF AUDIO",
        "make": "ABOUT 1 MIN TO MAKE HERE",
    }
    assert backend.previewText == "Start word word word word word word word word word."
    assert model.selected_rows() == [3]

    backend.setChapterSelected(3, False)
    assert backend.selectionSummary["sections"] == "0 OF 5 SECTIONS"
    assert backend.selectionSummary["audio"] == "NO AUDIO"
    backend.goTo("voice")
    assert backend.screen == "chapters"  # nothing to convert
    assert backend.errorMessage == "Choose at least one section to convert."


def test_front_matter_can_be_ticked_and_skipped_again(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _book_with_front_matter(tmp_path))
    assert backend.skipFrontMatter is True

    backend.setSkipFrontMatter(False)
    assert backend.skipFrontMatter is False
    assert backend.chapters.selected_rows() == [0, 1, 2, 3, 4]
    assert backend.selectionSummary["sections"] == "5 OF 5 SECTIONS"
    assert backend.previewText == "Test Book"

    backend.setSkipFrontMatter(True)
    assert backend.chapters.selected_rows() == [2, 3, 4]


def test_alice_is_listed_as_in_the_mockup() -> None:
    sections = DEMO["sections"]
    book = Book(
        title=DEMO["title"],
        authors=tuple(DEMO["authors"]),
        language="en",
        chapters=tuple(
            Chapter(s["title"], (" ".join(["word"] * s["words"]),) if s["words"] else ())
            for s in sections
        ),
    )
    model = ChapterModel()
    model.set_book(book, skip_front_matter=True)
    rows = _rows(model)

    assert [row["frontMatter"] for row in rows] == [True] * 3 + [False] * 12
    assert rows[3]["number"] == "[ 01 ]"
    assert rows[3]["title"] == "Down the Rabbit-Hole"
    assert rows[3]["wordsLabel"] == "2,181"
    assert rows[3]["minutesLabel"] == "14 min"
    assert [row["meterLit"] for row in rows] == [
        0, 0, 0, 14, 13, 11, 17, 14, 16, 14, 16, 14, 13, 12, 13
    ]  # fmt: skip
    assert model.sections[5].label == "[ 03 ] A CAUCUS-RACE AND A LONG TALE"
    assert model.summary(DEMO["speedup"]) == {
        "sections": "12 OF 15 SECTIONS",
        "words": "26,371 WORDS",
        "audio": "ABOUT 2 H 47 MIN OF AUDIO",
        "make": "ABOUT 36 MIN TO MAKE HERE",
    }


@pytest.mark.parametrize(
    ("title", "numbered", "shown"),
    [
        ("CHAPTER I. Down the Rabbit-Hole", True, "Down the Rabbit-Hole"),
        ("Chapter 12: The End", True, "The End"),
        ("Chapter Twenty-One", True, "Chapter Twenty-One"),
        ("IV. Pig and Pepper", True, "Pig and Pepper"),
        ("3", True, "3"),
        ("Part Two", True, "Part Two"),
        ("I Remember", False, "I Remember"),
        ("Mix: a story", False, "Mix: a story"),
        ("Preface", False, "Preface"),
    ],
)
def test_numbered_chapters_are_recognised(title: str, numbered: bool, shown: str) -> None:
    assert is_numbered_chapter(title) is numbered
    assert display_title(title) == shown


def test_a_book_without_numbered_chapters_only_loses_its_opening_pages() -> None:
    long_text = (" ".join(["word"] * 200),)
    book = Book(
        "Poems", (), "en",
        (Chapter("Copyright", ("All rights reserved.",)), Chapter("Spring", long_text),
         Chapter("Haiku", ("Short poem.",)), Chapter("Winter", long_text)),
    )  # fmt: skip
    model = ChapterModel()
    model.set_book(book, skip_front_matter=True)

    assert [section.front_matter for section in model.sections] == [True, False, False, False]


def test_the_summary_formats_hours_and_tiny_amounts() -> None:
    assert summarize(26371, 12, 15, 4.7)["audio"] == "ABOUT 2 H 47 MIN OF AUDIO"
    assert summarize(50, 1, 1, 4.7) == {
        "sections": "1 OF 1 SECTION",
        "words": "50 WORDS",
        "audio": "UNDER 1 MIN OF AUDIO",
        "make": "UNDER 1 MIN TO MAKE HERE",
    }
    assert summarize(1, 1, 2, 4.7)["words"] == "1 WORD"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("book.pdf", "PDF support is coming soon. For now, Tomecast reads EPUB books."),
        ("book.docx", "DOCX support is coming soon. For now, Tomecast reads EPUB books."),
        ("book.TXT", "TXT support is coming soon. For now, Tomecast reads EPUB books."),
        ("book.xyz", "Tomecast can't read .xyz files. Choose an EPUB book."),
        ("missing.epub", "Couldn't find missing.epub."),
    ],
)
def test_books_that_cant_be_read_yet_explain_why(
    make_backend: MakeBackend, tmp_path: Path, name: str, message: str
) -> None:
    backend = make_backend()
    if name != "missing.epub":
        (tmp_path / name).write_bytes(b"something")

    backend.openBook(_url(tmp_path / name))

    assert backend.errorMessage == message
    assert backend.screen == "drop"
    assert backend.loading is False
    backend.dismissError()
    assert backend.errorMessage == ""


def test_a_broken_epub_shows_the_readers_error(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip file")
    backend = make_backend()

    backend.openBook(str(broken.as_uri()))
    qtbot.waitUntil(lambda: bool(backend.errorMessage))

    assert backend.errorMessage == "broken.epub isn't an EPUB: it's not a zip file."
    assert backend.loading is False
    assert backend.screen == "drop"


def test_the_file_dialog_opens_in_the_last_books_folder(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    assert backend.chooseBookFolder() == QUrl.fromLocalFile(str(Path.home())).toString()

    _open(qtbot, backend, _short_book(tmp_path))

    assert make_backend().chooseBookFolder() == _url(tmp_path).toString()


# --- Settings, theme and voice ---------------------------------------------------------------


def test_voice_speed_and_theme_are_saved_between_runs(make_backend: MakeBackend) -> None:
    first = make_backend()
    assert first.voice == "af_heart"
    assert first.voiceLabel == "HEART · AMERICAN · FEMALE"
    assert first.speed == 1.0
    assert first.themeSetting == "system"

    first.setVoice("bm_george")
    first.setSpeed(1.1)
    first.setThemeSetting("light")
    first.setVoice("zf_xiaobei")  # not offered: ignored
    first.setThemeSetting("purple")  # ignored

    second = make_backend()
    assert second.voice == "bm_george"
    assert second.voiceLabel == "GEORGE · BRITISH · MALE"
    assert second.speed == pytest.approx(1.1)
    assert second.themeSetting == "light"
    assert second.theme == "light"


@pytest.mark.parametrize(("value", "speed"), [(1.12, 1.1), (1.13, 1.15), (0.2, 0.75), (3, 1.25)])
def test_speed_snaps_to_the_meter(make_backend: MakeBackend, value: float, speed: float) -> None:
    backend = make_backend()
    backend.setSpeed(value)
    assert backend.speed == pytest.approx(speed)


def test_the_theme_setting_drives_the_image_urls(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _book_with_front_matter(tmp_path))
    backend.setThemeSetting("dark")
    changes: list[str] = []
    backend.themeChanged.connect(lambda: changes.append(backend.theme))

    backend.setThemeSetting("light")

    assert changes == ["light"]
    assert backend.artSource == "image://dots/cantoria/light"
    assert re.fullmatch(r"image://dots/cover/light\?\d+", backend.coverSource)
    backend.setThemeSetting("system")
    assert backend.theme in ("dark", "light")


def test_voice_groups_are_american_and_british_english(make_backend: MakeBackend) -> None:
    groups = make_backend().voiceGroups

    assert [group["label"] for group in groups] == [
        "AMERICAN · FEMALE", "AMERICAN · MALE", "BRITISH · FEMALE", "BRITISH · MALE"
    ]  # fmt: skip
    assert groups[0]["voices"][0] == {"id": "af_heart", "name": "Heart"}
    assert [voice["name"] for voice in groups[3]["voices"]] == ["George", "Lewis", "Daniel",
                                                                 "Fable"]  # fmt: skip
    assert sum(len(group["voices"]) for group in groups) == 28


def test_reduce_motion_and_the_hardware_label(
    make_backend: MakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOMECAST_REDUCE_MOTION", "1")
    monkeypatch.setattr(backend_module, "default_threads", lambda: 8)

    backend = make_backend()

    assert backend.reduceMotion is True
    assert backend.hardwareLabel == "CPU · 8 THREADS"


# --- Converting ------------------------------------------------------------------------------


def test_a_whole_conversion_ends_on_the_done_screen(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = Engines()
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    labels: list[str] = []
    times: list[str] = []
    backend.chapterLabelChanged.connect(lambda: labels.append(backend.chapterLabel))
    backend.timeLeftChanged.connect(lambda: times.append(backend.timeLeft))

    _convert(qtbot, backend)

    assert backend.errorMessage == ""
    assert backend.screen == "done"
    output = tmp_path / "Short Book.m4b"
    assert output.is_file()
    assert backend.outputPath == str(output)
    assert re.fullmatch(r"0:0\d:\d\d", backend.audioLength)
    assert re.fullmatch(r"OF AUDIO · 2 CHAPTERS · <1 MB · MADE IN \d+ SEC", backend.doneSummary)
    assert backend.warnings == []
    assert backend.chunksDone == backend.chunksTotal > 5
    assert backend.progressLabel == f"{backend.chunksTotal} / {backend.chunksTotal} PIECES · 100%"
    assert labels[0] == "[ 01 ] THE NIGHT"
    assert labels[-1] == "[ 02 ] CHAPTER 2"
    assert any(re.fullmatch(r"\d\d:\d\d:\d\d", time) for time in times)
    assert re.fullmatch(r"[\d,]+\.\d\u00d7 REAL TIME", backend.speedLabel)
    assert re.fullmatch(r"CPU · ABOUT [\d,]+\.\d\u00d7 REAL TIME", backend.hardwareLabel)
    assert engines.made[0].closed
    assert engines.made[0].thread_id not in (None, threading.main_thread().ident)
    assert not list((tmp_path / "data" / "cache").glob("convert-*"))  # cleaned up

    opened: list[QUrl] = []

    class FakeDesktop:
        @staticmethod
        def openUrl(url: QUrl) -> bool:
            opened.append(url)
            return True

    monkeypatch.setattr(backend_module, "QDesktopServices", FakeDesktop)
    backend.openOutputFolder()
    assert opened == [_url(tmp_path)]

    backend.convertAnother()
    assert backend.screen == "drop"
    assert backend.bookLoaded is False
    assert backend.chapters.rowCount() == 0
    assert backend.voice == "af_heart"


def test_only_the_ticked_chapters_are_converted(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.setChapterSelected(0, False)

    _convert(qtbot, backend)

    assert backend.screen == "done"
    assert engines.texts == ["Chapter 2", "Morning came.", "The birds sang.",
                             "Everyone woke up at last."]  # fmt: skip
    assert backend.doneSummary.startswith("OF AUDIO · 1 CHAPTER · ")


def test_the_selection_estimate_uses_the_measured_speed(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend._speedup is None

    _convert(qtbot, backend)

    speedup = backend._speedup
    assert speedup is not None and speedup > 0
    assert backend.selectionSummary == backend.chapters.summary(speedup)


def test_pausing_holds_the_conversion_until_resumed(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.block_at = 1
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(lambda: engines.blocked.is_set() and backend.stage == "voicing")
    assert backend.nowSpeaking == "Chapter 1: The Night"

    backend.pauseConversion()
    engines.release.set()  # the piece being voiced finishes…
    control = _control(backend)
    qtbot.waitUntil(lambda: control.holding and backend.chunksDone == 1)

    assert backend.paused is True
    assert len(engines.texts) == 1  # …and nothing more is voiced while paused
    total = backend.chunksTotal
    assert backend.progressLabel == f"1 / {total} PIECES · {100 // total}%"
    assert backend.timeLeft == "--:--:--"

    backend.resumeConversion()
    assert backend.paused is False
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)
    assert backend.chunksDone == backend.chunksTotal


def test_cancelling_keeps_the_work_so_starting_again_resumes(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.block_at = 3
    backend = make_backend(engines)
    book_path = _short_book(tmp_path)
    _open(qtbot, backend, book_path)
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(engines.blocked.is_set)

    backend.cancelConversion()

    assert backend.screen == "voice"
    assert backend.errorMessage == STOPPED_MESSAGE
    assert backend.paused is False
    engines.release.set()  # the piece in progress finishes, then the conversion stops
    qtbot.waitUntil(lambda: bool(engines.made) and engines.made[0].closed)
    assert len(engines.texts) == 3

    backend.startConversion()
    assert backend.errorMessage == ""
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)

    book = backend._book
    assert book is not None
    assert len(engines.texts) == _speakable_chunks(book)  # nothing was voiced twice
    assert len(set(engines.texts)) == len(engines.texts)
    assert len(engines.made) == 2
    assert (tmp_path / "Short Book.m4b").is_file()
    assert not (tmp_path / "Short Book (2).m4b").exists()


@pytest.mark.parametrize("failure", ["start", "synthesize"])
def test_engine_errors_show_in_the_banner(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, failure: str
) -> None:
    engines = Engines()
    if failure == "start":
        engines.start_error = "the voice engine stopped while starting:\nmodel missing"
    else:
        engines.fail_from = 4
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "voice"
    assert backend.paused is False
    if failure == "start":
        assert backend.errorMessage == "The voice engine stopped while starting:\nmodel missing."
    else:
        assert backend.errorMessage.startswith("No usable audio for c000-p0001-s002-k00")
        assert backend.errorMessage.endswith(
            "The audio made so far is kept, so starting again continues from there."
        )
    assert engines.made[0].thread_id is not None
    assert not list(tmp_path.glob("*.m4b"))


def test_an_unsupported_system_is_reported(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    def check() -> None:
        raise EngineError("the Kokoro voice engine needs glibc 2.43 or newer")

    engines = Engines()
    backend = make_backend(engines, system_check=check)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "voice"
    assert backend.errorMessage == "The Kokoro voice engine needs glibc 2.43 or newer."
    assert engines.made == []


def test_an_existing_audiobook_is_never_overwritten(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    existing = tmp_path / "Short Book.m4b"
    existing.write_bytes(b"an earlier audiobook")
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.outputPath == str(tmp_path / "Short Book (2).m4b")
    assert existing.read_bytes() == b"an earlier audiobook"


def test_output_names_are_safe_and_unique(tmp_path: Path) -> None:
    assert safe_file_name("Either/Or: A Fragment\nof Life") == "Either-Or: A Fragment of Life"
    assert safe_file_name("...Hidden") == "Hidden"
    assert len(safe_file_name("é" * 300).encode()) <= 200
    (tmp_path / "Book.m4b").write_bytes(b"")
    (tmp_path / "Book (2).m4b").write_bytes(b"")

    assert unique_output(tmp_path, "Book") == tmp_path / "Book (3).m4b"
    assert unique_output(tmp_path, "/") == tmp_path / "-.m4b"
    assert unique_output(tmp_path, "  ") == tmp_path / "Audiobook.m4b"


def test_missing_engine_files_are_downloaded_first(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    source = tmp_path / "source" / "model.bin"
    source.parent.mkdir()
    source.write_bytes(b"model weights")
    asset = Asset(
        name="Test voice model",
        url=source.as_uri(),
        sha256=hashlib.sha256(b"model weights").hexdigest(),
        size=13,
        path="models/model.bin",
    )
    backend = make_backend(assets=(asset,))
    _open(qtbot, backend, _short_book(tmp_path))
    stages: list[str] = []
    labels: list[str] = []
    backend.stageChanged.connect(lambda: stages.append(backend.stage))
    backend.downloadLabelChanged.connect(lambda: labels.append(backend.downloadLabel))

    _convert(qtbot, backend)

    assert backend.screen == "done", backend.errorMessage
    assert stages[:2] == ["downloading", "starting"]
    assert stages[-2:] == ["voicing", "packaging"]
    assert labels[0] == "DOWNLOADING ONCE · TEST VOICE MODEL · 0% OF <1 MB"
    assert labels[-1] == "DOWNLOADING ONCE · TEST VOICE MODEL · 100% OF <1 MB"
    assert is_installed(asset, tmp_path / "data")


# --- Preview and shutting down ---------------------------------------------------------------


def test_previews_need_an_audio_player(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, player=None)
    _open(qtbot, backend, _short_book(tmp_path))

    assert backend.previewAvailable is False
    backend.togglePreview()

    assert backend.previewPlaying is False
    assert engines.made == []


def test_a_preview_is_voiced_once_and_the_engine_stops_at_shutdown(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    player = shutil.which("true")
    if player is None:
        pytest.skip("needs the `true` command as a stand-in audio player")
    engines = Engines()
    backend = make_backend(engines, player=player)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    assert backend.previewAvailable is True
    assert backend.previewText == "It was a dark night."

    backend.togglePreview()
    assert backend.previewPlaying is True
    qtbot.waitUntil(lambda: not backend.previewPlaying)
    assert backend.errorMessage == ""
    assert engines.texts == ["It was a dark night."]

    backend.togglePreview()  # the same voice, speed and text: played from the cache
    qtbot.waitUntil(lambda: not backend.previewPlaying)
    backend.setVoice("am_adam")
    backend.togglePreview()  # another voice: the running engine voices it
    qtbot.waitUntil(lambda: not backend.previewPlaying)

    assert engines.texts == ["It was a dark night.", "It was a dark night."]
    assert len(engines.made) == 1
    engine = engines.made[0]
    assert engine.started and not engine.closed  # kept running between previews
    assert engine.voice == "am_adam"

    backend.shutdown()

    assert engine.closed
    assert backend.worker_thread.isFinished()


def test_shutdown_cancels_a_running_conversion_and_joins_the_thread(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.block_at = 2
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(engines.blocked.is_set)
    control = _control(backend)

    engines.release.set()
    backend.shutdown()

    assert control.cancelled
    assert backend.worker_thread.isFinished()
    assert engines.made[0].closed
    assert len(engines.texts) == 2
    assert not list(tmp_path.glob("*.m4b"))
    backend.shutdown()  # a second call does nothing
