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
  by :func:`~tomecast.convert.audio_problem`. So every chunk goes through the listen-back
  check (:mod:`tomecast.spoken_check`, :class:`SpokenTextCheck` here), called from the
  conversion loop and never from inside the engine: it voices a failing chunk again with a
  new seed, then in smaller pieces, and if nothing fixes it, does what :data:`Unfixable` says
  (stop, by default).

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

**A preview, for now.** Until a recogniser is plugged in (:data:`TRANSCRIBER`), the
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
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

from tomecast import spoken_check
from tomecast.chunking import Chunk
from tomecast.convert import MissingAudioError
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
from tomecast.pronunciations import respelling
from tomecast.textprep import TextPrep

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
# The recogniser the listen-back check uses: (the data folder) -> a spoken_check.Transcriber.
# None until it's wired in: pinning Moonshine Streaming Tiny and running it on a CPU audio.cpp
# server is the follow-up (docs/spoken-check.md, "Wiring it in"). Setting it is the one
# switch: every premium-voice book is then checked (spoken_text_check), the preview opt-in and
# the notes below are no longer needed, and the work folder's key changes, so pieces voiced
# without the check are never reused as if they had been checked.
TRANSCRIBER: Callable[[Path], spoken_check.Transcriber] | None = None

# What happens to a chunk the premium voice still leaves words out of after every retry
# (spoken_check's on_unfixable): "stop", the default, stops the book with the error (the audio
# made so far is kept); "kokoro" has the closest Kokoro voice read that one chunk, and says so.
# Not decided yet (Ambrose): this is the one argument that chooses.
Unfixable = Literal["stop", "kokoro"]
DEFAULT_UNFIXABLE: Unfixable = "stop"

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
    """Whether the premium voices are offered: always once the listen-back check has a
    recogniser (:data:`TRANSCRIBER`), and until then only to someone who opted into the
    preview by setting ``TOMECAST_PREMIUM_PREVIEW=1``."""
    return TRANSCRIBER is not None or os.environ.get(PREVIEW_ENV) == "1"


def preview_note() -> str | None:
    """The sentence said before a premium-voice book while it isn't checked."""
    return UNCHECKED_NOTE if TRANSCRIBER is None else None


def capped_speed(speed: float) -> float:
    """``speed`` as the premium voice can make it: at most :data:`MAX_SPEED`."""
    return min(speed, MAX_SPEED)


CPU_NOTE = (
    "The graphics card stopped working, so Tomecast carried on with the premium voice on the "
    "CPU, which is much slower: the time left is measured again from here."
)
STOPPED_NOTE = "The graphics card stopped working, so Tomecast stopped."


class UncheckablePiece(MissingAudioError):
    """A chunk the listen-back check can't read (mostly another script): it isn't voiced at
    all rather than going unchecked. Like spoken_check's WordsSkippedError, it's the premium
    voice's problem, never the engine build's (:func:`not_the_builds_fault`)."""


def not_the_builds_fault(error: BaseException) -> bool:
    """True for failures that belong to the premium voice and its listen-back check, never to
    the shared engine build: words it kept leaving out (``WordsSkippedError``), and text the
    check can't read (``UncheckableTextError``, :class:`UncheckablePiece`), however they reach
    the caller (a conversion's ``MissingAudioError`` keeps them as its cause). They never roll
    a build back or count against it."""
    premium = (spoken_check.WordsSkippedError, spoken_check.UncheckableTextError, UncheckablePiece)
    seen: BaseException | None = error
    while seen is not None:
        if isinstance(seen, premium):
            return True
        seen = seen.__cause__
    return False


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
    checked = "on" if TRANSCRIBER is not None else "off"
    return f"{MODEL_ID}/{voice}/flow={flow}/spoken-check={checked}"


def _checked_speed(speed: float) -> float:
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise EngineError(f"the premium voice's speed must be between {MIN_SPEED} and {MAX_SPEED}")
    return speed


