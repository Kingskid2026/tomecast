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

**If the graphics card fails** (it runs out of memory, the device is lost, or the engine
build won't run on it, as the book starts or partway), :class:`AutoMing` does what
:data:`CardPolicy` says, chosen by the user (``--if-card-fails``; the window always uses the
default):

* ``"kokoro"`` (the default): say so, and voice the rest of the book with the Kokoro voice
  closest to the premium one by accent and gender (:data:`KOKORO_MATCH`), at the same speed,
  on the card if Kokoro works there and else on the CPU. The pieces Ming already voiced are
  kept, and each remaining one is voiced once, by Kokoro;
* ``"cpu"``: carry on with Ming on the CPU, much slower;
* ``"stop"``: stop (:class:`CardStopped`), keeping every finished piece, so the same book
  resumes on a working card.

None of these counts against the engine build: no rollback, no "keeps crashing". An error
the engine *answers* with (an HTTP error about the text), and the spoken-text check failing,
aren't the card failing: they fail the piece as before.

**A preview, for now.** Until a spoken-text check is plugged in (:data:`SPOKEN_CHECK`), the
premium voices are offered only to someone who opts into the preview by setting
``TOMECAST_PREMIUM_PREVIEW=1`` (:func:`preview_enabled`): the window, ``tomecast voices``, the
scan's recommendation and language routing leave them out otherwise, and ``--voice
UK_George`` says how to opt in. And it reads only the languages its piece size has been
measured for (:data:`CHECKED_LANGUAGES`: English so far), because a piece cut short there
would go unnoticed.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from tomecast.audio import concatenate_wavs
from tomecast.chunking import Chunk, split_long
from tomecast.downloads import Asset, IsCancelled, VoiceCheck, local_path
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import (
    MIN_SPEED,
    AutoKokoro,
    EngineCrashed,
    EngineError,
    GpuUnavailable,
    KokoroServer,
    engine_asset,
    self_check,
)
from tomecast.manifest import AUDIOCPP_MING_FLOW_STEPS, MING_Q8

# Ming's built-in voices. The default is UK_George, which upstream labels "audiobooks"
# (US_Emma: "narration"); the maintainer chose it (issue #11).
VOICES = ("UK_George", "US_Emma", "UK_Charlotte", "US_James")
DEFAULT_VOICE = "UK_George"

# The Kokoro voice that takes over from each premium voice when the graphics card fails: the
# same accent and gender, and then Kokoro's best-rated voice of that kind (the first of its
# group in the window's voice list). (Kokoro voice, its name, its accent.)
KOKORO_MATCH: dict[str, tuple[str, str, str]] = {
    "UK_George": ("bm_george", "George", "British"),  # British male: the same name, too
    "UK_Charlotte": ("bf_emma", "Emma", "British"),  # British female
    "US_Emma": ("af_heart", "Heart", "American"),  # American female (Kokoro's default)
    "US_James": ("am_michael", "Michael", "American"),  # American male
}

# What to do when the graphics card fails under the premium voice (see the module's docs).
CardPolicy = Literal["kokoro", "cpu", "stop"]
CARD_POLICIES: tuple[CardPolicy, ...] = ("kokoro", "cpu", "stop")
DEFAULT_CARD_POLICY: CardPolicy = "kokoro"
# Primary language subtags Ming speaks. The request names one of them.
LANGUAGES = frozenset({"en", "zh", "de", "es", "fr", "ko", "ja"})
# The ones Tomecast lets it read: those whose piece size has been measured. MAX_CHARS (200)
# was measured on English, where Ming is complete up to about 350 characters; nothing checks
# that a piece in another language (Chinese, Japanese or Korean especially, with far more
# sound per character) isn't cut short, and audio_problem wouldn't notice. German is the next
# to measure: one 133-character sentence came out whole for UK_George and US_Emma (8 Nov).
CHECKED_LANGUAGES = frozenset({"en"})

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
SpokenCheck = Callable[[Path, str], str]
# The speech-recognition check every premium-voice engine gets (being built separately, as
# spoken_check). Setting it is the one switch: the engines use it (tomecast.voices.make_engine),
# the preview opt-in and the notes below are no longer needed, and the work folder's key
# changes, so pieces voiced without the check are never reused as if they had been checked.
SPOKEN_CHECK: SpokenCheck | None = None

# Until then, the premium voices are a preview that has to be asked for.
PREVIEW_ENV = "TOMECAST_PREMIUM_PREVIEW"
UNCHECKED_NOTE = (
    "The premium voice is a preview: it sometimes leaves out a few words, and Tomecast doesn't "
    "check it for that yet."
)
PREVIEW_OFF_NOTE = (
    "The premium voices (Ming) are a preview: they sometimes leave out a few words, and "
    f"Tomecast can't check for that yet. To try them anyway, set {PREVIEW_ENV}=1, for "
    f"example: {PREVIEW_ENV}=1 tomecast convert BOOK --voice {{voice}}"
)
CAPPED_NOTE = "The premium voice goes up to 2\u00d7, so this book is made at 2\u00d7."


def preview_enabled() -> bool:
    """Whether the premium voices are offered: always once a spoken-text check is plugged in
    (:data:`SPOKEN_CHECK`), and until then only to someone who opted into the preview by
    setting ``TOMECAST_PREMIUM_PREVIEW=1``."""
    return SPOKEN_CHECK is not None or os.environ.get(PREVIEW_ENV) == "1"


def preview_note() -> str | None:
    """The sentence said before a premium-voice book while it has no spoken-text check."""
    return UNCHECKED_NOTE if SPOKEN_CHECK is None else None


def capped_speed(speed: float) -> float:
    """``speed`` as the premium voice can make it: at most :data:`MAX_SPEED`."""
    return min(speed, MAX_SPEED)


CPU_NOTE = (
    "The graphics card stopped working, so Tomecast carried on with the premium voice on the "
    "CPU, which is much slower: the time left is measured again from here."
)
STOPPED_NOTE = "The graphics card stopped working, so Tomecast stopped."


class WordsLeftOut(EngineError):
    """The spoken-text check still heard words missing after re-splitting: the piece fails
    (it isn't the graphics card failing)."""


class CardStopped(Exception):
    """The graphics card failed under the premium voice and the user chose ``"stop"``. Not a
    :class:`RuntimeError`, so a conversion doesn't retry the piece: it stops at once, keeping
    the pieces already made, and it never counts against the engine build."""


def switch_note(premium: str, *, chapters_done: int | None = 0, partway: bool = False) -> str:
    """The sentence for the user when Kokoro takes over from the premium voice ``premium``:
    which Kokoro voice, and which chapters are in the premium voice. ``chapters_done`` is how
    many of the audiobook's chapters were finished before the switch (``None``: not known, so
    nothing is said about them), ``partway`` whether it came in the middle of the next one.
    Before anything was voiced, it says so instead."""
    _kokoro, name, accent = KOKORO_MATCH[premium]
    voice = f"Kokoro's {name} (similar {accent} voice)"
    if chapters_done == 0 and not partway:
        return (
            f"The graphics card didn't work, so this book uses {voice} instead of the premium "
            "voice."
        )
    said = f"The graphics card stopped working, so the rest of this book uses {voice}."
    if chapters_done is None:
        return said
    if chapters_done == 0:
        return f"{said} The start of chapter 1 is in the premium voice."
    whole = "Chapter 1" if chapters_done == 1 else f"Chapters 1\u2013{chapters_done}"
    if partway:
        start = f"the start of chapter {chapters_done + 1}"
        return f"{said} {whole} and {start} are in the premium voice."
    return f"{said} {whole} {'is' if chapters_done == 1 else 'are'} in the premium voice."


class BookPosition:
    """Which chapter a conversion is in, for :func:`switch_note`: given the pieces it will go
    through, in order, and told after each one how many are done (as progress reports say),
    it knows where the piece being voiced now is. Chapters are counted as the audiobook has
    them: those with something to say, in order."""

    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self._chapter_of: list[int] = []  # each piece's chapter, as the audiobook counts them
        self._first: set[int] = set()  # the pieces that start a chapter
        previous: int | None = None
        for index, chunk in enumerate(chunks):
            if chunk.chapter != previous:
                self._first.add(index)
                previous = chunk.chapter
            self._chapter_of.append(len(self._first) - 1)
        self.done = 0

    def update(self, chunks_done: int) -> None:
        self.done = chunks_done

    def where(self) -> tuple[int, bool]:
        """(chapters finished before the piece being voiced now, whether that piece is
        partway into its chapter)."""
        if self.done >= len(self._chapter_of):
            return len(self._first), False
        return self._chapter_of[self.done], self.done not in self._first


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
    """What the work folder is keyed by in place of a Kokoro voice name: the voice, the flow
    steps, which change the audio (so a book is never half one setting, half the other), and
    whether its pieces were checked for left-out words, so pieces made without the check are
    never reused once there is one. (The speed isn't in it: the premium voice always speaks
    at 1.0 and the whole book is stretched afterwards, so a book resumes at any speed.)"""
    steps = flow_settings()
    flow = (
        f"{steps['AUDIOCPP_MING_FLOW_STEPS']}-{steps['AUDIOCPP_MING_FLOW_SCHEDULE']}"
        if steps
        else "default"
    )
    checked = "on" if SPOKEN_CHECK is not None else "off"
    return f"{MODEL_ID}/{voice}/flow={flow}/spoken-check={checked}"


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
        if language not in CHECKED_LANGUAGES:
            raise EngineError(f"the premium voice hasn't been checked for {language!r} yet")
        super().__init__(root, speed=_checked_speed(speed), **kwargs)  # type: ignore[arg-type]
        self.voice = voice  # after Kokoro's set-up, which only knows Kokoro's voices
        self.premium_voice = voice  # kept when Kokoro takes over (AutoMing)
        self.language = language
        self.verify = verify
        # Set once Kokoro has taken over from the premium voice (the graphics card failed):
        # from then on this server runs Kokoro, with this voice, as KokoroServer does.
        self.switched_to: str | None = None

    @property
    def default_voice(self) -> str:
        """What the speed test voices with: this engine's own default."""
        return DEFAULT_VOICE if self.switched_to is None else audiocpp.DEFAULT_VOICE

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
        """How the narrator is credited: both voices once Kokoro has taken over."""
        if self.switched_to is not None:
            return f"Ming ({self.premium_voice}) and Kokoro ({self.switched_to})"
        return f"Ming ({self.voice})"

    @property
    def max_chars(self) -> int:
        return MAX_CHARS  # Kokoro's cap too, so a takeover never changes how text is split

    @property
    def sample_rate(self) -> int:
        """44.1 kHz; Kokoro's 24 kHz once it has taken over (the packager makes them one)."""
        return SAMPLE_RATE if self.switched_to is None else audiocpp.SAMPLE_RATE

    @property
    def builds(self) -> dict[str, Path]:
        """The engine build this runs, by slot. Ming uses no eSpeak; Kokoro does."""
        if self.switched_to is not None:
            return super().builds
        return {self._build.slot: self.executable.parent}

    def synthesize(self, text: str, output: Path) -> None:
        """Voice ``text`` into ``output`` (44.1 kHz mono 16-bit WAV), checked by ``verify``.
        Once Kokoro has taken over, Kokoro voices it (24 kHz), unchecked: it doesn't skip."""
        if self.switched_to is not None:
            super().synthesize(text, output)
            return
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
            raise WordsLeftOut(f"the premium voice left out words ({problem}): {text[:80]!r}")
        parts = split_long(text, max(1, len(text) // 2))
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".checked-") as folder:
            wavs = []
            for index, part in enumerate(parts):
                wav = Path(folder) / f"{index}.wav"
                self._synthesize_checked(part, wav, depth=depth + 1)
                wavs.append(wav)
            concatenate_wavs(wavs, output, gap_seconds=0.05)

    def _request_body(self, text: str) -> dict[str, object]:
        if self.switched_to is not None:
            return super()._request_body(text)
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
        if self.switched_to is not None:
            return config
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
        if self.switched_to is not None:
            return environment
        environment.pop("AUDIOCPP_ESPEAK_LIBRARY", None)
        environment.pop("AUDIOCPP_ESPEAK_DATA", None)
        return {**environment, **flow_settings()}


class AutoMing(MingServer, AutoKokoro):
    """Ming that picks its own hardware as :class:`AutoKokoro` does (the best graphics card),
    and does what ``if_card_fails`` says if the card fails, as it starts or partway: Kokoro
    takes over (the default), Ming carries on on the CPU, or it stops (see the module's docs).

    ``where`` tells it which chapter the book is in (:meth:`BookPosition.where`), so the note
    can say which chapters are in the premium voice. After a takeover or a move to the CPU,
    :attr:`warning` says what happened, :attr:`fell_back` is True (the run proves nothing
    about the build) and so is :attr:`out_of_memory` (whatever failed afterwards is never
    blamed on the build, as after #49), and :attr:`switched_to` is the Kokoro voice in use.
    """

    def __init__(
        self,
        root: Path,
        *,
        use_gpu: bool = True,
        if_card_fails: CardPolicy = DEFAULT_CARD_POLICY,
        **kwargs: object,
    ) -> None:
        super().__init__(root, use_gpu=use_gpu, **kwargs)  # type: ignore[arg-type]
        self.if_card_fails: CardPolicy = if_card_fails
        self.where: Callable[[], tuple[int, bool]] | None = None
        self._takeover_failed: EngineError | None = None

    def __enter__(self) -> AutoMing:
        self.start()
        return self

    def start(self) -> None:
        if self._process is not None:
            return
        if self.switched_to is not None:
            self._start_kokoro()  # a later start of the same book's engine: still Kokoro
            return
        super().start()

    def synthesize(self, text: str, output: Path) -> None:
        if self.switched_to is not None or self.backend != "vulkan":
            if self._takeover_failed is not None:
                raise self._takeover_failed
            super().synthesize(text, output)
            return
        try:
            super().synthesize(text, output)
            return
        except _CardFailed as failure:  # out of memory, or back off the card (_carry_on_on_cpu)
            reason = str(failure)
        except (GpuUnavailable, EngineCrashed) as error:  # the engine died or hung on the card
            reason = str(error)
        self._card_failed(reason, at_start=False)
        self.synthesize(text, output)  # the same piece again, once, on what runs now

    def _use_cpu(self, note: str) -> None:
        """As the book starts, the card didn't work (AutoKokoro would use the CPU)."""
        if self.switched_to is not None or self.if_card_fails == "cpu":
            super()._use_cpu(note)
            return
        self._card_failed(note, at_start=True)

    def _carry_on_on_cpu(self, note: str) -> None:
        """Partway, the card ran out of memory or a crash restart came back off the card
        (AutoKokoro would carry on on the CPU)."""
        if self.switched_to is not None or self.if_card_fails == "cpu":
            super()._carry_on_on_cpu(note)
            return
        raise _CardFailed(note)

    def _card_failed(self, reason: str, *, at_start: bool) -> None:
        if self.if_card_fails == "stop":
            self._stop_process()
            raise CardStopped(STOPPED_NOTE)
        if self.if_card_fails == "cpu":
            self._ming_on_cpu()
            return
        if at_start:  # nothing voiced yet in this run
            note = switch_note(self.premium_voice)
        elif self.where is None:  # partway, but where in the book isn't known
            note = switch_note(self.premium_voice, chapters_done=None)
        else:
            done, partway = self.where()
            note = switch_note(self.premium_voice, chapters_done=done, partway=partway)
        self.switched_to = KOKORO_MATCH[self.premium_voice][0]
        self.voice = self.switched_to
        self.MODEL_ID = KokoroServer.MODEL_ID
        self.close()  # Ming's server and its temporary files: one model at a time
        try:
            self._start_kokoro()
        except EngineError as error:
            self._takeover_failed = EngineError(
                f"the graphics card stopped working, and Kokoro couldn't take over: {error}"
            )
            raise self._takeover_failed from error
        finally:
            self.warning, self.fell_back, self.out_of_memory = note, True, True

    def _start_kokoro(self) -> None:
        """Kokoro, wherever Kokoro runs: the card if it works, else the CPU (AutoKokoro)."""
        warning = self.warning
        self.backend, self.device = ("vulkan" if self.use_gpu else "cpu"), None
        self._consecutive_restarts = 0
        AutoKokoro.start(self)
        if warning is not None:
            self.warning = warning
        self.fell_back = self.out_of_memory = True

    def _ming_on_cpu(self) -> None:
        """Carry on with Ming on the CPU backend of the same build ("cpu"), as AutoKokoro
        does after running out of memory, and never blame the build for what follows."""
        AutoKokoro._carry_on_on_cpu(self, CPU_NOTE)
        self.out_of_memory = True


class _CardFailed(GpuUnavailable):
    """The card ran out of memory partway under the premium voice, and the user's choice
    isn't to carry on with it on the CPU (raised inside :class:`AutoMing` only)."""


def ming_check(root: Path, use_gpu: bool, cancelled: IsCancelled | None = None) -> VoiceCheck:
    """Ming's :class:`~tomecast.downloads.VoiceCheck` of a new engine build: the same check
    as Kokoro's (:func:`~tomecast.engines.audiocpp.self_check`), voiced by Ming. It runs on
    the engine build alone."""
    uses = frozenset({engine_asset(use_gpu).slot})
    return VoiceCheck("Ming", uses, self_check(root, use_gpu, cancelled, server=MingServer))
