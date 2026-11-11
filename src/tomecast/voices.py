"""Which voice a run uses: Kokoro's voices and the premium Ming voices in one list, the
files and engine each needs, and the language routing between them (docs/architecture.md,
section 4).

A voice name says which engine it belongs to: Kokoro's are ``af_heart`` and the like, Ming's
are ``UK_George`` and the like (:func:`tomecast.engines.ming.is_ming_voice`). So the command
line's ``--voice`` and the window's voice list take either, and everything else follows from
the name: what to download, which engine to start, how the work folder is keyed and which
self-checks a new engine build must pass.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tomecast.downloads import (
    Asset,
    Consent,
    DownloadReporter,
    IsCancelled,
    Prepared,
    VoiceCheck,
    build_in_use,
    needs_download,
    prepare,
)
from tomecast.engines import audiocpp, ming
from tomecast.engines.audiocpp import EXECUTABLE, AutoKokoro
from tomecast.hardware import list_devices, pick_device, vulkan_possible
from tomecast.machine import Recommendation, recommend, scan_machine
from tomecast.manifest import AUDIOCPP_VULKAN, ESPEAK_NG, KOKORO_Q8, MING_Q8

Engine = Literal["kokoro", "ming"]

ALL_VOICES: tuple[str, ...] = audiocpp.VOICES + ming.VOICES

# Primary language subtags Kokoro speaks (with its multilingual file for all but English).
KOKORO_LANGUAGES = frozenset({"en", "es", "fr", "it", "pt", "hi", "ja", "zh"})
LANGUAGE_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "it": "Italian", "pt": "Portuguese",
    "hi": "Hindi", "ja": "Japanese", "zh": "Chinese", "de": "German", "ko": "Korean",
}  # fmt: skip
# ISO 639-2 and 639-3 codes (some EPUBs use them) for the languages above.
_THREE_LETTER = {
    "eng": "en", "spa": "es", "fra": "fr", "fre": "fr", "ita": "it", "por": "pt", "hin": "hi",
    "jpn": "ja", "zho": "zh", "chi": "zh", "cmn": "zh", "deu": "de", "ger": "de", "kor": "ko",
}  # fmt: skip

PREMIUM_NOTE = "The premium voice is about {size} to download and needs a graphics card."
NEEDS_GPU = (
    "The premium voice (Ming) needs a graphics card, and Tomecast isn't using one here. "
    "Choose a Kokoro voice instead (see `tomecast voices`)."
)


def engine_of(voice: str) -> Engine:
    """Which engine ``voice`` belongs to."""
    return "ming" if ming.is_ming_voice(voice) else "kokoro"


def language_code(language: str | None) -> str:
    """A book's language as a two-letter primary subtag: ``"en-GB"`` and ``"eng"`` become
    ``"en"``, ``"deu"`` and ``"ger"`` become ``"de"``. Unknown: ``en``."""
    if not language:
        return "en"
    code = language.replace("_", "-").split("-")[0].strip().lower() or "en"
    return _THREE_LETTER.get(code, code)


@dataclass(frozen=True, slots=True)
class Route:
    """The voice a book is read in, and a sentence for the user if it isn't the chosen one
    (or if no voice speaks the book's language)."""

    voice: str
    language: str  # the primary subtag the voice is told to speak
    note: str | None = None


def route(
    voice: str, language: str | None, *, premium_ok: Callable[[], bool] = lambda: False
) -> Route:
    """The voice for a book in ``language`` when ``voice`` was chosen.

    * A premium voice reads only the languages it has been checked for
      (:data:`tomecast.engines.ming.CHECKED_LANGUAGES`: English so far). A book in another
      language goes to the Kokoro voice closest to it (:data:`~tomecast.engines.ming.KOKORO_MATCH`),
      read with English rules, and the note says so.
    * A Kokoro book goes to the premium voice only in a language Kokoro doesn't speak and the
      premium voice is checked for, and only if ``premium_ok()`` says so (asked only then):
      the preview is opted into, the premium voice is already installed (routing never
      downloads it), and the card in use has room for it (:func:`premium_can_take`).
    * Kokoro's other languages (Spanish, French, Italian, Portuguese, Hindi, Japanese,
      Chinese) need its larger multilingual voice file, which isn't pinned yet, so the book
      stays with the chosen voice and the note says it's read by an English voice. (Routing
      Italian, Portuguese and Hindi to if_sara, pf_dora and hf_alpha waits for that file.)
    * Anything else stays with the chosen voice, read with English rules, and says so.
    """
    code = language_code(language)
    name = LANGUAGE_NAMES.get(code, code)
    if engine_of(voice) == "ming":
        if code in ming.CHECKED_LANGUAGES:
            return Route(voice, code)
        kokoro, kokoro_name, accent = ming.KOKORO_MATCH[voice]
        return Route(
            kokoro,
            "en",
            f"The premium voice hasn't been checked for {name} books yet, so this one uses "
            f"Kokoro's {kokoro_name} (similar {accent} voice), read with English rules.",
        )
    if code == "en":
        return Route(voice, code)
    if code in ming.CHECKED_LANGUAGES and code not in KOKORO_LANGUAGES and premium_ok():
        return Route(
            ming.DEFAULT_VOICE,
            code,
            f"Kokoro doesn't speak {name}, so Tomecast used the premium voice "
            f"({ming.DEFAULT_VOICE}).",
        )
    if code in KOKORO_LANGUAGES:
        return Route(
            voice,
            "en",
            f"Kokoro's {name} voices need a larger voice file that Tomecast doesn't download "
            "yet, so this book is read by an English voice.",
        )
    if code in ming.LANGUAGES:
        return Route(voice, "en", f"Kokoro doesn't speak {name}, so it's read with English rules.")
    return Route(voice, "en", f"No voice speaks {name} yet, so it's read with English rules.")


def premium_can_take(root: Path, use_gpu: bool) -> bool:
    """Whether a book may go to the premium voice without the user choosing it (language
    routing): the preview is opted into, the card is in use, the premium voice is already
    installed (so routing never downloads 1.74 GB on its own), and the card has room for it,
    by the hardware scan's own rule (:func:`scan_recommendation`). It lists the cards, so
    it's only asked when a book needs it."""
    if not (use_gpu and ming.preview_enabled()):
        return False
    if any(needs_download(asset, root, verify=False) for asset in ming.required_assets(True)):
        return False
    return scan_recommendation(root).voice == "ming"


def required_assets(voice: str, use_gpu: bool, *, with_kokoro: bool = True) -> tuple[Asset, ...]:
    """Everything a run with ``voice`` needs installed. For a premium voice that's also
    Kokoro's files (eSpeak and its model) when Kokoro is to take over if the graphics card
    fails (``with_kokoro``: the default, ``--if-card-fails kokoro``), so a takeover never
    has to download anything partway through a book."""
    kokoro = audiocpp.required_assets(use_gpu)
    if engine_of(voice) == "ming":
        premium = ming.required_assets(use_gpu)
        if not with_kokoro:
            return premium
        return (*premium, *(asset for asset in kokoro if asset not in premium))
    return kokoro


def work_key(voice: str) -> str:
    """What the work folder is keyed by for ``voice`` (see :func:`ming.work_key`)."""
    return ming.work_key(voice) if engine_of(voice) == "ming" else voice


def premium_note(missing: Sequence[Asset]) -> str | None:
    """The one sentence to show before downloading the premium voice, if it's among
    ``missing``: "The premium voice is about 1.8 GB to download and needs a graphics card."
    """
    if not any(asset.opt_in for asset in missing):
        return None
    total = sum(asset.size for asset in missing)
    return PREMIUM_NOTE.format(size=f"{total / 1e9:.1f} GB")


def voice_checks(
    root: Path, voice: str, use_gpu: bool, cancelled: IsCancelled | None = None
) -> list[VoiceCheck]:
    """The self-checks a new engine build must pass before a run with ``voice`` switches to
    it: the voice's own, plus every other installed voice that runs on that build (Ming runs
    only on the graphics-card build), so a build that works for one voice never breaks the
    other."""
    engine = engine_of(voice)
    checks = []

    def installed(*assets: Asset) -> bool:  # without hashing a 1.7 GB model here
        return not any(needs_download(asset, root, verify=False) for asset in assets)

    if engine == "kokoro" or installed(KOKORO_Q8, ESPEAK_NG):
        checks.append(audiocpp.kokoro_check(root, use_gpu, cancelled))
    if use_gpu and (engine == "ming" or installed(MING_Q8)):
        checks.append(ming.ming_check(root, use_gpu, cancelled))
    return checks


def prepare_voice(
    root: Path,
    voice: str,
    use_gpu: bool,
    *,
    consent: Consent = lambda missing, updates: True,
    on_download: DownloadReporter | None = None,
    on_check: Callable[[], None] | None = None,
    cancelled: IsCancelled | None = None,
    take_over: bool = False,
    with_kokoro: bool = True,
    on_wait: Callable[[], None] | None = None,
) -> Prepared:
    """Install what a run with ``voice`` needs (:func:`required_assets`), switching to new
    engine builds only once they pass :func:`voice_checks` (see
    :func:`tomecast.downloads.prepare`)."""
    return prepare(
        required_assets(voice, use_gpu, with_kokoro=with_kokoro),
        root,
        check=voice_checks(root, voice, use_gpu, cancelled),
        consent=consent,
        on_download=on_download,
        on_check=on_check,
        cancelled=cancelled,
        take_over=take_over,
        on_wait=on_wait,
    )


def make_engine(
    root: Path,
    voice: str,
    *,
    speed: float = 1.0,
    use_gpu: bool = True,
    language: str = "en",
    builds: Mapping[str, Path] | None = None,
    threads: int | None = None,
    if_card_fails: ming.CardPolicy = ming.DEFAULT_CARD_POLICY,
    kokoro: Callable[..., AutoKokoro] = AutoKokoro,
    premium: Callable[..., AutoKokoro] = ming.AutoMing,
) -> AutoKokoro:
    """The engine for ``voice``, not started: the one place an engine is made, so a premium
    voice always gets the speed cap and the language it may read. (Its listen-back check
    isn't part of the engine: the conversion loop runs it, from
    :func:`~tomecast.engines.ming.spoken_text_check`.) :class:`~tomecast.engines.ming.AutoMing`
    for a premium voice (told to speak ``language``, doing ``if_card_fails`` if the card
    fails), else :class:`~tomecast.engines.audiocpp.AutoKokoro`. ``kokoro`` and ``premium``
    are the classes to make (the command line passes its own names, which its tests replace).
    """
    if engine_of(voice) == "ming":
        return premium(
            root,
            voice=voice,
            speed=ming.capped_speed(speed),
            use_gpu=use_gpu,
            language=language if language in ming.CHECKED_LANGUAGES else "en",
            builds=builds,
            threads=threads,
            if_card_fails=if_card_fails,
        )
    return kokoro(root, voice=voice, speed=speed, use_gpu=use_gpu, builds=builds, threads=threads)


def scan_recommendation(root: Path) -> Recommendation:
    """The hardware scan's recommendation, with the graphics card the engine would use: the
    premium voice only while it's offered (:func:`~tomecast.engines.ming.preview_enabled`)
    and the installed graphics-card build sees a card with room for it
    (:func:`tomecast.machine.recommend`). Never downloads; without that build it's Kokoro."""
    vulkan = build_in_use(AUDIOCPP_VULKAN, root)
    devices = None
    if ming.preview_enabled() and vulkan is not None and vulkan_possible():
        devices = list_devices(vulkan / EXECUTABLE)
    return recommend(scan_machine(), pick_device(devices or []))
