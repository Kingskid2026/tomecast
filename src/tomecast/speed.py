"""How fast this computer voices, and the time estimates made from it.

A short speed test (:func:`measure`, a few seconds) voices a fixed passage and divides the
seconds of audio by the seconds it took: "4.7x real time". The result is kept in
``speeds.json`` in the data folder, once per engine build and device, so it is measured again
only when the engine build changes, on a new device, or after ``tomecast scan`` forgets them
all. The command line measures on the engine it has just started for a book; the window
measures in the background at start-up (:func:`probe`).

The estimates (:func:`audio_minutes`, :func:`make_minutes`) are the same everywhere: the
window's chapter summary, its drop screen, and the command line before converting.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from tomecast import __version__
from tomecast.audio import wav_info
from tomecast.book import Book, Chapter
from tomecast.downloads import (
    FAILURE_DAYS,
    Cancelled,
    IsCancelled,
    build_in_use,
    build_sha256,
    local_path,
)
from tomecast.engines.audiocpp import (
    AUDIOCPP_CPU,
    AUDIOCPP_VULKAN,
    DEFAULT_VOICE,
    ESPEAK_NG,
    EXECUTABLE,
    KOKORO_Q8,
    EngineError,
    KokoroServer,
    default_threads,
    split_speed,
)
from tomecast.hardware import list_devices, pick_device

# Narration speed used for estimates (Kokoro at speed 1.0 reads about this fast).
WORDS_PER_MINUTE = 158

_SPEEDS = "speeds.json"
# From Alice's Adventures in Wonderland (1865, public domain). The first sentence warms the
# engine up (its first request sets things up) and isn't timed; the rest, about 15 s of
# audio, are.
WARM_UP = "Alice was beginning to get very tired of sitting by her sister on the bank."
SAMPLE = (
    "So she was considering in her own mind, as well as she could, whether the pleasure of "
    "making a daisy-chain would be worth the trouble of getting up and picking the daisies.",
    "Suddenly a White Rabbit with pink eyes ran close by her.",
)


class Tunable(Protocol):
    """An engine whose voice and speed can be changed between requests (as audio.cpp's)."""

    voice: str
    speed: float

    def synthesize(self, text: str, output: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class Speeds:
    """What :func:`probe` found: how many times real time, on the CPU and on the graphics card."""

    cpu: float | None = None
    gpu: float | None = None
    gpu_description: str = ""  # the card the GPU speed was measured on


# --- Estimates ------------------------------------------------------------------------------


def word_count(chapter: Chapter) -> int:
    """Words in the chapter's text (the title isn't counted)."""
    return sum(len(paragraph.split()) for paragraph in chapter.paragraphs)


def words_in(book: Book, chapters: Sequence[int]) -> int:
    """Words in the chosen chapters of ``book``."""
    return sum(word_count(book.chapters[index]) for index in chapters)


def audio_minutes(words: int, speed: float = 1.0) -> float:
    """How long the audiobook of ``words`` will be at the speaking speed ``speed``."""
    return words / WORDS_PER_MINUTE / speed


def make_minutes(words: int, speed: float, speedup: float) -> float:
    """How long voicing ``words`` takes at ``speedup`` times real time. ``speedup`` is measured on
    the voice's own audio, so what counts is the voice's speed, which stops at 1.5x (the
    time-stretch on top is quick)."""
    voiced = words / WORDS_PER_MINUTE / split_speed(speed)[0]
    return voiced / speedup if speedup > 0 else 0.0


# --- Measuring --------------------------------------------------------------------------------


def measure(engine: Tunable) -> float:
    """Voice the sample on ``engine`` (already started) and return how many times real time
    it voices. Takes a few seconds on a CPU, about one on a graphics card. Uses the default
    voice at speed 1.0, and puts the engine's own voice and speed back afterwards.

    Raises:
        EngineError: if the engine fails, or made no audio.
    """
    voice, speed = engine.voice, engine.speed
    engine.voice, engine.speed = DEFAULT_VOICE, 1.0
    try:
        with tempfile.TemporaryDirectory(prefix="tomecast-speed-") as folder:
            engine.synthesize(WARM_UP, Path(folder) / "warm-up.wav")
            audio = 0.0
            started = time.monotonic()
            for index, text in enumerate(SAMPLE):
                wav = Path(folder) / f"{index}.wav"
                engine.synthesize(text, wav)
                audio += wav_info(wav).seconds
            taken = time.monotonic() - started
    except (OSError, wave.Error, EOFError) as error:
        raise EngineError(f"the speed test failed: {error}") from error
    finally:
        engine.voice, engine.speed = voice, speed
    if audio <= 0 or taken <= 0:
        raise EngineError("the speed test made no audio")
    return audio / taken


def speed_key(
    build: Path, *, backend: Literal["cpu", "vulkan"], gpu_description: str, threads: int
) -> str:
    """What a speed belongs to: the engine build (its folder), and the CPU with this many
    threads or the graphics card."""
    sha = build_sha256(build) or "unknown"
    device = f"vulkan/{gpu_description}" if backend == "vulkan" else f"cpu/{threads} threads"
    return f"{sha[:12]}/{device}"


def known_speed(root: Path, key: str) -> float | None:
    """The speed measured for ``key``, if it has been."""
    entry = _load(root).get(key)
    value = entry.get("speedup") if isinstance(entry, dict) else None
    return float(value) if isinstance(value, int | float) and value > 0 else None


def failed_recently(root: Path, key: str) -> bool:
    """True if the speed test for ``key`` failed less than :data:`FAILURE_DAYS` days ago, so
    it isn't tried again yet (a card that crashes isn't started on every launch)."""
    entry = _load(root).get(key)
    when = entry.get("failed_at") if isinstance(entry, dict) else None
    return isinstance(when, int | float) and time.time() - when < FAILURE_DAYS * 86400


def remember_speed(root: Path, key: str, speedup: float) -> None:
    speeds = _load(root)
    speeds[key] = {
        "speedup": round(speedup, 2),
        "measured": date.today().isoformat(),
        "tomecast": __version__,
    }
    _save(root, speeds)


def remember_failure(root: Path, key: str, reason: str) -> None:
    """The speed test for ``key`` failed: don't try it again for a few days."""
    speeds = _load(root)
    speeds[key] = {
        "failed_at": round(time.time()),
        "reason": reason[-500:],
        "tomecast": __version__,
    }
    _save(root, speeds)


def forget_speeds(root: Path) -> None:
    """Forget every measured speed (and failed speed test), so each is measured again
    (``tomecast scan``)."""
    (root / _SPEEDS).unlink(missing_ok=True)


def engine_speed(
    root: Path, engine: KokoroServer, *, measuring: Callable[[], None] | None = None
) -> float | None:
    """How many times real time ``engine`` (already started) voices, on the build and device
    it uses: the speed measured before, or measured now on it (``measuring`` is called first).
    ``None`` if the speed test failed (now, or recently); that never stops a conversion."""
    key = speed_key(
        engine.executable.parent,
        backend="vulkan" if engine.backend == "vulkan" else "cpu",
        gpu_description=getattr(engine, "gpu_description", ""),
        threads=engine.threads,
    )
    if (known := known_speed(root, key)) is not None:
        return known
    if failed_recently(root, key):
        return None
    if measuring:
        measuring()
    try:
        value = round(measure(engine), 2)  # as it's remembered, so every run shows the same
    except EngineError as error:
        remember_failure(root, key, str(error))
        return None
    remember_speed(root, key, value)
    return value


def probe(root: Path, *, gpu: bool, cancelled: IsCancelled | None = None) -> Speeds:
    """The CPU's speed, and with ``gpu`` the graphics card's, using the builds in use. Each is
    the one measured before, or measured now (starting the engine, a few seconds, with short
    time limits). A failed measurement is remembered too, so it isn't tried again for a few
    days. Never downloads or raises, except :class:`~tomecast.downloads.Cancelled` when
    ``cancelled`` says the user stopped (closing the window); anything not installed, or not
    working, is left out. With ``gpu`` false it doesn't touch the graphics card at all.

    The CPU speed is the CPU build's, or the graphics-card build's CPU backend if that is the
    only one installed (they are the same code).
    """
    if not (local_path(KOKORO_Q8, root).is_file() and build_in_use(ESPEAK_NG, root)):
        return Speeds()
    cpu_build = build_in_use(AUDIOCPP_CPU, root) or build_in_use(AUDIOCPP_VULKAN, root)
    cpu = _speed_on(root, cpu_build, "cpu", None, "", cancelled) if cpu_build else None
    vulkan = build_in_use(AUDIOCPP_VULKAN, root) if gpu else None
    device = pick_device(list_devices(vulkan / EXECUTABLE)) if vulkan else None
    if vulkan is None or device is None:
        return Speeds(cpu=cpu)
    on_card = _speed_on(root, vulkan, "vulkan", device.index, device.description, cancelled)
    return Speeds(cpu, on_card, device.description if on_card is not None else "")


# The probe runs at the window's start-up: an engine that hangs mustn't hold the window's
# worker for long (a real conversion gives it 120 s to start and 300 s per request).
PROBE_STARTUP_TIMEOUT = 30.0
PROBE_REQUEST_TIMEOUT = 30.0


def _speed_on(
    root: Path,
    build: Path,
    backend: Literal["cpu", "vulkan"],
    device: int | None,
    description: str,
    cancelled: IsCancelled | None,
) -> float | None:
    threads = default_threads()
    key = speed_key(build, backend=backend, gpu_description=description, threads=threads)
    if (known := known_speed(root, key)) is not None:
        return known
    if failed_recently(root, key):
        return None
    try:
        with KokoroServer(
            root,
            executable=build / EXECUTABLE,
            backend=backend,
            device=device,
            threads=threads,
            startup_timeout=PROBE_STARTUP_TIMEOUT,
            request_timeout=PROBE_REQUEST_TIMEOUT,
            cancelled=cancelled,
        ) as engine:
            value = round(measure(engine), 2)
    except (EngineError, OSError) as error:
        if cancelled is not None and cancelled():
            raise Cancelled from error  # stopped, not failed: nothing to remember
        remember_failure(root, key, str(error))
        return None
    remember_speed(root, key, value)
    return value


def _load(root: Path) -> dict[str, object]:
    try:
        payload = json.loads((root / _SPEEDS).read_text())
    except (OSError, ValueError):
        return {}
    speeds = payload.get("speeds") if isinstance(payload, dict) else None
    return dict(speeds) if isinstance(speeds, dict) else {}


def _save(root: Path, speeds: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=root, prefix=f".{_SPEEDS}.", delete=False, encoding="utf-8"
    ) as f:
        json.dump({"format": 1, "speeds": speeds}, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(f.name, root / _SPEEDS)
