"""Run PaddleOCR-VL through llama.cpp's server, as an :class:`~tomecast.readers.scanned.OcrRunner`.

One server is started per model and kept for the whole book, so the model loads once. It
listens on 127.0.0.1 only, on a free port, and is stopped when the ``with`` block ends.
Pages go to its OpenAI-style chat endpoint as PNG data URLs with the prompt ``OCR:`` (the
model's plain-text task) at temperature 0, so the same page always reads the same way.

Prototype for #14: the server binary and the model files are passed in. Downloading and
pinning them (by version and SHA-256) belongs to :mod:`tomecast.downloads` when this ships.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from tomecast.readers.scanned import OcrResult, PageImage

PROMPT = "OCR:"
START_TIMEOUT = 120.0  # seconds to wait for the model to load
PAGE_TIMEOUT = 900.0  # seconds for one page; a slow CPU takes about a minute


class OcrServerError(RuntimeError):
    """The OCR server didn't start, or didn't answer."""


@dataclass(frozen=True, slots=True)
class LlamaOcrConfig:
    """Which files to run, and how. ``gpu_layers=0`` keeps everything on the CPU."""

    server: Path  # llama-server
    model: Path  # the language part (Q4_K_M or Q8_0)
    mmproj: Path  # the vision part (BF16)
    threads: int = 4
    gpu_layers: int = 0
    context: int = 4096  # a page is about 1,300 picture tokens plus up to ~1,000 of text
    max_tokens: int = 2048  # a page that needs more is almost always stuck in a loop
    extra_env: dict[str, str] = field(default_factory=dict)


def server_command(config: LlamaOcrConfig, port: int) -> list[str]:
    """The llama-server command line for ``config``, listening on ``port``."""
    command = [
        str(config.server),
        "--model", str(config.model),
        "--mmproj", str(config.mmproj),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--threads", str(config.threads),
        "--threads-batch", str(config.threads),
        "--ctx-size", str(config.context),
        "--parallel", "1",
        "--temp", "0",
        "--n-gpu-layers", str(config.gpu_layers),
        "--no-webui",
    ]  # fmt: skip
    if config.gpu_layers == 0:
        command += ["--device", "none", "--no-mmproj-offload"]
    return command


def request_body(image: PageImage, max_tokens: int) -> dict[str, Any]:
    """The chat request for one page."""
    data_url = "data:image/png;base64," + base64.b64encode(image.png).decode("ascii")
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }


def parse_reply(reply: dict[str, Any], seconds: float) -> OcrResult:
    """Turn the server's chat reply into an :class:`OcrResult`."""
    try:
        choice = reply["choices"][0]
        text = str(choice["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as error:
        raise OcrServerError(f"the OCR server sent an unexpected reply: {error}") from error
    return OcrResult(text, seconds, truncated=choice.get("finish_reason") == "length")


class LlamaServerRunner:
    """Starts llama-server on enter, reads pages through it, and stops it on exit."""

    def __init__(self, config: LlamaOcrConfig) -> None:
        self.config = config
        self.port = 0
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> Self:
        self.port = _free_port()
        env = {**os.environ, **self.config.extra_env}
        self._process = subprocess.Popen(
            server_command(self.config, self.port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            preexec_fn=_stop_with_parent,  # only calls prctl, safe after fork
        )
        try:
            self._wait_until_ready()
        except BaseException:
            self._stop()
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._stop()

    def read(self, image: PageImage) -> OcrResult:
        body = json.dumps(request_body(image, self.config.max_tokens)).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=PAGE_TIMEOUT) as response:
                reply = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise OcrServerError(f"the OCR server didn't read page {image.page + 1}") from error
        return parse_reply(reply, time.monotonic() - start)

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        url = f"http://127.0.0.1:{self.port}/health"
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise OcrServerError("the OCR server stopped while loading the model")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(0.25)
        raise OcrServerError("the OCR server didn't load the model in time")

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# Looked up once, here in the parent: loading a library in the forked child could deadlock
# if Tomecast has threads (the same approach as the audio.cpp engine).
_PRCTL = ctypes.CDLL(None, use_errno=True).prctl
_PR_SET_PDEATHSIG = 1


def _stop_with_parent() -> None:
    """Runs in the server's process before it starts: kill it if Tomecast itself dies.

    SIGKILL, not SIGTERM: llama-server finishes the page it's reading before it obeys
    SIGTERM, which on a slow CPU can be minutes. It keeps no state worth saving.
    """
    _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
