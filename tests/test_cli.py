import array
import fcntl
import hashlib
import io
import json
import math
import tarfile
import threading
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import ClassVar

import pytest
from epub_factory import NavPoint, Page, build_epub
from format_factory import book_in_format
from machine_factory import ON_BATTERY, PLUGGED_IN, pin_machine
from pdf_factory import PageBuilder, build_pdf

from tomecast import __version__, cli, machine
from tomecast.downloads import UPDATE_FAILED_NOTE, Asset, build_in_use, failure, roll_back
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import EngineError
from tomecast.hardware import GpuDevice
from tomecast.speed import Speeds
from tomecast.workdirs import work_folder


class FakeKokoro:
    """Stands in for KokoroServer: same constructor and context manager, no real engine."""

    fail_with: str | None = None
    fail_after: int | None = None  # chunks to voice before failing, like an interrupted run
    voiced = 0
    made: ClassVar[list["FakeKokoro"]] = []
    gpu_description = "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"
    gpu_warning: str | None = None  # set: a use_gpu run falls back to the CPU
    # set: a use_gpu run starts on the card, and it runs out of memory on this voicing (1 is
    # the first), which then carries on on the CPU, as AutoKokoro does
    oom_at: int | None = None
    on_card: ClassVar[list[str]] = []  # what was voiced on the graphics card
    texts: ClassVar[list[str]] = []  # everything the voice was asked to say, across all instances
    on_exit: ClassVar[Callable[[], None] | None] = None  # runs when the engine stops

    def __init__(
        self, root: Path, *, voice: str, speed: float, use_gpu: bool = False, **kwargs: object
    ) -> None:
        self.kwargs = kwargs  # builds=..., as prepare() chose them
        self.label = f"Fake ({voice})"
        self.max_chars = 200
        self.sample_rate = 24000
        self.use_gpu = use_gpu
        self.warning: str | None = None
        self.fell_back = False
        self.out_of_memory = False
        self.gpu_description = ""
        self.calls = 0
        FakeKokoro.made.append(self)

    def __enter__(self) -> "FakeKokoro":
        if self.use_gpu and FakeKokoro.gpu_warning:
            self.warning = FakeKokoro.gpu_warning
            self.fell_back = True
        elif self.use_gpu:
            self.gpu_description = FakeKokoro.gpu_description
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if FakeKokoro.on_exit is not None:
            FakeKokoro.on_exit()

    def synthesize(self, text: str, output: Path) -> None:
        self.calls += 1
        if self.use_gpu and not self.fell_back and FakeKokoro.oom_at == self.calls:
            self.warning = audiocpp.RAN_OUT_OF_MEMORY_NOTE  # and it carries on on the CPU
            self.fell_back = self.out_of_memory = True
            self.gpu_description = ""
        if self.fail_with:
            raise EngineError(self.fail_with)
        if FakeKokoro.fail_after is not None and FakeKokoro.voiced >= FakeKokoro.fail_after:
            raise EngineError("the voice engine failed (HTTP 500): interrupted")
        FakeKokoro.voiced += 1
        FakeKokoro.texts.append(text)
        if self.use_gpu and not self.fell_back:
            FakeKokoro.on_card.append(text)
        frames = round(len(text) / 15 * 24000)
        samples = array.array("h", (round(8000 * math.sin(i / 8)) for i in range(frames)))
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(samples.tobytes())


@pytest.fixture(autouse=True)
def no_real_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "AutoKokoro", FakeKokoro)
    monkeypatch.setattr(cli, "check_system", lambda: None)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: ())
    monkeypatch.setattr(cli, "engine_speed", lambda root, engine, measuring=None: None)
    monkeypatch.setattr(cli, "known_cpu_speed", lambda root, engine: None)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: False)
    monkeypatch.setattr(cli, "data_dir", lambda: tmp_path / "data")
    FakeKokoro.fail_with = None
    FakeKokoro.fail_after = None
    FakeKokoro.voiced = 0
    FakeKokoro.made = []
    FakeKokoro.gpu_warning = None
    FakeKokoro.oom_at = None
    FakeKokoro.on_card = []
    FakeKokoro.texts = []
    FakeKokoro.on_exit = None


def _epub(tmp_path: Path) -> Path:
    return build_epub(
        tmp_path / "story.epub",
        [
            Page("one.xhtml", "<h1>Chapter One</h1><p>It began. It went on.</p>"),
            Page("two.xhtml", "<h1>Chapter Two</h1><p>It ended without a period</p>"),
        ],
        toc=[NavPoint("Chapter One", "one.xhtml"), NavPoint("Chapter Two", "two.xhtml")],
        title="A Story",
    )


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"tomecast {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "usage: tomecast" in capsys.readouterr().out


def test_convert_writes_an_audiobook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    book = _epub(tmp_path)

    assert cli.main(["convert", str(book), "--voice", "bf_emma"]) == 0

    output = tmp_path / "story.m4b"
    assert output.is_file()
    out = capsys.readouterr().out
    assert "Converting “A Story”: 2 chapters" in out
    assert f"Done: {output}" in out
    assert not any((tmp_path / "data" / "cache").iterdir())  # work files cleaned up


@pytest.mark.parametrize("name", ["book.docx", "book.fb2", "book.txt", "book.html", "book.htm"])
def test_convert_accepts_every_readable_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    book = book_in_format(tmp_path, name)

    assert cli.main(["convert", str(book)]) == 0

    assert book.with_suffix(".m4b").is_file()
    assert "2 chapters" in capsys.readouterr().out


def test_convert_writes_one_mp3_per_chapter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _epub(tmp_path)

    assert cli.main(["convert", str(book), "--format", "mp3"]) == 0

    folder = tmp_path / "story"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Chapter One.mp3",
        "02 - Chapter Two.mp3",
    ]
    assert not (tmp_path / "story.m4b").exists()
    assert f"Done: {folder} " in capsys.readouterr().out


