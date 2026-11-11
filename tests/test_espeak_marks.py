"""The eSpeak NG measurements behind tomecast.marks and the pronunciation lists, re-checked.

Opt-in: run with ``uv run pytest -m integration``. It uses the eSpeak NG build Tomecast
downloads into its data folder, and is skipped when that isn't installed. Phonemes only,
through eSpeak's own C library: no audio, no network, no graphics card. If the eSpeak pin
moves, these say whether the choices made from these measurements still hold.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from functools import cache

import pytest

from tomecast.downloads import data_dir, local_path
from tomecast.manifest import ESPEAK_NG
from tomecast.marks import APOSTROPHE_LIKE, speakable

pytestmark = pytest.mark.integration

_SYNCHRONOUS = 0x02  # AUDIO_OUTPUT_SYNCHRONOUS: nothing is played
_UTF8 = 1  # espeakCHARS_UTF8
_IPA = 0x02  # phonemes as IPA


@cache
def _espeak() -> Callable[[str], str] | None:
    """eSpeak's phonemes for a text (American English), or None if it isn't installed."""
    folder = local_path(ESPEAK_NG, data_dir()) / "espeakng_loader"
    if not (folder / "libespeak-ng.so.1").is_file():
        return None
    library = ctypes.CDLL(str(folder / "libespeak-ng.so.1"))
    library.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    library.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    library.espeak_TextToPhonemes.restype = ctypes.c_char_p
    library.espeak_TextToPhonemes.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_int,
        ctypes.c_int,
    ]
    if library.espeak_Initialize(_SYNCHRONOUS, 0, str(folder).encode(), 0) <= 0:
        return None
    if library.espeak_SetVoiceByName(b"en-us") != 0:
        return None

    def phonemes(text: str) -> str:
        buffer = ctypes.create_string_buffer(text.encode())
        pointer = ctypes.c_void_p(ctypes.addressof(buffer))
        clauses = []
        while pointer.value:  # one clause per call; eSpeak moves the pointer on
            clause = library.espeak_TextToPhonemes(ctypes.byref(pointer), _UTF8, _IPA)
            if clause:
                clauses.append(clause.decode())
        return " ".join(clauses)

    return phonemes


@pytest.fixture
def phonemes() -> Callable[[str], str]:
    found = _espeak()
    if found is None:
        pytest.skip("Tomecast's eSpeak NG isn't installed (convert a book once to get it)")
    return found


@pytest.mark.parametrize("mark", list(APOSTROPHE_LIKE))
def test_each_mark_spells_its_word_out_until_it_is_an_apostrophe(
    phonemes: Callable[[str], str], mark: str
) -> None:
    word = f"ka{mark}ta"
    assert " " in phonemes(word)  # read out letter by letter, the mark by name
    assert " " not in phonemes(speakable(word))  # one word again


def test_the_okina_and_real_apostrophes_are_read_as_intended(
    phonemes: Callable[[str], str],
) -> None:
    for word in ("Hawaiʻi", "Hawai'i", "Hawai‘i", "Hawai’i"):
        assert phonemes(word) == "həwˈaɪiː", word


def test_an_apostrophe_usually_reads_better_than_dropping_the_mark(
    phonemes: Callable[[str], str],
) -> None:
    assert phonemes(speakable("Ilʹich")) == "ˈɪlɪtʃ"
    assert phonemes("Ilich") == "ˈɪlɪx"
    assert phonemes(speakable("Iʼm")) == "aɪm"
    assert phonemes("Im") == "ˈɪm"
    # The known exception: after a lone first letter, that letter is said by name.
    assert phonemes(speakable("Kʼicheʼ")) == "kˈeɪˈɪtʃ"


def test_ipa_is_read_out_by_name_and_the_respelling_is_not(
    phonemes: Callable[[str], str],
) -> None:
    assert "stɹˈɛs" in phonemes("ʃɪˈvɔːn")  # "...stress..."
    assert phonemes("shi-vawn") == "ʃivˈɔːn"  # the example every message gives
