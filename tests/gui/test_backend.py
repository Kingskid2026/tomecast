"""The window's Backend, driven directly (no QML), with a fake voice engine."""

from __future__ import annotations

import array
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import wave
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings, QUrl
from pytestqt.qtbot import QtBot

from tomecast import convert as convert_module
from tomecast import machine, packaging, pronunciations
from tomecast.audio import wav_info
from tomecast.book import Book, Chapter
from tomecast.cleanup import is_numbered_chapter
from tomecast.convert import plan_chunks
from tomecast.downloads import (
    UPDATE_FAILED_NOTE,
    Asset,
    build_in_use,
    failure,
    is_installed,
    mark_verified,
    waiting_check,
)
from tomecast.engines import ming
from tomecast.engines.audiocpp import (
    AUDIOCPP_CPU,
    AUDIOCPP_VULKAN,
    DEFAULT_VOICE,
    ESPEAK_NG,
    KOKORO_Q8,
    OUT_OF_MEMORY_NOTE,
    RAN_OUT_OF_MEMORY_NOTE,
    EngineError,
    split_speed,
)
from tomecast.gui import backend as backend_module
from tomecast.gui import fixes as fixes_module
from tomecast.gui.backend import STOPPED_MESSAGE, Backend, safe_file_name, unique_output
from tomecast.gui.models import DEFAULT_SPEEDUP, ChapterModel, display_title, summarize
from tomecast.gui.worker import BookRead, SelfCheckFor, TimedControl, preview_file
from tomecast.hardware import GpuDevice, short_name
from tomecast.manifest import MING_Q8
from tomecast.names import Name
from tomecast.speed import Speeds
from tomecast.textprep import TextPrep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # for epub_factory, pdf_factory
from epub_factory import NavPoint, Page, build_epub, tiny_png
from format_factory import book_in_format
from machine_factory import ON_BATTERY, PLUGGED_IN, pin_machine
from pdf_factory import SAMPLE_CHAPTERS, build_sample_book, plain

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
        self.gpu: str = "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"  # a use_gpu run
        self.gpu_warning: str | None = None  # set: a use_gpu run fell back to the CPU
        # set: a use_gpu run starts on the card and runs out of memory on the Nth synthesize
        # call (counting every engine's), then carries on on the CPU, as AutoKokoro does
        self.oom_at: int | None = None
        self.oom_at_start = False  # set: a use_gpu run runs out of memory as it starts
        self.fail_once_at: int | None = None  # the Nth synthesize call fails, once
        self.on_card: list[str] = []  # what was voiced on the graphics card
        self.before_start: Callable[[], None] | None = None  # runs as an engine starts
        self.after_close: Callable[[], None] | None = None  # runs once an engine has stopped

    def __call__(self, root: Path, voice: str, speed: float, use_gpu: bool = False) -> FakeEngine:
        engine = FakeEngine(self, voice, speed, use_gpu)
        self.made.append(engine)
        return engine


class FakeEngine:
    label = "Fake voice"
    max_chars = 60
    sample_rate = SAMPLE_RATE

    def __init__(self, engines: Engines, voice: str, speed: float, use_gpu: bool = False) -> None:
        self._engines = engines
        self.voice = voice
        self.speed = speed
        self.use_gpu = use_gpu
        self.device_label = "CPU"
        self.gpu_description = ""
        self.warning: str | None = None
        self.fell_back = False
        self.out_of_memory = False
        self._threads = machine.voice_threads()  # read when it's made, as AutoKokoro does
        self.started = False
        self.closed = False
        self.thread_id: int | None = None  # the OS thread that started it
        self.pinned: dict[str, Path] | None = None  # the builds the run was told to use

    def pin_builds(self, builds: Mapping[str, Path]) -> None:
        self.pinned = dict(builds)

    @property
    def threads(self) -> int:
        return self._threads

    @property
    def voice_speed(self) -> float:
        return split_speed(self.speed)[0]

    @property
    def tempo(self) -> float:
        return split_speed(self.speed)[1]

    def start(self) -> None:
        self.thread_id = threading.get_ident()
        if self._engines.before_start:
            self._engines.before_start()
        if self._engines.start_error:
            raise EngineError(self._engines.start_error)
        if self.use_gpu and self._engines.oom_at_start:
            self.warning = OUT_OF_MEMORY_NOTE
            self.fell_back = self.out_of_memory = True
        elif self.use_gpu and self._engines.gpu_warning:
            self.warning = self._engines.gpu_warning
            self.fell_back = True
        elif self.use_gpu and self._engines.gpu:
            self.gpu_description = self._engines.gpu
        self.started = True

    def close(self) -> None:
        assert threading.get_ident() == self.thread_id  # stopped on the thread that started it
        self.closed = True
        if self._engines.after_close:
            self._engines.after_close()

    def synthesize(self, text: str, output: Path) -> None:
        engines = self._engines
        engines.texts.append(text)
        count = len(engines.texts)
        if self.use_gpu and not self.fell_back and engines.oom_at == count:
            self.warning = RAN_OUT_OF_MEMORY_NOTE  # and it carries on on the CPU
            self.fell_back = self.out_of_memory = True
            self.gpu_description, self.device_label = "", "CPU"
        if self.use_gpu and not self.fell_back:
            engines.on_card.append(text)
        if engines.fail_once_at == count:
            raise RuntimeError("the voice engine failed (HTTP 500): something broke")
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
NO_SPEEDS = Speeds()  # nothing installed, so nothing measured


@pytest.fixture
def make_backend(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MakeBackend]:
    """Build Backends with a fake engine, no downloads, a temp data folder and settings file."""
    made: list[Backend] = []
    # Never run the real `gsettings` (it would read the developer's desktop settings).
    monkeypatch.setattr(
        backend_module,
        "system_reduce_motion",
        lambda: os.environ.get("TOMECAST_REDUCE_MOTION") == "1",
    )

    def make(
        engines: Engines | None = None,
        *,
        assets: tuple[Asset, ...] = (),
        player: str | None = None,
        system_check: Callable[[], None] = lambda: None,
        gpu: bool = False,  # pretend a graphics driver is installed
        scan: str = "",  # the card the start-up scan finds
        speeds: Speeds = NO_SPEEDS,  # what the start-up speed test finds
        engine_check: SelfCheckFor | None = None,
        probed: list[bool] | None = None,  # records each speed probe's "look at the card"
    ) -> Backend:
        def probe(root: Path, card: bool, cancelled: object, devices: object) -> Speeds:
            if probed is not None:
                probed.append(card)
            return speeds

        def scanner(root: Path, cancelled: object) -> list[GpuDevice]:
            return [GpuDevice(0, "Vulkan0", scan, "gpu", 7 * 2**30, 8 * 2**30)] if scan else []

        backend = Backend(
            engine_factory=engines if engines is not None else Engines(),
            gpu_check=lambda: gpu,
            device_scanner=scanner,
            speed_probe=probe,
            engine_check=engine_check,
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
    assert backend.formatsLabel == "EPUB · PDF · DOCX · FB2 · HTML · TXT"  # formats with a reader
    rows = _rows(backend.chapters)
    assert all("number" not in row for row in rows)  # no index tags: rows are listed by title
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
    assert rows[3]["title"] == "Down the Rabbit-Hole"
    assert rows[3]["wordsLabel"] == "2,181"
    assert rows[3]["minutesLabel"] == "14 min"
    assert [row["meterLit"] for row in rows] == [
        0, 0, 0, 14, 13, 11, 17, 14, 16, 14, 16, 14, 13, 12, 13
    ]  # fmt: skip
    assert model.sections[5].label == "A CAUCUS-RACE AND A LONG TALE"
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


def test_the_summary_follows_the_speed() -> None:
    # 26,371 words is 2 h 47 min at 1x. Audio shrinks with the speed; making it takes as
    # long as the voice's own audio (capped at 1.5x) divided by the measured speed.
    slow = summarize(26371, 12, 15, 4.7, 0.5)
    assert slow["audio"] == "ABOUT 5 H 34 MIN OF AUDIO"
    assert slow["make"] == "ABOUT 1 H 11 MIN TO MAKE HERE"
    assert summarize(26371, 12, 15, 4.7, 1.0)["make"] == "ABOUT 36 MIN TO MAKE HERE"
    assert summarize(26371, 12, 15, 4.7, 2.0)["audio"] == "ABOUT 1 H 23 MIN OF AUDIO"
    fast = summarize(26371, 12, 15, 4.7, 3.0)
    assert fast["audio"] == "ABOUT 56 MIN OF AUDIO"
    assert fast["make"] == summarize(26371, 12, 15, 4.7, 1.5)["make"] == "ABOUT 24 MIN TO MAKE HERE"


def test_changing_the_speed_updates_the_estimates(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    backend.openBook(_url(_book_with_front_matter(tmp_path)))
    qtbot.waitUntil(lambda: backend.screen == "chapters")
    seen: list[dict[str, str]] = []
    backend.selectionSummaryChanged.connect(lambda: seen.append(backend.selectionSummary))
    assert backend.selectionSummary["audio"] == "ABOUT 4 MIN OF AUDIO"  # 674 words at 1x

    backend.setSpeed(0.5)
    assert seen[-1] == backend.selectionSummary == backend.chapters.summary(4.7, 0.5)
    assert backend.selectionSummary["audio"] == "ABOUT 9 MIN OF AUDIO"
    backend.setSpeed(3.0)
    assert backend.selectionSummary["audio"] == "ABOUT 1 MIN OF AUDIO"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (
            "book.xyz",
            "Tomecast can't read .xyz files. It reads EPUB, PDF, DOCX, FB2, HTML and TXT books.",
        ),
        (
            "book",
            "Tomecast can't read files without an extension. It reads EPUB, PDF, DOCX, FB2, "
            "HTML and TXT books.",
        ),
        ("missing.epub", "Couldn't find missing.epub."),
        ("missing.pdf", "Couldn't find missing.pdf."),
    ],
)
def test_books_that_cant_be_read_explain_why(
    make_backend: MakeBackend, tmp_path: Path, name: str, message: str
) -> None:
    backend = make_backend()
    if not name.startswith("missing."):
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


@pytest.mark.parametrize("name", ["book.docx", "book.fb2", "book.txt", "book.html", "book.htm"])
def test_every_readable_format_opens_in_the_window(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, name: str
) -> None:
    path = book_in_format(tmp_path, name)
    backend = make_backend()

    _open(qtbot, backend, path)

    assert backend.bookLoaded is True
    assert [row["title"] for row in _rows(backend.chapters)] == ["Chapter 1", "Chapter 2"]


