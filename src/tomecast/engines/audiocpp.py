"""Kokoro through audio.cpp: the pinned files it needs, and a client for its local server.

audio.cpp (https://github.com/0xShug0/audio.cpp) runs the Kokoro voice model. Tomecast starts
its HTTP server once per conversion, so the model is loaded a single time, and sends it one
small chunk of text per request. Facts this module relies on were checked against v0.9.4:

* the prebuilt Linux binaries need glibc 2.43 or newer;
* Kokoro needs eSpeak NG at runtime for every sentence (found via environment variables);
* eSpeak's data path must be shorter than 160 bytes;
* more than 510 phonemes in one request is a hard error, not a silent cut;
* the server loads its bundled model files into ``$TMPDIR/audiocpp-gguf/<hash>``.
"""

from __future__ import annotations

import ctypes
import http.client
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import TracebackType

from tomecast.audio import concatenate_wavs
from tomecast.chunking import split_long
from tomecast.downloads import Asset, local_path

AUDIOCPP_VERSION = "0.9.4"
MIN_GLIBC = (2, 43)

AUDIOCPP_CPU = Asset(
    name=f"audio.cpp {AUDIOCPP_VERSION} voice engine (CPU)",
    url=(
        "https://github.com/0xShug0/audio.cpp/releases/download/"
        f"v{AUDIOCPP_VERSION}/audio-v{AUDIOCPP_VERSION}-bin-ubuntu-x64-cpu.tar.gz"
    ),
    sha256="13529ee5366daeb13546190ba25e8118f46bece9c8811e7eb113c41bd121a41a",
    size=64_040_913,
    path=f"engines/audiocpp-{AUDIOCPP_VERSION}-cpu",
    archive="tar.gz",
)