def test_convert_mp3_honours_output_and_refuses_an_existing_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _epub(tmp_path)
    taken = tmp_path / "mine"
    taken.mkdir()

    assert cli.main(["convert", str(book), "--format", "mp3", "-o", str(taken)]) == 1

    assert "already exists (use --force" in capsys.readouterr().err
    assert list(taken.iterdir()) == []
    assert cli.main(["convert", str(book), "--format", "mp3", "-o", str(taken), "--force"]) == 0
    assert len(list(taken.glob("*.mp3"))) == 2


def test_convert_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _epub(tmp_path)
    (tmp_path / "story.m4b").write_bytes(b"keep me")

    assert cli.main(["convert", str(book)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert (tmp_path / "story.m4b").read_bytes() == b"keep me"

    assert cli.main(["convert", str(book), "--force"]) == 0
    assert (tmp_path / "story.m4b").read_bytes() != b"keep me"


@pytest.mark.parametrize("force", [[], ["--force"]])
@pytest.mark.parametrize(
    ("options", "taken", "message"),
    [
        (["-o", "{tmp}/Audiobooks"], "folder", "Audiobooks is a folder. Give the audiobook"),
        (["--format", "mp3", "-o", "{tmp}/mine"], "file", "mine is a file, but --format mp3"),
        (["-o", "{tmp}/story.mp3"], None, "story.mp3 ends in .mp3, but the audiobook is an M4B"),
    ],
)
def test_convert_refuses_an_output_that_cant_work_before_voicing_anything(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    force: list[str],
    options: list[str],
    taken: str | None,
    message: str,
) -> None:
    book = _epub(tmp_path)
    options = [option.format(tmp=tmp_path) for option in options]
    output = Path(options[-1])
    if taken == "folder":
        output.mkdir()
    elif taken == "file":
        output.write_bytes(b"keep me")

    assert cli.main(["convert", str(book), *options, *force]) == 1

    err = capsys.readouterr().err
    assert message in err
    assert "Run the same command again" not in err
    assert FakeKokoro.voiced == 0  # refused in the first second, not after the whole book
    if taken == "folder":
        assert list(output.iterdir()) == []
    elif taken == "file":
        assert output.read_bytes() == b"keep me"
    else:
        assert not output.exists()


def test_convert_mp3_of_a_book_without_an_extension_doesnt_aim_at_the_book(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _epub(tmp_path).rename(tmp_path / "story")  # the reader sniffs it as an EPUB
    original = book.read_bytes()

    assert cli.main(["convert", str(book), "--format", "mp3", "--force"]) == 1

    assert "story is a file, but --format mp3" in capsys.readouterr().err
    assert FakeKokoro.voiced == 0
    assert book.read_bytes() == original
    assert cli.main(["convert", str(book), "--format", "mp3", "-o", str(tmp_path / "s")]) == 0


@pytest.mark.parametrize("output_format", ["m4b", "mp3"])
def test_convert_never_replaces_an_output_that_appears_while_voicing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    """Two runs with different voices aim at the same story.m4b: the one that finishes
    second doesn't replace the first one's book, though both passed the check at the start."""
    book = _epub(tmp_path)
    target = tmp_path / ("story" if output_format == "mp3" else "story.m4b")
    real = FakeKokoro.synthesize

    def another_run_finishes_first(self: FakeKokoro, text: str, output: Path) -> None:
        if output_format == "mp3":
            target.mkdir(exist_ok=True)
            (target / "01 - Chapter One.mp3").write_bytes(b"another run's book")
        else:
            target.write_bytes(b"another run's book")
        real(self, text, output)

    monkeypatch.setattr(FakeKokoro, "synthesize", another_run_finishes_first)
    arguments = ["convert", str(book), "--format", output_format]

    assert cli.main(arguments) == 1

    err = capsys.readouterr().err
    assert "appeared while this book was being made, so it wasn't replaced" in err
    theirs = target / "01 - Chapter One.mp3" if output_format == "mp3" else target
    assert theirs.read_bytes() == b"another run's book"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["data", "story.epub", target.name])

    # Its audio is kept, so another name carries on from there without voicing again.
    monkeypatch.setattr(FakeKokoro, "synthesize", real)
    voiced = FakeKokoro.voiced
    mine = tmp_path / ("mine" if output_format == "mp3" else "mine.m4b")
    assert cli.main([*arguments, "-o", str(mine)]) == 0
    assert "Picked up where the last run stopped (5 pieces)" in capsys.readouterr().out
    assert FakeKokoro.voiced == voiced
    assert mine.exists()
    assert theirs.read_bytes() == b"another run's book"


def test_convert_with_force_replaces_an_output_that_appears_while_voicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _epub(tmp_path)
    target = tmp_path / "story.m4b"
    real = FakeKokoro.synthesize

    def another_run_finishes_first(self: FakeKokoro, text: str, output: Path) -> None:
        target.write_bytes(b"another run's book")
        real(self, text, output)

    monkeypatch.setattr(FakeKokoro, "synthesize", another_run_finishes_first)

    assert cli.main(["convert", str(book), "--force"]) == 0
    assert target.read_bytes() != b"another run's book"


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("missing.epub", None, "doesn't exist"),
        ("book.pdf", b"%PDF-1.7", "book.pdf isn't a PDF, or it's damaged"),
        ("notes.xyz", b"hello", "reads EPUB, PDF, DOCX, FB2, HTML and TXT books"),
        ("picture.txt", bytes(range(256)) * 20, "isn't a text file"),
        ("broken.epub", b"not a zip", "not"),
    ],
)
def test_convert_explains_bad_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: bytes | None,
    message: str,
) -> None:
    path = tmp_path / name
    if content is not None:
        path.write_bytes(content)
    assert cli.main(["convert", str(path)]) == 1
    assert message in capsys.readouterr().err


