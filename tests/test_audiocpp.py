import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from tomecast.audio import wav_info
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import EngineError, KokoroServer, check_system

# A stand-in for audiocpp_server: same command line, same HTTP API, no model.
FAKE_SERVER = textwrap.dedent(
    """
    import http.server, json, math, os, struct, sys, wave, io

    config = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
    log = open(os.environ["FAKE_LOG"], "a")
    log.write(json.dumps({"event": "start", "config": config, "env": {
        k: v for k, v in os.environ.items() if k.startswith(("AUDIOCPP_", "TMPDIR"))
    }}) + "\\n")
    log.flush()
    if os.environ.get("FAKE_CRASH"):
        print("model failed to load", flush=True)
        sys.exit(3)

    def wav_for(text):
        frames = max(1, int(len(text) / 15 * 24000))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"".join(
                struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(frames)))
        return buf.getvalue()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
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


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "audiocpp_server"
    script.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    return script


def _events(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "fake.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _server(tmp_path: Path, script: Path, **kwargs: object) -> KokoroServer:
    return KokoroServer(tmp_path / "data", executable=script, startup_timeout=20, **kwargs)  # type: ignore[arg-type]


def test_voices_a_sentence_through_the_server(tmp_path: Path, fake_server: Path) -> None:
    output = tmp_path / "out.wav"
    with _server(tmp_path, fake_server, voice="bf_emma", speed=1.2) as engine:
        engine.synthesize("Hello there, Namárië.", output)

    info = wav_info(output)
    assert (info.sample_rate, info.channels, info.sample_width) == (24000, 1, 2)
    start, request = _events(tmp_path)[:2]  # start, then the voiced request
    config = start["config"]
    assert isinstance(config, dict)
    assert config["backend"] == "cpu"
    assert request["body"] == {
        "model": "kokoro",
        "input": "Hello there, Namárië.",
        "voice": "bf_emma",
        "speed": 1.2,
        "response_format": "wav",
        "options": {"seed": 42},
    }


def test_passes_espeak_and_a_fixed_tmpdir_to_the_server(tmp_path: Path, fake_server: Path) -> None:
    with _server(tmp_path, fake_server):
        pass
    env = _events(tmp_path)[0]["env"]
    assert isinstance(env, dict)
    assert env["AUDIOCPP_ESPEAK_LIBRARY"].endswith("espeakng_loader/libespeak-ng.so.1")
    assert env["AUDIOCPP_ESPEAK_DATA"].endswith("espeak-ng-data")
    assert env["TMPDIR"] == str(tmp_path / "data" / "cache" / "audiocpp-tmp")


def test_text_with_too_many_sounds_is_split_and_joined(tmp_path: Path, fake_server: Path) -> None:
    text = "DIGITS 1234567 7654321 1234567 7654321"
    output = tmp_path / "out.wav"
    with _server(tmp_path, fake_server) as engine:
        engine.synthesize(text, output)

    voiced = [e["body"]["input"] for e in _events(tmp_path) if e["event"] == "voiced"]  # type: ignore[index]
    assert len(voiced) > 1
    assert "".join(voiced).replace(" ", "") == text.replace(" ", "")  # nothing lost
    assert wav_info(output).seconds > 0


def test_other_engine_errors_are_reported(tmp_path: Path, fake_server: Path) -> None:
    with (
        _server(tmp_path, fake_server) as engine,
        pytest.raises(EngineError, match=r"HTTP 500.*something broke"),
    ):
        engine.synthesize("EXPLODE now", tmp_path / "out.wav")


def test_a_crash_at_startup_shows_the_engine_log(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH", "1")
    with pytest.raises(EngineError, match="model failed to load"):
        _server(tmp_path, fake_server).start()


def test_synthesize_needs_a_running_server(tmp_path: Path, fake_server: Path) -> None:
    with pytest.raises(EngineError, match="isn't running"):
        _server(tmp_path, fake_server).synthesize("Hi.", tmp_path / "out.wav")


def test_rejects_unknown_voices_and_bad_speeds(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="unknown voice"):
        KokoroServer(tmp_path, voice="xx_nobody")
    with pytest.raises(EngineError, match="speed"):
        KokoroServer(tmp_path, speed=5.0)


@pytest.mark.parametrize(
    ("version", "ok"),
    [((2, 44), True), ((2, 43), True), ((2, 39), False), (None, False)],
)
def test_check_system_needs_a_new_enough_glibc(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int] | None, ok: bool
) -> None:
    monkeypatch.setattr(audiocpp, "glibc_version", lambda: version)
    if ok:
        check_system()
    else:
        with pytest.raises(EngineError, match=r"glibc 2\.43"):
            check_system()


def test_label_names_the_voice(tmp_path: Path) -> None:
    assert KokoroServer(tmp_path, voice="am_michael").label == "Kokoro (am_michael)"
