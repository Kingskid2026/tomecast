"""Kokoro through audio.cpp: which pinned files it needs, a client for its local server, and
the self-check a new engine build must pass before Tomecast switches to it.

audio.cpp (https://github.com/0xShug0/audio.cpp) runs the Kokoro voice model. Tomecast starts
its HTTP server once per conversion, so the model is loaded a single time, and sends it one
small chunk of text per request. Facts this module relies on were checked against v0.9.4:

* upstream's prebuilt Linux binaries need glibc 2.43, so Tomecast uses its own build of the
  same release (.github/workflows/engine-build.yml), which needs glibc 2.28 and a CPU with
  AVX2, and changes nothing else unless one of its opt-in patches is turned on;
* Kokoro needs eSpeak NG at runtime for every sentence (found via environment variables);
* eSpeak's data path must be shorter than 160 bytes;
* more than 510 phonemes in one request is a hard error, not a silent cut;
* the server loads its bundled model files into ``$TMPDIR/audiocpp-gguf/<hash>``.
"""

from __future__ import annotations

import contextlib
import ctypes
import http.client
import json
import os
import platform
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Literal

from tomecast.audio import concatenate_wavs
from tomecast.chunking import split_long
from tomecast.convert import audio_problem
from tomecast.downloads import (
    Asset,
    Cancelled,
    CheckResult,
    Consent,
    DownloadReporter,
    Inconclusive,
    IsCancelled,
    Prepared,
    SelfCheck,
    VoiceCheck,
    local_path,
    prepare,
)
from tomecast.hardware import GpuDevice, list_devices, pick_device, short_name
from tomecast.machine import voice_threads

# The pins live in tomecast.manifest; they're re-exported so callers can import them here.
from tomecast.manifest import AUDIOCPP_BUILD as AUDIOCPP_BUILD
from tomecast.manifest import AUDIOCPP_CPU as AUDIOCPP_CPU
from tomecast.manifest import AUDIOCPP_VERSION as AUDIOCPP_VERSION
from tomecast.manifest import AUDIOCPP_VULKAN as AUDIOCPP_VULKAN
from tomecast.manifest import ESPEAK_NG as ESPEAK_NG
from tomecast.manifest import KOKORO_Q8 as KOKORO_Q8

# Our engine build needs glibc 2.28; the eSpeak NG library from the espeakng-loader wheel
# needs 2.29 (its exp and pow), so that's the floor for voicing.
MIN_GLIBC = (2, 29)
# The engine's CPU kernels are built for x86-64-v3: Intel from 2013 (Haswell) and AMD from
# 2015 (Excavator) on. Without these, it would crash with "illegal instruction".
CPU_FEATURES = ("avx", "avx2", "fma", "f16c", "bmi2")
EXECUTABLE = "audiocpp_server"  # in each engine build's folder


def engine_asset(use_gpu: bool) -> Asset:
    """The engine build a run needs: the Vulkan one to use the graphics card, else the CPU one."""
    return AUDIOCPP_VULKAN if use_gpu else AUDIOCPP_CPU


def required_assets(use_gpu: bool) -> tuple[Asset, ...]:
    """Everything a run needs installed: the engine build, eSpeak and the Kokoro model."""
    return (engine_asset(use_gpu), ESPEAK_NG, KOKORO_Q8)


# Kokoro's built-in voices. The first letter is the language, the second the voice's gender.
VOICES = (
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
    "ef_dora",
    "em_alex",
    "em_santa",
    "ff_siwis",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
    "if_sara",
    "im_nicola",
    "jf_alpha",
    "jf_gongitsune",
    "jf_nezumi",
    "jf_tebukuro",
    "jm_kumo",
    "pf_dora",
    "pm_alex",
    "pm_santa",
    "zf_xiaobei",
    "zf_xiaoni",
    "zf_xiaoxiao",
    "zf_xiaoyi",
    "zm_yunjian",
    "zm_yunxi",
    "zm_yunxia",
    "zm_yunyang",
)
DEFAULT_VOICE = "af_heart"
DEFAULT_SEED = 42  # fixed, so a resumed book sounds the same as one made in one go