def test_downloads_need_permission_when_not_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset = Asset(
        "Kokoro voice model", "https://example.invalid/m.gguf", "0" * 64, 190_000_000, "m"
    )
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cli.main(["convert", str(_epub(tmp_path))]) == 1

    captured = capsys.readouterr()
    assert "Kokoro voice model (190 MB)" in captured.out
    assert "--yes" in captured.err


def test_engine_failures_are_reported_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeKokoro.fail_with = "the voice engine failed (HTTP 500): out of memory"

    assert cli.main(["convert", str(_epub(tmp_path))]) == 1

    err = capsys.readouterr().err
    assert "out of memory" in err
    assert "saved" not in err  # nothing was voiced, so there's nothing to resume
    assert not (tmp_path / "story.m4b").exists()


def test_voices_lists_every_language(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    assert "American English: af_alloy" in out
    assert "bm_george" in out
    assert "Default: af_heart" in out


@pytest.mark.parametrize("speed", ["3.5", "0.1", "fast"])
def test_a_bad_speed_is_rejected_before_anything_happens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], speed: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["convert", str(_epub(tmp_path)), "--speed", speed])
    assert exit_info.value.code == 2
    assert "--speed" in capsys.readouterr().err


def test_a_failed_run_keeps_its_work_and_the_next_run_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _epub(tmp_path)
    FakeKokoro.fail_after = 2

    assert cli.main(["convert", str(book)]) == 1
    assert "Run the same command again to continue" in capsys.readouterr().err

    FakeKokoro.fail_after = None
    assert cli.main(["convert", str(book)]) == 0
    out = capsys.readouterr().out
    assert "Picked up where the last run stopped (2 pieces)" in out
    assert not list((tmp_path / "data" / "cache").glob("convert-*"))  # cleaned up on success


def test_the_same_book_cant_be_converted_twice_at_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _epub(tmp_path)
    workdir = work_folder(tmp_path / "data", book, "af_heart", 1.0)
    with (workdir / ".lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # another run is busy with this book

        assert cli.main(["convert", str(book)]) == 1

    assert "already being converted" in capsys.readouterr().err


def test_gui_passes_the_book_to_the_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: Sequence[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr("tomecast.gui.run", fake_run)
    assert cli.main(["gui"]) == 0
    assert cli.main(["gui", str(tmp_path / "book.epub")]) == 0
    assert seen == [[], [str(tmp_path / "book.epub")]]


def test_gui_that_cannot_start_reports_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(argv: Sequence[str] | None = None) -> int:
        raise ImportError("libGL.so.1: cannot open shared object file")

    monkeypatch.setattr("tomecast.gui.run", broken)
    assert cli.main(["gui"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("tomecast: error: the desktop window can't start")
    assert "libGL" in err


def test_desktop_entry_install_and_uninstall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "none"))
    assert cli.main(["desktop-entry", "install"]) == 0
    assert (tmp_path / "applications" / "tomecast.desktop").is_file()
    assert "app launcher" in capsys.readouterr().out
    assert cli.main(["desktop-entry", "uninstall"]) == 0
    assert not (tmp_path / "applications" / "tomecast.desktop").exists()


def test_desktop_entry_needs_an_action() -> None:
    with pytest.raises(SystemExit):
        cli.main(["desktop-entry"])


def test_the_cpu_is_used_when_there_is_no_vulkan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    assert [engine.use_gpu for engine in FakeKokoro.made] == [False]
    assert "Using the CPU (no graphics card found)." in capsys.readouterr().out


def test_the_graphics_card_is_the_default_when_vulkan_is_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    assert [engine.use_gpu for engine in FakeKokoro.made] == [True]
    out = capsys.readouterr().out
    assert "Using the graphics card: NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU." in out


def test_no_gpu_turns_the_graphics_card_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)

    assert cli.main(["convert", str(_epub(tmp_path)), "--no-gpu"]) == 0

    assert [engine.use_gpu for engine in FakeKokoro.made] == [False]
    assert "Using the CPU (turned off with --no-gpu)." in capsys.readouterr().out


def test_gpu_without_a_vulkan_driver_says_so_and_uses_the_cpu(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["convert", str(_epub(tmp_path)), "--gpu"]) == 0

    assert [engine.use_gpu for engine in FakeKokoro.made] == [False]
    out = capsys.readouterr().out
    assert "No usable Vulkan graphics driver was found" in out
    assert "vulkan-icd-loader" in out


def test_a_card_that_didnt_work_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    FakeKokoro.gpu_warning = "The graphics card didn't work, so Tomecast used the CPU."

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "Using the CPU." in out
    assert "Note: The graphics card didn't work, so Tomecast used the CPU." in out


def test_the_work_folder_depends_on_the_engine_build(tmp_path: Path) -> None:
    book = _epub(tmp_path)
    root = tmp_path / "data"
    assert work_folder(root, book, "af_heart", 1.0, use_gpu=True) != work_folder(
        root, book, "af_heart", 1.0, use_gpu=False
    )


def test_doctor_prints_what_a_bug_report_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)

    assert cli.main(["doctor"]) == 0

    out = capsys.readouterr().out
    for expected in (
        f"Tomecast {__version__}",
        "Distro: ",
        "glibc: ",
        "CPU threads: ",
        "Vulkan possible: yes",
        "Vulkan devices: not checked (the graphics-card engine isn't installed yet)",
        "audio.cpp 0.9.4 voice engine (CPU): no",
        "Kokoro voice model (82M, 8-bit): no",
    ):
        assert expected in out