ESPEAK_NG = Asset(
    name="eSpeak NG 1.52 pronunciation rules",
    url=(
        "https://files.pythonhosted.org/packages/de/1e/"
        "25ec5ab07528c0fbb215a61800a38eca05c8a99445515a02d7fa5debcb32/"
        "espeakng_loader-0.2.4-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    ),
    sha256="08721baf27d13d461f6be6eed9a65277e70d68234ff484fd8b9897b222cdcb6d",
    size=10_078_484,
    path="engines/espeak-ng-1.52",
    archive="zip",
)

KOKORO_Q8 = Asset(
    name="Kokoro voice model (82M, 8-bit)",
    url=(
        "https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/"
        "fd2fe4c3195bc410f8b817bf7c7519fd822686fb/Kokoro-82M-GGUF/kokoro-82m-q8_0.gguf"
    ),
    sha256="9a4ca6176460cdc8c7845b804c0bbe85a4791ee3ffff568a3fdadbb708c63f47",
    size=189_550_976,
    path="models/kokoro-82m-q8_0.gguf",
)

REQUIRED_ASSETS = (AUDIOCPP_CPU, ESPEAK_NG, KOKORO_Q8)

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

# audio.cpp splits requests over 240 characters itself; staying under that keeps every
# request a single model pass, and well clear of the 510-phoneme limit for normal prose.
MAX_CHARS = 200
_ESPEAK_PATH_LIMIT = 150  # bytes; eSpeak NG 1.52 has a 160-byte path buffer
SAMPLE_RATE = 24_000  # Kokoro's output
_MAX_RESTARTS = 2  # per conversion, if the engine crashes mid-book
_PHONEME_LIMIT_MESSAGE = "exceeds 510"


class EngineError(RuntimeError):
    """Raised when the voice engine can't start or can't voice a piece of text."""


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


def check_system() -> None:
    """Raise :class:`EngineError` if this system can't run the prebuilt engine."""
    found = glibc_version()
    if found is None or found < MIN_GLIBC:
        have = "a non-glibc system" if found is None else f"glibc {found[0]}.{found[1]}"
        raise EngineError(
            f"The Kokoro voice engine needs glibc {MIN_GLIBC[0]}.{MIN_GLIBC[1]} or newer, and "
            f"this system has {have}. Recent rolling distros (Arch, openSUSE Tumbleweed) work "
            "today; support for older ones is planned: "
            "https://github.com/Kingskid2026/tomecast/issues"
        )


def default_threads() -> int:
    """Physical cores, roughly: audio.cpp was slower with every hyperthread in use."""
    logical = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return max(1, min(8, (logical or 2) // 2))


class KokoroServer:
    """A running audio.cpp server with Kokoro loaded. Use it as a context manager."""

    def __init__(
        self,
        root: Path,
        *,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        threads: int | None = None,
        seed: int = 42,
        startup_timeout: float = 120.0,
        request_timeout: float = 300.0,
        executable: Path | None = None,
    ) -> None:
        if voice not in VOICES:
            raise EngineError(f"unknown voice {voice!r}; choose one of: {', '.join(VOICES)}")
        if not 0.5 <= speed <= 2.0:
            raise EngineError("speed must be between 0.5 and 2.0")
        self.voice = voice
        self.speed = speed
        self.seed = seed
        self._root = root
        self._threads = threads or default_threads()
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._executable = executable or local_path(AUDIOCPP_CPU, root) / "audiocpp_server"
        self._process: subprocess.Popen[bytes] | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._base_url = ""
        self._restarts = 0

    @property
    def label(self) -> str:
        return f"Kokoro ({self.voice})"

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
        # A private (0700) folder for the config, the log and the short eSpeak link.
        self._tempdir = tempfile.TemporaryDirectory(prefix="tomecast-audiocpp-")
        try:
            self._launch()
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
        """Send ``text``; if the engine has crashed, restart it (a couple of times) and retry."""
        try:
            return self._request(text)
        except _ConnectionLost as error:
            if self._process is None or self._process.poll() is None:
                raise  # still running, just not answering
            log = _tail(self._log_path)
            if self._restarts >= _MAX_RESTARTS:
                raise EngineError(f"the voice engine keeps crashing:\n{log}") from error
            self._restarts += 1
            self._stop_process()
            self._launch()
            return self._request(text)

    @property
    def _log_path(self) -> Path:
        assert self._tempdir is not None
        return Path(self._tempdir.name) / "server.log"

    def _launch(self) -> None:
        """Start the server process in the temp folder and wait until it's healthy."""
        assert self._tempdir is not None
        workdir = Path(self._tempdir.name)
        port = _free_port()
        self._base_url = f"http://127.0.0.1:{port}"
        config = workdir / "server.json"
        config.write_text(json.dumps(self._server_config(port)))
        with self._log_path.open("ab") as log:
            self._process = subprocess.Popen(
                [str(self._executable), "--config", str(config), "--no-ui"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self._environment(workdir),
                preexec_fn=_stop_with_parent,  # only calls prctl, safe after fork
            )
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise EngineError(
                    f"the voice engine stopped while starting:\n{_tail(self._log_path)}"
                )
            if self._healthy():
                return
            time.sleep(0.2)
        raise EngineError(
            f"the voice engine didn't start within {self._startup_timeout:.0f}s:\n"
            f"{_tail(self._log_path)}"
        )

    def _stop_process(self) -> None:
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
                "speed": self.speed,
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

    def _healthy(self) -> bool:
        try:
            with _LOCAL.open(f"{self._base_url}/health", timeout=2) as response:
                payload = json.loads(response.read())
        except (OSError, http.client.HTTPException, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("status") == "ok"

    def _server_config(self, port: int) -> dict[str, object]:
        return {
            "host": "127.0.0.1",
            "port": port,
            "backend": "cpu",  # the server defaults to CUDA
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
                    "default_request_options": {"speed": self.speed},
                }
            ],
        }

    def _environment(self, private_dir: Path) -> dict[str, str]:
        espeak = local_path(ESPEAK_NG, self._root) / "espeakng_loader"
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
        }


def _stop_with_parent() -> None:
    """Runs in the engine's process before it starts: stop it if Tomecast itself dies."""
    pr_set_pdeathsig = 1
    ctypes.CDLL(None).prctl(pr_set_pdeathsig, signal.SIGTERM)


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


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"