def test_a_book_in_a_format_the_reader_recognizes_opens_whatever_its_name(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    renamed = tmp_path / "short.txt"  # an EPUB, whatever its extension says
    renamed.write_bytes(_short_book(tmp_path).read_bytes())

    _open(qtbot, backend, renamed)

    assert backend.bookTitle == "Short Book"


def test_a_binary_file_named_txt_shows_the_readers_error(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    path = tmp_path / "picture.txt"
    path.write_bytes(bytes(range(256)) * 20)
    backend = make_backend()

    backend.openBook(_url(path))
    qtbot.waitUntil(lambda: bool(backend.errorMessage))

    assert backend.errorMessage == "picture.txt isn't a text file: it looks like binary data."
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
    first.setSpeed(2.25)
    first.setThemeSetting("light")
    first.setVoice("zf_xiaobei")  # not offered: ignored
    first.setThemeSetting("purple")  # ignored

    second = make_backend()
    assert second.voice == "bm_george"
    assert second.voiceLabel == "GEORGE · BRITISH · MALE"
    assert second.speed == pytest.approx(2.25)
    assert second.themeSetting == "light"
    assert second.theme == "light"


@pytest.mark.parametrize(
    ("value", "speed"), [(1.05, 1.0), (1.12, 1.0), (1.13, 1.25), (1.5, 1.5), (0.2, 0.5), (9, 3.0)]
)
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
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)

    backend = make_backend()

    assert backend.reduceMotion is True
    assert backend.hardwareLabel == "CPU · 8 THREADS"


def test_the_hardware_label_says_when_the_cpu_runs_on_battery(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "laptop", supplies=ON_BATTERY)

    backend = make_backend(speeds=Speeds(cpu=2.6))

    assert backend.hardwareLabel == "CPU · ON BATTERY · 4 THREADS"
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)
    assert backend.hardwareLabel == "CPU · ON BATTERY · ABOUT 2.6× REAL TIME"
    # Plugged in again: 2.6x was measured with 4 threads, and the voice now uses 8, so the
    # label and the estimate go back to the thread count until 8 threads are measured.
    pin_machine(monkeypatch, tmp_path / "plugged", supplies=PLUGGED_IN)
    backend.setUseGpu(not backend.useGpu)
    assert backend.hardwareLabel == "CPU · 8 THREADS"
    assert backend._speeds == {"CPU/4": 2.6}
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend.selectionSummary == backend.chapters.summary(DEFAULT_SPEEDUP)


def test_each_thread_count_keeps_its_own_cpu_speed(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "plugged", supplies=PLUGGED_IN)
    backend = make_backend(speeds=Speeds(cpu=4.7))
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend.selectionSummary == backend.chapters.summary(4.7)

    # Unplugged, then a conversion measures the CPU with 4 threads.
    pin_machine(monkeypatch, tmp_path / "laptop", supplies=ON_BATTERY)
    backend.goTo("drop")
    assert backend.hardwareLabel == "CPU · ON BATTERY · 4 THREADS"
    _open(qtbot, backend, _short_book(tmp_path))
    _convert(qtbot, backend)
    on_battery = backend._speeds["CPU/4"]
    assert backend._speeds["CPU/8"] == 4.7  # still there for when it's plugged in again
    assert backend.hardwareLabel == f"CPU · ON BATTERY · ABOUT {on_battery:.1f}× REAL TIME"

    pin_machine(monkeypatch, tmp_path / "plugged-again", supplies=PLUGGED_IN)
    backend.goTo("drop")
    assert backend.hardwareLabel == "CPU · ABOUT 4.7× REAL TIME"


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
    assert labels[0] == "THE NIGHT"
    assert labels[-1] == "CHAPTER 2"
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
    assert engines.texts == ["Chapter two", "Morning came.", "The birds sang.",
                             "Everyone woke up at last."]  # fmt: skip
    assert backend.doneSummary.startswith("OF AUDIO · 1 CHAPTER · ")