# audio.cpp splits requests over 240 characters itself; staying under that keeps every
# request a single model pass, and well clear of the 510-phoneme limit for normal prose.
MAX_CHARS = 200
_ESPEAK_PATH_LIMIT = 150  # bytes; eSpeak NG 1.52 has a 160-byte path buffer
SAMPLE_RATE = 24_000  # Kokoro's output
_MAX_RESTARTS = 2  # in a row, on the same text, before giving up
# The port is picked free, then released for the engine to take, so on a busy machine another
# program can grab it first. The engine then says so and exits; Tomecast tries a fresh port.
_PORT_ATTEMPTS = 3
_PORT_TAKEN = re.compile(
    r"could not bind|failed to bind ip|failed to listen|address already in use", re.IGNORECASE
)
_PHONEME_LIMIT_MESSAGE = "exceeds 510"


class EngineError(RuntimeError):
    """Raised when the voice engine can't start or can't voice a piece of text."""


class GpuUnavailable(EngineError):
    """The engine was asked to use the graphics card and couldn't (or used the CPU instead)."""


class _ConnectionLost(EngineError):
    """The engine didn't answer at all (as opposed to answering with an error)."""


# Requests only ever go to the engine on 127.0.0.1, so never through a proxy: with
# ``http_proxy`` set, urllib would otherwise send the book's text to the proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def glibc_version() -> tuple[int, int] | None:
    """This system's glibc version, or ``None`` if it isn't a glibc system."""
    try:
        reported = os.confstr("CS_GNU_LIBC_VERSION")  # e.g. "glibc 2.44"
    except (ValueError, OSError):
        return None
    if not reported:
        return None
    name, _, version = reported.partition(" ")
    if name != "glibc":
        return None
    major, _, minor = version.partition(".")
    try:
        return int(major), int(minor.split(".")[0])
    except ValueError:
        return None


def cpu_flags(cpuinfo: Path = Path("/proc/cpuinfo")) -> frozenset[str] | None:
    """The CPU's feature flags from ``/proc/cpuinfo``, or ``None`` if they can't be read."""
    try:
        with cpuinfo.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                name, _, value = line.partition(":")
                if name.strip() == "flags":
                    return frozenset(value.split())
    except OSError:
        pass
    return None


def check_system() -> None:
    """Raise :class:`EngineError` if this computer can't run the voice engine."""
    machine = platform.machine()
    if machine != "x86_64":
        raise EngineError(
            "The Kokoro voice engine needs a 64-bit Intel or AMD processor (x86-64), and this "
            f"computer has {machine or 'an unknown kind'}, which isn't supported yet."
        )
    found = glibc_version()
    if found is None or found < MIN_GLIBC:
        have = "a system without glibc" if found is None else f"glibc {found[0]}.{found[1]}"
        raise EngineError(
            f"The Kokoro voice engine needs glibc {MIN_GLIBC[0]}.{MIN_GLIBC[1]} or newer, and "
            f"this computer has {have}. Distros from 2019 on have it (Ubuntu 20.04, Debian 11, "
            "Fedora 30, RHEL 9 and newer); Alpine and other musl-based ones aren't supported."
        )
    flags = cpu_flags()
    if flags is None:
        return  # can't tell; let the engine try
    missing = [feature for feature in CPU_FEATURES if feature not in flags]
    if missing:
        raise EngineError(
            "The Kokoro voice engine needs a processor with AVX2 (Intel from 2013 or AMD from "
            f"2015 on), and this one is missing: {', '.join(missing)}."
        )


def default_threads() -> int:
    """The threads the hardware scan picks now: physical cores, roughly (audio.cpp was slower
    with every hyperthread in use), and fewer on battery or with little memory (see
    :func:`tomecast.machine.recommend`)."""
    return voice_threads()


MIN_SPEED, MAX_SPEED = 0.5, 3.0
# Kokoro's own ``speed`` is linear up to about 1.5 and then saturates: measured on one
# sentence, 2.0 only made it 1.87x faster and 3.0 only 2.12x. So the voice never goes past
# this, and the rest of a faster request is a pitch-preserving time-stretch of the audiobook.
VOICE_SPEED_LIMIT = 1.5


