"""Run a text model with llama.cpp's ``llama-server``, for the judge.

One server per job: it starts when the AI pass needs it, loads the model once, answers one
request at a time and stops afterwards (docs/architecture.md, section 9). Replies are forced
into a JSON schema by llama.cpp's grammar, so the model can't answer in any other shape.

Memory: the context is capped (``context``) and the working memory (KV cache) is 8-bit
(``-ctk q8_0 -ctv q8_0``), as section 10 says. The thread count comes from the caller, which
uses the hardware scan's choice (fewer on battery or with little memory).
"""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from tomecast.ai.judge import Reply

# Only ever talks to its own server on 127.0.0.1, so any proxy settings are ignored.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LlamaError(RuntimeError):
    """llama-server didn't start, stopped, or answered with an error."""


def server_command(
    executable: Path,
    model: Path,
    *,
    port: int,
    threads: int,
    context: int,
    extra: Sequence[str] = (),
) -> list[str]:
    """The command line that starts ``llama-server`` for one model, on the CPU."""
    return [
        str(executable),
        "--model",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--threads",
        str(threads),
        "--threads-batch",
        str(threads),
        "--ctx-size",
        str(context),
        "--parallel",
        "1",
        "--cache-type-k",
        "q8_0",
        "--cache-type-v",
        "q8_0",
        "--n-gpu-layers",
        "0",
        "--no-webui",
        *extra,
    ]


def request_body(
    system: str,
    prompt: str,
    schema: Mapping[str, object],
    max_tokens: int,
    *,
    thinking: bool = False,
) -> dict[str, object]:
    """The ``/v1/chat/completions`` request: greedy, and held to ``schema`` by a grammar."""
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdicts", "strict": True, "schema": dict(schema)},
        },
        # LFM3 thinks first by default; for line numbers that only costs time.
        "chat_template_kwargs": {"enable_thinking": thinking},
        "cache_prompt": True,
    }


def parse_response(payload: object) -> Reply:
    """Read llama-server's chat completion into a :class:`Reply`.

    Raises:
        LlamaError: if it isn't a chat completion.
    """
    unexpected = LlamaError(f"llama-server sent an unexpected reply: {payload!r:.300}")
    if not isinstance(payload, dict):
        raise unexpected
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise unexpected
    message = choices[0].get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise unexpected
    timings = payload.get("timings")
    if not isinstance(timings, dict):
        return Reply(text)

    def number(key: str) -> float:
        value = timings.get(key, 0)
        return float(value) if isinstance(value, int | float) else 0.0

    return Reply(
        text=text,
        prompt_tokens=int(number("prompt_n")),
        generated_tokens=int(number("predicted_n")),
        prompt_ms=number("prompt_ms"),
        generated_ms=number("predicted_ms"),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class LlamaServer:
    """A running ``llama-server`` for one model. Use it as a context manager.

    It implements :class:`tomecast.ai.judge.JsonModel`.
    """

    def __init__(
        self,
        executable: Path,
        model: Path,
        *,
        threads: int,
        context: int = 16_384,
        startup_timeout: float = 120.0,
        request_timeout: float = 600.0,
        thinking: bool = False,
        log: Path | None = None,
    ) -> None:
        self.executable = executable
        self.model = model
        self.threads = threads
        self.context = context
        self.thinking = thinking
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._log = log
        self._process: subprocess.Popen[bytes] | None = None
        self._base_url = ""
        self._log_file: Path | None = None

    @property
    def pid(self) -> int | None:
        """The server's process ID while it runs (for measuring its memory)."""
        return self._process.pid if self._process is not None else None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.stop()

    def start(self) -> None:
        """Start the server and wait until the model is loaded.

        Raises:
            LlamaError: if it exits or doesn't get ready in time.
        """
        port = _free_port()
        self._base_url = f"http://127.0.0.1:{port}"
        if self._log is not None:
            self._log_file = self._log
        else:
            handle = tempfile.NamedTemporaryFile(prefix="tomecast-llama-", suffix=".log")
            handle.close()
            self._log_file = Path(handle.name)
        command = server_command(
            self.executable, self.model, port=port, threads=self.threads, context=self.context
        )
        with self._log_file.open("ab") as log:
            self._process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
            )
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise LlamaError(f"llama-server stopped while starting:\n{self._tail()}")
            if self._ready():
                return
            time.sleep(0.25)
        self.stop()
        raise LlamaError(f"llama-server didn't start within {self._startup_timeout:.0f}s")

    def _ready(self) -> bool:
        try:
            with _LOCAL.open(f"{self._base_url}/health", timeout=2) as response:
                return bool(response.status == 200)
        except (urllib.error.URLError, OSError):
            return False

    def _tail(self) -> str:
        if self._log_file is None or not self._log_file.exists():
            return ""
        return self._log_file.read_text("utf-8", "replace")[-2000:]

    def stop(self) -> None:
        """Stop the server (and wait for it to exit)."""
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def generate_json(
        self, system: str, prompt: str, schema: Mapping[str, object], max_tokens: int
    ) -> Reply:
        """Ask once; the reply is JSON held to ``schema``.

        Raises:
            LlamaError: if the server isn't running or answers with an error.
        """
        if self._process is None:
            raise LlamaError("llama-server isn't running")
        body = json.dumps(
            request_body(system, prompt, schema, max_tokens, thinking=self.thinking)
        ).encode()
        request = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _LOCAL.open(request, timeout=self._request_timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise LlamaError(f"llama-server answered {error.code}: {detail}") from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            raise LlamaError(f"couldn't reach llama-server: {error}\n{self._tail()}") from None
        return parse_response(payload)