def test_doctor_lists_devices_when_the_vulkan_engine_is_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "is_installed", lambda asset, root: "graphics" in asset.name)
    monkeypatch.setattr(
        cli,
        "build_in_use",
        lambda asset, root: root / "engines" / "vulkan" if "graphics" in asset.name else None,
    )
    monkeypatch.setattr(
        cli,
        "list_devices",
        lambda executable: [
            GpuDevice(0, "Vulkan0", "NVIDIA RTX PRO 1000", "gpu", 7 * 2**30, 8 * 2**30)
        ],
    )

    assert cli.main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "0: NVIDIA RTX PRO 1000 (gpu, 7.0 of 8.0 GiB free)" in out
    assert "(graphics card): yes" in out


# --- text prep and pronunciation lists --------------------------------------------------------


def _story_with_names(tmp_path: Path) -> Path:
    paragraph = "Then Zorp met Quillon. Zorp paid $5 to Dr. Smith. " * 2
    return build_epub(
        tmp_path / "names.epub",
        [Page("one.xhtml", f"<h1>Chapter 1</h1><p>{paragraph}</p><p>Then Zorp left Quillon.</p>")],
        toc=[NavPoint("Chapter 1", "one.xhtml")],
        title="Names",
    )


def test_convert_says_numbers_and_abbreviations_right(tmp_path: Path) -> None:
    assert cli.main(["convert", str(_story_with_names(tmp_path))]) == 0
    spoken = " ".join(FakeKokoro.texts)
    assert "Chapter one" in spoken
    assert "paid five dollars to Doctor Smith" in spoken
    assert "$" not in spoken


def test_convert_uses_the_global_and_the_books_pronunciation_lists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _story_with_names(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pronunciations.txt").write_text(
        "Zorp → zorp-ee\nQuillon → kwill-ON\n", encoding="utf-8"
    )
    (tmp_path / "names.pronunciations.txt").write_text("Zorp → zawp\n", encoding="utf-8")

    assert cli.main(["convert", str(book)]) == 0

    spoken = " ".join(FakeKokoro.texts)
    assert "Then zawp met kwill-on." in spoken  # the book's list wins; ON was lowercased
    assert "Zorp" not in spoken
    assert "Using 3 pronunciation fixes." in capsys.readouterr().out


def test_convert_warns_about_bad_lines_in_a_list_and_carries_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _story_with_names(tmp_path)
    (tmp_path / "names.pronunciations.txt").write_text(
        "Zorp → /zɔːp/\nthis line has no arrow\nQuillon → kwill-on\n", encoding="utf-8"
    )

    assert cli.main(["convert", str(book)]) == 0

    err = capsys.readouterr().err
    assert "names.pronunciations.txt, line 1" in err
    assert "names.pronunciations.txt, line 2" in err
    assert "kwill-on" in " ".join(FakeKokoro.texts)


def test_names_lists_the_unusual_words_with_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["names", str(_story_with_names(tmp_path))]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Zorp", "5"]
    assert lines[1].split() == ["Quillon", "3"]
    assert any("tomecast preview" in line and "names.pronunciations.txt" in line for line in lines)


def test_names_skips_words_that_already_have_a_fix_and_honours_min_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _story_with_names(tmp_path)
    (tmp_path / "names.pronunciations.txt").write_text("zorp → zawp\n", encoding="utf-8")
    assert cli.main(["names", str(book)]) == 0
    assert capsys.readouterr().out.splitlines()[0].split() == ["Quillon", "3"]

    assert cli.main(["names", str(book), "--min-count", "50"]) == 0
    assert capsys.readouterr().out == "No unusual names found.\n"


def test_names_limit_shows_the_most_frequent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["names", str(_story_with_names(tmp_path)), "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "Zorp" in out
    assert "Quillon" not in out.split("…and")[0]
    assert "…and 1 more" in out


def test_names_explains_bad_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["names", str(tmp_path / "missing.epub")]) == 1
    assert "doesn't exist" in capsys.readouterr().err
    odt = tmp_path / "book.odt"  # a format Tomecast doesn't read
    odt.write_bytes(b"PK\x03\x04 not really")
    assert cli.main(["names", str(odt)]) == 1
    assert "can't read .odt files" in capsys.readouterr().err


# --- preview ----------------------------------------------------------------------------------


