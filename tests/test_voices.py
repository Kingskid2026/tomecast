"""Choosing between Kokoro and the premium voice (Ming): language routing, the files and
self-checks each needs, the work folder's key, and the hardware scan's recommendation."""

from pathlib import Path

import pytest

from tomecast import machine, voices
from tomecast.downloads import Asset
from tomecast.engines import audiocpp, ming
from tomecast.hardware import GpuDevice
from tomecast.machine import Machine, Memory, Power, recommend
from tomecast.manifest import AUDIOCPP_VULKAN, ESPEAK_NG, KOKORO_Q8, MING_Q8
from tomecast.workdirs import work_folder

GiB = 2**30


@pytest.fixture(autouse=True)
def _premium_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests use the premium voice, a preview for now: opted into."""
    monkeypatch.setenv("TOMECAST_PREMIUM_PREVIEW", "1")


def test_every_voice_belongs_to_one_engine() -> None:
    assert set(audiocpp.VOICES).isdisjoint(ming.VOICES)
    assert voices.ALL_VOICES == audiocpp.VOICES + ming.VOICES
    assert {voices.engine_of(v) for v in ming.VOICES} == {"ming"}
    assert {voices.engine_of(v) for v in audiocpp.VOICES} == {"kokoro"}


def test_the_default_premium_voice_is_uk_george() -> None:
    assert ming.DEFAULT_VOICE == "UK_George"  # the maintainer's choice (#11)
    assert ming.DEFAULT_VOICE in ming.VOICES


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("en-GB", "en"), ("de_DE", "de"), ("FR", "fr"), (None, "en"), ("", "en"),
        ("ko", "ko"), ("eng", "en"), ("deu", "de"), ("ger", "de"), ("fre", "fr"),
        ("zho", "zh"), ("jpn", "ja"),
    ],
)  # fmt: skip
def test_language_codes(language: str | None, code: str) -> None:
    assert voices.language_code(language) == code


def _never() -> bool:
    raise AssertionError("asked whether the premium voice may take the book, needlessly")


def test_english_stays_with_the_chosen_voice() -> None:
    for voice in ("af_heart", "UK_George"):
        assert voices.route(voice, "en-US", premium_ok=_never) == voices.Route(voice, "en")


@pytest.mark.parametrize("language", ["fr", "zh", "ja", "de", "ko", "it"])
def test_the_premium_voice_reads_only_languages_it_is_checked_for(language: str) -> None:
    """Its 200-character pieces were measured on English only: a CJK piece could be cut short
    and nothing would notice. So another language goes to the closest Kokoro voice."""
    route = voices.route("UK_Charlotte", language, premium_ok=_never)
    assert route.voice == "bf_emma"
    assert route.language == "en"
    assert route.note is not None
    assert "hasn't been checked for" in route.note
    assert "Kokoro's Emma (similar British voice)" in route.note
    assert {"en"} == ming.CHECKED_LANGUAGES


@pytest.mark.parametrize("language", ["it", "pt-BR", "hi", "es", "fr", "ja", "zh"])
def test_kokoros_other_languages_wait_for_its_multilingual_file(language: str) -> None:
    """No automatic routing to if_sara, pf_dora or hf_alpha yet: just the note."""
    route = voices.route("af_heart", language, premium_ok=_never)
    assert route.voice == "af_heart"
    assert route.note is not None
    assert "larger voice file" in route.note


@pytest.mark.parametrize("language", ["de", "ko-KR"])
def test_german_and_korean_stay_with_kokoro_while_the_premium_voice_isnt_checked_for_them(
    language: str,
) -> None:
    route = voices.route("af_heart", language, premium_ok=lambda: True)
    assert route.voice == "af_heart"
    assert route.note is not None
    assert "Kokoro doesn't speak" in route.note


def test_routing_to_the_premium_voice_needs_its_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once German is checked: a Kokoro book goes to the premium voice only if premium_ok()."""
    monkeypatch.setattr(ming, "CHECKED_LANGUAGES", frozenset({"en", "de"}))
    taken = voices.route("af_heart", "de", premium_ok=lambda: True)
    assert taken == voices.Route(ming.DEFAULT_VOICE, "de", taken.note)
    assert taken.note is not None and "premium voice" in taken.note
    kept = voices.route("af_heart", "de", premium_ok=lambda: False)
    assert kept.voice == "af_heart"
    # Korean isn't checked, so it never asks.
    assert voices.route("af_heart", "ko", premium_ok=_never).voice == "af_heart"


def test_a_language_neither_speaks_says_so() -> None:
    for voice in ("af_heart",):
        route = voices.route(voice, "pl", premium_ok=_never)
        assert route.voice == voice
        assert route.note is not None
        assert "No voice speaks pl" in route.note


# --- Routing never downloads the premium voice on its own ---------------------------------------


@pytest.fixture
def room_on_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        voices, "scan_recommendation", lambda root: machine.Recommendation("ming", 8, 8)
    )


def _installed(monkeypatch: pytest.MonkeyPatch, *assets: Asset) -> None:
    there = set(assets)
    monkeypatch.setattr(
        voices, "needs_download", lambda asset, root, verify=True: asset not in there
    )


def test_the_premium_voice_may_take_a_book_only_when_opted_in_installed_and_fitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, room_on_the_card: None
) -> None:
    root = tmp_path / "data"
    _installed(monkeypatch, AUDIOCPP_VULKAN, MING_Q8)
    assert voices.premium_can_take(root, use_gpu=True)
    assert not voices.premium_can_take(root, use_gpu=False)  # the card isn't in use
    monkeypatch.delenv("TOMECAST_PREMIUM_PREVIEW")  # not opted into
    assert not voices.premium_can_take(root, use_gpu=True)


