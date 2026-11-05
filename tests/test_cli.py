import array
import fcntl
import math
import wave
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

import pytest
from epub_factory import NavPoint, Page, build_epub

from tomecast import __version__, cli
from tomecast.downloads import Asset
from tomecast.engines.audiocpp import EngineError
from tomecast.workdirs import work_folder


class FakeKokoro:
    """Stands in for KokoroServer: same constructor and context manager, no real engine."""

    fail_with: str | None = None
    fail_after: int | None = None  # chunks to voice before failing, like an interrupted run
    voiced = 0

    def __init__(self, root: Path, *, voice: str, speed: float) -> None:
        self.label = f"Fake ({voice})"
        self.max_chars = 200
        self.sample_rate = 24000

    def __enter__(self) -> "FakeKokoro":
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
    monkeypatch.setattr(cli, "KokoroServer", FakeKokoro)
    monkeypatch.setattr(cli, "check_system", lambda: None)
    monkeypatch.setattr(cli, "REQUIRED_ASSETS", ())
    monkeypatch.setattr(cli, "data_dir", lambda: tmp_path / "data")
    FakeKokoro.fail_with = None
    FakeKokoro.fail_after = None
    FakeKokoro.voiced = 0


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
        ("book.pdf", b"%PDF-1.7", "only EPUB"),
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
    monkeypatch.setattr(cli, "REQUIRED_ASSETS", (asset,))
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


@pytest.mark.parametrize("speed", ["3", "0.1", "fast"])
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