@pytest.fixture
def played(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Pretend there is an audio player, and record which WAV files it was asked to play."""
    heard: list[Path] = []

    def fake_play(wav: Path) -> None:
        assert wav.stat().st_size > 0
        heard.append(wav)

    monkeypatch.setattr(cli, "find_player", lambda: "/usr/bin/fake-player")
    monkeypatch.setattr(cli, "play", fake_play)
    return heard


def test_preview_voices_the_text_and_plays_it(
    played: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["preview", "Galadriel"]) == 0
    assert FakeKokoro.texts == ["Galadriel"]
    assert len(played) == 1
    assert "Saying" not in capsys.readouterr().out  # nothing changed, so nothing to announce


def test_preview_takes_several_words_without_quotes(played: list[Path]) -> None:
    assert cli.main(["preview", "Hello", "there", "Galadriel"]) == 0
    assert FakeKokoro.texts == ["Hello there Galadriel"]


def test_preview_applies_the_lists_and_the_number_rules(
    tmp_path: Path, played: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pronunciations.txt").write_text("Galadriel → ga-LAD-ree-el\n")
    assert cli.main(["preview", "Galadriel paid $5."]) == 0
    assert FakeKokoro.texts == ["ga-lad-ree-el paid five dollars."]
    assert "Saying: ga-lad-ree-el paid five dollars." in capsys.readouterr().out


def test_preview_can_use_a_books_list(tmp_path: Path, played: list[Path]) -> None:
    book = tmp_path / "story.epub"
    (tmp_path / "story.pronunciations.txt").write_text("Zorp → zawp\n")
    assert cli.main(["preview", "Zorp", "--book", str(book)]) == 0
    assert FakeKokoro.texts == ["zawp"]
    assert cli.main(["preview", "Zorp"]) == 0
    assert FakeKokoro.texts[-1] == "Zorp"


def test_preview_raw_says_exactly_what_was_typed(tmp_path: Path, played: list[Path]) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pronunciations.txt").write_text("Galadriel → ga-lad-ree-el\n")
    assert cli.main(["preview", "--raw", "Galadriel $5"]) == 0
    assert FakeKokoro.texts == ["Galadriel $5"]


def test_preview_writes_a_wav_file_instead_of_playing(
    tmp_path: Path, played: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "word.wav"
    assert cli.main(["preview", "Galadriel", "-o", str(out)]) == 0
    assert played == []
    with wave.open(str(out)) as wav:
        assert wav.getframerate() == 24000
        assert wav.getnframes() > 0
    assert f"Saved {out}" in capsys.readouterr().out


def test_preview_will_not_overwrite_without_force(
    tmp_path: Path, played: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "word.wav"
    out.write_bytes(b"keep me")
    assert cli.main(["preview", "Galadriel", "-o", str(out)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert out.read_bytes() == b"keep me"
    assert cli.main(["preview", "Galadriel", "-o", str(out), "--force"]) == 0
    assert out.read_bytes() != b"keep me"


def test_preview_with_a_long_text_voices_it_in_pieces_and_joins_them(
    tmp_path: Path, played: list[Path]
) -> None:
    sentence = "The boats drifted slowly toward the far green shore. "
    out = tmp_path / "long.wav"
    assert cli.main(["preview", sentence * 8, "-o", str(out)]) == 0
    assert len(FakeKokoro.texts) > 1
    assert all(len(text) <= 200 for text in FakeKokoro.texts)
    assert " ".join(FakeKokoro.texts) == (sentence * 8).strip()


def test_preview_passes_the_voice_and_speed_to_the_engine(
    played: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    class Recording(FakeKokoro):
        def __init__(
            self, root: Path, *, voice: str, speed: float, use_gpu: bool = False, **kwargs: object
        ) -> None:
            super().__init__(root, voice=voice, speed=speed, use_gpu=use_gpu, **kwargs)
            seen.update(voice=voice, speed=speed)

    monkeypatch.setattr(cli, "AutoKokoro", Recording)
    assert cli.main(["preview", "Hi", "--voice", "bm_george", "--speed", "1.25"]) == 0
    assert seen == {"voice": "bm_george", "speed": 1.25}


def test_preview_uses_the_graphics_card_when_there_is_one(
    played: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    assert cli.main(["preview", "Hi"]) == 0
    assert FakeKokoro.made[-1].use_gpu is True
    assert cli.main(["preview", "Hi", "--no-gpu"]) == 0
    assert FakeKokoro.made[-1].use_gpu is False


def test_preview_says_when_it_fell_back_to_the_cpu(
    played: list[Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    FakeKokoro.gpu_warning = "The graphics card didn't start, so the CPU is used."
    assert cli.main(["preview", "Hi"]) == 0
    assert "the CPU is used" in capsys.readouterr().err


def test_preview_says_when_the_card_ran_out_of_memory_partway(
    played: list[Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    FakeKokoro.oom_at = 1
    assert cli.main(["preview", "Hi"]) == 0
    assert capsys.readouterr().err.count(f"Note: {audiocpp.RAN_OUT_OF_MEMORY_NOTE}") == 1


def test_preview_needs_something_to_say(
    played: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["preview", "...", "***"]) == 1
    assert "nothing to say" in capsys.readouterr().err
    assert FakeKokoro.texts == []


def test_preview_without_an_audio_player_suggests_a_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "find_player", lambda: None)
    assert cli.main(["preview", "Hi"]) == 1
    assert "-o FILE.wav" in capsys.readouterr().err
    assert FakeKokoro.texts == []


def test_preview_reports_a_player_that_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(wav: Path) -> None:
        raise RuntimeError("pw-play failed: no sound server")

    monkeypatch.setattr(cli, "find_player", lambda: "/usr/bin/fake-player")
    monkeypatch.setattr(cli, "play", fail)
    assert cli.main(["preview", "Hi"]) == 1
    assert "no sound server" in capsys.readouterr().err


def test_preview_reports_engine_failures(
    played: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    FakeKokoro.fail_with = "the voice engine failed (HTTP 500): out of memory"
    assert cli.main(["preview", "Hi"]) == 1
    assert "out of memory" in capsys.readouterr().err


def test_preview_and_names_are_listed_in_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "preview" in out
    assert "names" in out


# --- PDF books and cleanup ---------------------------------------------------------------------


def _pdf(tmp_path: Path) -> Path:
    """Contents, then two short chapters over three pages with a running header, page
    numbers and a footnote."""
    contents = PageBuilder()
    contents.heading("Contents", size=16)
    contents.line("Chapter One ........ 1")
    contents.line("Chapter Two ........ 2")
    contents.line("Afterword ........ 3")
    one = PageBuilder().header("A STORY").footer("1")
    one.heading("Chapter One", size=16)
    one.paragraph(["It began.^1^ It went on."])
    one.footnotes(["^1^ As stories do."])
    two = PageBuilder().header("A STORY").footer("2")
    two.heading("Chapter Two", size=16)
    two.paragraph(["It ended without a"])
    three = PageBuilder().header("A STORY").footer("3")
    three.paragraph(["period"], indent=0)
    return build_pdf(
        tmp_path / "story.pdf", [contents.page, one.page, two.page, three.page], title="A Story"
    )


def test_convert_reads_a_pdf_and_leaves_out_the_page_furniture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["convert", str(_pdf(tmp_path))]) == 0

    assert (tmp_path / "story.m4b").is_file()
    out = capsys.readouterr().out
    assert "Skipping 1 section: “Contents”." in out
    assert (
        "Left out: page numbers and running headers and footers (6), footnote markers (1), "
        "footnote text (1)."
    ) in out
    assert "Converting “A Story”: 2 chapters" in out
    assert " ".join(FakeKokoro.texts) == (
        "Chapter One It began. It went on. Chapter Two It ended without a period"
    )


def test_convert_cleanup_options(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    book = _pdf(tmp_path)
    arguments = ["convert", str(book), "--force", "--keep-front-matter", "--keep-page-numbers"]

    assert cli.main([*arguments, "--footnotes", "end"]) == 0

    out = capsys.readouterr().out
    assert "Skipping" not in out
    assert "Left out: footnote markers (1)." in out
    assert "Converting “A Story”: 3 chapters" in out
    assert " ".join(FakeKokoro.texts) == (
        # Kept page numbers and headers are spoken like any text, numbers as words.
        "Contents Chapter One ........ one Chapter Two ........ two Afterword ........ three "
        "A STORY Chapter One It began. It went on. one A STORY As stories do. Chapter Two "
        "It ended without a two A STORY period three"
    )


def test_convert_rejects_an_unknown_footnote_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        cli.main(["convert", str(_pdf(tmp_path)), "--footnotes", "inline"])
    assert "invalid choice: 'inline'" in capsys.readouterr().err


# --- engine updates, speed and estimates ------------------------------------------------------


def _engine_update(tmp_path: Path, *, gpu: bool = False) -> tuple[Asset, Path]:
    """An engine whose 0.1.x build is installed, with a newer build pinned (downloadable from
    a local file). Returns the asset and the old build's folder."""
    root = tmp_path / "data"
    legacy = root / "engines" / "engine-0.1"
    legacy.mkdir(parents=True)
    (legacy / ".tomecast-sha256").write_text("e" * 64 + "\n")
    archive = tmp_path / "new-engine.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"new build"
        info = tarfile.TarInfo("audiocpp_server")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    blob = archive.read_bytes()
    asset = Asset(
        "audio.cpp voice engine (CPU)", archive.as_uri(), hashlib.sha256(blob).hexdigest(),
        len(blob), "engines/engine", archive="tar.gz", replaces=("engines/engine-0.1",),
        id="audiocpp-vulkan" if gpu else "audiocpp-cpu",
    )  # fmt: skip
    return asset, legacy


def test_convert_shows_the_estimate_before_converting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    measured: list[object] = []

    def speed(root: Path, engine: object, measuring: object = None) -> float:
        measured.append(engine)
        return 4.0

    monkeypatch.setattr(cli, "engine_speed", speed)

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "Under 1 min of audio, under 1 min to make here (4.0× real time)." in out
    assert measured == FakeKokoro.made  # measured on the engine that converts
    assert out.index("real time).") < out.index("Done:")


def test_the_estimate_line_rounds_like_the_window() -> None:
    hour = 158 * 60
    assert cli._estimate_line(hour * 3, 1.0, 4.5) == (
        "About 3 h 00 min of audio, about 40 min to make here (4.5× real time)."
    )
    assert cli._rough_minutes(0.4) == "under 1 min"
    assert cli._rough_minutes(9.5) == "about 10 min"
    assert cli._rough_minutes(167) == "about 2 h 47 min"


def test_an_update_isnt_downloaded_without_permission_and_the_old_build_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, legacy = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0  # a script keeps working

    out = capsys.readouterr().out
    assert "Tomecast has an update to download:" in out
    assert "run again with --yes to download the update" in out
    assert build_in_use(asset, tmp_path / "data") == legacy


def test_an_update_that_fails_its_check_is_reported_and_the_old_build_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, legacy = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: "crashed")

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0

    out = capsys.readouterr().out
    assert "Checking the new voice engine on this computer" in out
    assert (
        "Note: The voice engine update didn't work on this computer, so Tomecast kept using "
        "the previous version."
    ) in out
    assert build_in_use(asset, tmp_path / "data") == legacy
    assert (tmp_path / "story.m4b").is_file()  # and the book was still made


