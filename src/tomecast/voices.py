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
    is_installed,
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
# The Kokoro voice a book in this language gets when it's routed away from Ming.
KOKORO_VOICE_FOR = {"it": "if_sara", "pt": "pf_dora", "hi": "hf_alpha"}

PREMIUM_NOTE = "The premium voice is about {size} to download and needs a graphics card."
NEEDS_GPU = (
    "The premium voice (Ming) needs a graphics card, and Tomecast isn't using one here. "
    "Choose a Kokoro voice instead (see `tomecast voices`)."
)


def engine_of(voice: str) -> Engine:
    """Which engine ``voice`` belongs to."""
    return "ming" if ming.is_ming_voice(voice) else "kokoro"


def language_code(language: str | None) -> str:
    """A book's language as a primary subtag: ``"en-GB"`` becomes ``"en"``. Unknown: ``en``."""
    if not language:
        return "en"
    return language.replace("_", "-").split("-")[0].strip().lower() or "en"


@dataclass(frozen=True, slots=True)
class Route:
    """The voice a book is read in, and a sentence for the user if it isn't the chosen one
    (or if no voice speaks the book's language)."""

    voice: str
    language: str  # the primary subtag the voice is told to speak
    note: str | None = None


def route(voice: str, language: str | None, *, premium_possible: bool) -> Route:
    """The voice for a book in ``language`` when ``voice`` was chosen.

    If the chosen voice's engine doesn't speak the language and the other one does, the book
    goes to the other one, and the note says so: Italian, Portuguese and Hindi go to Kokoro;
    German and Korean go to Ming, but only when ``premium_possible`` (a graphics card is in
    use), since Ming isn't offered without one. If neither speaks it, the chosen voice reads
    it with English rules, as before, and the note says that too.
    """
    code = language_code(language)
    name = LANGUAGE_NAMES.get(code, code)
    if engine_of(voice) == "ming":
        if code in ming.LANGUAGES:
            return Route(voice, code)
        if code in KOKORO_LANGUAGES:
            kokoro = KOKORO_VOICE_FOR.get(code, audiocpp.DEFAULT_VOICE)
            return Route(
                kokoro,
                code,
                f"The premium voice doesn't speak {name}, so Tomecast used Kokoro ({kokoro}).",
            )
        return Route(voice, "en", f"No voice speaks {name} yet, so it's read with English rules.")
    if code in KOKORO_LANGUAGES:
        return Route(voice, code)
    if code in ming.LANGUAGES:
        if premium_possible:
            return Route(
                ming.DEFAULT_VOICE,
                code,
                f"Kokoro doesn't speak {name}, so Tomecast used the premium voice "
                f"({ming.DEFAULT_VOICE}).",
            )
        return Route(
            voice,
            "en",
            f"Kokoro doesn't speak {name}. The premium voice does, but it needs a graphics "
            "card, so it's read with English rules.",
        )
    return Route(voice, "en", f"No voice speaks {name} yet, so it's read with English rules.")


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
    kokoro_installed = is_installed(KOKORO_Q8, root) and is_installed(ESPEAK_NG, root)
    if engine == "kokoro" or kokoro_installed:
        checks.append(audiocpp.kokoro_check(root, use_gpu, cancelled))
    if use_gpu and (engine == "ming" or is_installed(MING_Q8, root)):
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
    )


def make_engine(
    root: Path,
    voice: str,
    *,
    speed: float = 1.0,
    use_gpu: bool = True,
    language: str = "en",
    verify: ming.SpokenCheck | None = None,
    builds: Mapping[str, Path] | None = None,
    threads: int | None = None,
    cancelled: IsCancelled | None = None,
) -> AutoKokoro:
    """The engine for ``voice``: :class:`~tomecast.engines.ming.AutoMing` for a Ming voice
    (told to speak ``language`` and checked by ``verify``), else
    :class:`~tomecast.engines.audiocpp.AutoKokoro`. Not started."""
    if engine_of(voice) == "ming":
        return ming.AutoMing(
            root,
            voice=voice,
            speed=speed,
            use_gpu=use_gpu,
            language=language if language in ming.LANGUAGES else "en",
            verify=verify,
            builds=builds,
            threads=threads,
            cancelled=cancelled,
        )
    return AutoKokoro(
        root,
        voice=voice,
        speed=speed,
        use_gpu=use_gpu,
        builds=builds,
        threads=threads,
        cancelled=cancelled,
    )


def scan_recommendation(root: Path) -> Recommendation:
    """The hardware scan's recommendation, with the graphics card the engine would use: the
    premium voice only if the installed graphics-card build sees a card with room for it
    (:func:`tomecast.machine.recommend`). Never downloads; without that build it's Kokoro."""
    vulkan = build_in_use(AUDIOCPP_VULKAN, root)
    devices = None
    if vulkan is not None and vulkan_possible():
        devices = list_devices(vulkan / EXECUTABLE)
    return recommend(scan_machine(), pick_device(devices or []))
