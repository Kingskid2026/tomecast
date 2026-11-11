"""The command line with the premium voice (Ming, issue #11): choosing it, refusing it without
a graphics card, asking before its download, and routing a book's language to it."""

from pathlib import Path

import pytest
from epub_factory import NavPoint, Page, build_epub
from test_cli import FakeKokoro

from tomecast import cli, machine, voices
from tomecast.downloads import Asset
from tomecast.engines import audiocpp, ming
from tomecast.speed import Speeds

# The premium voice's real file list, before the fixture below replaces it for most tests.
real_premium_assets = ming.required_assets


class FakeMing(FakeKokoro):
    """Stands in for AutoMing: records the language it was told to speak and what to do if
    the card fails, and can have the card fail: Kokoro takes over at the ``switch_at``th
    voicing (as AutoMing does by default), or it stops there (``"stop"``)."""

    languages: list[str] = []  # noqa: RUF012
    policies: list[str] = []  # noqa: RUF012
    speeds: list[float] = []  # noqa: RUF012
    fail_at: int | None = None  # the card fails on this voicing (1 is the first)
    words_left_out = False  # every piece fails the spoken-text check

    def __init__(
        self,
        root: Path,
        *,
        voice: str,
        language: str,
        if_card_fails: str = ming.DEFAULT_CARD_POLICY,
        **kwargs: object,
    ) -> None:
        super().__init__(root, voice=voice, **kwargs)  # type: ignore[arg-type]
        FakeMing.speeds.append(float(kwargs["speed"]))  # type: ignore[arg-type]
        self.label = f"Ming ({voice})"
        self.premium = voice
        self.if_card_fails = if_card_fails
        FakeMing.languages.append(language)
        FakeMing.policies.append(if_card_fails)

    @property
    def voice_speed(self) -> float:
        return 1.0  # all of its speed is the stretch

    @property
    def tempo(self) -> float:
        return FakeMing.speeds[-1] if FakeMing.speeds else 1.0

    def synthesize(self, text: str, output: Path) -> None:
        if FakeMing.words_left_out:
            raise ming.WordsLeftOut(f"the premium voice left out words (skipped): {text!r}")
        if FakeMing.fail_at == self.calls + 1 and not self.fell_back:
            if self.if_card_fails == "stop":
                raise ming.CardStopped(ming.STOPPED_NOTE)
            self.warning = ming.switch_note(self.premium, chapters_done=1, partway=True)
            self.fell_back = self.out_of_memory = True
            self.label = f"Ming ({self.premium}) and Kokoro (bm_george)"
        super().synthesize(text, output)