def test_an_update_that_passes_is_switched_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, legacy = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0

    assert "Note:" not in capsys.readouterr().out
    in_use = build_in_use(asset, tmp_path / "data")
    assert in_use is not None and in_use.parent.name == "engine"
    assert legacy.is_dir()  # kept as the fallback


def test_doctor_says_when_an_update_failed_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, _ = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(
        audiocpp,
        "self_check",
        lambda root, gpu, cancelled: lambda new, old: "illegal instruction\nmore",
    )
    monkeypatch.setattr(cli, "ASSETS", (asset,))
    cli.main(["convert", str(_epub(tmp_path)), "--yes"])
    capsys.readouterr()

    assert cli.main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert (
        "audio.cpp voice engine (CPU): an older build (eeeeeeeeeeee); the update failed its "
        "check here: illegal instruction"
    ) in out


def test_scan_tries_failed_updates_again_and_measures_the_speeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, legacy = _engine_update(tmp_path, gpu=True)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    # Fails in the conversion (and its one retry), then passes in the scan.
    results = ["crashed", "crashed", None]
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, c: lambda n, o: results.pop(0))
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    probed: list[bool] = []

    def fake_probe(root: Path, *, gpu: bool) -> Speeds:
        probed.append(gpu)
        return Speeds(cpu=4.6, gpu=19.8, gpu_description="NVIDIA RTX PRO 1000")

    monkeypatch.setattr(cli, "probe", fake_probe)
    root = tmp_path / "data"
    cli.main(["convert", str(_epub(tmp_path)), "--yes"])
    assert build_in_use(asset, root) == legacy
    (root / "speeds.json").write_text("{}")

    assert cli.main(["scan"]) == 0

    out = capsys.readouterr().out
    assert build_in_use(asset, root) != legacy  # tried again, and it passed
    assert not (root / "speeds.json").exists()  # forgotten, so measured again
    assert probed == [True]
    assert "CPU, " in out and "about 4.6× real time." in out
    assert "NVIDIA RTX PRO 1000: about 19.8× real time, 4.3× as fast as the CPU." in out