def test_routing_never_downloads_the_premium_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, room_on_the_card: None
) -> None:
    _installed(monkeypatch, AUDIOCPP_VULKAN)  # the engine, but not the 1.74 GB model
    assert not voices.premium_can_take(tmp_path / "data", use_gpu=True)


def test_the_premium_voice_takes_a_book_only_on_a_card_with_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, AUDIOCPP_VULKAN, MING_Q8)
    monkeypatch.setattr(
        voices, "scan_recommendation", lambda root: machine.Recommendation("kokoro", 8, 8)
    )
    assert not voices.premium_can_take(tmp_path / "data", use_gpu=True)


def test_the_scan_recommends_the_premium_voice_only_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    folder = root / AUDIOCPP_VULKAN.path / AUDIOCPP_VULKAN.sha256[:12]
    monkeypatch.setattr(voices, "build_in_use", lambda asset, root: folder)
    monkeypatch.setattr(voices, "vulkan_possible", lambda: True)
    big = GpuDevice(0, "Vulkan0", "A big card", "gpu", 6 * GiB, 8 * GiB)
    monkeypatch.setattr(voices, "list_devices", lambda executable: [big])
    assert voices.scan_recommendation(root).voice == "ming"
    monkeypatch.delenv("TOMECAST_PREMIUM_PREVIEW")
    assert voices.scan_recommendation(root).voice == "kokoro"


def test_each_voice_needs_its_own_files() -> None:
    assert voices.required_assets("af_heart", True) == audiocpp.required_assets(True)
    # Kokoro's files too, so Kokoro can take over if the card fails without downloading.
    assert voices.required_assets("UK_George", True) == (
        AUDIOCPP_VULKAN, MING_Q8, ESPEAK_NG, KOKORO_Q8,
    )  # fmt: skip
    without = voices.required_assets("UK_George", True, with_kokoro=False)
    assert without == (AUDIOCPP_VULKAN, MING_Q8)


def test_the_premium_download_says_its_size_and_that_it_needs_a_card() -> None:
    assert voices.premium_note([ESPEAK_NG, KOKORO_Q8]) is None
    note = voices.premium_note([AUDIOCPP_VULKAN, MING_Q8])
    assert note == "The premium voice is about 1.8 GB to download and needs a graphics card."


def test_new_engine_builds_are_checked_by_every_installed_voice_on_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    installed: set[Asset] = set()
    monkeypatch.setattr(
        voices, "needs_download", lambda asset, root, verify=True: asset not in installed
    )

    def names(voice: str, use_gpu: bool = True) -> list[str]:
        return [check.name for check in voices.voice_checks(root, voice, use_gpu)]

    assert names("af_heart") == ["Kokoro"]  # Ming isn't installed
    assert names("UK_George") == ["Ming"]  # nor is Kokoro: its check isn't run without it
    installed.update({KOKORO_Q8, ESPEAK_NG, MING_Q8})
    assert names("af_heart") == ["Kokoro", "Ming"]
    assert names("UK_George") == ["Kokoro", "Ming"]
    # Ming runs only on the graphics-card build, so the CPU build is Kokoro's alone.
    assert names("af_heart", use_gpu=False) == ["Kokoro"]


def test_the_work_folder_tells_the_two_voices_apart(tmp_path: Path) -> None:
    book = tmp_path / "book.epub"
    book.write_bytes(b"a book")
    root = tmp_path / "data"
    kokoro = work_folder(root, book, "af_heart", 1.0, use_gpu=True)
    premium = work_folder(root, book, "UK_George", 1.0, use_gpu=True)
    assert kokoro != premium
    assert premium != work_folder(root, book, "US_Emma", 1.0, use_gpu=True)
    # Kokoro voices at the speed asked; the premium voice always at 1.0, then the whole book
    # is stretched, so its pieces fit any speed and a book resumes at another one.
    assert kokoro != work_folder(root, book, "af_heart", 1.25, use_gpu=True)
    assert premium == work_folder(root, book, "UK_George", 1.75, use_gpu=True)


def test_pieces_made_without_the_spoken_check_are_never_reused_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = tmp_path / "book.epub"
    book.write_bytes(b"a book")
    root = tmp_path / "data"
    unchecked = work_folder(root, book, "UK_George", 1.0, use_gpu=True)
    monkeypatch.setattr(ming, "SPOKEN_CHECK", lambda wav, text: "")
    assert work_folder(root, book, "UK_George", 1.0, use_gpu=True) != unchecked
    assert ming.preview_enabled() and ming.preview_note() is None  # no preview any more


# --- The recommendation ----------------------------------------------------------------------


def _machine() -> Machine:
    return Machine(16, Memory(32 * GiB, 16 * GiB), Power(on_battery=False))


def _card(total: int, free: int) -> GpuDevice:
    return GpuDevice(0, "Vulkan0", "NVIDIA RTX PRO 1000", "gpu", free, total)


def test_kokoro_is_recommended_without_a_graphics_card() -> None:
    assert recommend(_machine()).voice == "kokoro"
    assert recommend(_machine(), None).voice == "kokoro"


def test_the_premium_voice_is_recommended_on_a_card_with_room_for_it() -> None:
    assert recommend(_machine(), _card(8 * GiB, 6 * GiB)).voice == "ming"
    # Too small a card, or one another program has nearly filled.
    assert recommend(_machine(), _card(2 * GiB, 2 * GiB)).voice == "kokoro"
    assert recommend(_machine(), _card(8 * GiB, GiB)).voice == "kokoro"
    assert 4 * GiB == machine.PREMIUM_MIN_GPU_BYTES
