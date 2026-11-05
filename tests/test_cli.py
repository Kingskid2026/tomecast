import array
import math
import wave
from pathlib import Path
from types import TracebackType

import pytest
from epub_factory import NavPoint, Page, build_epub

from tomecast import __version__, cli
from tomecast.downloads import Asset
from tomecast.engines.audiocpp import EngineError


class FakeKokoro:
    """Stands in for KokoroServer: same constructor and context manager, no real engine."""

    fail_with: str | None = None

    def __init__(self, root: Path, *, voice: str, speed: float) -> None:
        self.label = f"Fake ({voice})"
        self.max_chars = 200

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

    assert "out of memory" in capsys.readouterr().err
    assert not (tmp_path / "story.m4b").exists()


def test_voices_lists_every_language(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    assert "American English: af_alloy" in out
    assert "bm_george" in out
    assert "Default: af_heart" in out