def test_scan_without_a_card_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    monkeypatch.setattr(cli, "probe", lambda root, gpu, cancelled=None: Speeds(cpu=4.6))

    assert cli.main(["scan"]) == 0

    assert "Graphics card: not usable, so Tomecast uses the CPU." in capsys.readouterr().out


# --- Memory and battery ----------------------------------------------------------------------


def test_doctor_prints_memory_power_and_the_thread_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(
        monkeypatch, tmp_path / "laptop", total_gib=16, available_gib=9.5, supplies=ON_BATTERY
    )

    assert cli.main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "CPU threads: 16\n" in out
    assert "Memory: 16.0 GiB, 9.5 GiB available\n" in out
    assert "Power: on battery (a laptop)\n" in out
    assert "Voice threads: 4 (usually 8; fewer on battery)\n" in out


def test_doctor_on_a_desktop_and_on_a_machine_it_cant_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 4)
    pin_machine(monkeypatch, tmp_path / "desktop", total_gib=3.7, available_gib=None, supplies={})
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Memory: 3.7 GiB, unknown available\n" in out
    assert "Power: no battery (a desktop)\n" in out
    assert "Voice threads: 1 (usually 2; fewer with little memory)\n" in out

    monkeypatch.setattr(machine, "MEMINFO", tmp_path / "missing")
    monkeypatch.setattr(machine, "POWER_SUPPLY", tmp_path / "missing")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Memory: unknown\nPower: unknown\nVoice threads: 2\n" in out


def test_convert_on_battery_uses_fewer_threads_and_warns_about_free_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "laptop", total_gib=8, available_gib=1, supplies=ON_BATTERY)

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert FakeKokoro.made[-1].kwargs["threads"] == 4
    assert "Using 4 CPU threads (usually 8; fewer on battery).\n" in out
    note = "Note: Only 1.1 GB of memory is free, and the voice needs about 1.8 GB while it works"
    assert note in out
    assert out.index(note) < out.index("Using the CPU") < out.index("Done:")


def test_convert_plugged_in_uses_the_usual_threads_and_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "laptop", supplies=PLUGGED_IN)

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert FakeKokoro.made[-1].kwargs["threads"] == 8
    assert "CPU thread" not in out
    assert "memory" not in out


def test_scan_shows_the_machine_and_why_it_uses_fewer_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(
        monkeypatch, tmp_path / "laptop", total_gib=16, available_gib=1, supplies=ON_BATTERY
    )
    monkeypatch.setattr(cli, "probe", lambda root, gpu, cancelled=None: Speeds(cpu=2.6))

    assert cli.main(["scan"]) == 0

    out = capsys.readouterr().out
    assert "This computer: 16 CPU threads, 17.2 GB of memory (1.1 GB free), on battery.\n" in out
    assert "Note: Only 1.1 GB of memory is free" in out
    assert "CPU, 4 threads (usually 8; fewer on battery): about 2.6× real time.\n" in out


def test_an_update_that_crashes_on_the_book_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, legacy = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)
    FakeKokoro.fail_with = "the voice engine keeps crashing. Its log:\nSegmentation fault"
    root = tmp_path / "data"

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 1

    err = capsys.readouterr().err
    assert "keeps crashing" in err
    assert (
        "Note: The voice engine update didn't work on this computer, so Tomecast kept using "
        "the previous version. Run the same command again to make the book with it."
    ) in err
    assert build_in_use(asset, root) == legacy
    pinned = [made.kwargs["builds"] for made in FakeKokoro.made]
    assert pinned == [{"audiocpp-cpu": root / asset.path / asset.sha256[:12]}]  # the new one

    FakeKokoro.fail_with = None
    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0  # the next run
    assert FakeKokoro.made[-1].kwargs["builds"] == {"audiocpp-cpu": legacy}
    assert "Checking the new voice engine" not in capsys.readouterr().out  # not tried again


def test_a_finished_book_proves_the_new_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset, _ = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)
    root = tmp_path / "data"
    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0
    builds = {"audiocpp-cpu": root / asset.path / asset.sha256[:12]}

    assert not roll_back(root, builds, "a later failure is the book's fault, not the build's")
    assert build_in_use(asset, root) == builds["audiocpp-cpu"]


# --- Second review: Done first, fallbacks prove nothing, scans keep the choice ----------------


def _slot(tmp_path: Path, name: str) -> dict[str, object]:
    state = json.loads((tmp_path / "data" / "installed.json").read_text())
    slot: dict[str, object] = state["slots"][name]
    return slot


def test_done_doesnt_wait_for_another_tomecasts_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, _ = _engine_update(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)
    held: list[object] = []

    def another_tomecast_starts_installing() -> None:  # as the book is being packaged
        lock = (tmp_path / "data" / ".install.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        held.append(lock)
        threading.Timer(8, lock.close).start()  # it holds the lock for 8 s

    FakeKokoro.on_exit = another_tomecast_starts_installing
    started = time.monotonic()

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0

    assert time.monotonic() - started < 6  # not held up by the other install
    assert "Done: " in capsys.readouterr().out
    assert held and _slot(tmp_path, "audiocpp-cpu")["proven"] is False  # proven next time


def test_a_book_made_on_the_cpu_fallback_doesnt_prove_a_card_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset, legacy = _engine_update(tmp_path, gpu=True)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)
    FakeKokoro.gpu_warning = "The graphics card didn't work, so Tomecast used the CPU."

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0

    slot = _slot(tmp_path, "audiocpp-vulkan")
    assert slot["proven"] is False  # the card was never tried with the new build...
    assert slot["previous"] == "engines/engine-0.1"  # ...so the one that worked is kept
    builds = {"audiocpp-vulkan": tmp_path / "data" / asset.path / asset.sha256[:12]}
    assert roll_back(tmp_path / "data", builds, "it later failed on the card")
    assert build_in_use(asset, tmp_path / "data") == legacy