def split_speed(speed: float) -> tuple[float, float]:
    """Split a requested speed into ``(voice_speed, tempo)``, where
    ``voice_speed * tempo == speed``. The one place this is decided."""
    voice_speed = min(speed, VOICE_SPEED_LIMIT)
    return voice_speed, speed / voice_speed


def _checked_speed(speed: float) -> float:
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise EngineError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}")
    return speed


class KokoroServer:
    """A running audio.cpp server with Kokoro loaded. Use it as a context manager."""

    def __init__(
        self,
        root: Path,
        *,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        threads: int | None = None,
        seed: int = DEFAULT_SEED,
        startup_timeout: float = 120.0,
        request_timeout: float = 300.0,
        executable: Path | None = None,
        backend: Literal["cpu", "vulkan"] = "cpu",
        device: int | None = None,
        espeak: Path | None = None,
        builds: Mapping[str, Path] | None = None,
        cancelled: IsCancelled | None = None,
    ) -> None:
        if voice not in VOICES:
            raise EngineError(f"unknown voice {voice!r}; choose one of: {', '.join(VOICES)}")
        self.voice = voice
        self._speed = _checked_speed(speed)
        self.seed = seed
        self._root = root
        self._threads = threads or default_threads()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self.backend = backend
        self.device = device
        # Which builds to run: given here (a self-check runs a new one), or pinned by
        # prepare() for this run, or else the ones in use when it first starts. Either way
        # they're decided once (see _resolve) and kept, so crash restarts and the CPU fallback
        # never start a different build in the middle of a book.
        self._chosen_executable = executable
        self._build = engine_asset(backend == "vulkan")  # kept when falling back to the CPU
        self._espeak = espeak
        self._pinned: dict[str, Path] = dict(builds or {})
        self._run: tuple[Path, Path] | None = None  # (program, eSpeak folder), once decided
        self._cancelled = cancelled
        self._aborted = threading.Event()
        self._reported_backend = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._base_url = ""
        self._consecutive_restarts = 0

    @property
    def speed(self) -> float:
        """The speed the listener asked for, from MIN_SPEED to MAX_SPEED."""
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        self._speed = _checked_speed(value)

    @property
    def voice_speed(self) -> float:
        """The speed the voice itself speaks at: ``speed``, capped at VOICE_SPEED_LIMIT."""
        return split_speed(self._speed)[0]

    @property
    def tempo(self) -> float:
        """The time-stretch left over after the voice: ``speed / voice_speed``. The M4B is
        stretched by this much when it is encoded."""
        return split_speed(self._speed)[1]

    @property
    def label(self) -> str:
        return f"Kokoro ({self.voice})"

    @property
    def threads(self) -> int:
        """CPU threads the engine is told to use."""
        return self._threads

    @property
    def executable(self) -> Path:
        """The engine program this runs (its folder is the engine build): the one given, else
        the one pinned for this run, else the build in use of the engine it was made for (the
        Vulkan build even after falling back to its CPU backend). Decided at the first start
        and kept; before that it's what a start would use now, so an engine made before an
        update was switched to starts the new build."""
        return (self._run or self._resolve())[0]

    @property
    def builds(self) -> dict[str, Path]:
        """The engine and eSpeak NG builds this runs, by slot (decided at the first start)."""
        program, espeak = self._run or self._resolve()
        return {self._build.slot: program.parent, ESPEAK_NG.slot: espeak}

    def pin_builds(self, builds: Mapping[str, Path]) -> None:
        """Run exactly these builds (slot -> folder), as :func:`prepare_voice` chose them for
        this run and keyed its work folder by. Takes effect at the next start."""
        self._pinned = dict(builds)
        if self._process is None:
            self._run = None

    def abort(self) -> None:
        """Stop the engine now, from any thread: a start-up or request in progress fails at
        once, and it isn't restarted (closing the window does this)."""
        self._aborted.set()
        process = self._process
        if process is not None:
            with contextlib.suppress(OSError):
                process.terminate()

    def _resolve(self) -> tuple[Path, Path]:
        program = self._chosen_executable or (
            self._pinned[self._build.slot] / EXECUTABLE
            if self._build.slot in self._pinned
            else local_path(self._build, self._root) / EXECUTABLE
        )
        espeak = (
            self._espeak or self._pinned.get(ESPEAK_NG.slot) or local_path(ESPEAK_NG, self._root)
        )
        return program, espeak

    @property
    def max_chars(self) -> int:
        return MAX_CHARS

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    def __enter__(self) -> KokoroServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        """Start the server and wait until the model is loaded."""
        if self._process is not None:
            return
        if self._run is None:
            self._run = self._resolve()
        # A private (0700) folder for the config, the log and the short eSpeak link.
        self._tempdir = tempfile.TemporaryDirectory(prefix="tomecast-audiocpp-")
        try:
            self._launch()
        except GpuUnavailable:
            self.close()
            raise
        except EngineError as error:
            self.close()
            if self.backend == "vulkan":
                raise GpuUnavailable(str(error)) from error
            raise
        except BaseException:  # including Ctrl-C while the model loads
            self.close()
            raise

    def close(self) -> None:
        """Stop the server and clean up its temporary files."""
        self._stop_process()
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def synthesize(self, text: str, output: Path) -> None:
        """Voice ``text`` into ``output`` (24 kHz mono 16-bit WAV)."""
        if self._process is None:
            raise EngineError("the voice engine isn't running")
        try:
            data = self._request_with_recovery(text)
        except EngineError as error:
            if _PHONEME_LIMIT_MESSAGE not in str(error) or len(text) < 2:
                raise
            # Too many sounds for one request (long numbers, mostly): split it and join.
            self._synthesize_in_parts(text, output)
            return
        output.write_bytes(data)

    def _synthesize_in_parts(self, text: str, output: Path) -> None:
        parts = split_long(text, max(1, len(text) // 2))
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".split-") as tmp:
            wavs = []
            for index, part in enumerate(parts):
                wav = Path(tmp) / f"{index}.wav"
                self.synthesize(part, wav)
                wavs.append(wav)
            concatenate_wavs(wavs, output, gap_seconds=0.05)

    def _request_with_recovery(self, text: str) -> bytes:
        """Send ``text``; if the engine has crashed, restart it and try once more.

        Restarts are counted per piece of text, so a long book survives an occasional crash,
        but an engine that crashes on the same text again and again gives up with its log.
        """
        try:
            data = self._request(text)
        except _ConnectionLost as error:
            if self._process is None or not _has_exited(self._process):
                raise  # still running, just not answering
            # Stopped on purpose (Cancel, or closing the window): not a crash, so neither a
            # restart nor "keeps crashing", which could be blamed on the build.
            self._stop_if_asked()
            log = _tail(self._log_path)
            if self._consecutive_restarts >= _MAX_RESTARTS:
                raise EngineError(f"the voice engine keeps crashing. Its log:\n{log}") from error
            self._consecutive_restarts += 1
            self._stop_process()
            self._launch()
            try:
                data = self._request(text)
            except _ConnectionLost as retry_error:
                raise EngineError(
                    f"the voice engine crashed and didn't recover. Its log:\n{log}"
                ) from retry_error
        self._consecutive_restarts = 0
        return data

    @property
    def _log_path(self) -> Path:
        assert self._tempdir is not None
        return Path(self._tempdir.name) / "server.log"

    def _launch(self) -> None:
        """Start the server process in the temp folder and wait until it's healthy.

        If the port was taken in the moment between picking and binding it, try a new one.
        """
        for attempt in range(1, _PORT_ATTEMPTS + 1):
            try:
                self._launch_once()
                return
            except _PortTaken as taken:
                self._process = None
                if attempt == _PORT_ATTEMPTS:
                    raise EngineError(
                        "the voice engine couldn't get a free network port on this computer "
                        f"after {_PORT_ATTEMPTS} tries:\n{taken}"
                    ) from None

    def _launch_once(self) -> None:
        assert self._tempdir is not None
        workdir = Path(self._tempdir.name)
        log_start = self._log_path.stat().st_size if self._log_path.exists() else 0
        port = _free_port()
        self._base_url = f"http://127.0.0.1:{port}"
        config = workdir / "server.json"
        config.write_text(json.dumps(self._server_config(port)))
        self._stop_if_asked()
        with self._log_path.open("ab") as log:
            self._process = subprocess.Popen(
                [str(self.executable), "--config", str(config), "--no-ui"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self._environment(workdir),
                preexec_fn=_stop_with_parent,  # only calls prctl, safe after fork
            )
        with _RUNNING_LOCK:
            _RUNNING.add(self)
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            self._stop_if_asked()
            if self._process.poll() is not None:
                with self._log_path.open("rb") as log_file:
                    log_file.seek(log_start)
                    this_start = log_file.read().decode("utf-8", "replace")
                if _PORT_TAKEN.search(this_start):
                    raise _PortTaken(this_start.strip()[-400:])
                said = _tail(self._log_path).strip()
                if not said:  # e.g. a damaged program, which dies before it can log anything
                    said = f"(no log; it exited with {_exit_status(self._process.returncode)})"
                raise EngineError(f"the voice engine stopped while starting:\n{said}")
            if self._healthy():
                if self.backend == "vulkan" and self._reported_backend != "vulkan":
                    raise GpuUnavailable(
                        f"the voice engine reported the {self._reported_backend or 'unknown'} "
                        "backend, not the graphics card"
                    )
                return
            time.sleep(0.2)
        raise EngineError(
            f"the voice engine didn't start within {self._startup_timeout:.0f}s:\n"
            f"{_tail(self._log_path)}"
        )

    def _stop_if_asked(self) -> None:
        if self._aborted.is_set() or (self._cancelled is not None and self._cancelled()):
            raise Cancelled

    def _stop_process(self) -> None:
        with _RUNNING_LOCK:
            _RUNNING.discard(self)
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None

    def _request(self, text: str) -> bytes:
        body = json.dumps(
            {
                "model": "kokoro",
                "input": text,
                "voice": self.voice,
                "speed": self.voice_speed,
                "response_format": "wav",
                "options": {"seed": self.seed},
            }
        ).encode()
        request = urllib.request.Request(
            f"{self._base_url}/v1/audio/speech",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _LOCAL.open(request, timeout=self._request_timeout) as response:
                data: bytes = response.read()
                return data
        except urllib.error.HTTPError as error:
            raise EngineError(_error_message(error)) from error
        except (OSError, http.client.HTTPException) as error:
            raise _ConnectionLost(f"couldn't reach the voice engine: {error}") from error

    def loaded_device(self) -> int | None:
        """The device the server says the voice model is loaded on, or ``None`` if it doesn't
        say. ``/health`` only names the backend. Checked against audio.cpp 0.9.4 on this
        project's machines: ``GET /v1/models`` returns ``{"object": "list", "data": [{"id":
        "kokoro", "object": "model", "owned_by": "engine", "family": "kokoro_tts", "task":
        "tts", "mode": "offline", "device": 1, "loaded": true, "configured": true, "path":
        "..."}]}``, where ``device`` is the ``device`` index from the config (1 for the AMD
        860M after the NVIDIA card). On the CPU backend it says 0, which means nothing, so
        it's only read on Vulkan."""
        try:
            with _LOCAL.open(f"{self._base_url}/v1/models", timeout=5) as response:
                payload = json.loads(response.read())
        except (OSError, http.client.HTTPException, ValueError):
            return None
        models = payload.get("data") if isinstance(payload, dict) else None
        for model in models if isinstance(models, list) else ():
            if isinstance(model, dict) and model.get("id") == "kokoro" and model.get("loaded"):
                device = model.get("device")
                return device if isinstance(device, int) and not isinstance(device, bool) else None
        return None

    def _healthy(self) -> bool:
        try:
            with _LOCAL.open(f"{self._base_url}/health", timeout=2) as response:
                payload = json.loads(response.read())
        except (OSError, http.client.HTTPException, ValueError):
            return False
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False
        self._reported_backend = str(payload.get("backend", ""))
        return True

    def _server_config(self, port: int) -> dict[str, object]:
        return {
            "host": "127.0.0.1",
            "port": port,
            "backend": self.backend,  # the server defaults to CUDA, so always say
            **(
                {"device": self.device}
                if self.backend == "vulkan" and self.device is not None
                else {}
            ),
            "threads": self._threads,
            "lazy_load": False,
            "ui": False,
            "models": [
                {
                    "id": "kokoro",
                    "family": "kokoro_tts",
                    "path": str(local_path(KOKORO_Q8, self._root).resolve()),
                    "task": "tts",
                    "mode": "offline",
                    "default_voice_preset": {"voice_id": self.voice},
                    "default_request_options": {"speed": self.voice_speed},
                }
            ],
        }

    def _environment(self, private_dir: Path) -> dict[str, str]:
        espeak = (self._run or self._resolve())[1] / "espeakng_loader"
        data = espeak / "espeak-ng-data"
        if len(os.fsencode(data)) > _ESPEAK_PATH_LIMIT:
            # eSpeak can't take a long path; link to it from our private temp folder instead.
            link = private_dir / "espeak-ng-data"
            if not link.is_symlink():
                link.symlink_to(data.resolve(), target_is_directory=True)
            data = link
        cache = self._root / "cache" / "audiocpp-tmp"
        cache.mkdir(parents=True, exist_ok=True)
        return {
            **os.environ,
            "AUDIOCPP_ESPEAK_LIBRARY": str(espeak / "libespeak-ng.so.1"),
            "AUDIOCPP_ESPEAK_DATA": str(data),
            # The server unpacks model files under TMPDIR on every start and never removes
            # them; a fixed folder means they're reused instead of piling up in /tmp.
            "TMPDIR": str(cache),
            **(_NO_GRAPHICS_CARD if self.backend == "cpu" else {}),
        }


# On the CPU backend, the Vulkan build still starts Vulkan: the loader loads every graphics
# driver and ggml sets up each card it finds (checked on the maintainer's AMD 860M), though it
# never computes there. "Use graphics card" off means the card isn't touched at all, so a CPU
# run hides every device from ggml and gives the Vulkan loader no driver to load. (The CPU
# build has no Vulkan, so this changes nothing for it.)
_NO_GRAPHICS_CARD = {
    "GGML_VK_VISIBLE_DEVICES": "",
    "VK_DRIVER_FILES": "/nonexistent/tomecast-cpu-run.json",
    "VK_ICD_FILENAMES": "/nonexistent/tomecast-cpu-run.json",  # older loaders' name for it
}

# Every engine with a running process, so closing the window can stop them all at once.
_RUNNING: weakref.WeakSet[KokoroServer] = weakref.WeakSet()
_RUNNING_LOCK = threading.Lock()


def stop_all_engines() -> None:
    """Stop every engine this process is running, now (see :meth:`KokoroServer.abort`). The
    window calls this when it closes, so a long start-up or request can't hold it open."""
    with _RUNNING_LOCK:
        engines = list(_RUNNING)
    for engine in engines:
        engine.abort()


# Looked up once, here in the parent: loading a library in the forked child (before the
# engine starts) could deadlock if Tomecast has threads, as the desktop window will.
_PRCTL = ctypes.CDLL(None, use_errno=True).prctl
_PR_SET_PDEATHSIG = 1


def _stop_with_parent() -> None:
    """Runs in the engine's process before it starts: stop it if Tomecast itself dies.

    Linux sends the signal when the *thread* that started the engine exits, so the desktop
    window must start the engine from a thread that lives as long as the conversion.
    """
    _PRCTL(_PR_SET_PDEATHSIG, signal.SIGTERM)


def _has_exited(process: subprocess.Popen[bytes], grace: float = 2.0) -> bool:
    """Whether ``process`` has stopped. A crashed engine drops its connections a moment
    before the system reports it as exited, so this waits briefly instead of just looking."""
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return False
    return True


class _PortTaken(Exception):
    """The engine exited at startup because its port was already in use."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _error_message(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read())
        message = str(payload["error"]["message"])
    except (ValueError, KeyError, TypeError):
        message = error.reason if isinstance(error.reason, str) else "unknown error"
    return f"the voice engine failed (HTTP {error.code}): {message}"


def _exit_status(code: int) -> str:
    """``"status 3"``, or the signal for a crash: ``"signal SIGSEGV"``."""
    if code < 0:
        try:
            return f"signal {signal.Signals(-code).name}"
        except ValueError:
            return f"signal {-code}"
    return f"status {code}"


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


CPU_FALLBACK_NOTE = "The graphics card didn't work, so Tomecast used the CPU."
NO_DEVICE_NOTE = "No usable graphics card was found, so Tomecast used the CPU."


class AutoKokoro(KokoroServer):
    """Kokoro that picks its own hardware: the graphics card if it can, else the CPU.

    This is the one place the choice and the fallback live; the command line and the window
    both use it. With ``use_gpu`` it runs the Vulkan build, asks it which devices the installed
    drivers expose, and starts on the best one. If there is none, or the engine won't start on
    it (or reports another backend), it starts again on the CPU with the same build and sets
    :attr:`warning`. After :meth:`start`, :attr:`device_label` says what is in use ("CPU" or
    the card's name), :attr:`gpu_description` is the card's full description (or ``""``),
    and :attr:`fell_back` is True if the card was wanted but the CPU is used, so the run says
    nothing about how a new build does on the card (it isn't proven by it).
    """

    def __init__(self, root: Path, *, use_gpu: bool = True, **kwargs: object) -> None:
        self.use_gpu = use_gpu
        self.warning: str | None = None
        self.fell_back = False
        self.device_label = "CPU"
        self.gpu_description = ""
        super().__init__(root, backend="vulkan" if use_gpu else "cpu", **kwargs)  # type: ignore[arg-type]

    def __enter__(self) -> AutoKokoro:
        self.start()
        return self

    def start(self) -> None:
        if self._process is not None:
            return
        self.warning, self.fell_back = None, False
        self.device_label, self.gpu_description = "CPU", ""
        if not self.use_gpu:
            super().start()
            return
        devices = list_devices(self.executable, cancelled=self._cancelled)
        if devices is None:  # the card didn't answer: that's not "no card"
            self._use_cpu(CPU_FALLBACK_NOTE)
            return
        device = pick_device(devices)
        if device is None:
            self._use_cpu(NO_DEVICE_NOTE)
            return
        self.backend, self.device = "vulkan", device.index
        try:
            super().start()
        except GpuUnavailable:
            self._use_cpu(CPU_FALLBACK_NOTE)
            return
        self.device_label = short_name(device.description)
        self.gpu_description = device.description

    def _use_cpu(self, note: str) -> None:
        """Run on the CPU backend of the same build, and say why."""
        self.backend, self.device = "cpu", None
        super().start()
        self.warning, self.fell_back = note, True


# --- Installing, and checking a new build before switching to it ------------------------------

SELF_CHECK_TEXT = "Tomecast is checking that its voice works on this computer."


def check_build(
    root: Path,
    engine: Path,
    espeak: Path | None,
    *,
    backend: Literal["cpu", "vulkan"],
    device: int | None = None,
    cancelled: IsCancelled | None = None,
) -> str | None:
    """Try the engine build in the folder ``engine``: ``None`` if it works, else why not.

    It must start with ``backend`` (and ``device``), ``/health`` must report that backend,
    the voice model must be loaded on ``device`` when the server says where it is, and
    :data:`SELF_CHECK_TEXT` must come out as audio that passes the same checks as a book's.
    ``espeak`` is the eSpeak NG build to use (``None``: the one in use). It gets the same
    time as a real run (a new build's first start on a graphics card compiles its shaders).
    If ``cancelled`` says the user stopped, this raises :class:`~tomecast.downloads.Cancelled`
    rather than report a failure.
    """
    with tempfile.TemporaryDirectory(prefix="tomecast-check-") as folder:
        wav = Path(folder) / "check.wav"
        try:
            with KokoroServer(
                root,
                executable=engine / EXECUTABLE,
                espeak=espeak,
                backend=backend,
                device=device,
                cancelled=cancelled,
            ) as server:
                if backend == "vulkan":
                    loaded = server.loaded_device()
                    if loaded is not None and loaded != device:
                        return (
                            f"the voice model was loaded on device {loaded}, not on the "
                            f"graphics card (device {device})"
                        )
                server.synthesize(SELF_CHECK_TEXT, wav)
        except (EngineError, OSError) as error:
            if cancelled is not None and cancelled():
                raise Cancelled from error
            return str(error) or type(error).__name__
        return audio_problem(wav, SELF_CHECK_TEXT) or None


def self_check(root: Path, use_gpu: bool, cancelled: IsCancelled | None = None) -> SelfCheck:
    """Kokoro's check of new engine and eSpeak builds, for a run with or without the card.

    Without the graphics card, the new builds must voice on the CPU. With it, they must voice
    on the card a run would use. If they fail there, the builds in use are tried on the card
    too: if those work, the new builds have failed; if those fail as well, the card (or its
    driver, or another program using it) is the problem, not the update, so the result is
    :class:`~tomecast.downloads.Inconclusive`: nothing is switched or recorded, and the next
    run checks again. A graphics-card build is never switched to on the strength of a CPU
    check while there's a card to use; only when every build answers that it sees no card
    (so a run voices on that build's CPU side anyway) is it checked on the CPU. If a build
    doesn't answer at all, there's no verdict either. Each build's devices are listed once
    per check, retries included.
    """
    engine_slot = engine_asset(use_gpu).slot

    listed: dict[Path, list[GpuDevice] | None] = {}  # once per build, even when retried

    def devices_of(engine: Path) -> list[GpuDevice] | None:
        if engine not in listed:
            listed[engine] = list_devices(engine / EXECUTABLE, cancelled=cancelled)
            if cancelled is not None and cancelled():
                raise Cancelled
        return listed[engine]

    def check(new: Mapping[str, Path], old: Mapping[str, Path]) -> CheckResult:
        new_engine, new_espeak = new[engine_slot], new.get(ESPEAK_NG.slot)
        old_engine, old_espeak = old.get(engine_slot), old.get(ESPEAK_NG.slot)
        if use_gpu:
            new_devices = devices = devices_of(new_engine)
            if not devices and old_engine is not None:
                devices = devices_of(old_engine)  # the new one may not see it
            device = pick_device(devices or [])
            if device is None and (new_devices is None or devices is None):
                # A build that didn't answer (it crashed or hung listing them) says nothing
                # about whether there's a card, so a CPU check couldn't stand in for one.
                return Inconclusive("the graphics card didn't answer when asked what it is")
            if device is not None:
                problem = check_build(
                    root,
                    new_engine,
                    new_espeak,
                    backend="vulkan",
                    device=device.index,
                    cancelled=cancelled,
                )
                if problem is None or old_engine is None:
                    return problem
                old_problem = check_build(
                    root,
                    old_engine,
                    old_espeak,
                    backend="vulkan",
                    device=device.index,
                    cancelled=cancelled,
                )
                if old_problem is None:
                    return problem  # the card worked with the builds in use, not these
                return Inconclusive(f"the graphics card didn't work with any build: {problem}")
        return check_build(root, new_engine, new_espeak, backend="cpu", cancelled=cancelled)

    return check


def kokoro_check(root: Path, use_gpu: bool, cancelled: IsCancelled | None = None) -> VoiceCheck:
    """Kokoro's :class:`~tomecast.downloads.VoiceCheck`: it runs on the engine build for the
    choice and on eSpeak NG. A voice added later (Ming, #11) adds its own, so a new engine
    build is switched to only once every installed voice that uses it has passed."""
    uses = frozenset({engine_asset(use_gpu).slot, ESPEAK_NG.slot})
    return VoiceCheck("Kokoro", uses, self_check(root, use_gpu, cancelled))


def prepare_voice(
    root: Path,
    use_gpu: bool,
    *,
    consent: Consent = lambda missing, updates: True,
    on_download: DownloadReporter | None = None,
    on_check: Callable[[], None] | None = None,
    cancelled: IsCancelled | None = None,
    take_over: bool = False,
) -> Prepared:
    """Install what a run needs (:func:`required_assets`), switching to new engine builds
    only once they pass :func:`kokoro_check`. See :func:`tomecast.downloads.prepare`; give
    its ``builds`` to the engine and the work folder."""
    return prepare(
        required_assets(use_gpu),
        root,
        check=[kokoro_check(root, use_gpu, cancelled)],
        consent=consent,
        on_download=on_download,
        on_check=on_check,
        cancelled=cancelled,
        take_over=take_over,
    )