def test_the_selection_estimate_uses_the_measured_speed(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend._speeds == {}
    assert backend.selectionSummary == backend.chapters.summary(DEFAULT_SPEEDUP)

    _convert(qtbot, backend)

    speedup = backend._speeds[f"CPU/{machine.voice_threads()}"]
    assert speedup > 0
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
    assert backend.nowSpeaking == "Chapter one: The Night"

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
    assert safe_file_name("Either/Or: A Fragment\nof Life") == "Either-Or - A Fragment of Life"
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


@pytest.mark.parametrize("speed", [1.75, 2.0])
def test_a_fast_book_from_the_window_keeps_its_time_stretch(
    qtbot: QtBot,
    make_backend: MakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    speed: float,
) -> None:
    """Above 1.5x the voice speaks at 1.5x and the packager stretches the rest, as the command
    line does. The worker wraps the engine to measure it, and the wrapper must pass the speed
    split on, or the book comes out at 1.5x whatever was chosen."""
    packaged: list[float] = []
    write_m4b = packaging.write_m4b  # what convert_book calls

    def recording(*args: Any, tempo: float = 1.0, **kwargs: Any) -> Any:
        packaged.append(tempo)
        return write_m4b(*args, tempo=tempo, **kwargs)

    monkeypatch.setattr(convert_module, "write_m4b", recording)
    backend = make_backend(Engines())
    _open(qtbot, backend, _short_book(tmp_path))
    backend.setSpeed(speed)
    _convert(qtbot, backend)

    assert backend.screen == "done"
    voice_speed, tempo = split_speed(speed)
    assert voice_speed == 1.5
    assert packaged == [pytest.approx(tempo)]
    assert tempo > 1.0


def test_a_fast_preview_is_time_stretched_like_the_book(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    player = shutil.which("true")
    if player is None:
        pytest.skip("needs the `true` command as a stand-in audio player")
    engines = Engines()
    backend = make_backend(engines, player=player)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    text = backend.previewText
    seconds: dict[float, float] = {}
    for speed in (1.0, 3.0):  # 3x is voiced at 1.5x and stretched by 2
        backend.setSpeed(speed)
        backend.togglePreview()
        qtbot.waitUntil(lambda: not backend.previewPlaying)
        assert backend.errorMessage == ""
        assert backend._preview_cache is not None
        seconds[speed] = wav_info(
            preview_file(backend._preview_cache, "af_heart", speed, text)
        ).seconds
    assert seconds[3.0] == pytest.approx(seconds[1.0] / 2, rel=0.05)
    assert engines.texts == [text, text]  # the whole sentence was voiced both times


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


# --- Untitled sections ------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["", "   ", "\n"])
def test_a_section_without_a_title_is_called_untitled(title: str) -> None:
    assert display_title(title) == "Untitled section"
    model = ChapterModel()
    book = Book("B", (), None, (Chapter(title, ("Some words here.",)), Chapter("Named", ("x",))))
    model.set_book(book, skip_front_matter=False)
    assert [row["title"] for row in _rows(model)] == ["Untitled section", "Named"]
    assert model.sections[0].label == "UNTITLED SECTION"  # the converting screen's label


# --- Pause and cancel by stage -----------------------------------------------------------------


class SlowPackaging:
    """Wraps ``convert.write_m4b`` so a test can act while the audiobook is being packaged."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tomecast.packaging import write_m4b as real

        self.entered = threading.Event()
        self.release = threading.Event()
        self.before: Callable[[], None] | None = None  # runs just before packaging

        def slow(*args: object, **kwargs: object) -> object:
            if self.before is not None:
                self.before()
            self.entered.set()
            self.release.wait(timeout=10)
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("tomecast.convert.write_m4b", slow)


def test_cancel_and_pause_do_nothing_while_packaging(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaging = SlowPackaging(monkeypatch)
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(lambda: backend.stage == "packaging")

    backend.cancelConversion()
    backend.pauseConversion()

    assert backend.screen == "converting"  # not "Stopped"
    assert backend.errorMessage == ""
    assert backend.paused is False
    packaging.release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)
    assert backend.errorMessage == ""
    assert (tmp_path / "Short Book.m4b").is_file()  # and the book really was written


def test_a_cancel_that_arrives_after_packaging_began_is_refused(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window still shows "voicing" (the stage message is on its way), but the worker has
    already committed to packaging: Cancel must not claim to have stopped anything."""
    packaging = SlowPackaging(monkeypatch)
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(packaging.entered.is_set)
    backend._update("stage", "voicing")  # the window hasn't heard about packaging yet

    backend.cancelConversion()
    backend.pauseConversion()

    assert backend.screen == "converting"
    assert backend.errorMessage == ""
    assert backend.paused is False
    packaging.release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)
    assert (tmp_path / "Short Book.m4b").is_file()


def test_cancelling_on_the_last_piece_writes_nothing(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    total = sum(1 for _ in _speakable_texts(backend))
    engines.block_at = total
    backend.startConversion()
    qtbot.waitUntil(engines.blocked.is_set)

    backend.cancelConversion()
    engines.release.set()  # the last piece finishes; the checkpoint before packaging stops it
    qtbot.waitUntil(lambda: bool(engines.made) and engines.made[0].closed)

    assert backend.screen == "voice"
    assert backend.errorMessage == STOPPED_MESSAGE
    assert not list(tmp_path.glob("*.m4b"))
    assert not list(tmp_path.glob(".*.partial.m4b"))


def _speakable_texts(backend: Backend) -> list[str]:
    book = backend._book
    assert book is not None
    return [
        chunk.text
        for chunks in plan_chunks(book, FakeEngine.max_chars)
        for chunk in chunks
        if chunk.speakable
    ]


def test_pause_works_only_while_voicing(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    entered = threading.Event()
    from tomecast.downloads import ensure as real_ensure

    def slow_ensure(*args: object, **kwargs: object) -> object:
        entered.set()
        release.wait(timeout=10)
        return real_ensure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("tomecast.downloads.ensure", slow_ensure)
    asset = _test_asset(tmp_path, "model.bin", b"model weights")
    backend = make_backend(assets=(asset,))
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(lambda: entered.is_set() and backend.stage == "downloading")

    backend.pauseConversion()

    assert backend.paused is False  # nothing to pause between pieces yet
    assert _control(backend).paused is False
    release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)


# --- A book finishing its read during a conversion -----------------------------------------------


def test_start_waits_while_another_book_is_being_read(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    other = _book_with_front_matter(tmp_path)

    backend.openBook(_url(other))
    assert backend.loading is True
    backend.startConversion()

    assert backend.screen == "voice"  # Start did nothing: the read would replace this book
    assert backend._control is None

    qtbot.waitUntil(lambda: backend.screen == "chapters")
    assert backend.bookTitle == "Test Book"
    assert engines.made == []


def test_a_book_read_finishing_mid_conversion_is_dropped(
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

    # A read that was still running when Start was pressed now finishes.
    stranger = Book("Stranger", ("Z",), None, (Chapter("Elsewhere", ("Not this one.",)),))
    backend._read_job = 99
    backend._update("loading", True)
    backend._worker.bookRead.emit(99, BookRead(stranger))
    qtbot.waitUntil(lambda: not backend.loading)

    assert backend.screen == "converting"  # the conversion is not stranded…
    assert backend.bookTitle == "Short Book"  # …and the other book was not taken over
    assert backend.chapters.rowCount() == 2
    assert backend._control is control

    backend.cancelConversion()  # still cancellable
    assert backend.screen == "voice"
    assert backend.errorMessage == STOPPED_MESSAGE
    assert control.cancelled
    engines.release.set()
    qtbot.waitUntil(lambda: bool(engines.made) and engines.made[0].closed)


def test_shutdown_after_a_dropped_read_does_not_wait_for_the_conversion(
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
    backend._read_job = 99
    backend._worker.bookRead.emit(99, BookRead(Book("Stranger", ("Z",), None, ())))

    engines.release.set()
    backend.shutdown()

    assert control.cancelled
    assert backend.worker_thread.isFinished()
    assert len(engines.texts) == 2  # it stopped after the piece in progress, not at the end
    assert not list(tmp_path.glob("*.m4b"))


# --- Chapter ticks and resuming -------------------------------------------------------------------


def test_untick_after_a_cancel_keeps_the_chunks_already_voiced(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    """Chunk IDs use the book's own chapter numbers, so unticking an earlier section between
    two runs doesn't make the later ones voice again."""
    engines = Engines()
    engines.block_at = 7  # the 7th piece is chapter 2's first sentence
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(engines.blocked.is_set)
    backend.cancelConversion()
    engines.release.set()
    qtbot.waitUntil(lambda: bool(engines.made) and engines.made[0].closed)
    first_run = list(engines.texts)
    assert first_run[-1] == "Morning came."

    backend.goTo("chapters")
    backend.setChapterSelected(0, False)
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)

    second_run = engines.texts[len(first_run) :]
    assert second_run == ["The birds sang.", "Everyone woke up at last."]
    book = backend._book
    assert book is not None
    chapter_two = [c.text for c in plan_chunks(book, FakeEngine.max_chars, [1])[0]]
    assert chapter_two == ["Chapter two", "Morning came.", "The birds sang.",
                           "Everyone woke up at last."]  # fmt: skip
    # Every sentence of the ticked section was voiced, once, across the two runs.
    assert [t for t in engines.texts if t in chapter_two] == chapter_two
    assert backend.doneSummary.startswith("OF AUDIO · 1 CHAPTER · ")
    assert (tmp_path / "Short Book.m4b").is_file()


def test_the_window_passes_the_whole_book_and_the_ticked_indices(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.block_at = 1
    backend = make_backend(engines)
    _open(qtbot, backend, _book_with_front_matter(tmp_path))  # rows 2, 3, 4 are ticked
    jobs: list[object] = []
    backend._jobRequested.connect(jobs.append)

    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(engines.blocked.is_set)

    job = jobs[-1]
    assert job.chapters == (2, 3, 4)  # type: ignore[attr-defined]
    assert len(job.book.chapters) == 5  # type: ignore[attr-defined]
    engines.release.set()
    backend.cancelConversion()


# --- Never overwriting, even later ---------------------------------------------------------------


def test_a_file_that_appears_while_converting_is_not_overwritten(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = tmp_path / "Short Book.m4b"
    packaging = SlowPackaging(monkeypatch)

    def someone_saves_a_file() -> None:
        taken.write_bytes(b"saved after Start was pressed")

    packaging.before = someone_saves_a_file
    packaging.release.set()
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    assert not taken.exists()  # free when Start is pressed

    _convert(qtbot, backend)

    assert backend.screen == "done", backend.errorMessage
    assert backend.outputPath == str(tmp_path / "Short Book (2).m4b")
    assert (tmp_path / "Short Book (2).m4b").is_file()
    assert taken.read_bytes() == b"saved after Start was pressed"
    assert backend.doneSummary.startswith("OF AUDIO · 2 CHAPTERS")


# --- MP3 output ---------------------------------------------------------------------------


def test_the_output_format_is_remembered(make_backend: MakeBackend, tmp_path: Path) -> None:
    backend = make_backend()
    assert backend.outputFormat == "m4b"
    changes: list[str] = []
    backend.outputFormatChanged.connect(lambda: changes.append(backend.outputFormat))

    backend.setOutputFormat("mp3")
    backend.setOutputFormat("mp3")  # no change, no signal
    backend.setOutputFormat("wav")  # not a format: ignored

    assert changes == ["mp3"]
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    assert settings.value("outputFormat") == "mp3"
    assert make_backend().outputFormat == "mp3"  # the next window remembers


def test_a_bad_saved_format_falls_back_to_m4b(tmp_path: Path, make_backend: MakeBackend) -> None:
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("outputFormat", "ogg")
    settings.sync()
    assert make_backend().outputFormat == "m4b"


def test_unique_output_names_a_folder_for_mp3(tmp_path: Path) -> None:
    assert unique_output(tmp_path, "Book", "mp3") == tmp_path / "Book"
    (tmp_path / "Book").mkdir()
    (tmp_path / "Book.m4b").write_bytes(b"")  # a different kind of output: no clash
    assert unique_output(tmp_path, "Book", "mp3") == tmp_path / "Book (2)"
    assert unique_output(tmp_path, "Book") == tmp_path / "Book (2).m4b"


def test_converting_to_mp3_writes_a_folder_and_opens_it(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make_backend()
    backend.setOutputFormat("mp3")
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.errorMessage == ""
    assert backend.screen == "done"
    folder = tmp_path / "Short Book"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Chapter 1 - The Night.mp3",
        "02 - Chapter 2.mp3",
    ]
    assert not list(tmp_path.glob("*.m4b"))
    assert backend.outputPath == str(folder)
    assert re.fullmatch(r"OF AUDIO · 2 MP3 FILES · <1 MB · MADE IN \d+ SEC", backend.doneSummary)

    opened: list[QUrl] = []

    class FakeDesktop:
        @staticmethod
        def openUrl(url: QUrl) -> bool:
            opened.append(url)
            return True

    monkeypatch.setattr(backend_module, "QDesktopServices", FakeDesktop)
    backend.openOutputFolder()
    assert opened == [_url(folder)]  # the folder itself, not the one holding it


def test_one_mp3_chapter_is_one_file(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    backend.setOutputFormat("mp3")
    _open(qtbot, backend, _short_book(tmp_path))
    backend.setChapterSelected(0, False)

    _convert(qtbot, backend)

    assert backend.doneSummary.startswith("OF AUDIO · 1 MP3 FILE · ")


# --- What Start will download -------------------------------------------------------------


def _test_asset(folder: Path, name: str, content: bytes, size: int | None = None) -> Asset:
    source = folder / "source" / name
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(content)
    return Asset(
        name=f"Test {name}",
        url=source.as_uri(),
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content) if size is None else size,
        path=f"models/{name}",
    )


def _install(asset: Asset, root: Path, content: bytes, *, verified: bool = True) -> None:
    """Put ``asset`` in place as a download does: the file, plus its verified marker (which a
    damaged file, ``verified=False``, never gets)."""
    target = root / asset.path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    if verified:
        mark_verified(asset, root)


def test_download_note_says_what_start_will_download(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engine_asset = _test_asset(tmp_path, "engine.bin", b"engine", size=1_500_000)
    voice_asset = _test_asset(tmp_path, "voice.bin", b"voice", size=270_000_001)
    backend = make_backend(assets=(engine_asset, voice_asset))
    _open(qtbot, backend, _short_book(tmp_path))
    notes: list[str] = []
    backend.downloadNoteChanged.connect(lambda: notes.append(backend.downloadNote))

    backend.goTo("voice")  # nothing installed: both are missing (1.5 + 270.000001, rounded up)
    assert backend.downloadNote == "STARTING DOWNLOADS THE VOICE ONCE · 272 MB"

    backend.goTo("chapters")
    _install(engine_asset, tmp_path / "data", b"engine")
    backend.goTo("voice")  # only the missing voice counts
    assert backend.downloadNote == "STARTING DOWNLOADS THE VOICE ONCE · 271 MB"

    backend.goTo("chapters")
    _install(voice_asset, tmp_path / "data", b"a damaged file", verified=False)  # still missing
    backend.goTo("voice")
    assert backend.downloadNote == "STARTING DOWNLOADS THE VOICE ONCE · 271 MB"

    backend.goTo("chapters")
    _install(voice_asset, tmp_path / "data", b"voice")
    backend.goTo("voice")  # everything is installed
    assert backend.downloadNote == ""
    assert notes == [
        "STARTING DOWNLOADS THE VOICE ONCE · 272 MB",
        "STARTING DOWNLOADS THE VOICE ONCE · 271 MB",
        "",
    ]


def test_download_note_clears_after_the_downloads_finish(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset = _test_asset(tmp_path, "model.bin", b"model weights", size=80_000_000)
    backend = make_backend(assets=(asset,))
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    assert backend.downloadNote == "STARTING DOWNLOADS THE VOICE ONCE · 80 MB"
    notes: list[str] = []
    backend.downloadNoteChanged.connect(lambda: notes.append(backend.downloadNote))

    backend.startConversion()
    qtbot.waitUntil(lambda: backend.stage not in ("downloading", "starting") or bool(notes))

    assert notes == [""]  # cleared once the files were in, while the conversion carries on
    assert backend.downloadNote == ""
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)
    assert backend.downloadNote == ""


def test_download_note_is_back_after_a_failed_download(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset = _test_asset(tmp_path, "model.bin", b"model weights", size=2_000_000)
    (tmp_path / "source" / "model.bin").write_bytes(b"changed on the server")  # bad checksum
    backend = make_backend(assets=(asset,))
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")

    backend.startConversion()
    qtbot.waitUntil(lambda: backend.screen == "voice" and bool(backend.errorMessage))

    assert backend.downloadNote == "STARTING DOWNLOADS THE VOICE ONCE · 2 MB"


def test_no_assets_means_no_note(qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path) -> None:
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    assert backend.downloadNote == ""


# --- The graphics card -----------------------------------------------------------------------

NVIDIA = "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"


def test_no_graphics_driver_means_no_gpu_row(make_backend: MakeBackend) -> None:
    backend = make_backend(gpu=False)
    assert backend.gpuAvailable is False
    assert backend.useGpu is True  # the saved choice defaults to on, it just isn't offered
    assert backend.gpuName == ""
    assert backend.hardwareLabel.startswith("CPU · ")


def test_the_graphics_card_is_on_by_default_and_named_once_found(
    qtbot: QtBot, make_backend: MakeBackend
) -> None:
    backend = make_backend(gpu=True, scan=NVIDIA)
    assert backend.gpuAvailable is True
    assert backend.useGpu is True
    names: list[str] = []
    backend.gpuNameChanged.connect(lambda: names.append(backend.gpuName))

    qtbot.waitUntil(lambda: backend.gpuName != "")

    assert backend.gpuName == NVIDIA
    assert names == [NVIDIA]
    assert backend.hardwareLabel == "NVIDIA RTX PRO 1000"


def test_the_start_up_scan_only_runs_when_a_driver_is_there(
    qtbot: QtBot, make_backend: MakeBackend
) -> None:
    backend = make_backend(gpu=False, scan=NVIDIA)
    qtbot.wait(100)
    assert backend.gpuName == ""


def test_the_graphics_card_choice_is_saved(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend(gpu=True)
    changes: list[bool] = []
    backend.useGpuChanged.connect(lambda: changes.append(backend.useGpu))

    backend.setUseGpu(False)
    backend.setUseGpu(False)  # no change, no signal

    assert backend.useGpu is False
    assert changes == [False]
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    assert settings.value("useGpu") in (False, "false")
    assert make_backend(gpu=True).useGpu is False  # the next window remembers


def test_turning_the_card_off_shows_the_cpu_label(qtbot: QtBot, make_backend: MakeBackend) -> None:
    backend = make_backend(gpu=True, scan=NVIDIA)
    qtbot.waitUntil(lambda: backend.hardwareLabel == "NVIDIA RTX PRO 1000")

    backend.setUseGpu(False)
    assert re.fullmatch(r"CPU · \d+ THREADS?", backend.hardwareLabel)
    backend.setUseGpu(True)
    assert backend.hardwareLabel == "NVIDIA RTX PRO 1000"


# What a fresh install downloads for each choice: one engine build, plus eSpeak and Kokoro.
GPU_DOWNLOADS = (AUDIOCPP_VULKAN, ESPEAK_NG, KOKORO_Q8)
CPU_DOWNLOADS = (AUDIOCPP_CPU, ESPEAK_NG, KOKORO_Q8)


def _note_for(assets: tuple[Asset, ...]) -> str:
    """The voice screen's note for downloading ``assets``, from their pinned sizes (rounded up
    to whole MB, as test_download_note_says_what_start_will_download checks)."""
    mb = math.ceil(sum(asset.size for asset in assets) / 1_000_000)
    return f"STARTING DOWNLOADS THE VOICE ONCE · {mb:,} MB"


def test_the_download_note_follows_the_engine_the_choice_needs(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    # The two engine builds differ in size, so the note tells them apart.
    assert _note_for(GPU_DOWNLOADS) != _note_for(CPU_DOWNLOADS)
    backend = make_backend(gpu=True, assets=None)
    _open(qtbot, backend, _short_book(tmp_path))

    backend.goTo("voice")  # nothing installed
    assert backend.downloadNote == _note_for(GPU_DOWNLOADS)
    backend.setUseGpu(False)
    assert backend.downloadNote == _note_for(CPU_DOWNLOADS)
    backend.setUseGpu(True)
    assert backend.downloadNote == _note_for(GPU_DOWNLOADS)


def test_without_a_driver_the_cpu_engine_is_what_gets_downloaded(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend(gpu=False, assets=None)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    assert backend.downloadNote == _note_for(CPU_DOWNLOADS)


def test_an_installed_engine_build_is_not_downloaded_again(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    _fake_build(AUDIOCPP_VULKAN, tmp_path / "data")
    backend = make_backend(gpu=True, assets=None)
    _open(qtbot, backend, _short_book(tmp_path))

    backend.goTo("voice")  # the Vulkan build is there; eSpeak and Kokoro aren't
    assert backend.downloadNote == _note_for((ESPEAK_NG, KOKORO_Q8))
    backend.setUseGpu(False)  # the CPU build is a separate download
    assert backend.downloadNote == _note_for(CPU_DOWNLOADS)


def _fake_build(asset: Asset, root: Path, sha256: str | None = None) -> Path:
    """Put an extracted build of ``asset`` (the pinned one, or another) where it goes."""
    sha = sha256 or asset.sha256
    folder = root / asset.path / sha[:12]
    folder.mkdir(parents=True)
    (folder / ".tomecast-sha256").write_text(sha + "\n")
    return folder


def test_an_engine_update_says_so_in_the_download_note(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    root = tmp_path / "data"
    # Tomecast 0.1.x left upstream's Vulkan build here; eSpeak and Kokoro are current.
    legacy = root / "engines" / "audiocpp-0.9.4-vulkan"
    legacy.mkdir(parents=True)
    (legacy / ".tomecast-sha256").write_text("e" * 64 + "\n")
    _fake_build(ESPEAK_NG, root)
    model = root / KOKORO_Q8.path
    model.parent.mkdir(parents=True)
    model.write_bytes(b"")
    mark_verified(KOKORO_Q8, root)  # as if downloaded (or hashed) already
    backend = make_backend(gpu=True, assets=None)
    _open(qtbot, backend, _short_book(tmp_path))

    backend.goTo("voice")

    mb = math.ceil(AUDIOCPP_VULKAN.size / 1_000_000)
    assert backend.downloadNote == f"STARTING DOWNLOADS A VOICE ENGINE UPDATE · {mb} MB"


def test_a_conversion_on_the_card_reports_its_name_and_speed(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, gpu=True)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.errorMessage == ""
    assert [engine.use_gpu for engine in engines.made] == [True]
    assert backend.gpuName == NVIDIA
    assert re.fullmatch(r"NVIDIA RTX PRO 1000 · ABOUT [\d,]+\.\d× REAL TIME", backend.hardwareLabel)
    assert backend.warnings == []


def test_a_conversion_with_the_card_off_uses_the_cpu(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, gpu=True)
    backend.setUseGpu(False)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert [engine.use_gpu for engine in engines.made] == [False]
    assert re.fullmatch(r"CPU · ABOUT [\d,]+\.\d× REAL TIME", backend.hardwareLabel)


def test_without_a_driver_the_engine_is_never_asked_for_the_card(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, gpu=False)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert [engine.use_gpu for engine in engines.made] == [False]


def test_a_card_that_did_not_work_is_a_warning_and_the_cpu_label(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.gpu_warning = "The graphics card didn't work, so Tomecast used the CPU."
    backend = make_backend(engines, gpu=True, scan=NVIDIA)
    qtbot.waitUntil(lambda: backend.gpuName != "")
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"
    assert backend.warnings == ["The graphics card didn't work, so Tomecast used the CPU."]
    assert backend.gpuName == ""
    assert re.fullmatch(r"CPU · ABOUT [\d,]+\.\d× REAL TIME", backend.hardwareLabel)


def test_switching_hardware_forgets_the_speed_measured_on_the_other(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend(gpu=True)
    _open(qtbot, backend, _short_book(tmp_path))
    _convert(qtbot, backend)
    assert "REAL TIME" in backend.hardwareLabel

    backend.setUseGpu(False)

    assert re.fullmatch(r"CPU · \d+ THREADS?", backend.hardwareLabel)
    backend.setUseGpu(True)  # the card's own speed is still known
    assert re.fullmatch(r"NVIDIA RTX PRO 1000 · ABOUT [\d,]+\.\d× REAL TIME", backend.hardwareLabel)


def test_the_work_folder_keeps_graphics_card_and_cpu_audio_apart(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.fail_from = 3
    backend = make_backend(engines, gpu=True)
    _open(qtbot, backend, _short_book(tmp_path))
    _convert(qtbot, backend)  # fails part way on the card: the work is kept
    assert backend.screen == "voice"
    kept = list((tmp_path / "data" / "cache").glob("convert-*"))
    assert len(kept) == 1

    engines.fail_from = None
    backend.setUseGpu(False)
    backend.startConversion()
    qtbot.waitUntil(lambda: backend.screen != "converting", timeout=CONVERSION_TIMEOUT)

    assert backend.screen == "done"
    assert backend._book is not None
    # Nothing was reused: after the card's attempts (the failed piece is retried once), the
    # CPU run voiced every piece from the beginning.
    cpu_run = engines.texts[4:]
    assert len(cpu_run) == _speakable_chunks(backend._book)
    assert cpu_run[0] == "Chapter one: The Night"
    leftovers = list((tmp_path / "data" / "cache").glob("convert-*"))
    assert leftovers == kept  # the card's unfinished folder is still its own


def test_the_preview_follows_the_hardware_choice(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, gpu=True, player="true")
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")

    backend.togglePreview()
    qtbot.waitUntil(lambda: len(engines.made) == 1)
    qtbot.waitUntil(lambda: backend.gpuName != "")
    backend.setUseGpu(False)
    backend.setSpeed(1.2)
    backend.togglePreview()
    qtbot.waitUntil(lambda: len(engines.made) == 2)

    assert [engine.use_gpu for engine in engines.made] == [True, False]
    assert engines.made[0].closed


def test_switching_the_card_off_stops_the_preview_engine_on_it(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    """With "Use graphics card" off, nothing of Tomecast's stays on the card: the preview
    engine left running there stops now, not at the next preview, conversion or scan."""
    engines = Engines()
    backend = make_backend(engines, gpu=True, scan=NVIDIA, player="true")
    qtbot.waitUntil(lambda: backend.gpuName != "")  # the start-up scan is done
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.togglePreview()
    qtbot.waitUntil(lambda: not backend.previewPlaying)
    assert backend.errorMessage == ""
    [engine] = engines.made
    assert engine.use_gpu and engine.started and not engine.closed  # kept between previews

    backend.setUseGpu(False)
    qtbot.waitUntil(lambda: engine.closed)
    assert len(engines.made) == 1  # and nothing was started on the CPU in its place

    backend.setSpeed(1.2)  # not in the preview cache
    backend.togglePreview()  # the next preview starts on the CPU, as before
    qtbot.waitUntil(lambda: len(engines.made) == 2)
    qtbot.waitUntil(lambda: not backend.previewPlaying)
    assert engines.made[1].use_gpu is False


# --- PDF books and the skip switches ---------------------------------------------------------


def _words_of(model: ChapterModel) -> list[int]:
    return [section.words for section in model.sections]


def test_a_pdf_opens_with_its_front_matter_flagged(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, build_sample_book(tmp_path / "magi.pdf"))

    assert backend.bookHasPages is True
    assert backend.skipPageNumbers is True
    assert backend.footnotesAtEnd is False
    rows = _rows(backend.chapters)
    assert [row["title"] for row in rows] == [
        "The Gift of the Magi. O. Henry", "Contents", "One Dollar and Eighty-Seven Cents",
        "Mr. James Dillingham Young", "The Pier-Glass",
    ]  # fmt: skip
    assert [row["matterLabel"] for row in rows] == ["FRONT MATTER"] * 2 + [""] * 3
    assert [row["frontMatter"] for row in rows] == [True, True, False, False, False]
    assert [row["selected"] for row in rows] == [False, False, True, True, True]
    assert backend.previewText == "One dollar and eighty-seven cents."


def test_page_numbers_can_be_read_and_the_list_follows(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, build_sample_book(tmp_path / "magi.pdf"))
    backend.setChapterSelected(4, False)  # the reader's own choice is kept
    skipped = _words_of(backend.chapters)
    seen: list[bool] = []
    backend.skipPageNumbersChanged.connect(lambda: seen.append(backend.skipPageNumbers))

    backend.setSkipPageNumbers(False)

    assert seen == [False]
    kept = _words_of(backend.chapters)
    # Each of the 4 story pages has a header ("THE GIFT OF THE MAGI", 5 words) and a number.
    # A header is read before the chapter heading under it, so it ends the chapter before.
    assert [after - before for before, after in zip(skipped, kept, strict=True)] == [
        0, 5, 6, 12, 1
    ]  # fmt: skip
    assert backend.chapters.selected_rows() == [2, 3]
    assert backend._book is not None
    assert "THE GIFT OF THE MAGI" in backend._book.chapters[2].paragraphs

    backend.setSkipPageNumbers(True)
    assert _words_of(backend.chapters) == skipped
    backend.setSkipPageNumbers(True)  # no change, no signal
    assert seen == [False, True]


def test_footnotes_can_be_read_at_chapter_end(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, build_sample_book(tmp_path / "magi.pdf"))
    before = _words_of(backend.chapters)

    backend.setFootnotesAtEnd(True)

    assert backend.footnotesAtEnd is True
    assert backend._book is not None
    assert backend._book.chapters[2].paragraphs[-1] == "Della is Mrs. James Dillingham Young."
    assert _words_of(backend.chapters)[2] == before[2] + 6
    assert backend.selectionSummary["sections"] == "3 OF 5 SECTIONS"


def test_an_epub_has_no_page_numbers_to_skip(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, build_sample_book(tmp_path / "magi.pdf"))
    assert backend.bookHasPages is True

    backend.goTo("drop")
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend.bookHasPages is False
    backend.goTo("drop")
    _open(qtbot, backend, build_sample_book(tmp_path / "magi.pdf"))
    backend.convertAnother()
    assert backend.bookHasPages is False


def test_a_pdf_is_voiced_without_its_page_furniture(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines)
    _open(qtbot, backend, build_sample_book(tmp_path / "magi.pdf"))
    for row in (2, 3):
        backend.setChapterSelected(row, False)  # only "The Pier-Glass", to keep it quick

    _convert(qtbot, backend)

    assert backend.screen == "done"
    voiced = " ".join(engines.texts)
    assert "THE GIFT OF THE MAGI" not in voiced
    title, paragraphs = SAMPLE_CHAPTERS[2]
    # Spoken as text prep says it ("III." as "three.", "$8 flat" as "eight-dollar flat").
    prep = TextPrep.for_language("en")
    assert voiced == " ".join([prep.heading(title), *(prep.body(plain(p)) for p in paragraphs)])


# --- Names: the name finder and the pronunciation lists -------------------------------------------


def _names_book(folder: Path) -> Path:
    """A short book with three made-up names: Zorp (5 times), Glimmerdale and Mira (3 each)."""
    pages = [
        Page("c1.xhtml", "<p>Zorp walked to Glimmerdale with Mira. Later, Zorp and Mira rested.</p>"
                         "<p>At Glimmerdale the river ran fast, and Zorp swam.</p>"),
        Page("c2.xhtml", "<p>Mira laughed. Zorp laughed too, far from Glimmerdale.</p>"),
    ]  # fmt: skip
    toc = [NavPoint("Chapter 1: Zorp", "c1.xhtml"), NavPoint("Chapter 2", "c2.xhtml")]
    return build_epub(folder / "names.epub", pages, toc=toc, title="Names Book")


def _sleepy_player(folder: Path) -> str:
    """A stand-in audio player that takes its time, so "playing" can be seen."""
    script = folder / "slow-player"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _book_list(folder: Path) -> Path:
    return folder / "names.pronunciations.txt"


def test_the_name_rows_are_log_scaled_like_the_mockup() -> None:
    rows = backend_module.name_rows(
        [Name("Alice", 399), Name("Gryphon", 55), Name("Dinah", 14), Name("Eaglet", 3)]
    )
    assert [(r["word"], r["countLabel"], r["meterLit"]) for r in rows] == [
        ("Alice", "399×", 11), ("Gryphon", "55×", 7), ("Dinah", "14×", 5), ("Eaglet", "3×", 2),
    ]  # fmt: skip
    assert backend_module.name_rows([]) == []


def test_a_book_with_names_has_a_names_step_after_voice(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _names_book(tmp_path))
    assert [(r["word"], r["count"]) for r in backend.names] == [
        ("Zorp", 5), ("Glimmerdale", 3), ("Mira", 3),
    ]  # fmt: skip
    assert backend.bookListName == "names.pronunciations.txt"
    assert backend.fixes == [] and backend.nameFixes == {}

    backend.goTo("names")  # only from Voice
    assert backend.screen == "chapters"
    backend.goTo("voice")
    backend.goTo("names")
    assert backend.screen == "names"
    backend.goTo("voice")  # Back
    assert backend.screen == "voice"


def test_a_book_without_names_skips_the_step(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend.names == []
    backend.goTo("voice")
    backend.goTo("names")
    assert backend.screen == "voice"


def test_a_fix_made_in_the_window_reaches_the_spoken_text(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pronunciations.txt").write_text(
        "# mine\nMira → MEER-uh\n", encoding="utf-8"
    )
    _open(qtbot, backend, _names_book(tmp_path))
    assert backend.nameFixes == {"Mira": "MEER-uh"}  # the global list already has one

    backend.setNameFix("Zorp", "ZAWRP")
    assert backend.errorMessage == ""
    assert _book_list(tmp_path).read_text(encoding="utf-8") == "Zorp → ZAWRP\n"
    assert backend.nameFixes == {"Zorp": "ZAWRP", "Mira": "MEER-uh"}
    assert backend.fixes == [
        {"word": "Zorp", "say": "ZAWRP", "scope": "book"},
        {"word": "Mira", "say": "MEER-uh", "scope": "all"},
    ]

    backend.goTo("voice")
    backend.goTo("names")
    backend.startConversion()  # from the Names screen, as from Voice
    assert backend.screen == "converting"
    qtbot.waitUntil(lambda: backend.screen != "converting", timeout=CONVERSION_TIMEOUT)
    assert backend.screen == "done", backend.errorMessage

    spoken = " ".join(engines.texts)
    assert "Zorp" not in spoken and "Mira" not in spoken
    assert "zawrp walked to Glimmerdale with meer-uh." in spoken
    # Every sentence was still spoken: the same pieces as the command line makes with the lists.
    prep = TextPrep.for_language(
        "en", pronunciations.load_for_book(tmp_path / "data", tmp_path / "names.epub")[0]
    )
    book = backend._book
    assert book is not None
    expected = [c.text for chunks in plan_chunks(book, FakeEngine.max_chars, prep=prep)
                for c in chunks if c.speakable]  # fmt: skip
    assert engines.texts == expected


def test_editing_moving_and_removing_fixes(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _names_book(tmp_path))
    global_list = tmp_path / "data" / "pronunciations.txt"

    backend.setNameFix("Zorp", "ZAWRP")
    backend.toggleFixScope("book", "Zorp")  # to every book
    assert global_list.read_text(encoding="utf-8") == "Zorp → ZAWRP\n"
    assert not _book_list(tmp_path).exists()  # nothing left in it: no empty file
    assert backend.fixes == [{"word": "Zorp", "say": "ZAWRP", "scope": "all"}]

    backend.setNameFix("Zorp", "ZORP")  # changed where it is: the global list
    assert global_list.read_text(encoding="utf-8") == "Zorp → ZORP\n"
    backend.toggleFixScope("all", "Zorp")  # back to this book only
    assert _book_list(tmp_path).read_text(encoding="utf-8") == "Zorp → ZORP\n"
    assert backend.fixes == [{"word": "Zorp", "say": "ZORP", "scope": "book"}]

    backend.setNameFix("Zorp", "   ")  # clearing the field removes it
    assert backend.fixes == [] and backend.nameFixes == {}
    backend.setNameFix("Mira", "MEER-uh")
    backend.removeFix("book", "Mira")
    assert backend.fixes == []
    backend.setNameFix("Nobody", "x")  # not a listed name: ignored
    backend.removeFix("nowhere", "Zorp")
    assert backend.fixes == [] and backend.errorMessage == ""


def test_a_respelling_the_list_cant_hold_is_explained_and_not_saved(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    _open(qtbot, backend, _names_book(tmp_path))
    for say, message in (
        ("/zɔrp/", fixes_module.PHONETIC),
        ("zɔrp", fixes_module.PHONETIC),
        ("[zɔrp]", fixes_module.PHONETIC),
        ("ˈzawrp", fixes_module.PHONETIC),
        ("[[z'O@p]]", fixes_module.MARKUP),
        ("[zawrp]", fixes_module.NOTATIONS["brackets"]),
        ("/zawrp/", fixes_module.NOTATIONS["slashes"]),
    ):
        backend.setNameFix("Zorp", say)
        assert backend.errorMessage == message, say
        assert backend.nameFixes == {}
        assert not _book_list(tmp_path).exists()
    backend.setNameFix("Zorp", "zawrp")  # a good one clears the banner the bad one put up
    assert backend.errorMessage == ""
    assert backend.nameFixes == {"Zorp": "zawrp"}


def test_a_read_only_book_folder_keeps_the_fix_for_the_session(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write to read-only folders")
    engines = Engines()
    backend = make_backend(engines, player=_sleepy_player(tmp_path))
    books = tmp_path / "books"
    books.mkdir()
    _open(qtbot, backend, _names_book(books))
    books.chmod(0o555)
    try:
        backend.setNameFix("Zorp", "ZAWRP")
        assert backend.errorMessage.startswith(
            f"Tomecast couldn't save names.pronunciations.txt in {books} (permission denied)."
        )
        assert backend.errorMessage.endswith("The change works until you close Tomecast.")
        assert backend.nameFixes == {"Zorp": "ZAWRP"}  # kept for now…
        assert not _book_list(books).exists()  # …but not saved
        backend.playName("Zorp")  # and the players use it
        qtbot.waitUntil(lambda: bool(engines.texts))
        assert engines.texts == ["zawrp"]
    finally:
        books.chmod(0o755)
        backend.playName("Zorp")


def test_a_fix_that_couldnt_be_saved_still_reaches_the_conversion(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write to read-only folders")
    engines = Engines()
    backend = make_backend(engines)
    # The global list is a link into a read-only folder: it can't be saved, while the data
    # folder (which holds the work in progress) and the book's folder are fine.
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "pronunciations.txt").write_text("Zorp → zorp-o\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pronunciations.txt").symlink_to(locked / "pronunciations.txt")
    _open(qtbot, backend, _names_book(tmp_path))
    locked.chmod(0o555)
    try:
        backend.setNameFix("Zorp", "ZAWRP")  # a global fix, changed in the global list
        assert "couldn't save pronunciations.txt" in backend.errorMessage
        _convert(qtbot, backend)
    finally:
        locked.chmod(0o755)
    assert backend.screen == "done", backend.errorMessage
    assert any(text.startswith("zawrp") for text in engines.texts)
    assert (locked / "pronunciations.txt").read_text(encoding="utf-8") == "Zorp → zorp-o\n"


def test_list_lines_that_cant_be_used_are_noted_on_the_done_screen(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend()
    book = _names_book(tmp_path)
    _book_list(tmp_path).write_text("Zorp → /zɔrp/\nMira → ˈmɪərə\n", encoding="utf-8")
    _open(qtbot, backend, book)
    _convert(qtbot, backend)
    assert backend.screen == "done"
    for number, say in ((1, "/zɔrp/"), (2, "ˈmɪərə")):
        assert any(
            w.startswith(
                f"Pronunciation list: names.pronunciations.txt, line {number}: "
                f"'{say}' looks like IPA"
            )
            for w in backend.warnings
        ), backend.warnings


def test_one_thing_plays_at_a_time_with_the_fixes(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, player=_sleepy_player(tmp_path))
    _open(qtbot, backend, _names_book(tmp_path))
    backend.setNameFix("Zorp", "ZAWRP")
    backend.goTo("voice")
    backend.goTo("names")

    backend.playName("Zorp")
    assert backend.playingName == "Zorp"
    qtbot.waitUntil(lambda: backend._playback is not None)
    backend.playName("Mira")  # another row: switches to it
    assert backend.playingName == "Mira"
    qtbot.waitUntil(lambda: backend._playback is not None)
    backend.playText("Zorp met Mira on 4 July.")  # TRY ANY WORD: switches again
    assert (backend.playingName, backend.tryPlaying) == ("", True)
    qtbot.waitUntil(lambda: len(engines.texts) == 3)
    backend.playText("anything")  # a second press stops it
    assert backend.tryPlaying is False and backend._playback is None
    # Each was voiced as the book will be: with the fixes and the number rules.
    assert engines.texts == ["zawrp", "Mira", "zawrp met Mira on the fourth of July."]

    backend.playName("Zorp")  # voiced already: played from the cache
    assert backend.playingName == "Zorp"
    backend.goTo("voice")  # leaving the screen stops it
    assert backend.playingName == "" and backend._playback is None
    assert len(engines.texts) == 3
    backend.playText("   ")  # nothing to say
    assert backend.tryPlaying is False


def test_the_voice_preview_uses_the_fixes_too(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, player=shutil.which("true"))
    if backend.previewAvailable is False:
        pytest.skip("needs the `true` command as a stand-in audio player")
    _open(qtbot, backend, _names_book(tmp_path))
    backend.setNameFix("Zorp", "ZAWRP")
    backend.goTo("voice")
    assert backend.previewText == "Zorp walked to Glimmerdale with Mira."  # shown as written
    backend.togglePreview()
    qtbot.waitUntil(lambda: not backend.previewPlaying)
    assert engines.texts == ["zawrp walked to Glimmerdale with Mira."]  # spoken with the fix


# --- Engine updates, and the speed test -------------------------------------------------------


def _engine_update(tmp_path: Path) -> tuple[Asset, Path]:
    """An engine with an older build in use (from 0.1.x), and a newer one pinned."""
    legacy = tmp_path / "data" / "engines" / "engine-0.1"
    legacy.mkdir(parents=True)
    (legacy / ".tomecast-sha256").write_text("e" * 64 + "\n")
    archive = tmp_path / "source" / "engine.tar.gz"
    archive.parent.mkdir(exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        data = b"new build"
        info = tarfile.TarInfo("audiocpp_server")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    blob = archive.read_bytes()
    asset = Asset(
        name="Test engine", url=archive.as_uri(), sha256=hashlib.sha256(blob).hexdigest(),
        size=len(blob), path="engines/engine", archive="tar.gz",
        replaces=("engines/engine-0.1",), id="engine",
    )  # fmt: skip
    return asset, legacy


def test_an_update_that_fails_its_check_is_a_note_and_the_old_build_stays(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, legacy = _engine_update(tmp_path)
    checked: list[bool] = []

    def failing(root: Path, gpu: bool, cancelled: object) -> Callable[..., str]:
        checked.append(gpu)
        return lambda new, old: "illegal instruction"

    backend = make_backend(assets=(asset,), engine_check=failing)
    labels: list[str] = []
    backend.downloadLabelChanged.connect(lambda: labels.append(backend.downloadLabel))
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"  # the book is made anyway, with the build that works
    assert backend.warnings[0] == UPDATE_FAILED_NOTE
    assert checked == [False]
    assert any(label.startswith("DOWNLOADING AN UPDATE · TEST ENGINE") for label in labels)
    assert build_in_use(asset, tmp_path / "data") == legacy


def test_an_update_that_passes_is_used_without_a_note(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, legacy = _engine_update(tmp_path)
    backend = make_backend(assets=(asset,), engine_check=lambda root, gpu, c: lambda n, o: None)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.warnings == []
    assert build_in_use(asset, tmp_path / "data") != legacy
    assert legacy.is_dir()  # kept to go back to


def test_the_start_up_speed_test_fills_the_estimates(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend(speeds=Speeds(cpu=4.6))
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)

    assert backend.hardwareLabel == "CPU · ABOUT 4.6× REAL TIME"  # the drop screen's corner
    _open(qtbot, backend, _short_book(tmp_path))
    assert backend.selectionSummary == backend.chapters.summary(4.6)


def test_a_conversion_measures_better_than_the_speed_test(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend(speeds=Speeds(cpu=4.6))
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend._speeds[f"CPU/{machine.voice_threads()}"] != 4.6  # the fake engine is faster
    assert "4.6×" not in backend.hardwareLabel


def test_packaging_time_doesnt_count_towards_the_measured_speed(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaging = SlowPackaging(monkeypatch)
    backend = make_backend(speeds=Speeds(cpu=4.6))
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(lambda: backend.stage == "packaging", timeout=CONVERSION_TIMEOUT)
    key = backend._speed_key("CPU")
    voicing = backend._speeds[key]  # measured on the voicing alone
    assert voicing != 4.6

    time.sleep(1.0)  # a long book takes minutes to encode, after the last piece is voiced
    packaging.release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)

    # The speed kept for the next book's estimates and the card-or-CPU note is how fast it
    # voices, as the speed test measures it: packaging (or a resumed book's few new pieces
    # against a whole book's encode) doesn't drag it down.
    assert backend._speeds[key] == pytest.approx(voicing)
    assert backend.hardwareLabel.endswith(f"ABOUT {voicing:.1f}× REAL TIME")


def test_the_graphics_card_note_shows_the_measured_speed_up(
    qtbot: QtBot, make_backend: MakeBackend
) -> None:
    backend = make_backend(
        gpu=True, scan=NVIDIA, speeds=Speeds(cpu=4.6, gpu=19.8, gpu_description=NVIDIA)
    )
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)

    assert backend.hardwareLabel == "NVIDIA RTX PRO 1000 · ABOUT 19.8× REAL TIME"
    assert backend.gpuNote == "ABOUT 4.3× FASTER HERE · USES THE DRIVER ALREADY INSTALLED"
    backend.setUseGpu(False)
    assert backend.hardwareLabel == "CPU · ABOUT 4.6× REAL TIME"
    assert backend.gpuNote == "USING THE CPU · THE GRAPHICS CARD IS ABOUT 4.3× FASTER HERE"


def test_the_graphics_card_note_is_honest_about_a_slow_card(
    qtbot: QtBot, make_backend: MakeBackend
) -> None:
    backend = make_backend(
        gpu=True, scan=NVIDIA, speeds=Speeds(cpu=4.6, gpu=4.7, gpu_description=NVIDIA)
    )
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)

    assert backend.gpuNote == "NO FASTER THAN THE CPU HERE · USES THE DRIVER ALREADY INSTALLED"
    backend.setUseGpu(False)
    assert backend.gpuNote == "USING THE CPU · THE GRAPHICS CARD WAS NO FASTER HERE"


def test_voicing_on_the_cpu_with_the_card_off_keeps_what_is_known_about_the_card(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    player = shutil.which("true")
    if player is None:
        pytest.skip("needs the `true` command as a stand-in audio player")
    engines = Engines()
    backend = make_backend(
        engines, gpu=True, scan=NVIDIA, player=player,
        speeds=Speeds(cpu=5.0, gpu=4.0, gpu_description=NVIDIA),
    )  # fmt: skip
    qtbot.waitUntil(lambda: "REAL TIME" in backend.hardwareLabel)
    backend.setUseGpu(False)
    measured = "USING THE CPU · THE GRAPHICS CARD WAS NO FASTER HERE"
    assert backend.gpuNote == measured
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")

    backend.togglePreview()  # a preview on the CPU...
    qtbot.waitUntil(lambda: not backend.previewPlaying)
    assert backend.errorMessage == ""
    _convert(qtbot, backend)  # ...and a book

    assert backend.screen == "done"
    assert [engine.use_gpu for engine in engines.made] == [False, False]
    # An engine started on the CPU because the card is switched off says nothing about the card.
    assert backend.gpuName == NVIDIA
    assert backend.gpuNote == measured
    backend.setUseGpu(True)
    assert backend.hardwareLabel == "NVIDIA RTX PRO 1000 · ABOUT 4.0× REAL TIME"


def test_without_measurements_the_graphics_card_note_is_the_general_one(
    qtbot: QtBot, make_backend: MakeBackend
) -> None:
    backend = make_backend(gpu=True, scan=NVIDIA)
    qtbot.waitUntil(lambda: backend.gpuName != "")

    assert backend.gpuNote == "FASTER · USES THE DRIVER ALREADY INSTALLED"
    backend.setUseGpu(False)
    assert backend.gpuNote == "USING THE CPU · THE GRAPHICS CARD IS USUALLY SEVERAL TIMES FASTER"


def test_the_start_up_scan_looks_at_the_card_only_when_it_is_switched_on(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asked: list[bool] = []

    def probe(root: Path, gpu: bool, cancelled: object, devices: object) -> Speeds:
        asked.append(gpu)
        return Speeds()

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    # No driver; a driver and the switch on; a driver and the switch off.
    for driver, switched_on in ((False, True), (True, True), (True, False)):
        settings.setValue("useGpu", switched_on)
        before = len(asked)
        backend = Backend(
            engine_factory=Engines(),
            gpu_check=lambda driver=driver: driver,  # type: ignore[misc]
            device_scanner=lambda root, cancelled: [],
            speed_probe=probe,
            assets=(),
            data_root=tmp_path / "data",
            player=None,
            settings=settings,
            system_check=lambda: None,
        )
        try:
            qtbot.waitUntil(lambda before=before: len(asked) > before)  # type: ignore[misc]
        finally:
            backend.shutdown()  # even if the wait fails: never leave its thread running

    assert asked == [False, True, False]  # off means the card isn't touched at all


# --- After the review: rollback, Cancel and closing never wait on installs --------------------


def test_an_update_that_fails_on_the_book_is_rolled_back(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, legacy = _engine_update(tmp_path)
    engines = Engines()
    engines.start_error = "the voice engine keeps crashing. Its log:\nSegmentation fault"
    backend = make_backend(engines, assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)  # the update passed its check, then crashed on the book

    assert backend.screen == "voice"
    assert backend.errorMessage.endswith(
        "The voice engine update didn't work on this computer, so Tomecast kept using the "
        "previous version. Start again to make the book with it."
    )
    new_build = tmp_path / "data" / asset.path / asset.sha256[:12]
    assert engines.made[0].pinned == {"engine": new_build}
    assert build_in_use(asset, tmp_path / "data") == legacy

    engines.start_error = None
    _convert(qtbot, backend)  # Start again

    assert backend.screen == "done"
    assert engines.made[-1].pinned == {"engine": legacy}  # the build that worked


def test_a_failed_update_download_is_tried_again_in_the_same_session(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, legacy = _engine_update(tmp_path)
    source = Path(asset.url.removeprefix("file://"))
    away = source.with_suffix(".away")
    source.rename(away)  # no network
    backend = make_backend(assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None)
    labels: list[str] = []
    backend.downloadLabelChanged.connect(lambda: labels.append(backend.downloadLabel))
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"
    assert backend.warnings[0] == (
        "The voice engine update couldn't be downloaded, so Tomecast kept using the previous "
        "version."
    )
    backend.goTo("voice")
    assert backend.downloadNote.startswith("STARTING DOWNLOADS A VOICE ENGINE UPDATE")  # it will

    away.rename(source)  # the network is back
    labels.clear()
    _convert(qtbot, backend)

    assert any(label.startswith("DOWNLOADING AN UPDATE") for label in labels)
    assert build_in_use(asset, tmp_path / "data") != legacy
    backend.goTo("voice")
    assert backend.downloadNote == ""


def _book_is_read(qtbot: QtBot, backend: Backend, tmp_path: Path) -> None:
    """The worker isn't stuck: it reads another book."""
    other = build_epub(
        tmp_path / "other.epub",
        [Page("o.xhtml", "<p>Another book entirely.</p>")],
        toc=[NavPoint("Only", "o.xhtml")],
        title="Other Book",
    )
    backend.goTo("drop")
    backend.openBook(_url(other))
    qtbot.waitUntil(lambda: backend.bookTitle == "Other Book", timeout=5000)


def test_cancel_works_while_another_tomecast_is_installing(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    import fcntl

    asset = _test_asset(tmp_path, "model.bin", b"model weights")
    root = tmp_path / "data"
    root.mkdir()
    with (root / ".install.lock").open("w") as other:
        fcntl.flock(other, fcntl.LOCK_EX)  # the command line is installing
        backend = make_backend(assets=(asset,))
        _open(qtbot, backend, _short_book(tmp_path))
        backend.goTo("voice")
        backend.startConversion()
        qtbot.wait(300)  # the worker is waiting for the lock

        backend.cancelConversion()

        assert backend.errorMessage == STOPPED_MESSAGE
        _book_is_read(qtbot, backend, tmp_path)
    assert not (root / asset.path).exists()


def test_cancel_works_during_the_self_check_and_blames_nothing(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, legacy = _engine_update(tmp_path)
    checking = threading.Event()

    def slow_check(root: Path, gpu: bool, cancelled: Callable[[], bool]) -> Callable[..., str]:
        def check(new: object, old: object) -> str:
            checking.set()
            while not cancelled():  # a first start on a slow card
                time.sleep(0.02)
            return "the voice engine stopped while starting"

        return check

    backend = make_backend(assets=(asset,), engine_check=slow_check)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(checking.is_set)

    backend.cancelConversion()

    _book_is_read(qtbot, backend, tmp_path)
    assert failure(asset, tmp_path / "data") is None  # stopped, not failed
    assert waiting_check(asset, tmp_path / "data")
    assert build_in_use(asset, tmp_path / "data") == legacy


def test_closing_the_window_doesnt_wait_for_a_stuck_engine(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "SHUTDOWN_WAIT_MS", 200)
    stopped: list[bool] = []
    monkeypatch.setattr(backend_module, "stop_all_engines", lambda: stopped.append(True))
    engines = Engines()
    engines.block_at = 1  # the first piece hangs (a request to a stuck card)
    backend = make_backend(engines)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    assert engines.blocked.wait(timeout=10)
    started = time.monotonic()

    backend.shutdown()

    assert time.monotonic() - started < 2
    assert stopped  # every engine was told to stop
    engines.release.set()
    assert backend.worker_thread.wait(10_000)


def test_closing_the_window_while_packaging_waits_for_the_book(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "SHUTDOWN_WAIT_MS", 1)
    backend = make_backend()
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(lambda: _control(backend).committed, timeout=CONVERSION_TIMEOUT)

    backend.shutdown()

    assert not backend.worker_thread.isRunning()  # it waited for packaging to finish
    assert list(tmp_path.glob("Short Book*.m4b"))


def test_turning_the_card_on_looks_at_it_once(qtbot: QtBot, make_backend: MakeBackend) -> None:
    probed: list[bool] = []
    backend = make_backend(gpu=True, probed=probed)
    qtbot.waitUntil(lambda: probed == [True])  # on at start-up: the card is looked at

    backend.setUseGpu(False)
    backend.setUseGpu(True)  # its name and speed are still unknown: look again

    qtbot.waitUntil(lambda: probed == [True, True])


# --- Second review: Done first, fallbacks prove nothing, stops, the shutdown race -------------


def _installed_state(tmp_path: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads((tmp_path / "data" / "installed.json").read_text())
    return state


def test_the_done_screen_doesnt_wait_for_another_install(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    import fcntl

    asset, _ = _engine_update(tmp_path)
    engines = Engines()
    held: list[Any] = []

    def another_tomecast_starts_installing() -> None:  # just as the book is finished
        lock = (tmp_path / "data" / ".install.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        held.append(lock)

    engines.after_close = another_tomecast_starts_installing
    backend = make_backend(engines, assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()

    qtbot.waitUntil(lambda: backend.screen == "done", timeout=5000)  # not after the lock

    assert held  # it was held the whole time
    qtbot.wait(3000)  # the worker gave up proving after a couple of seconds...
    held[0].close()
    assert _installed_state(tmp_path)["slots"]["engine"]["proven"] is False  # ...quietly


def test_a_book_made_on_the_cpu_fallback_doesnt_prove_a_card_build(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, _ = _engine_update(tmp_path)
    engines = Engines()
    engines.gpu_warning = "The graphics card didn't work, so Tomecast used the CPU."
    backend = make_backend(
        engines, gpu=True, assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None
    )
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"
    slot = _installed_state(tmp_path)["slots"]["engine"]
    assert slot["proven"] is False  # the card was never tried with it
    assert slot["previous"] == "engines/engine-0.1"  # so the build that worked is kept


def test_a_book_made_as_asked_proves_the_build(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, _ = _engine_update(tmp_path)
    backend = make_backend(assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None)
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    qtbot.waitUntil(lambda: _installed_state(tmp_path)["slots"]["engine"]["proven"] is True)


def test_stopping_doesnt_roll_back_an_update(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, _ = _engine_update(tmp_path)
    engines = Engines()
    engines.start_error = "the voice engine keeps crashing. Its log:\n(stopped)"
    backend = make_backend(engines, assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None)

    def press_stop() -> None:  # Stop, as the engine goes down
        _control(backend).cancel()

    engines.before_start = press_stop
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()

    qtbot.waitUntil(lambda: bool(engines.made) and engines.made[0].thread_id is not None)
    qtbot.wait(500)

    new_build = tmp_path / "data" / asset.path / asset.sha256[:12]
    assert build_in_use(asset, tmp_path / "data") == new_build  # still the update
    assert failure(asset, tmp_path / "data") is None  # a stop says nothing about the build


def test_closing_during_packaging_waits_even_if_it_began_a_moment_ago(
    make_backend: MakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[bool] = []
    monkeypatch.setattr(backend_module, "stop_all_engines", lambda: stopped.append(True))
    backend = make_backend()

    class JustCommitted(TimedControl):
        """Packaging began between looking at ``committed`` and calling ``cancel()``."""

        @property
        def committed(self) -> bool:
            return False  # what shutdown would have seen a moment before

        def cancel(self) -> bool:
            return False  # packaging has begun: cancel refuses

    backend._control = JustCommitted()

    backend.shutdown()

    assert stopped == []  # it took the packaging path: no engine is stopped mid-book


def test_the_start_up_scan_lists_the_cards_once(qtbot: QtBot, tmp_path: Path) -> None:
    scanned: list[bool] = []
    given: list[object] = []
    card = GpuDevice(0, "Vulkan0", NVIDIA, "gpu", 7 * 2**30, 8 * 2**30)

    def scanner(root: Path, cancelled: object) -> list[GpuDevice]:
        scanned.append(True)
        return [card]

    def probe(root: Path, gpu: bool, cancelled: object, devices: object) -> Speeds:
        given.append(devices)
        return Speeds()

    backend = Backend(
        engine_factory=Engines(),
        gpu_check=lambda: True,
        device_scanner=scanner,
        speed_probe=probe,
        assets=(),
        data_root=tmp_path / "data",
        player=None,
        settings=QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
        system_check=lambda: None,
    )
    try:
        qtbot.waitUntil(lambda: bool(given))
    finally:
        backend.shutdown()  # even if the wait fails: never leave its thread running

    assert scanned == [True]
    assert given == [[card]]  # the probe gets that list instead of asking again
    assert backend.gpuName == NVIDIA


# --- The graphics card running out of memory (#49) --------------------------------------------


def _planned_pieces(backend: Backend) -> list[str]:
    assert backend._book is not None
    return [
        chunk.text
        for chapter in plan_chunks(backend._book, FakeEngine.max_chars)
        for chunk in chapter
        if chunk.speakable
    ]


def test_running_out_of_graphics_memory_mid_book_is_in_the_banner_and_the_book_goes_on(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.oom_at = 3  # another program takes the card's memory on the third piece
    engines.block_at = 5  # hold a later piece, to look at the window mid-book
    backend = make_backend(engines, gpu=True, scan=NVIDIA, speeds=Speeds(cpu=3.1))
    qtbot.waitUntil(lambda: backend.gpuName != "")
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()

    qtbot.waitUntil(engines.blocked.is_set, timeout=CONVERSION_TIMEOUT)
    qtbot.waitUntil(lambda: backend.errorMessage != "")
    assert backend.screen == "converting"  # still going
    assert backend.errorMessage == RAN_OUT_OF_MEMORY_NOTE
    assert backend.gpuName == ""
    assert backend.hardwareLabel == "CPU · ABOUT 3.1× REAL TIME"
    # The time left is the CPU's straight away, from the speed test's 3.1x.
    qtbot.waitUntil(lambda: backend.timeLeft != "--:--:--")
    assert backend.speedLabel == "3.1× REAL TIME"

    engines.release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)
    assert backend.warnings == [RAN_OUT_OF_MEMORY_NOTE]
    assert backend.errorMessage == ""  # the done screen's notes say it now, not twice
    assert engines.texts == _planned_pieces(backend)  # each piece once, in order: nothing lost
    assert engines.on_card == engines.texts[:2]  # the rest on the CPU
    assert [engine.use_gpu for engine in engines.made] == [True]  # one engine, one book


def test_running_out_of_graphics_memory_at_start_is_in_the_banner_too(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.oom_at_start = True
    engines.block_at = 1
    backend = make_backend(engines, gpu=True, scan=NVIDIA)
    qtbot.waitUntil(lambda: backend.gpuName != "")
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()

    qtbot.waitUntil(engines.blocked.is_set, timeout=CONVERSION_TIMEOUT)
    qtbot.waitUntil(lambda: backend.errorMessage != "")
    assert backend.errorMessage == OUT_OF_MEMORY_NOTE
    assert backend.gpuName == ""
    engines.release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)
    assert backend.warnings == [OUT_OF_MEMORY_NOTE]
    assert backend.errorMessage == ""


def test_the_card_not_working_at_start_stays_off_the_banner(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.gpu_warning = "The graphics card didn't work, so Tomecast used the CPU."
    seen: list[str] = []
    backend = make_backend(engines, gpu=True)
    backend.errorMessageChanged.connect(lambda: seen.append(backend.errorMessage))
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"
    assert [message for message in seen if message] == []  # unchanged: the done screen only


def test_a_book_that_ran_out_of_graphics_memory_doesnt_prove_the_build(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, _ = _engine_update(tmp_path)
    engines = Engines()
    engines.oom_at = 2
    backend = make_backend(
        engines, gpu=True, assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None
    )
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"
    qtbot.wait(300)  # proving (which mustn't happen) comes after the done screen
    slot = _installed_state(tmp_path)["slots"]["engine"]
    assert slot["proven"] is False
    assert slot["previous"] == "engines/engine-0.1"  # the build that worked is kept


def test_a_failure_after_running_out_of_graphics_memory_doesnt_roll_back(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    asset, _ = _engine_update(tmp_path)
    engines = Engines()
    engines.oom_at = 2
    engines.fail_from = 4  # then the CPU fails too (short of memory as well)
    backend = make_backend(
        engines, gpu=True, assets=(asset,), engine_check=lambda r, g, c: lambda n, o: None
    )
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "voice"
    assert UPDATE_FAILED_NOTE not in backend.errorMessage
    assert backend.errorMessage.endswith(
        "The audio made so far is kept, so starting again continues from there."
    )
    new_build = tmp_path / "data" / asset.path / asset.sha256[:12]
    assert build_in_use(asset, tmp_path / "data") == new_build  # not rolled back...
    assert failure(asset, tmp_path / "data") is None  # ...and not blamed


def test_the_switch_is_said_even_if_the_cpus_first_try_fails(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    engines.oom_at = 3  # the card runs out on the third piece...
    engines.fail_once_at = 3  # ...and the CPU's first try at it fails too (retried after)
    backend = make_backend(engines, gpu=True, scan=NVIDIA, speeds=Speeds(cpu=3.1))
    qtbot.waitUntil(lambda: backend.gpuName != "")
    seen: list[str] = []
    backend.errorMessageChanged.connect(lambda: seen.append(backend.errorMessage))
    _open(qtbot, backend, _short_book(tmp_path))

    _convert(qtbot, backend)

    assert backend.screen == "done"
    assert seen == [RAN_OUT_OF_MEMORY_NOTE, ""]  # in the banner while it went on
    assert backend.gpuName == ""
    assert short_name(NVIDIA) not in backend._speeds  # the CPU's speed isn't filed as the card's
    assert backend.warnings == [RAN_OUT_OF_MEMORY_NOTE]
    pieces = _planned_pieces(backend)
    assert engines.texts == [*pieces[:3], *pieces[2:]]  # the third tried once more, on the CPU


def test_the_cpus_speed_after_the_switch_is_kept_under_the_engines_threads(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "plugged", supplies=PLUGGED_IN)  # 8 threads
    engines = Engines()
    engines.oom_at = 3
    engines.block_at = 2
    backend = make_backend(engines, gpu=True, scan=NVIDIA)
    qtbot.waitUntil(lambda: backend.gpuName != "")
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    backend.startConversion()
    qtbot.waitUntil(engines.blocked.is_set, timeout=CONVERSION_TIMEOUT)
    assert engines.made[0].threads == 8

    # The charger is pulled mid-book (the scan would now say 4), then the card runs out.
    pin_machine(monkeypatch, tmp_path / "laptop", supplies=ON_BATTERY)
    engines.release.set()
    qtbot.waitUntil(lambda: backend.screen == "done", timeout=CONVERSION_TIMEOUT)

    assert "CPU/8" in backend._speeds  # what the engine voiced with
    assert "CPU/4" not in backend._speeds


# --- The premium voice (Ming, issue #11) --------------------------------------------------


def _premium_labels(backend: Backend) -> list[str]:
    return [group["label"] for group in backend.voiceGroups if "PREMIUM" in group["label"]]


def test_the_premium_voices_are_listed_only_while_the_card_is_in_use(
    make_backend: MakeBackend,
) -> None:
    assert _premium_labels(make_backend(gpu=False)) == []
    backend = make_backend(gpu=True)
    assert _premium_labels(backend) == ["PREMIUM · BRITISH", "PREMIUM · AMERICAN"]
    names = [v["id"] for g in backend.voiceGroups if "PREMIUM" in g["label"] for v in g["voices"]]
    assert sorted(names) == sorted(ming.VOICES)
    backend.setUseGpu(False)
    assert _premium_labels(backend) == []


def test_choosing_a_premium_voice(make_backend: MakeBackend) -> None:
    backend = make_backend(gpu=True)
    backend.setSpeed(3.0)
    backend.setVoice("UK_George")
    assert backend.voice == "UK_George"
    assert backend.voiceLabel == "GEORGE · PREMIUM · BRITISH"
    assert backend.speed == ming.MAX_SPEED  # all of Ming's speed is the stretch, up to 2x
    backend.setSpeed(2.5)
    assert backend.speed == ming.MAX_SPEED
    backend.setVoice("af_heart")
    backend.setSpeed(2.5)
    assert backend.speed == 2.5  # Kokoro still goes to 3x


def test_turning_the_card_off_goes_back_to_a_kokoro_voice(make_backend: MakeBackend) -> None:
    backend = make_backend(gpu=True)
    backend.setVoice("US_Emma")
    backend.setUseGpu(False)
    assert backend.voice == DEFAULT_VOICE
    backend.setVoice("US_Emma")  # not listed without the card
    assert backend.voice == DEFAULT_VOICE


def test_the_download_note_says_when_start_downloads_the_premium_voice(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    backend = make_backend(gpu=True, assets=None)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.goTo("voice")
    assert backend.downloadNote == _note_for(GPU_DOWNLOADS)
    backend.setVoice("UK_George")  # the engine build and Ming; no eSpeak or Kokoro
    mb = math.ceil((AUDIOCPP_VULKAN.size + MING_Q8.size) / 1_000_000)
    assert backend.downloadNote == (
        f"STARTING DOWNLOADS THE PREMIUM VOICE ONCE · {mb:,} MB · NEEDS A GRAPHICS CARD"
    )


def test_a_premium_voice_converts_with_ming_and_says_it_is_a_preview(
    qtbot: QtBot, make_backend: MakeBackend, tmp_path: Path
) -> None:
    engines = Engines()
    backend = make_backend(engines, gpu=True)
    _open(qtbot, backend, _short_book(tmp_path))
    backend.setVoice("UK_Charlotte")
    _convert(qtbot, backend)
    assert backend.screen == "done"
    assert [engine.voice for engine in engines.made] == ["UK_Charlotte"]
    assert ming.UNCHECKED_NOTE in backend.warnings