def _newer_tomecasts_build(tmp_path: Path) -> Path:
    """The build in use was chosen by a newer Tomecast (since uninstalled)."""
    root = tmp_path / "data"
    theirs = root / "engines" / "engine" / ("9" * 12)
    theirs.mkdir(parents=True)
    (theirs / ".tomecast-sha256").write_text("9" * 64 + "\n")
    (root / "installed.json").write_text(
        json.dumps(
            {
                "format": 2,
                "slots": {
                    "audiocpp-cpu": {
                        "current": "engines/engine/" + "9" * 12,
                        "previous": None,
                        "chosen_by": "9.9.9",
                        "proven": True,
                    }
                },
            }
        )
    )
    return theirs


def test_scan_takes_the_build_back_for_a_downgraded_tomecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset, _ = _engine_update(tmp_path)
    theirs = _newer_tomecasts_build(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    checked: list[bool] = []

    def passes(new: object, old: object) -> None:
        checked.append(True)

    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: passes)
    monkeypatch.setattr(cli, "probe", lambda root, gpu, cancelled=None: Speeds(cpu=4.6))
    root = tmp_path / "data"
    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0
    assert build_in_use(asset, root) == theirs  # a normal run leaves the newer choice alone

    assert cli.main(["scan", "--no-gpu", "--yes"]) == 0

    assert checked == [True]  # its own build was checked first
    assert build_in_use(asset, root) == root / asset.path / asset.sha256[:12]
    assert _slot(tmp_path, "audiocpp-cpu")["chosen_by"] == __version__  # the new choice


def test_scan_keeps_the_choice_when_its_own_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset, _ = _engine_update(tmp_path)
    theirs = _newer_tomecasts_build(tmp_path)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, c: lambda n, o: "broken here")
    monkeypatch.setattr(cli, "probe", lambda root, gpu, cancelled=None: Speeds(cpu=4.6))

    assert cli.main(["scan", "--no-gpu", "--yes"]) == 0

    assert build_in_use(asset, tmp_path / "data") == theirs
    assert _slot(tmp_path, "audiocpp-cpu")["chosen_by"] == "9.9.9"  # never wiped


# --- The graphics card running out of memory (#49) --------------------------------------------


def test_running_out_of_graphics_memory_mid_book_is_said_and_every_piece_is_voiced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    book = _epub(tmp_path)
    assert cli.main(["convert", str(book)]) == 0  # on the card all the way
    every_piece = list(FakeKokoro.texts)
    assert FakeKokoro.on_card == every_piece
    capsys.readouterr()
    FakeKokoro.texts, FakeKokoro.on_card = [], []
    FakeKokoro.oom_at = 2  # another program takes the card's memory on the second piece
    monkeypatch.setattr(cli, "known_cpu_speed", lambda root, engine: 3.1)

    assert cli.main(["convert", str(book), "--force"]) == 0

    out = capsys.readouterr().out
    assert "Using the graphics card: NVIDIA RTX PRO 1000" in out
    note = (
        "Note: The graphics card ran out of memory, so Tomecast carried on with the same voice "
        "on the CPU (slower)."
    )
    assert out.count(note) == 1  # once, when it happened
    assert "Under 1 min more to make on the CPU (3.1× real time)." in out
    assert out.index(note) < out.index("Done:")
    assert FakeKokoro.texts == every_piece  # each piece once, in order: nothing lost
    assert FakeKokoro.on_card == every_piece[:1]  # the rest on the CPU


def test_running_out_of_graphics_memory_at_start_is_said_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    FakeKokoro.gpu_warning = audiocpp.OUT_OF_MEMORY_NOTE

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "Using the CPU." in out
    assert out.count(f"Note: {audiocpp.OUT_OF_MEMORY_NOTE}") == 1


def test_a_book_that_ran_out_of_graphics_memory_doesnt_prove_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset, _ = _engine_update(tmp_path, gpu=True)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)
    FakeKokoro.oom_at = 1

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 0

    slot = _slot(tmp_path, "audiocpp-vulkan")
    assert slot["proven"] is False  # the book was made on the CPU...
    assert slot["previous"] == "engines/engine-0.1"  # ...so the build that worked is kept


def test_a_failure_after_running_out_of_graphics_memory_doesnt_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asset, _ = _engine_update(tmp_path, gpu=True)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: (asset,))
    monkeypatch.setattr(audiocpp, "self_check", lambda root, gpu, cancelled: lambda n, o: None)
    FakeKokoro.oom_at = 1
    FakeKokoro.fail_after = 1  # then the CPU fails too (short of memory as well)
    root = tmp_path / "data"

    assert cli.main(["convert", str(_epub(tmp_path)), "--yes"]) == 1

    err = capsys.readouterr().err
    assert "interrupted" in err
    assert UPDATE_FAILED_NOTE not in err
    new_build = root / asset.path / asset.sha256[:12]
    assert build_in_use(asset, root) == new_build  # not rolled back...
    assert failure(asset, root) is None  # ...and not blamed


def test_the_threads_line_follows_a_switch_to_the_cpu_partway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "cpu_count", lambda: 16)
    pin_machine(monkeypatch, tmp_path / "laptop", supplies=ON_BATTERY)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    FakeKokoro.oom_at = 2

    assert cli.main(["convert", str(_epub(tmp_path))]) == 0

    out = capsys.readouterr().out
    threads = "Using 4 CPU threads (usually 8; fewer on battery).\n"
    assert out.count(threads) == 1  # not at first: it began on the card
    assert f"Note: {audiocpp.RAN_OUT_OF_MEMORY_NOTE}\n{threads}" in out  # right after the note
    assert FakeKokoro.made[-1].kwargs["threads"] == 4
