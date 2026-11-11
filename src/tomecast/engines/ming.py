"""Ming-3.0-tts, the premium voice (issue #11), on the same audio.cpp server as Kokoro.

Ming runs on Tomecast's own audio.cpp builds (the Vulkan one: it's offered only with a
graphics card, since it is far too slow on a CPU), from audio.cpp's official Q8_0 file
(:data:`~tomecast.manifest.MING_Q8`, about 1.74 GB, downloaded only after the user chooses
it). It needs no eSpeak. Everything else is Kokoro's: the same server, chunk size, sentence
IDs, crash restarts, graphics-card choice and CPU fallback (:class:`AutoMing` is an
:class:`~tomecast.engines.audiocpp.AutoKokoro`), and the same packaging, at 44.1 kHz.

Facts this module relies on, checked against audio.cpp v0.9.4 (``local/ming-test/report.md``):

* the model's family is ``ming3_tts``; a request is ``{"model": "ming", "input": ...,
  "response_format": "wav", "options": {"template_name": "ip", "voice_id": ..., "language":
  ..., "seed": ...}}``. A top-level ``speed`` (even 1.0) is an HTTP 500, and a top-level
  ``voice`` is ignored, so neither is sent;
* unknown voice ids are accepted silently, so the voice is checked here;
* there's no speed of its own: all of it is the packager's pitch-preserving ``atempo``, which
  goes up to 2.0;
* one model pass holds about 22 s of speech; text is complete up to 350 characters, so
  Kokoro's 200-character chunks fit with room to spare;
* output is 44.1 kHz mono 16-bit;
* **it skips words** in about 1-2% of text (whole repeated phrases, mostly), the same words
  whatever the seed, guidance or thread count, and audio that's merely shorter isn't caught
  by :func:`~tomecast.convert.audio_problem`. So every chunk goes through a spoken-text
  check (:data:`SpokenCheck`, the hook a speech-recognition check plugs into), and a chunk
  that fails is voiced again in smaller pieces, each checked again.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from tomecast.audio import concatenate_wavs
from tomecast.chunking import split_long
from tomecast.downloads import Asset, IsCancelled, VoiceCheck, local_path
from tomecast.engines.audiocpp import (
    MIN_SPEED,
    AutoKokoro,
    EngineError,
    KokoroServer,
    engine_asset,
    self_check,
)
from tomecast.manifest import AUDIOCPP_MING_FLOW_STEPS, MING_Q8

# Ming's built-in voices. Which one is the default isn't decided yet (issue #11): UK_George
# is the provisional choice, because upstream labels it "audiobooks" (US_Emma: "narration").
VOICES = ("UK_George", "US_Emma", "UK_Charlotte", "US_James")
DEFAULT_VOICE = "UK_George"
# Primary language subtags Ming speaks. The request names one of them.
LANGUAGES = frozenset({"en", "zh", "de", "es", "fr", "ko", "ja"})

MODEL_ID = "ming"
FAMILY = "ming3_tts"
SAMPLE_RATE = 44_100
MAX_CHARS = 200  # the same cap as Kokoro's: complete up to 350, so well clear of the limit
MAX_SPEED = 2.0  # all of it is the packager's atempo, which goes up to 2.0
# Fewer flow steps: our own patch, in Tomecast's -tc2 engine build (not upstream). 5 uniform
# Euler steps measured 6.5x real time against 4.2x for upstream's 10 on CUDA, with the closest
# spectrum of every reduced setting and words lost within a chunk or two of the default.
FLOW_SETTINGS = {"AUDIOCPP_MING_FLOW_STEPS": "5", "AUDIOCPP_MING_FLOW_SCHEDULE": "uniform"}
# A chunk that fails the spoken-text check is split in halves at most this many times over
# (200 characters -> about 100 -> about 50), and pieces shorter than this aren't split again.
MAX_SPLIT_DEPTH = 2
MIN_SPLIT_CHARS = 24

# The hook for the spoken-text check: (a chunk's WAV, the text it should say) -> "" if every
# word was heard, else a short description of what's missing ("skipped 'and the Queen'").
# A speech-recognition check (being built separately) is plugged in here; until then there is
# none, and Ming's skips go through unnoticed, which is why Ming isn't offered yet.
SpokenCheck = Callable[[Path, str], str]
# Said before a book is voiced with Ming while no spoken-text check is plugged in. Empty once
# one is: the command line and the window only show it when it's set.
UNCHECKED_NOTE = (
    "The premium voice is a preview: it sometimes leaves out a few words, and Tomecast doesn't "
    "check it for that yet."
)


def is_ming_voice(voice: str) -> bool:
    """True for Ming's voices; every other voice name is Kokoro's."""
    return voice in VOICES


def required_assets(use_gpu: bool) -> tuple[Asset, ...]:
    """Everything a Ming run needs installed: the engine build and the Ming model."""
    return (engine_asset(use_gpu), MING_Q8)


def flow_settings() -> dict[str, str]:
    """The environment that makes Ming take fewer flow steps, if the pinned engine build
    reads it (:data:`~tomecast.manifest.AUDIOCPP_MING_FLOW_STEPS`), else nothing."""
    return dict(FLOW_SETTINGS) if AUDIOCPP_MING_FLOW_STEPS else {}


def work_key(voice: str) -> str:
    """What the work folder is keyed by in place of a Kokoro voice name: the voice, and the
    flow steps, which change the audio (so a book is never half one setting, half the other)."""
    steps = flow_settings()
    flow = (
        f"{steps['AUDIOCPP_MING_FLOW_STEPS']}-{steps['AUDIOCPP_MING_FLOW_SCHEDULE']}"
        if steps
        else "default"
    )
    return f"{MODEL_ID}/{voice}/flow={flow}"


def _checked_speed(speed: float) -> float:
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise EngineError(f"the premium voice's speed must be between {MIN_SPEED} and {MAX_SPEED}")
    return speed


class MingServer(KokoroServer):
    """A running audio.cpp server with Ming loaded. Use it as a context manager.

    ``language`` is the book's language (a code from :data:`LANGUAGES`), ``verify`` the
    spoken-text check run on every chunk (``None``: no check yet).
    """

    MODEL_ID = MODEL_ID
    default_voice = DEFAULT_VOICE  # what the speed test voices with

    def __init__(
        self,
        root: Path,
        *,
        voice: str = DEFAULT_VOICE,
        language: str = "en",
        verify: SpokenCheck | None = None,
        speed: float = 1.0,
        **kwargs: object,
    ) -> None:
        if voice not in VOICES:
            raise EngineError(
                f"unknown premium voice {voice!r}; choose one of: {', '.join(VOICES)}"
            )
        if language not in LANGUAGES:
            raise EngineError(f"the premium voice doesn't speak {language!r}")
        super().__init__(root, speed=_checked_speed(speed), **kwargs)  # type: ignore[arg-type]
        self.voice = voice  # after Kokoro's set-up, which only knows Kokoro's voices
        self.language = language
        self.verify = verify

    @property
    def speed(self) -> float:
        """The speed the listener asked for, from MIN_SPEED to :data:`MAX_SPEED`."""
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        self._speed = _checked_speed(value)

    @property
    def voice_speed(self) -> float:
        """Always 1.0: Ming has no speed of its own."""
        return 1.0

    @property
    def tempo(self) -> float:
        """All of the speed, applied by the packager."""
        return self._speed

    @property
    def label(self) -> str:
        return f"Ming ({self.voice})"

    @property
    def max_chars(self) -> int:
        return MAX_CHARS

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def builds(self) -> dict[str, Path]:
        """The engine build this runs, by slot. Ming uses no eSpeak."""
        return {self._build.slot: self.executable.parent}

    def synthesize(self, text: str, output: Path) -> None:
        """Voice ``text`` into ``output`` (44.1 kHz mono 16-bit WAV), checked by ``verify``."""
        self._synthesize_checked(text, output, depth=0)

    def _synthesize_checked(self, text: str, output: Path, *, depth: int) -> None:
        super().synthesize(text, output)
        if self.verify is None:
            return
        problem = self.verify(output, text)
        if not problem:
            return
        # The same text skips the same words again, so a retry has to change the text: voice
        # it in smaller pieces, each checked, and join them.
        if depth >= MAX_SPLIT_DEPTH or len(text) < MIN_SPLIT_CHARS:
            raise EngineError(f"the premium voice left out words ({problem}): {text[:80]!r}")
        parts = split_long(text, max(1, len(text) // 2))
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".checked-") as folder:
            wavs = []
            for index, part in enumerate(parts):
                wav = Path(folder) / f"{index}.wav"
                self._synthesize_checked(part, wav, depth=depth + 1)
                wavs.append(wav)
            concatenate_wavs(wavs, output, gap_seconds=0.05)

    def _request_body(self, text: str) -> dict[str, object]:
        return {
            "model": MODEL_ID,
            "input": text,
            "response_format": "wav",
            "options": {
                "template_name": "ip",
                "voice_id": self.voice,
                "language": self.language,
                "seed": self.seed,
            },
        }

    def _server_config(self, port: int) -> dict[str, object]:
        config = super()._server_config(port)
        config["models"] = [
            {
                "id": MODEL_ID,
                "family": FAMILY,
                "path": str(local_path(MING_Q8, self._root).resolve()),
                "task": "tts",
                "mode": "offline",
            }
        ]
        return config

    def _environment(self, private_dir: Path) -> dict[str, str]:
        environment = super()._environment(private_dir)
        environment.pop("AUDIOCPP_ESPEAK_LIBRARY", None)
        environment.pop("AUDIOCPP_ESPEAK_DATA", None)
        return {**environment, **flow_settings()}


class AutoMing(MingServer, AutoKokoro):
    """Ming that picks its own hardware exactly as :class:`AutoKokoro` does: the best graphics
    card, else the CPU backend of the same build (with :attr:`warning` set), and whatever
    else AutoKokoro learns (running out of graphics memory, #49) comes with it."""

    def __init__(self, root: Path, *, use_gpu: bool = True, **kwargs: object) -> None:
        super().__init__(root, use_gpu=use_gpu, **kwargs)  # type: ignore[arg-type]

    def __enter__(self) -> AutoMing:
        self.start()
        return self


def ming_check(root: Path, use_gpu: bool, cancelled: IsCancelled | None = None) -> VoiceCheck:
    """Ming's :class:`~tomecast.downloads.VoiceCheck` of a new engine build: the same check
    as Kokoro's (:func:`~tomecast.engines.audiocpp.self_check`), voiced by Ming. It runs on
    the engine build alone."""
    uses = frozenset({engine_asset(use_gpu).slot})
    return VoiceCheck("Ming", uses, self_check(root, use_gpu, cancelled, server=MingServer))
