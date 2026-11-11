"""A stand-in for audio.cpp's ``audiocpp_server``: the same command line and HTTP API, no
model. Tests run it as the engine, alone or packed into a fake engine build's tarball.

It writes one JSON line per start and per voiced request to ``$FAKE_LOG``, and these
environment variables (or ``env`` given to :func:`write_fake_server`, which a build carries
with it) change how it behaves:

``FAKE_DEVICES``          what ``--list-devices --json`` prints (default: no devices)
``FAKE_LIST_FAIL``        ``--list-devices`` crashes; ``FAKE_LIST_HANG``: it hangs
``FAKE_CRASH``            exit at start-up; ``FAKE_CRASH_VULKAN``: only on the Vulkan backend
``FAKE_REPORT_BACKEND``   the backend ``/health`` reports (default: the configured one)
``FAKE_LOADED_DEVICE``    the device ``/v1/models`` says the model is on (default: the config's)
``FAKE_SILENT``           voice silence instead of a tone
``FAKE_DIE_AFTER``        crash mid-request after this many requests (``FAKE_SLOW_EXIT``: slowly;
                          ``FAKE_DIE_SAYING``: logging this first)
``FAKE_DELAY``            seconds each request takes
``FAKE_START_DELAY``      seconds before it starts serving (a slow first start)
``FAKE_OOM_VULKAN_AFTER`` on the Vulkan backend, requests after this many log ggml's lines and
                          fail with HTTP 500, and the server stays up: what Kokoro did when the
                          graphics card was out of memory (``OOM_KOKORO_*``);
                          ``FAKE_OOM_CPU_TOO``: on the CPU backend too; ``FAKE_OOM_MESSAGE``:
                          another reply; ``FAKE_OOM_QUIET``: the reply alone, nothing logged
``FAKE_OOM_VULKAN_START`` on the Vulkan backend, log what Ming did when it ran out while
                          loading (``OOM_MING_START``) and exit; ``FAKE_OOM_HANG``: log it
                          and keep running without ever serving
``FAKE_OOM_LOG``          log this at start-up (a warning the engine carries on after)
``FAKE_BUSY``             a file it creates as each request begins (one is under way)
``FAKE_RATE``             the sample rate it voices at (default: 24000, Kokoro's)

Each voiced request is logged with the backend that voiced it.

SIGINT and SIGTERM are handled the way audio.cpp 0.9.4 handles them (its handler only sets a
flag, app/server/main.cpp): each is logged as a ``signal`` event; while the model loads nothing
happens until loading ends, and then it exits without serving; an idle server exits at once;
and one in the middle of a request crashes with SIGSEGV, as audio.cpp does when it frees the
model under the request's own thread. It never writes a core dump. SIGKILL is the stop that
runs none of this, so a test that finds no ``signal`` event knows the engine never got one.
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

# Copied from audio.cpp 0.9.4's own output on the maintainer's laptop, with 7.1 of the NVIDIA
# card's 7.6 GiB in use by other programs (#49). Kokoro started, then answered every request
# with HTTP 500 and this reply, logging these lines, and stayed up:
OOM_KOKORO_REPLY = "failed to allocate Kokoro predictor tail tensors"
OOM_KOKORO_LOG = (
    "ggml_vulkan: Device memory allocation of size 186707936 failed.",
    "ggml_vulkan: vk::Device::allocateMemory: ErrorOutOfDeviceMemory",
    "alloc_tensor_range: failed to allocate Vulkan0 buffer of size 186707936",
)
# Ming exited (status 1) 0.9 s after starting, having logged:
OOM_MING_START = (
    "ggml_vulkan: Device memory allocation of size 652936336 failed.",
    "ggml_vulkan: vk::Device::allocateMemory: ErrorOutOfDeviceMemory",
    "alloc_tensor_range: failed to allocate Vulkan0 buffer of size 652936336",
    "audiocpp_server failed: failed to allocate ming3_tts.ar.weights backend weight buffer",
)

FAKE_SERVER = textwrap.dedent(
    """
    import http.server, json, math, os, socket, struct, sys, time, wave, io

    if "--list-devices" in sys.argv:
        if os.environ.get("FAKE_LOG"):
            with open(os.environ["FAKE_LOG"], "a") as listed:
                listed.write(json.dumps({"event": "list", "exe": sys.argv[0]}) + "\\n")
        if os.environ.get("FAKE_LIST_HANG"):
            time.sleep(60)
        if os.environ.get("FAKE_LIST_FAIL"):
            sys.exit(3)  # crashed while asking the driver
        print("ggml_vulkan: Found 2 Vulkan devices", file=sys.stderr)
        print(os.environ.get("FAKE_DEVICES", '{"devices": []}'))
        sys.exit(0)
    config = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
    log = open(os.environ["FAKE_LOG"], "a")
    log.write(json.dumps({"event": "start", "exe": sys.argv[0], "config": config, "env": {
        k: v for k, v in os.environ.items()
        if k.startswith(("AUDIOCPP_", "TMPDIR", "GGML_", "VK_"))
    }}) + "\\n")
    log.flush()

    import ctypes, signal
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0: its crashes write no core dump
    STATE = {"serving": False, "busy": 0, "stop": False}
    SIGNAL_LOG = os.open(os.environ["FAKE_LOG"], os.O_WRONLY | os.O_APPEND)

    def on_signal(signum, frame):  # audio.cpp 0.9.4's request_shutdown, and what follows it
        event = {"event": "signal", "signal": signal.Signals(signum).name}
        os.write(SIGNAL_LOG, (json.dumps(event) + "\\n").encode())
        if STATE["busy"]:  # the model is freed under the running request
            signal.signal(signal.SIGSEGV, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGSEGV)
        STATE["stop"] = True
        if STATE["serving"]:
            os._exit(0)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))  # a slow first start
    if os.environ.get("FAKE_CRASH") or (
        os.environ.get("FAKE_CRASH_VULKAN") and config["backend"] == "vulkan"
    ):
        print("model failed to load", flush=True)
        sys.exit(3)
    if os.environ.get("FAKE_OOM_LOG"):
        print(os.environ["FAKE_OOM_LOG"], file=sys.stderr, flush=True)
    if os.environ.get("FAKE_OOM_VULKAN_START") and config["backend"] == "vulkan":
        print("\\n".join(OOM_MING_START), file=sys.stderr, flush=True)
        if os.environ.get("FAKE_OOM_HANG"):
            time.sleep(60)
        sys.exit(1)
    if STATE["stop"]:  # asked to stop while loading: audio.cpp only looks once it has loaded
        sys.exit(0)

    def wav_for(text):
        rate = int(os.environ.get("FAKE_RATE", "24000"))
        frames = max(1, int(len(text) / 15 * rate))
        loud = 0 if os.environ.get("FAKE_SILENT") else 8000
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes(b"".join(
                struct.pack("<h", int(loud * math.sin(i / 10))) for i in range(frames)))
        return buf.getvalue()

    REQUESTS = [0]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/v1/models":
                device = int(os.environ.get("FAKE_LOADED_DEVICE", config.get("device", 0)))
                # audio.cpp 0.9.4's reply, checked on the AMD 860M (device 1) and the CPU.
                models = {"object": "list", "data": [{
                    "id": "kokoro", "object": "model", "owned_by": "engine",
                    "family": "kokoro_tts", "task": "tts", "mode": "offline",
                    "device": device, "loaded": True, "configured": True,
                    "path": config["models"][0]["path"]}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(models).encode())
                return
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            # The real audio.cpp 0.9.4 reply (checked against the binary).
            backend = os.environ.get("FAKE_REPORT_BACKEND") or config["backend"]
            self.wfile.write(
                json.dumps({"status": "ok", "backend": backend, "version": "0.9.4"}).encode())

        def do_POST(self):
            STATE["busy"] += 1
            try:
                self.speak()
            finally:
                STATE["busy"] -= 1

        def speak(self):
            REQUESTS[0] += 1
            if os.environ.get("FAKE_BUSY"):
                open(os.environ["FAKE_BUSY"], "a").close()
            time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
            die_after = os.environ.get("FAKE_DIE_AFTER")
            if die_after is not None and REQUESTS[0] > int(die_after):
                if os.environ.get("FAKE_DIE_SAYING"):
                    print(os.environ["FAKE_DIE_SAYING"], file=sys.stderr, flush=True)
                if os.environ.get("FAKE_SLOW_EXIT"):
                    # The connection drops first, and the process only ends a bit later.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    time.sleep(float(os.environ["FAKE_SLOW_EXIT"]))
                os._exit(1)  # crash mid-request, like an engine running out of memory
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body["input"]
            if "DIGITS" in text and len(text) > 12:
                return self.fail("Kokoro phoneme string exceeds 510 symbols")
            if "EXPLODE" in text:
                return self.fail("something broke")
            oom_after = os.environ.get("FAKE_OOM_VULKAN_AFTER")
            if (
                oom_after is not None
                and (config["backend"] == "vulkan" or os.environ.get("FAKE_OOM_CPU_TOO"))
                and REQUESTS[0] > int(oom_after)
            ):
                log.write(json.dumps({"event": "out of memory", "body": body}) + "\\n")
                log.flush()
                if not os.environ.get("FAKE_OOM_QUIET"):
                    print("\\n".join(OOM_KOKORO_LOG), file=sys.stderr, flush=True)
                return self.fail(os.environ.get("FAKE_OOM_MESSAGE", OOM_KOKORO_REPLY))
            log.write(json.dumps(
                {"event": "voiced", "body": body, "backend": config["backend"]}) + "\\n")
            log.flush()
            data = wav_for(text)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def fail(self, message):
            data = json.dumps({"error": {"message": message, "type": "server_error"}}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    listener = http.server.HTTPServer((config["host"], config["port"]), Handler)
    STATE["serving"] = True
    listener.serve_forever()
    """
)


def write_fake_server(path: Path, env: Mapping[str, str] | None = None) -> Path:
    """Write the fake server to ``path`` as an executable script. ``env`` is set inside it,
    so a build made from it behaves that way whatever the test's environment says."""
    header = f"import os; os.environ.update({json.dumps(dict(env or {}))})\n" if env else ""
    messages = (
        f"OOM_KOKORO_REPLY = {OOM_KOKORO_REPLY!r}\nOOM_KOKORO_LOG = {OOM_KOKORO_LOG!r}\n"
        f"OOM_MING_START = {OOM_MING_START!r}\n"
    )
    path.write_text(f"#!{sys.executable}\n{header}{messages}{FAKE_SERVER}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
