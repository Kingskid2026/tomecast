"""A stand-in for audio.cpp's ``audiocpp_server``: the same command line and HTTP API, no
model. Tests run it as the engine, alone or packed into a fake engine build's tarball.

It writes one JSON line per start and per voiced request to ``$FAKE_LOG``, and these
environment variables (or ``env`` given to :func:`write_fake_server`, which a build carries
with it) change how it behaves:

``FAKE_DEVICES``          what ``--list-devices --json`` prints
``FAKE_CRASH``            exit at start-up; ``FAKE_CRASH_VULKAN``: only on the Vulkan backend
``FAKE_REPORT_BACKEND``   the backend ``/health`` reports (default: the configured one)
``FAKE_LOADED_DEVICE``    the device ``/v1/models`` says the model is on (default: the config's)
``FAKE_SILENT``           voice silence instead of a tone
``FAKE_DIE_AFTER``        crash mid-request after this many requests (``FAKE_SLOW_EXIT``: slowly)
``FAKE_DELAY``            seconds each request takes
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

FAKE_SERVER = textwrap.dedent(
    """
    import http.server, json, math, os, socket, struct, sys, time, wave, io

    if "--list-devices" in sys.argv:
        print("ggml_vulkan: Found 2 Vulkan devices", file=sys.stderr)
        print(os.environ.get("FAKE_DEVICES", "{}"))
        sys.exit(0)
    config = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
    log = open(os.environ["FAKE_LOG"], "a")
    log.write(json.dumps({"event": "start", "exe": sys.argv[0], "config": config, "env": {
        k: v for k, v in os.environ.items() if k.startswith(("AUDIOCPP_", "TMPDIR"))
    }}) + "\\n")
    log.flush()
    if os.environ.get("FAKE_CRASH") or (
        os.environ.get("FAKE_CRASH_VULKAN") and config["backend"] == "vulkan"
    ):
        print("model failed to load", flush=True)
        sys.exit(3)

    def wav_for(text):
        frames = max(1, int(len(text) / 15 * 24000))
        loud = 0 if os.environ.get("FAKE_SILENT") else 8000
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
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
                models = {"object": "list", "data": [
                    {"id": "kokoro", "loaded": True, "device": device}]}
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
            REQUESTS[0] += 1
            time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
            die_after = os.environ.get("FAKE_DIE_AFTER")
            if die_after is not None and REQUESTS[0] > int(die_after):
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
            log.write(json.dumps({"event": "voiced", "body": body}) + "\\n")
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

    http.server.HTTPServer((config["host"], config["port"]), Handler).serve_forever()
    """
)


def write_fake_server(path: Path, env: Mapping[str, str] | None = None) -> Path:
    """Write the fake server to ``path`` as an executable script. ``env`` is set inside it,
    so a build made from it behaves that way whatever the test's environment says."""
    header = f"import os; os.environ.update({json.dumps(dict(env or {}))})\n" if env else ""
    path.write_text(f"#!{sys.executable}\n{header}{FAKE_SERVER}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
