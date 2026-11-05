import array
import fcntl
import math
import wave
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import ClassVar

import pytest
from epub_factory import NavPoint, Page, build_epub
from format_factory import book_in_format

from tomecast import __version__, cli
from tomecast.downloads import Asset
from tomecast.engines.audiocpp import EngineError
from tomecast.hardware import GpuDevice
from tomecast.workdirs import work_folder


class FakeKokoro:
    """Stands in for KokoroServer: same constructor and context manager, no real engine."""

    fail_with: str | None = None
    fail_after: int | None = None  # chunks to voice before failing, like an interrupted run
    voiced = 0
    made: ClassVar[list["FakeKokoro"]] = []
    gpu_description = "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"
    gpu_warning: str | None = None  # set: a use_gpu run falls back to the CPU

    def __init__(self, root: Path, *, voice: str, speed: float, use_gpu: bool = False) -> None:
        self.label = f"Fake ({voice})"
        self.max_chars = 200
        self.sample_rate = 24000
        self.use_gpu = use_gpu
        self.warning: str | None = None
        self.gpu_description = ""
        FakeKokoro.made.append(self)

    def __enter__(self) -> "FakeKokoro":
        if self.use_gpu and FakeKokoro.gpu_warning:
            self.warning = FakeKokoro.gpu_warning
        elif self.use_gpu:
            self.gpu_description = FakeKokoro.gpu_description
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    def synthesize(self, text: str, output: Path) -> None:
        if self.fail_with:
            raise EngineError(self.fail_with)
        if FakeKokoro.fail_after is not None and FakeKokoro.voiced >= FakeKokoro.fail_after:
            raise EngineError("the voice engine failed (HTTP 500): interrupted")
        FakeKokoro.voiced += 1
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
    monkeypatch.setattr(cli, "required_assets", lambda use_gpu: ())
    monkeypatch.setattr(cli, "vulkan_possible", lambda: False)
    monkeypatch.setattr(cli, "data_dir", lambda: tmp_path / "data")
    FakeKokoro.fail_with = None
    FakeKokoro.fail_after = None
    FakeKokoro.voiced = 0
    FakeKokoro.made = []
    FakeKokoro.gpu_warning = None


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


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("missing.epub", None, "doesn't exist"),
        ("book.pdf", b"%PDF-1.7", "can't read .pdf files"),
        ("notes.xyz", b"hello", "reads EPUB, DOCX, FB2, HTML and TXT books"),
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
    monkeypatch.setattr(cli, "required_assets", lambda use_gpu: (asset,))
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
        "list_devices",
        lambda executable: [
            GpuDevice(0, "Vulkan0", "NVIDIA RTX PRO 1000", "gpu", 7 * 2**30, 8 * 2**30)
        ],
    )

    assert cli.main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "0: NVIDIA RTX PRO 1000 (gpu, 7.0 of 8.0 GiB free)" in out
    assert "(graphics card): yes" in out