@pytest.fixture(autouse=True)
def _premium_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests use the premium voice, a preview for now: opted into."""
    monkeypatch.setenv("TOMECAST_PREMIUM_PREVIEW", "1")


@pytest.fixture(autouse=True)
def no_real_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "AutoKokoro", FakeKokoro)
    monkeypatch.setattr(cli, "AutoMing", FakeMing)
    monkeypatch.setattr(cli, "check_system", lambda: None)
    monkeypatch.setattr(audiocpp, "required_assets", lambda use_gpu: ())
    monkeypatch.setattr(ming, "required_assets", lambda use_gpu: ())
    monkeypatch.setattr(cli, "engine_speed", lambda root, engine, measuring=None: None)
    monkeypatch.setattr(cli, "known_cpu_speed", lambda root, engine: None)
    monkeypatch.setattr(cli, "known_card_speed", lambda root, engine: None)
    monkeypatch.setattr(cli, "vulkan_possible", lambda: True)
    monkeypatch.setattr(cli, "data_dir", lambda: tmp_path / "data")
    FakeKokoro.fail_with = None
    FakeKokoro.fail_after = None
    FakeKokoro.voiced = 0
    FakeKokoro.made = []
    FakeKokoro.texts = []
    FakeKokoro.gpu_warning = None
    FakeKokoro.oom_at = None
    FakeKokoro.on_card = []
    FakeKokoro.on_exit = None
    FakeMing.languages = []
    FakeMing.policies = []
    FakeMing.speeds = []
    FakeMing.fail_at = None
    FakeMing.words_left_out = False


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
    assert "Premium (Ming, a preview, needs a graphics card): UK_George, US_Emma" in out


# --- The preview has to be asked for -----------------------------------------------------


def test_without_the_opt_in_voices_leaves_the_premium_ones_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TOMECAST_PREMIUM_PREVIEW")
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    assert "UK_George" not in out
    assert "Premium" not in out


def test_without_the_opt_in_a_premium_voice_says_how_to_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TOMECAST_PREMIUM_PREVIEW")
    book = _book(tmp_path)
    for command in (["convert", str(book)], ["preview", "Hello"]):
        assert cli.main([*command, "--voice", "US_James"]) == 1
        err = capsys.readouterr().err
        assert "The premium voices (Ming) are a preview" in err
        assert "TOMECAST_PREMIUM_PREVIEW=1 tomecast convert BOOK --voice US_James" in err
    assert FakeKokoro.made == []


def test_without_the_opt_in_the_scan_doesnt_recommend_the_premium_voice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TOMECAST_PREMIUM_PREVIEW")
    monkeypatch.setattr(
        cli,
        "probe",
        lambda root, gpu, cancelled=None: Speeds(cpu=4.6, gpu=19.0, gpu_description="A card"),
    )
    assert cli.main(["scan"]) == 0
    assert cli.PREMIUM_RECOMMENDED not in capsys.readouterr().out


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
    assert cli.main(["convert", str(book), "--voice", "UK_George", "--speed", "2.5"]) == 0
    assert FakeMing.speeds == [2.0]
    assert ming.CAPPED_NOTE in capsys.readouterr().out


def test_a_german_book_stays_with_kokoro_until_the_premium_voice_is_checked_for_german(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _book(tmp_path, language="de")
    assert cli.main(["convert", str(book), "--yes"]) == 0
    (engine,) = FakeKokoro.made
    assert not isinstance(engine, FakeMing)
    assert "Kokoro doesn't speak German" in capsys.readouterr().out


@pytest.fixture
def german_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ming, "CHECKED_LANGUAGES", frozenset({"en", "de"}))


def test_a_german_book_goes_to_the_premium_voice_only_if_it_may(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    german_checked: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    book = _book(tmp_path, language="de")
    asked: list[bool] = []

    def may(root: Path, use_gpu: bool) -> bool:
        asked.append(use_gpu)
        return allowed

    monkeypatch.setattr(voices, "premium_can_take", may)
    allowed = False  # not installed, or no room on the card, or not opted into
    assert cli.main(["convert", str(book), "--yes"]) == 0
    assert not isinstance(FakeKokoro.made[-1], FakeMing)
    allowed = True
    assert cli.main(["convert", str(book), "--yes", "--force"]) == 0
    assert isinstance(FakeKokoro.made[-1], FakeMing)
    assert FakeMing.languages == ["de"]
    assert asked == [True, True]
    assert "Kokoro doesn't speak German, so Tomecast used the premium voice" in (
        capsys.readouterr().out
    )


def test_routing_never_downloads_the_premium_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, german_checked: None
) -> None:
    """Opted in, a card with room, -y given: still no 1.74 GB download for a German book."""
    monkeypatch.setattr(ming, "required_assets", real_premium_assets)  # really not installed
    monkeypatch.setattr(
        voices, "scan_recommendation", lambda root: machine.Recommendation("ming", 8, 8)
    )
    downloads: list[str] = []
    real = voices.prepare_voice

    def recording(root: Path, voice: str, *args: object, **kwargs: object) -> object:
        downloads.append(voice)
        return real(root, voice, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(voices, "prepare_voice", recording)
    book = _book(tmp_path, language="de")
    assert cli.main(["convert", str(book), "--yes"]) == 0  # Ming isn't installed here
    assert downloads == ["af_heart"]
    assert not isinstance(FakeKokoro.made[-1], FakeMing)


def test_a_kokoro_user_routed_to_the_premium_voice_is_never_refused_for_speed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    german_checked: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(voices, "premium_can_take", lambda root, use_gpu: True)
    book = _book(tmp_path, language="de")
    assert cli.main(["convert", str(book), "--speed", "2.5", "--yes"]) == 0
    assert FakeMing.speeds == [2.0]  # made at the premium voice's 2x, and said
    assert ming.CAPPED_NOTE in capsys.readouterr().out


def test_an_italian_book_with_a_premium_voice_goes_to_the_closest_kokoro_voice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _book(tmp_path, language="it")
    assert cli.main(["convert", str(book), "--voice", "UK_George"]) == 0
    (engine,) = FakeKokoro.made
    assert not isinstance(engine, FakeMing)
    assert engine.label == "Fake (bm_george)"  # not if_sara: that file isn't pinned yet
    assert "hasn't been checked for Italian books" in capsys.readouterr().out


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


# --- If the graphics card fails --------------------------------------------------------------


def _book_of_three(tmp_path: Path) -> Path:
    pages = [
        Page(f"c{i}.xhtml", f"<h1>Chapter {i}</h1><p>It began. It went on. It ended.</p>")
        for i in (1, 2, 3)
    ]
    toc = [NavPoint(f"Chapter {i}", f"c{i}.xhtml") for i in (1, 2, 3)]
    return build_epub(tmp_path / "three.epub", pages, toc=toc, title="Three")


def test_kokoro_takes_over_by_default_and_the_run_proves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proven: list[object] = []
    monkeypatch.setattr(cli, "mark_proven", lambda *args: proven.append(args))
    FakeMing.fail_at = 4
    assert cli.main(["convert", str(_book_of_three(tmp_path)), "--voice", "UK_George"]) == 0
    assert FakeMing.policies == ["kokoro"]
    out = capsys.readouterr().out
    assert "Note: The graphics card stopped working, so the rest of this book uses Kokoro's " in out
    assert "Chapter 1 and the start of chapter 2 are in the premium voice." in out
    assert proven == []  # a run where the card failed says nothing about the build
    # Every piece voiced once, in order, whichever voice made it.
    assert FakeKokoro.texts[0] == "Chapter one" and FakeKokoro.texts[-1] == "It ended."
    assert len(FakeKokoro.texts) == 12


def test_if_card_fails_is_passed_on(tmp_path: Path) -> None:
    book = _book(tmp_path)
    for policy in ("cpu", "stop", "kokoro"):
        assert (
            cli.main(
                ["convert", str(book), "--voice", "UK_George", "--if-card-fails", policy, "--force"]
            )
            == 0
        )
    assert FakeMing.policies == ["cpu", "stop", "kokoro"]


def test_without_kokoro_taking_over_kokoro_isnt_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[bool] = []
    real = voices.prepare_voice

    def recording(*args: object, with_kokoro: bool = True, **kwargs: object) -> object:
        asked.append(with_kokoro)
        return real(*args, with_kokoro=with_kokoro, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(voices, "prepare_voice", recording)
    book = _book(tmp_path)
    assert cli.main(["convert", str(book), "--voice", "UK_George", "--if-card-fails", "cpu"]) == 0
    assert cli.main(["convert", str(book), "--voice", "UK_George", "--force"]) == 0
    assert asked == [False, True]


def test_with_stop_the_book_stops_keeps_its_audio_and_blames_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_rollback(*args: object) -> bool:
        raise AssertionError("a card that stopped working must not roll a build back")

    monkeypatch.setattr(cli, "roll_back", no_rollback)
    FakeMing.fail_at = 3
    book = _book_of_three(tmp_path)
    args = ["convert", str(book), "--voice", "UK_George", "--if-card-fails", "stop"]
    assert cli.main(args) == 1
    err = capsys.readouterr().err
    assert ming.STOPPED_NOTE in err
    assert "The audio made so far is saved. Run the same command again to continue." in err
    assert not (tmp_path / "three.m4b").exists()

    FakeMing.fail_at = None  # a working card: the same command finishes it
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "Picked up where the last run stopped (2 pieces)." in out
    assert len(FakeKokoro.texts) == 12  # 2, then the other 10: nothing voiced twice


def test_a_premium_voice_failure_never_rolls_the_engine_build_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Words left out are the voice's problem, not the shared build's: no rollback."""

    def no_rollback(*args: object) -> bool:
        raise AssertionError("a premium-voice failure must not roll a build back")

    monkeypatch.setattr(cli, "roll_back", no_rollback)
    FakeMing.words_left_out = True
    assert cli.main(["convert", str(_book(tmp_path)), "--voice", "UK_George"]) == 1
    assert "left out words" in capsys.readouterr().err


def test_a_kokoro_failure_can_still_roll_the_engine_build_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rolled: list[object] = []

    def recording(*args: object) -> bool:
        rolled.append(args)
        return False

    monkeypatch.setattr(cli, "roll_back", recording)
    FakeKokoro.fail_with = "the voice engine failed (HTTP 500): broken"
    assert cli.main(["convert", str(_book(tmp_path))]) == 1
    assert len(rolled) == 1