class MingServer(KokoroServer):
    """A running audio.cpp server with Ming loaded. Use it as a context manager.

    ``language`` is the book's language (a code from :data:`CHECKED_LANGUAGES`). The listen-back
    check isn't in here: the conversion loop runs it (:class:`SpokenTextCheck`).
    """

    MODEL_ID = MODEL_ID

    def __init__(
        self,
        root: Path,
        *,
        voice: str = DEFAULT_VOICE,
        language: str = "en",
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


# --- The listen-back check, from the conversion loop -------------------------------------------


class SpokenTextCheck:
    """The listen-back check for one premium-voice book (docs/spoken-check.md). It's handed to
    :func:`tomecast.convert.convert_book` as ``checked`` and called from the conversion loop
    for every chunk, never from inside the engine (where convert's own retry would run its
    whole plan again). Use it as a context manager, or call :meth:`close`.

    For each chunk it runs :func:`tomecast.spoken_check.speak_checked`: voice and listen; if
    words are missing, a new seed, then smaller pieces, each checked. The engine's seed is set
    for each request and put back. Once Kokoro has taken over from the premium voice (the card
    failed), chunks are voiced once and not checked: Kokoro doesn't skip words.

    When nothing fixes a chunk, ``on_unfixable`` decides (:data:`Unfixable`): ``"stop"`` (the
    default) raises spoken_check's ``WordsSkippedError``, a ``MissingAudioError``, so the book
    stops and keeps what it made; ``"kokoro"`` has the closest Kokoro voice read that whole
    chunk (on the CPU of the same engine build, started when first needed, so the card's
    memory stays the premium voice's), checks it too, and :attr:`note` says so. Text the check
    can't read raises :class:`UncheckablePiece` before anything is voiced; check the whole book
    first with :meth:`uncheckable`.
    """

    def __init__(
        self,
        engine: MingServer,
        listener: spoken_check.Listener,
        *,
        on_unfixable: Unfixable = DEFAULT_UNFIXABLE,
        plan: spoken_check.RetryPlan = spoken_check.DEFAULT_PLAN,
    ) -> None:
        self._engine = engine
        self._listener = listener
        self._plan = plan
        self.on_unfixable: Unfixable = on_unfixable
        self.read_by_kokoro: list[str] = []  # chunk IDs Kokoro read instead ("kokoro")
        self._kokoro: KokoroServer | None = None
        self._scratch: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> SpokenTextCheck:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def uncheckable(self, texts: Iterable[str]) -> str:
        """Why the check can't read one of ``texts`` (the book's chunks), or "" if it can
        read them all: so a book is refused before it starts, not at that chunk."""
        for text in texts:
            if reason := spoken_check.uncheckable(text):
                return reason
        return ""

    def __call__(self, chunk_id: str, text: str, voice: Callable[[str], Path]) -> list[Path]:
        """The WAVs that make up the chunk, in reading order (convert joins them).
        ``voice(text)`` voices any text into a new WAV with the engine as it is now."""
        if self._engine.switched_to is not None:  # Kokoro took over: it doesn't skip
            return [voice(text)]

        def seeded(piece: str, seed: int) -> Path:
            previous = self._engine.seed
            self._engine.seed = seed
            try:
                return voice(piece)
            finally:
                self._engine.seed = previous

        strategy: spoken_check.OnUnfixable = spoken_check.stop
        if self.on_unfixable == "kokoro":
            strategy = spoken_check.UseOtherVoice(
                self._read_with_kokoro, voice="Kokoro", listener=self._listener
            )
        try:
            checked = spoken_check.speak_checked(
                chunk_id, text, seeded, self._listener, seed=self._engine.seed,
                plan=self._plan, on_unfixable=strategy,
            )  # fmt: skip
        except spoken_check.UncheckableTextError as error:
            raise UncheckablePiece(
                f"the premium voice can't be checked on {chunk_id} ({text[:80]!r}): {error}"
            ) from error
        if checked.fallback is not None:
            self.read_by_kokoro.append(chunk_id)
        return [piece.wav for piece in checked.pieces]

    @property
    def note(self) -> str | None:
        """What to tell the user when Kokoro read some chunks instead ("kokoro")."""
        if not self.read_by_kokoro:
            return None
        _voice, name, accent = KOKORO_MATCH[self._engine.premium_voice]
        count = len(self.read_by_kokoro)
        pieces = "piece" if count == 1 else "pieces"
        return (
            f"Kokoro's {name} (similar {accent} voice) read {count} {pieces} of text the premium "
            "voice kept leaving words out of."
        )

    def _read_with_kokoro(self, text: str) -> Path:
        if self._kokoro is None:
            self._scratch = tempfile.TemporaryDirectory(prefix="tomecast-kokoro-")
            self._kokoro = KokoroServer(
                self._engine._root,
                voice=KOKORO_MATCH[self._engine.premium_voice][0],
                speed=1.0,  # as the premium voice: the whole book is stretched afterwards
                executable=self._engine.executable,  # the same build, on its CPU backend
                backend="cpu",
                builds=self._engine._pinned,
                threads=self._engine.threads,
            )
            self._kokoro.start()
        assert self._scratch is not None
        wav = Path(self._scratch.name) / f"{len(self.read_by_kokoro)}-{abs(hash(text))}.wav"
        self._kokoro.synthesize(text, wav)
        return wav

    def close(self) -> None:
        """Stop the Kokoro engine started for "kokoro", if any, and remove its files."""
        if self._kokoro is not None:
            self._kokoro.close()
            self._kokoro = None
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None


def spoken_text_check(
    root: Path,
    engine: object,
    *,
    prep: TextPrep,
    on_unfixable: Unfixable = DEFAULT_UNFIXABLE,
) -> SpokenTextCheck | None:
    """The listen-back check for a book voiced by ``engine`` with the text prep ``prep``, or
    ``None``: for Kokoro (it doesn't skip words), and while there's no recogniser yet
    (:data:`TRANSCRIBER`). The transcript goes through the book's language rules before it's
    compared (so "1865" matches "eighteen sixty-five"), and the spoken sides of its
    pronunciation lists count as heard when anything was heard in their place."""
    if TRANSCRIBER is None or not isinstance(engine, MingServer):
        return None
    listener = spoken_check.Listener(
        TRANSCRIBER(root),
        prepare_heard=TextPrep(prep.rules).body,  # the language's rules, without the fixes
        respellings=tuple(respelling(entry.spoken) for entry in prep.pronunciations.entries),
    )
    return SpokenTextCheck(engine, listener, on_unfixable=on_unfixable)


def ming_check(root: Path, use_gpu: bool, cancelled: IsCancelled | None = None) -> VoiceCheck:
    """Ming's :class:`~tomecast.downloads.VoiceCheck` of a new engine build: the same check
    as Kokoro's (:func:`~tomecast.engines.audiocpp.self_check`), voiced by Ming. It runs on
    the engine build alone."""
    uses = frozenset({engine_asset(use_gpu).slot})
    return VoiceCheck("Ming", uses, self_check(root, use_gpu, cancelled, server=MingServer))
