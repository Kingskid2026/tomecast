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


def test_every_voice_belongs_to_one_engine() -> None:
    assert set(audiocpp.VOICES).isdisjoint(ming.VOICES)
    assert voices.ALL_VOICES == audiocpp.VOICES + ming.VOICES
    assert {voices.engine_of(v) for v in ming.VOICES} == {"ming"}
    assert {voices.engine_of(v) for v in audiocpp.VOICES} == {"kokoro"}


def test_the_provisional_default_premium_voice_is_uk_george() -> None:
    assert ming.DEFAULT_VOICE == "UK_George"  # not decided yet (#11)
    assert ming.DEFAULT_VOICE in ming.VOICES


@pytest.mark.parametrize(
    ("language", "code"),
    [("en-GB", "en"), ("de_DE", "de"), ("FR", "fr"), (None, "en"), ("", "en"), ("ko", "ko")],
)
def test_language_codes(language: str | None, code: str) -> None:
    assert voices.language_code(language) == code


def test_a_language_both_speak_stays_with_the_chosen_voice() -> None:
    for voice in ("af_heart", "UK_George"):
        route = voices.route(voice, "en-US", premium_possible=True)
        assert route == voices.Route(voice, "en")


@pytest.mark.parametrize(
    ("language", "kokoro"), [("it", "if_sara"), ("pt-BR", "pf_dora"), ("hi", "hf_alpha")]
)
def test_italian_portuguese_and_hindi_go_to_kokoro(language: str, kokoro: str) -> None:
    route = voices.route("UK_George", language, premium_possible=True)
    assert route.voice == kokoro
    assert route.note is not None
    assert "premium voice doesn't speak" in route.note


@pytest.mark.parametrize("language", ["de", "ko-KR"])
def test_german_and_korean_go_to_ming_with_a_graphics_card(language: str) -> None:
    route = voices.route("af_heart", language, premium_possible=True)
    assert route.voice == ming.DEFAULT_VOICE
    assert route.language == voices.language_code(language)
    assert route.note is not None
    assert "premium voice" in route.note


def test_without_a_graphics_card_german_stays_with_kokoro_and_says_so() -> None:
    route = voices.route("af_heart", "de", premium_possible=False)
    assert route.voice == "af_heart"
    assert route.note is not None
    assert "needs a graphics card" in route.note


def test_a_language_neither_speaks_says_so() -> None:
    for voice in ("af_heart", "UK_George"):
        route = voices.route(voice, "pl", premium_possible=True)
        assert route.voice == voice
        assert route.note is not None
        assert "No voice speaks pl" in route.note


def test_each_voice_needs_its_own_files() -> None:
    assert voices.required_assets("af_heart", True) == audiocpp.required_assets(True)
    assert voices.required_assets("UK_George", True) == (AUDIOCPP_VULKAN, MING_Q8)


def test_the_premium_download_says_its_size_and_that_it_needs_a_card() -> None:
    assert voices.premium_note([ESPEAK_NG, KOKORO_Q8]) is None
    note = voices.premium_note([AUDIOCPP_VULKAN, MING_Q8])
    assert note == "The premium voice is about 1.8 GB to download and needs a graphics card."


def test_new_engine_builds_are_checked_by_every_installed_voice_on_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    installed: set[Asset] = set()
    monkeypatch.setattr(voices, "is_installed", lambda asset, root: asset in installed)

    def names(voice: str, use_gpu: bool = True) -> list[str]:
        return [check.name for check in voices.voice_checks(root, voice, use_gpu)]

    assert names("af_heart") == ["Kokoro"]  # Ming isn't installed
    assert names("UK_George") == ["Ming"]  # nor is Kokoro
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
