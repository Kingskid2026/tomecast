"""The command line with the premium voice (Ming, issue #11): choosing it, refusing it without
a graphics card, asking before its download, and routing a book's language to it."""

from pathlib import Path

import pytest
from epub_factory import NavPoint, Page, build_epub
from test_cli import FakeKokoro

from tomecast import cli, voices
from tomecast.downloads import Asset
from tomecast.engines import audiocpp, ming
from tomecast.speed import Speeds


class FakeMing(FakeKokoro):
    """Stands in for AutoMing: records the language it was told to speak."""

    languages: list[str] = []  # noqa: RUF012

    def __init__(self, root: Path, *, voice: str, language: str, **kwargs: object) -> None:
        super().__init__(root, voice=voice, **kwargs)  # type: ignore[arg-type]
        self.label = f"Ming ({voice})"
        FakeMing.languages.append(language)


@pytest.fixture(autouse=True)
def no_real_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "AutoKokoro", FakeKokoro)
    monkeypatch.setattr(cli, "AutoMing", FakeMing)
    monkeypatch.setattr(cli, "check_system", lambda: None)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: ())
    monkeypatch.setattr(ming, "required_assets", lambda use_gpu: ())
    monkeypatch.setattr(cli, "engine_speed", lambda root, engine, measuring=None: None)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    monkeypatch.setattr(cli, "data_dir", lambda: tmp_path / "data")
    FakeKokoro.made = []
    FakeKokoro.texts = []
    FakeKokoro.gpu_warning = None
    FakeMing.languages = []


def _book(tmp_path: Path, language: str = "en") -> Path:
    return build_epub(
        tmp_path / "story.epub",
        [Page("one.xhtml", "<h1>Chapter One</h1><p>It began. It ended without a period</p>")],
        toc=[NavPoint("Chapter One", "one.xhtml")],
        title="A Story",
        language=language,
    )


def test_voices_lists_the_premium_voices(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    assert "Premium (Ming, needs a graphics card): UK_George, US_Emma" in out


def test_convert_with_a_premium_voice_uses_ming_on_the_card(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _book(tmp_path)
    assert cli.main(["convert", str(book), "--voice", "US_James"]) == 0
    (engine,) = FakeKokoro.made
    assert isinstance(engine, FakeMing)
    assert engine.use_gpu
    assert FakeMing.languages == ["en"]
    out = capsys.readouterr().out
    assert "voice US_James" in out
    assert ming.UNCHECKED_NOTE in out  # until the spoken-text check is plugged in
    assert " ".join(FakeKokoro.texts).endswith("It ended without a period")


def test_the_premium_voice_needs_a_graphics_card(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _book(tmp_path)
    assert cli.main(["convert", str(book), "--voice", "UK_George", "--no-gpu"]) == 1
    assert voices.NEEDS_GPU in capsys.readouterr().err
    assert FakeKokoro.made == []


def test_the_premium_voice_goes_up_to_twice_the_speed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _book(tmp_path)
    assert cli.main(["convert", str(book), "--voice", "UK_George", "--speed", "2.5"]) == 1
    assert "goes up to speed 2.0" in capsys.readouterr().err


def test_a_german_book_goes_to_the_premium_voice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _book(tmp_path, language="de")
    assert cli.main(["convert", str(book), "--yes"]) == 0
    (engine,) = FakeKokoro.made
    assert isinstance(engine, FakeMing)
    assert FakeMing.languages == ["de"]
    assert "Kokoro doesn't speak German" in capsys.readouterr().out


def test_an_italian_book_goes_to_kokoro(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    book = _book(tmp_path, language="it")
    assert cli.main(["convert", str(book), "--voice", "UK_George"]) == 0
    (engine,) = FakeKokoro.made
    assert not isinstance(engine, FakeMing)
    assert engine.label == "Fake (if_sara)"
    assert "premium voice doesn't speak Italian" in capsys.readouterr().out


def test_the_premium_download_is_asked_for_with_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model = Asset(
        name="Ming-3.0-tts premium voice model",
        url="https://example.invalid/ming.gguf",
        sha256="0" * 64,
        size=1_742_280_896,
        path="models/ming.gguf",
        opt_in=True,
    )
    monkeypatch.setattr(ming, "required_assets", lambda use_gpu: (model,))
    book = _book(tmp_path)
    # Not a terminal, and no --yes: nothing is downloaded.
    assert cli.main(["convert", str(book), "--voice", "UK_George"]) == 1
    out = capsys.readouterr().out
    assert "Ming-3.0-tts premium voice model" in out
    assert "The premium voice is about 1.7 GB to download and needs a graphics card." in out
    assert FakeKokoro.made == []


def test_scan_recommends_the_premium_voice_on_a_card_with_room(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tomecast.machine import Recommendation

    monkeypatch.setattr(
        cli,
        "probe",
        lambda root, gpu, cancelled=None: Speeds(cpu=4.6, gpu=19.0, gpu_description="A card"),
    )
    monkeypatch.setattr(voices, "scan_recommendation", lambda root: Recommendation("ming", 8, 8))
    assert cli.main(["scan"]) == 0
    assert cli.PREMIUM_RECOMMENDED in capsys.readouterr().out
