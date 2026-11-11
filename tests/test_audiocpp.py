import dataclasses
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tarfile
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fake_engine import write_fake_server

from tomecast import downloads
from tomecast.audio import wav_info
from tomecast.downloads import (
    UPDATE_FAILED_NOTE,
    Asset,
    Cancelled,
    Prepared,
    build_folder,
    build_in_use,
    failure,
    prepare,
    waiting_check,
)
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import (
    AUDIOCPP_CPU,
    AUDIOCPP_VULKAN,
    ESPEAK_NG,
    AutoKokoro,
    EngineError,
    GpuUnavailable,
    KokoroServer,
    check_system,
    required_assets,
)
from tomecast.hardware import GpuDevice, list_devices


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    return write_fake_server(tmp_path / "audiocpp_server")


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


# A damaged build that crashes at once with SIGSEGV, saying nothing. It makes itself
# non-dumpable first, so the kernel never hands its crash to the system's core-dump handler
# (systemd-coredump, and the desktop's crash notification): the test needs the real signal,
# not a core file. -I keeps PYTHON* settings (faulthandler) out, and SIG_DFL makes sure
# nothing catches the signal and writes to the log.
_SEGFAULTS = textwrap.dedent(
    """
    import ctypes, os, signal
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0: no core dump
    signal.signal(signal.SIGSEGV, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGSEGV)
    """
)


def test_an_engine_that_dies_without_a_word_says_how_it_ended(tmp_path: Path) -> None:
    program = tmp_path / "audiocpp_server"
    program.write_text(f"#!{sys.executable} -I\n{_SEGFAULTS}")
    program.chmod(0o755)

    with pytest.raises(EngineError, match="no log; it exited with signal SIGSEGV"):
        _server(tmp_path, program).start()


def test_synthesize_needs_a_running_server(tmp_path: Path, fake_server: Path) -> None:
    with pytest.raises(EngineError, match="isn't running"):
        _server(tmp_path, fake_server).synthesize("Hi.", tmp_path / "out.wav")


def test_rejects_unknown_voices_and_bad_speeds(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="unknown voice"):
        KokoroServer(tmp_path, voice="xx_nobody")
    for bad in (0.4, 3.1, 5.0):
        with pytest.raises(EngineError, match="speed"):
            KokoroServer(tmp_path, speed=bad)
    for good in (0.5, 3.0):
        assert KokoroServer(tmp_path, speed=good).speed == good


@pytest.mark.parametrize(
    ("speed", "voice_speed", "tempo"),
    [(0.5, 0.5, 1.0), (1.0, 1.0, 1.0), (1.5, 1.5, 1.0), (2.0, 1.5, 4 / 3), (3.0, 1.5, 2.0)],
)
def test_speed_is_split_into_a_voice_speed_and_a_tempo(
    tmp_path: Path, speed: float, voice_speed: float, tempo: float
) -> None:
    engine = KokoroServer(tmp_path, speed=speed)
    assert engine.voice_speed == pytest.approx(voice_speed)
    assert engine.tempo == pytest.approx(tempo)
    assert engine.voice_speed * engine.tempo == pytest.approx(speed)
    assert audiocpp.split_speed(speed) == (engine.voice_speed, engine.tempo)
    engine.speed = 3.0  # the window changes the speed of a running preview engine
    assert (engine.voice_speed, engine.tempo) == (1.5, 2.0)
    with pytest.raises(EngineError, match="speed"):
        engine.speed = 3.5
    with pytest.raises(AttributeError):
        engine.tempo = 1.0  # type: ignore[misc]


def test_the_server_is_only_asked_for_the_voice_speed(tmp_path: Path, fake_server: Path) -> None:
    with _server(tmp_path, fake_server, speed=3.0) as engine:
        engine.synthesize("Hello there.", tmp_path / "out.wav")
    start, request = _events(tmp_path)[:2]
    config = start["config"]
    assert isinstance(config, dict)
    assert config["models"][0]["default_request_options"] == {"speed": 1.5}
    assert request["body"]["speed"] == 1.5  # type: ignore[index]


# An x86-64 machine with every CPU feature the engine needs, on Ubuntu 22.04's glibc.
GOOD_FLAGS = frozenset(["fpu", "sse4_2", "avx", "avx2", "fma", "f16c", "bmi1", "bmi2"])


@pytest.fixture
def capable_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(audiocpp, "glibc_version", lambda: (2, 35))
    monkeypatch.setattr(audiocpp, "cpu_flags", lambda: GOOD_FLAGS)


@pytest.mark.parametrize(
    ("version", "ok"),
    [((2, 44), True), ((2, 35), True), ((2, 31), True), ((2, 29), True), ((2, 28), False),
     ((2, 17), False), (None, False)],
)  # fmt: skip
@pytest.mark.usefixtures("capable_machine")
def test_check_system_needs_glibc_2_29(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int] | None, ok: bool
) -> None:
    monkeypatch.setattr(audiocpp, "glibc_version", lambda: version)
    if ok:
        check_system()
    else:
        with pytest.raises(EngineError, match=r"needs glibc 2\.29 or newer"):
            check_system()


@pytest.mark.usefixtures("capable_machine")
def test_check_system_needs_avx2(monkeypatch: pytest.MonkeyPatch) -> None:
    old_cpu = frozenset(["fpu", "sse4_2", "avx"])  # e.g. a 2012 Ivy Bridge laptop
    monkeypatch.setattr(audiocpp, "cpu_flags", lambda: old_cpu)
    with pytest.raises(EngineError, match=r"AVX2.*missing: avx2, fma, f16c, bmi2"):
        check_system()


@pytest.mark.usefixtures("capable_machine")
def test_check_system_lets_the_engine_try_when_cpu_flags_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audiocpp, "cpu_flags", lambda: None)
    check_system()


@pytest.mark.usefixtures("capable_machine")
def test_check_system_needs_an_x86_64_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    with pytest.raises(EngineError, match=r"x86-64.*aarch64"):
        check_system()


def test_cpu_flags_come_from_cpuinfo(tmp_path: Path) -> None:
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "processor\t: 0\nmodel name\t: Some CPU\nflags\t\t: fpu avx avx2 fma\n\n"
        "processor\t: 1\nflags\t\t: fpu avx avx2 fma\n"
    )
    assert audiocpp.cpu_flags(cpuinfo) == frozenset({"fpu", "avx", "avx2", "fma"})


def test_cpu_flags_are_unknown_without_a_flags_line(tmp_path: Path) -> None:
    arm = tmp_path / "cpuinfo"
    arm.write_text("processor\t: 0\nFeatures\t: fp asimd\n")  # ARM says "Features"
    assert audiocpp.cpu_flags(arm) is None
    assert audiocpp.cpu_flags(tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("asset", "variant"), [(audiocpp.AUDIOCPP_CPU, "cpu"), (audiocpp.AUDIOCPP_VULKAN, "vulkan")]
)
def test_the_engine_comes_from_tomecasts_own_build(asset: Asset, variant: str) -> None:
    assert asset.url == (
        "https://github.com/Kingskid2026/tomecast/releases/download/"
        f"{audiocpp.AUDIOCPP_BUILD}/{audiocpp.AUDIOCPP_BUILD}-linux-x86_64-{variant}.tar.gz"
    )
    assert audiocpp.AUDIOCPP_BUILD.startswith(f"audiocpp-v{audiocpp.AUDIOCPP_VERSION}-tc")
    assert len(asset.sha256) == 64
    assert int(asset.sha256, 16) >= 0  # a real hex digest, not a placeholder
    assert asset.size > 0
    assert asset.archive == "tar.gz"
    # Each build gets its own folder in here; 0.1.x's folder is taken over, not downloaded again.
    assert asset.path == f"engines/audiocpp-{variant}"
    assert asset.replaces == (f"engines/audiocpp-0.9.4-{variant}",)


def test_label_names_the_voice(tmp_path: Path) -> None:
    assert KokoroServer(tmp_path, voice="am_michael").label == "Kokoro (am_michael)"


def test_requests_never_go_through_a_proxy(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A proxy that doesn't exist: if the book's text were sent to it, this would fail.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    with _server(tmp_path, fake_server) as engine:
        engine.synthesize("Private text.", tmp_path / "out.wav")
    assert wav_info(tmp_path / "out.wav").frames > 0


def test_a_crash_mid_book_restarts_the_engine(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")  # each engine process answers once, then crashes
    with _server(tmp_path, fake_server) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        engine.synthesize("Second.", tmp_path / "2.wav")  # crashes, restarts, succeeds
    starts = [e for e in _events(tmp_path) if e["event"] == "start"]
    assert len(starts) == 2
    assert wav_info(tmp_path / "2.wav").frames > 0


def test_a_crash_is_handled_even_if_the_engine_takes_a_moment_to_exit(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")
    monkeypatch.setenv("FAKE_SLOW_EXIT", "0.5")
    with _server(tmp_path, fake_server) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        engine.synthesize("Second.", tmp_path / "2.wav")  # crashes slowly, restarts, succeeds
    voiced = [e["body"]["input"] for e in _events(tmp_path) if e["event"] == "voiced"]  # type: ignore[index]
    assert voiced == ["First.", "Second."]
    assert wav_info(tmp_path / "2.wav").frames > 0


def test_an_engine_that_keeps_crashing_gives_a_clear_error(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "0")  # every request crashes the engine
    with (
        _server(tmp_path, fake_server) as engine,
        pytest.raises(EngineError, match=r"crashed and didn't recover\. Its log:"),
    ):
        engine.synthesize("Hello.", tmp_path / "out.wav")


def test_a_long_install_path_gets_a_short_private_espeak_link(
    tmp_path: Path, fake_server: Path
) -> None:
    root = tmp_path / ("very-long-folder-name-" * 8)
    with KokoroServer(root, executable=fake_server, startup_timeout=20) as engine:
        engine.synthesize("Hello.", tmp_path / "out.wav")
        env = _events(tmp_path)[0]["env"]
        assert isinstance(env, dict)
        link = Path(env["AUDIOCPP_ESPEAK_DATA"])
        assert link.is_symlink()
        assert len(str(link).encode()) <= 150
        assert link.parent.stat().st_mode & 0o077 == 0  # only this user can see it
    assert not link.exists()  # cleaned up with the engine


def test_ctrl_c_while_starting_stops_the_engine(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _server(tmp_path, fake_server)

    def interrupt(self: KokoroServer) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(KokoroServer, "_healthy", interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine.start()
    assert engine._process is None
    assert engine._tempdir is None


# --- The graphics card ------------------------------------------------------------------------

_DEVICES = json.dumps(
    {
        "schema_version": 1,
        "devices": [
            {"backend": "vulkan", "index": 0, "name": "Vulkan0", "type": "gpu",
             "description": "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",
             "memory": {"free_bytes": 7_000_000_000, "total_bytes": 8_000_000_000}},
            {"backend": "vulkan", "index": 1, "name": "Vulkan1", "type": "igpu",
             "description": "AMD Radeon 860M Graphics (RADV KRACKAN1)",
             "memory": {"free_bytes": 9_000_000_000, "total_bytes": 20_000_000_000}},
            {"backend": "cpu", "index": 0, "name": "CPU", "type": "cpu", "description": "CPU",
             "memory": {"free_bytes": 1, "total_bytes": 2}},
        ],
    }
)  # fmt: skip


@pytest.fixture
def with_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_DEVICES", _DEVICES)


def _starts(tmp_path: Path) -> list[dict[str, object]]:
    return [event for event in _events(tmp_path) if event["event"] == "start"]


def test_the_server_config_says_which_backend_and_device(tmp_path: Path, fake_server: Path) -> None:
    with _server(tmp_path, fake_server, backend="vulkan", device=1):
        pass
    config = _starts(tmp_path)[0]["config"]
    assert isinstance(config, dict)
    assert (config["backend"], config["device"]) == ("vulkan", 1)


def test_the_cpu_config_has_no_device(tmp_path: Path, fake_server: Path) -> None:
    with _server(tmp_path, fake_server):
        pass
    config = _starts(tmp_path)[0]["config"]
    assert isinstance(config, dict)
    assert config["backend"] == "cpu"
    assert "device" not in config


def test_a_vulkan_server_that_reports_the_cpu_is_gpu_unavailable(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REPORT_BACKEND", "cpu")
    with pytest.raises(GpuUnavailable, match="reported the cpu backend"):
        _server(tmp_path, fake_server, backend="vulkan", device=0).start()


def test_a_vulkan_server_that_fails_to_start_is_gpu_unavailable(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with pytest.raises(GpuUnavailable, match="model failed to load"):
        _server(tmp_path, fake_server, backend="vulkan", device=0).start()


def test_a_cpu_server_that_fails_is_a_plain_engine_error(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH", "1")
    with pytest.raises(EngineError) as error:
        _server(tmp_path, fake_server).start()
    assert not isinstance(error.value, GpuUnavailable)


def test_auto_uses_the_discrete_card(tmp_path: Path, fake_server: Path, with_devices: None) -> None:
    engine = AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20)
    with engine:
        assert (engine.backend, engine.device) == ("vulkan", 0)
        assert engine.device_label == "NVIDIA RTX PRO 1000"
        assert engine.gpu_description.startswith("NVIDIA RTX PRO 1000 Blackwell")
        assert engine.warning is None
    config = _starts(tmp_path)[0]["config"]
    assert isinstance(config, dict)
    assert (config["backend"], config["device"]) == ("vulkan", 0)


def test_auto_falls_back_to_the_cpu_on_the_same_build(
    tmp_path: Path, fake_server: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    output = tmp_path / "out.wav"
    engine = AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20)
    with engine:
        assert engine.backend == "cpu"
        assert engine.device_label == "CPU"
        assert engine.gpu_description == ""
        assert engine.warning == "The graphics card didn't work, so Tomecast used the CPU."
        engine.synthesize("It still speaks.", output)
    assert wav_info(output).seconds > 0
    configs = [start["config"] for start in _starts(tmp_path)]
    assert [config["backend"] for config in configs] == ["vulkan", "cpu"]  # type: ignore[index]


def test_auto_falls_back_when_the_server_reports_the_cpu(
    tmp_path: Path, fake_server: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REPORT_BACKEND", "cpu")
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.backend == "cpu"
        assert engine.warning == "The graphics card didn't work, so Tomecast used the CPU."


def test_a_crash_restart_that_comes_back_off_the_card_carries_on_on_the_cpu_and_says_so(
    tmp_path: Path, fake_server: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")  # each engine process answers once, then crashes
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        assert engine.device_label == "NVIDIA RTX PRO 1000"
        monkeypatch.setenv("FAKE_REPORT_BACKEND", "cpu")  # the card is gone when it restarts
        engine.synthesize("Second.", tmp_path / "2.wav")  # crashes, comes back, not on the card

        assert (engine.backend, engine.device_label, engine.gpu_description) == ("cpu", "CPU", "")
        assert engine.warning == audiocpp.CPU_FALLBACK_NOTE
        assert engine.fell_back is True  # so the run proves nothing for the card's build
        assert engine.out_of_memory is False
    backends = [start["config"]["backend"] for start in _starts(tmp_path)]  # type: ignore[index]
    assert backends == ["vulkan", "vulkan", "cpu"]  # the one on the wrong backend was stopped
    voiced = [(e["body"], e["backend"]) for e in _events(tmp_path) if e["event"] == "voiced"]
    texts = [(body["input"], backend) for body, backend in voiced]  # type: ignore[index]
    assert texts == [("First.", "vulkan"), ("Second.", "cpu")]  # nothing skipped
    assert wav_info(tmp_path / "2.wav").frames > 0


def test_a_crash_restart_on_another_backend_is_never_left_running(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")
    with _server(tmp_path, fake_server, backend="vulkan", device=0) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        monkeypatch.setenv("FAKE_REPORT_BACKEND", "cpu")
        with pytest.raises(GpuUnavailable, match="reported the cpu backend"):
            engine.synthesize("Second.", tmp_path / "2.wav")
        assert engine._process is None  # stopped, as start() would have
        with pytest.raises(EngineError, match="isn't running"):  # a retry doesn't voice there
            engine.synthesize("Second.", tmp_path / "2.wav")
    assert [e["body"]["input"] for e in _events(tmp_path) if e["event"] == "voiced"] == ["First."]  # type: ignore[index]


def test_auto_uses_the_cpu_when_no_card_is_found(tmp_path: Path, fake_server: Path) -> None:
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.backend == "cpu"
        assert engine.warning == "No usable graphics card was found, so Tomecast used the CPU."
    assert len(_starts(tmp_path)) == 1  # no failed GPU attempt first


def test_auto_without_the_gpu_never_asks_about_devices(
    tmp_path: Path, fake_server: Path, with_devices: None
) -> None:
    engine = AutoKokoro(tmp_path / "data", use_gpu=False, executable=fake_server)
    with engine:
        assert (engine.backend, engine.device, engine.warning) == ("cpu", None, None)


def test_the_engine_build_follows_the_choice(tmp_path: Path) -> None:
    assert AutoKokoro(tmp_path, use_gpu=True).executable.parent.parent.name == "audiocpp-vulkan"
    assert AutoKokoro(tmp_path, use_gpu=False).executable.parent.parent.name == "audiocpp-cpu"
    assert AUDIOCPP_VULKAN in required_assets(True)
    assert AUDIOCPP_CPU not in required_assets(True)
    assert AUDIOCPP_CPU in required_assets(False)
    assert AUDIOCPP_VULKAN not in required_assets(False)
    assert AUDIOCPP_VULKAN.path == "engines/audiocpp-vulkan"


def test_a_port_taken_at_startup_is_retried_on_a_fresh_one(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    # Another program holds the first port Tomecast picks, as happens on a busy machine.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    real_free_port = audiocpp._free_port
    ports = iter([taken])
    monkeypatch.setattr(audiocpp, "_free_port", lambda: next(ports, None) or real_free_port())
    try:
        with _server(tmp_path, fake_server) as engine:
            engine.synthesize("Hello there.", tmp_path / "out.wav")
    finally:
        blocker.close()
    assert wav_info(tmp_path / "out.wav").frames > 0
    starts = [event for event in _events(tmp_path) if "config" in event]
    first = starts[0]["config"]
    assert isinstance(first, dict)
    assert first["port"] == taken
    assert len(starts) == 2  # the first start failed on the taken port; the second worked


def test_a_port_that_stays_taken_gives_a_clear_error(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    monkeypatch.setattr(audiocpp, "_free_port", lambda: taken)
    try:
        with pytest.raises(EngineError, match="couldn't get a free network port"):
            _server(tmp_path, fake_server).start()
    finally:
        blocker.close()


# --- The self-check a new engine build must pass ----------------------------------------------


def _engine_build(tmp_path: Path, name: str, gpu: bool, env: dict[str, str] | None = None) -> Asset:
    """A fake engine build (the fake server, behaving as ``env`` says), pinned like ours."""
    folder = tmp_path / "builds" / name
    folder.mkdir(parents=True)
    server = write_fake_server(folder / "audiocpp_server", env)
    archive = folder.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(server, arcname="audiocpp_server")
    data = archive.read_bytes()
    return dataclasses.replace(
        AUDIOCPP_VULKAN if gpu else AUDIOCPP_CPU,
        url=archive.as_uri(),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


@pytest.fixture
def engine_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data folder with eSpeak installed (a stand-in: the fake engine never reads it)."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    root = tmp_path / "data"
    espeak = build_folder(ESPEAK_NG, root)
    espeak.mkdir(parents=True)
    (espeak / ".tomecast-sha256").write_text(ESPEAK_NG.sha256 + "\n")
    return root


def _update(root: Path, old: Asset, new: Asset, *, gpu: bool) -> Prepared:
    """Install ``old``, then update to ``new`` with the real self-check."""
    prepare([old, ESPEAK_NG], root, check=audiocpp.self_check(root, gpu))
    assert build_in_use(old, root) == build_folder(old, root)
    return prepare([new, ESPEAK_NG], root, check=audiocpp.self_check(root, gpu))


def _checks(tmp_path: Path) -> list[tuple[str, str]]:
    """(build folder name, backend) for each engine start, in order."""
    return [
        (Path(str(event["exe"])).parent.name, event["config"]["backend"])  # type: ignore[index]
        for event in _starts(tmp_path)
    ]


def test_a_new_build_that_voices_on_the_cpu_is_switched_to(
    tmp_path: Path, engine_root: Path
) -> None:
    old = _engine_build(tmp_path, "old", gpu=False)
    new = _engine_build(tmp_path, "new", gpu=False)

    prepared = _update(engine_root, old, new, gpu=False)

    assert prepared.switched == (new,)
    assert build_in_use(new, engine_root) == build_folder(new, engine_root)
    assert _checks(tmp_path) == [(new.sha256[:12], "cpu")]
    voiced = [e["body"] for e in _events(tmp_path) if e["event"] == "voiced"]
    assert voiced[0]["input"] == audiocpp.SELF_CHECK_TEXT  # type: ignore[index]


@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"FAKE_CRASH": "1"}, "model failed to load"),  # e.g. a missing library
        ({"FAKE_SILENT": "1"}, "silent"),  # it starts, but the audio is no good
    ],
)
def test_a_new_build_that_doesnt_voice_is_not_switched_to(
    tmp_path: Path, engine_root: Path, env: dict[str, str], why: str
) -> None:
    old = _engine_build(tmp_path, "old", gpu=False)
    new = _engine_build(tmp_path, "new", gpu=False, env=env)

    prepared = _update(engine_root, old, new, gpu=False)

    assert build_in_use(new, engine_root) == build_folder(old, engine_root)
    assert prepared.notes == (UPDATE_FAILED_NOTE,)
    reason = failure(new, engine_root)
    assert reason is not None and why in reason


def test_a_new_vulkan_build_must_voice_on_the_card_the_old_one_could(
    tmp_path: Path, engine_root: Path, with_devices: None
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_REPORT_BACKEND": "cpu"})

    prepared = _update(engine_root, old, new, gpu=True)

    assert build_in_use(new, engine_root) == build_folder(old, engine_root)
    assert prepared.notes == (UPDATE_FAILED_NOTE,)
    assert "reported the cpu backend" in (failure(new, engine_root) or "")
    # The new one on the card, then the old one on the card (it worked, so the new one
    # failed), and the same once more before the failure is recorded.
    assert _checks(tmp_path) == [(new.sha256[:12], "vulkan"), (old.sha256[:12], "vulkan")] * 2
    config = _starts(tmp_path)[0]["config"]
    assert config["device"] == 0  # type: ignore[index]  # the discrete card, as a run would


def test_a_new_vulkan_build_must_load_the_voice_on_the_card(
    tmp_path: Path, engine_root: Path, with_devices: None
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_LOADED_DEVICE": "1"})

    _update(engine_root, old, new, gpu=True)

    assert build_in_use(new, engine_root) == build_folder(old, engine_root)
    assert "not on the graphics card" in (failure(new, engine_root) or "")


def test_when_the_card_fails_with_every_build_nothing_is_switched_or_blamed(
    tmp_path: Path, engine_root: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True)
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")  # a broken driver: no build can use the card

    prepared = _update(engine_root, old, new, gpu=True)

    # The card is the problem, not the update: no switch (never on a CPU check while there's
    # a card), nothing recorded, no note, and the next run checks again.
    assert prepared.switched == ()
    assert prepared.notes == ()
    assert build_in_use(new, engine_root) == build_folder(old, engine_root)
    assert failure(new, engine_root) is None
    assert waiting_check(new, engine_root)
    assert _checks(tmp_path) == [(new.sha256[:12], "vulkan"), (old.sha256[:12], "vulkan")]

    monkeypatch.delenv("FAKE_CRASH_VULKAN")  # the driver works again
    again = prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))
    assert again.switched == (new,)


def test_running_out_of_graphics_memory_in_the_check_is_no_verdict(
    tmp_path: Path, engine_root: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True)
    install = prepare([old, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))
    assert build_in_use(old, engine_root) == build_folder(old, engine_root), install
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "0")  # another program has the card's memory

    prepared = prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))

    # Other programs did that, not the update: nothing switched, blamed or noted, the old
    # build isn't started to compare, and the next run checks again.
    assert prepared.switched == ()
    assert prepared.notes == ()
    assert failure(new, engine_root) is None
    assert waiting_check(new, engine_root)
    assert _checks(tmp_path)[-1:] == [(new.sha256[:12], "vulkan")]
    assert sum(1 for build, _ in _checks(tmp_path) if build == new.sha256[:12]) == 1  # no retry

    monkeypatch.delenv("FAKE_OOM_VULKAN_AFTER")  # the memory is free again
    again = prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))
    assert again.switched == (new,)


def test_check_build_says_no_verdict_when_the_card_runs_out_of_memory(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_START", "1")
    result = audiocpp.check_build(tmp_path, tmp_path, None, backend="vulkan", device=0)
    assert isinstance(result, downloads.Inconclusive)
    assert "ran out of memory" in result.reason


def test_without_a_card_a_vulkan_build_is_checked_on_the_cpu(
    tmp_path: Path, engine_root: Path
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True)

    assert _update(engine_root, old, new, gpu=True).switched == (new,)
    assert _checks(tmp_path) == [(new.sha256[:12], "cpu")]


def test_an_engine_made_before_the_switch_starts_the_new_build(
    tmp_path: Path, engine_root: Path
) -> None:
    old = _engine_build(tmp_path, "old", gpu=False)
    new = _engine_build(tmp_path, "new", gpu=False)
    prepare([old, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, False))
    # Made before the switch, as the window does (it makes the engine, then downloads).
    engine = AutoKokoro(engine_root, use_gpu=False, startup_timeout=20)

    prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, False))
    with engine:
        pass

    assert _checks(tmp_path)[-1] == (new.sha256[:12], "cpu")


# --- After the review: one build per run, real time limits, stopping -------------------------


def _two_cpu_builds(root: Path) -> tuple[Path, Path]:
    """An older CPU build in use (A) and the pinned one (B) on disk beside it."""
    old = root / AUDIOCPP_CPU.path / ("a" * 12)
    new = build_folder(AUDIOCPP_CPU, root)
    for folder, sha in ((old, "a" * 64), (new, AUDIOCPP_CPU.sha256)):
        folder.mkdir(parents=True)
        (folder / ".tomecast-sha256").write_text(sha + "\n")
        write_fake_server(folder / "audiocpp_server")
    prepare([AUDIOCPP_CPU], root, check=lambda n, o: "not yet")  # A stays, B waits
    assert build_in_use(AUDIOCPP_CPU, root) == old
    return old, new


def test_a_crash_restart_mid_book_uses_the_same_build(tmp_path: Path, engine_root: Path) -> None:
    old, new = _two_cpu_builds(engine_root)
    with KokoroServer(engine_root, startup_timeout=20) as engine:
        # Another Tomecast switches to B while this one is voicing a book with A...
        downloads._promote(AUDIOCPP_CPU, engine_root, proven=False)
        process = engine._process
        assert process is not None
        process.kill()  # ...and then A crashes
        process.wait()
        engine.synthesize("Hello there, this is a sentence.", tmp_path / "x.wav")
    assert build_in_use(AUDIOCPP_CPU, engine_root) == new
    builds = [Path(str(start["exe"])).parent for start in _starts(tmp_path)]
    assert builds == [old, old]  # restarted with A: one book, one build


def test_an_engine_runs_the_builds_prepare_chose(tmp_path: Path, engine_root: Path) -> None:
    old, _ = _two_cpu_builds(engine_root)
    downloads._promote(AUDIOCPP_CPU, engine_root, proven=False)  # B is in use now...
    espeak = build_folder(ESPEAK_NG, engine_root)
    with KokoroServer(engine_root, builds={"audiocpp-cpu": old, "espeak-ng": espeak}) as engine:
        assert engine.builds == {"audiocpp-cpu": old, "espeak-ng": espeak}
    assert Path(str(_starts(tmp_path)[0]["exe"])).parent == old  # ...but the run uses A


def test_the_self_check_gives_a_new_build_a_real_runs_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, object]] = []

    class Recording(KokoroServer):
        def __init__(self, root: Path, **kwargs: object) -> None:
            made.append(kwargs)
            raise EngineError("stop here")

    monkeypatch.setattr(audiocpp, "KokoroServer", Recording)
    audiocpp.check_build(tmp_path, tmp_path, None, backend="vulkan", device=0)

    assert "startup_timeout" not in made[0] and "request_timeout" not in made[0]
    defaults = KokoroServer(tmp_path)
    assert (defaults._startup_timeout, defaults._request_timeout) == (120.0, 300.0)


def test_a_start_up_can_be_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    slow = write_fake_server(tmp_path / "audiocpp_server", {"FAKE_START_DELAY": "30"})
    started = time.monotonic()
    stop_at = started + 0.5

    with pytest.raises(Cancelled):
        KokoroServer(
            tmp_path / "data", executable=slow, cancelled=lambda: time.monotonic() > stop_at
        ).start()
    assert time.monotonic() - started < 5


def test_a_cancelled_self_check_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    folder = tmp_path / "build"
    folder.mkdir()
    write_fake_server(folder / "audiocpp_server", {"FAKE_START_DELAY": "30"})
    stop_at = time.monotonic() + 0.5

    with pytest.raises(Cancelled):
        audiocpp.check_build(
            tmp_path / "data", folder, None, backend="cpu",
            cancelled=lambda: time.monotonic() > stop_at,
        )  # fmt: skip


def test_closing_the_window_stops_every_engine_at_once(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    monkeypatch.setenv("FAKE_DELAY", "20")  # a request that would take 20 s
    engine = KokoroServer(tmp_path / "data", executable=fake_server, startup_timeout=20)
    engine.start()
    errors: list[BaseException] = []

    def voice() -> None:
        try:
            engine.synthesize("This takes a while to say.", tmp_path / "x.wav")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=voice)
    thread.start()
    time.sleep(0.5)  # the request is under way
    started = time.monotonic()
    audiocpp.stop_all_engines()
    thread.join(timeout=10)
    engine.close()

    assert not thread.is_alive()
    assert time.monotonic() - started < 5  # cut off, not waited for
    assert len(errors) == 1 and isinstance(errors[0], Cancelled)  # and not restarted
    assert len(_starts(tmp_path)) == 1


def test_an_aborted_engine_is_not_restarted(tmp_path: Path, fake_server: Path) -> None:
    with KokoroServer(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        engine.abort()
        assert engine._process is not None
        engine._process.wait(timeout=10)
        with pytest.raises(Cancelled):
            engine.synthesize("Anyone there?", tmp_path / "x.wav")
    assert len(_starts(tmp_path)) == 1


# --- Stopping without a crash -----------------------------------------------------------------
# audio.cpp 0.9.4 frees the model when it gets SIGINT or SIGTERM, while a request may still run
# on its own thread: it segfaults and leaves a core dump of hundreds of MB. The fake engine does
# the same (see fake_engine.py), so these tests notice any stop that isn't SIGKILL.


def _signals(tmp_path: Path) -> list[object]:
    """The SIGINT and SIGTERM the engine got: each one runs audio.cpp's own (crashing) stop."""
    return [event["signal"] for event in _events(tmp_path) if event["event"] == "signal"]


def _wait_for(path: Path, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.02)


@pytest.mark.parametrize("how", ["close", "abort"])
def test_stopping_the_engine_mid_sentence_kills_it_without_a_crash(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    monkeypatch.setenv("FAKE_DELAY", "20")  # a sentence that takes 20 s to voice
    monkeypatch.setenv("FAKE_BUSY", str(tmp_path / "busy"))
    engine = _server(tmp_path, fake_server)
    engine.start()
    process = engine._process
    assert process is not None
    errors: list[BaseException] = []

    def voice() -> None:
        try:
            engine._request("This takes a while to say.")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=voice)
    thread.start()
    _wait_for(tmp_path / "busy")  # the engine is voicing it
    started = time.monotonic()
    if how == "close":
        engine.close()  # the command line, after Ctrl-C or `kill`
    else:
        audiocpp.stop_all_engines()  # closing the window
        process.wait(timeout=10)
        engine.close()
    thread.join(timeout=10)

    assert time.monotonic() - started < 5  # at once, not after a grace period
    assert process.returncode == -signal.SIGKILL  # not -SIGSEGV
    assert _signals(tmp_path) == []
    assert not thread.is_alive() and len(errors) == 1


def test_a_start_up_is_stopped_at_once_even_while_the_model_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    loading = write_fake_server(tmp_path / "audiocpp_server", {"FAKE_START_DELAY": "30"})
    looks: list[None] = []

    def ctrl_c_on_the_third_look(self: KokoroServer) -> bool:
        looks.append(None)
        if len(looks) == 3:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(KokoroServer, "_healthy", ctrl_c_on_the_third_look)
    engine = KokoroServer(tmp_path / "data", executable=loading, startup_timeout=60)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        engine.start()

    assert time.monotonic() - started < 5  # audio.cpp looks at SIGTERM only once it has loaded
    assert engine._process is None and engine._tempdir is None
    assert _signals(tmp_path) == []


# Tomecast running the engine, voicing a sentence that takes a while. It prints the engine's
# process ID once it's running; the test then stops it the way a terminal or the system would.
_RUNS_THE_ENGINE = textwrap.dedent(
    """
    import ctypes, sys
    from pathlib import Path
    from tomecast.engines.audiocpp import KokoroServer

    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0: no core dump, whatever happens
    data, program, output = map(Path, sys.argv[1:4])
    with KokoroServer(data, executable=program, startup_timeout=20) as engine:
        print(engine._process.pid, flush=True)
        engine.synthesize("This takes a while to say.", output)
    """
)


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state in ("Z", "X")  # exited, and only waiting to be reaped


@pytest.mark.parametrize("stop", ["ctrl-c", "tomecast killed"])
def test_the_engine_never_gets_ctrl_c_or_tomecasts_death_as_a_signal_it_handles(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    monkeypatch.setenv("FAKE_DELAY", "20")
    monkeypatch.setenv("FAKE_BUSY", str(tmp_path / "busy"))
    script = tmp_path / "run_the_engine.py"
    script.write_text(_RUNS_THE_ENGINE)
    arguments = [str(tmp_path / "data"), str(fake_server), str(tmp_path / "out.wav")]
    engine = 0
    with (tmp_path / "stderr.txt").open("w") as stderr:
        # A session of its own, like a terminal's foreground job, so the Ctrl-C below can go to
        # its whole process group (as a terminal sends it) without reaching pytest.
        child = subprocess.Popen(
            [sys.executable, str(script), *arguments],
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
    try:
        assert child.stdout is not None
        engine = int(child.stdout.readline())
        _wait_for(tmp_path / "busy")  # the engine is voicing the sentence
        if stop == "ctrl-c":
            os.killpg(child.pid, signal.SIGINT)  # what a terminal does on Ctrl-C
        else:
            child.kill()  # the out-of-memory killer, say: the parent-death signal stops it
        child.wait(timeout=20)
        deadline = time.monotonic() + 10
        while not _gone(engine):
            assert time.monotonic() < deadline, "the engine outlived Tomecast"
            time.sleep(0.05)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if engine and not _gone(engine):
            os.kill(engine, signal.SIGKILL)
        if child.stdout is not None:
            child.stdout.close()

    assert _signals(tmp_path) == []  # stopped by SIGKILL alone: audio.cpp's own stop never ran


def test_loaded_device_reads_audiocpps_reply(tmp_path: Path, fake_server: Path) -> None:
    with KokoroServer(
        tmp_path / "data", executable=fake_server, backend="vulkan", device=1, startup_timeout=20
    ) as engine:
        assert engine.loaded_device() == 1


# --- Second review: CPU fallbacks, stops, cards that don't answer, the CPU path ---------------


def test_auto_says_when_it_fell_back_from_the_card(
    tmp_path: Path, fake_server: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.fell_back is False  # on the card, as asked
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.fell_back is True  # the card didn't work: a CPU run proves nothing for it
    with AutoKokoro(tmp_path / "data", use_gpu=False, executable=fake_server) as engine:
        assert engine.fell_back is False  # the CPU was asked for


def test_auto_without_a_usable_card_hasnt_fallen_back(tmp_path: Path, fake_server: Path) -> None:
    # No build sees a usable card, so the self-check ran on the CPU, and the run uses the CPU
    # just as checked: that proves the build (#53), or a CPU-only machine never could.
    builds = {AUDIOCPP_VULKAN.slot: tmp_path / "engine", ESPEAK_NG.slot: tmp_path / "espeak"}
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.warning == audiocpp.NO_DEVICE_NOTE
        assert engine.fell_back is False
        assert engine.proves(builds) == builds


def test_after_falling_back_only_the_engine_build_goes_unproven(
    tmp_path: Path, fake_server: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = {AUDIOCPP_VULKAN.slot: tmp_path / "engine", ESPEAK_NG.slot: tmp_path / "espeak"}
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.proves(builds) == builds  # on the card, as asked
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.fell_back is True
        # The card was never tried with the engine build; eSpeak NG worked as in any run.
        assert engine.proves(builds) == {ESPEAK_NG.slot: tmp_path / "espeak"}


def test_closing_the_window_stops_the_card_probe_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The window's engines have no "cancelled": closing it calls stop_all_engines(), which
    # must reach an engine still asking the build which cards it sees (#53).
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    stalled = write_fake_server(tmp_path / "audiocpp_server", {"FAKE_LIST_HANG": "1"})
    engine = AutoKokoro(tmp_path / "data", executable=stalled, startup_timeout=20)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            engine.start()
        except BaseException as error:  # recorded for the test
            errors.append(error)

    worker = threading.Thread(target=start)
    worker.start()
    deadline = time.monotonic() + 10
    while not any(e["event"] == "list" for e in _events_if_any(tmp_path)):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    started = time.monotonic()
    audiocpp.stop_all_engines()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert time.monotonic() - started < 3  # not the 20 s the listing may take
    assert len(errors) == 1 and isinstance(errors[0], Cancelled)  # stopped, not a CPU run
    assert _starts(tmp_path) == []
    assert engine not in audiocpp._RUNNING


def test_a_card_that_doesnt_answer_isnt_taken_for_no_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    stalled = write_fake_server(tmp_path / "audiocpp_server", {"FAKE_LIST_FAIL": "1"})

    with AutoKokoro(tmp_path / "data", executable=stalled, startup_timeout=20) as engine:
        assert engine.warning == audiocpp.CPU_FALLBACK_NOTE  # "didn't work", not "none found"
        assert engine.fell_back is True


@pytest.mark.parametrize("broken", ["FAKE_LIST_FAIL", "FAKE_LIST_HANG"])
def test_a_new_build_that_cant_list_the_cards_fails_when_the_old_one_can(
    tmp_path: Path, engine_root: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    monkeypatch.setattr(audiocpp, "list_devices", _quick_list_devices)
    old = _engine_build(tmp_path, "old", gpu=True)  # answers: no card
    new = _engine_build(tmp_path, "new", gpu=True, env={broken: "1"})  # crashes, or hangs

    prepared = _update(engine_root, old, new, gpu=True)

    assert prepared.switched == ()  # a CPU check can't stand in for a card nobody could see
    assert build_in_use(new, engine_root) == build_folder(old, engine_root)
    reason = failure(new, engine_root)
    assert reason is not None and "couldn't list the graphics cards" in reason  # a verdict
    assert prepared.notes == (UPDATE_FAILED_NOTE,)
    assert not waiting_check(new, engine_root)  # so the next books don't ask it again
    assert _checks(tmp_path) == []  # not even started
    asked = [Path(str(e["exe"])).parent.name for e in _listings(tmp_path)]
    assert asked.count(build_folder(new, engine_root).name) == 2  # asked again on the retry

    listed = len(_listings(tmp_path))
    prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))
    assert len(_listings(tmp_path)) == listed  # the next run doesn't probe it again


def test_a_new_build_that_cant_list_the_cards_fails_even_when_the_old_one_sees_one(
    tmp_path: Path, engine_root: Path, with_devices: None
) -> None:
    # It might voice on the card it's given, but every run lists the cards first, gets no
    # answer and uses the CPU: switching to it would leave the card unused for good (#53).
    old = _engine_build(tmp_path, "old", gpu=True)  # sees the NVIDIA card
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_LIST_FAIL": "1"})

    prepared = _update(engine_root, old, new, gpu=True)

    assert prepared.switched == ()
    reason = failure(new, engine_root)
    assert reason is not None and "couldn't list the graphics cards" in reason
    assert _checks(tmp_path) == []  # not tried on the card on the old build's word


def test_a_new_build_that_sees_no_card_where_the_old_one_sees_one_fails(
    tmp_path: Path, engine_root: Path
) -> None:
    # Every run would use the CPU ("no usable card"), and a book made there would prove a
    # build the card was never tried with (#53 review).
    old = _engine_build(tmp_path, "old", gpu=True, env={"FAKE_DEVICES": _DEVICES})
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_DEVICES": '{"devices": []}'})

    prepared = _update(engine_root, old, new, gpu=True)

    assert prepared.switched == ()
    reason = failure(new, engine_root)
    assert reason is not None and "found no usable graphics card" in reason
    assert "NVIDIA RTX PRO 1000" in reason
    assert _checks(tmp_path) == []  # not tried on the card on the old build's word


def test_the_same_card_with_less_free_memory_just_then_is_still_tried(
    tmp_path: Path, engine_root: Path
) -> None:
    def igpu(free: int) -> str:
        return json.dumps({"devices": [{"backend": "vulkan", "index": 0, "name": "Vulkan0",
                          "type": "igpu", "description": "AMD Radeon 860M Graphics (RADV)",
                          "memory": {"free_bytes": free, "total_bytes": 20 * 2**30}}]})  # fmt: skip

    old = _engine_build(tmp_path, "old", gpu=True, env={"FAKE_DEVICES": igpu(3 * 2**30)})
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_DEVICES": igpu(1 * 2**30)})

    prepared = _update(engine_root, old, new, gpu=True)

    assert prepared.switched == (new,)  # it sees the card, just with less free right then
    assert _checks(tmp_path) == [(build_folder(new, engine_root).name, "vulkan")]


def test_a_new_build_that_answers_on_the_retry_is_judged_on_its_answer(
    tmp_path: Path, engine_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)  # no card
    new = _engine_build(tmp_path, "new", gpu=True)  # no card either, but slow once
    prepare([old, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))
    hiccups = [build_folder(new, engine_root) / "audiocpp_server"]

    def list_once_badly(
        executable: Path | str, timeout: float = 20, cancelled: Callable[[], bool] | None = None
    ) -> list[GpuDevice] | None:
        if Path(executable) in hiccups:
            hiccups.remove(Path(executable))
            return None  # a driver slow to wake, once
        return list_devices(executable, timeout=timeout, cancelled=cancelled)

    monkeypatch.setattr(audiocpp, "list_devices", list_once_badly)

    prepared = prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))

    assert hiccups == []  # asked once more on the retry...
    assert prepared.switched == (new,)  # ...and judged on that answer: no card, CPU check passed
    assert failure(new, engine_root) is None


def test_no_build_that_can_list_the_cards_is_still_no_verdict(
    tmp_path: Path, engine_root: Path
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True, env={"FAKE_LIST_FAIL": "1"})
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_LIST_FAIL": "1"})

    prepared = _update(engine_root, old, new, gpu=True)

    assert prepared.switched == ()
    assert failure(new, engine_root) is None  # the card (or its driver), not the update
    assert waiting_check(new, engine_root)


def _quick_list_devices(
    executable: Path | str, timeout: float = 20, cancelled: Callable[[], bool] | None = None
) -> list[GpuDevice] | None:
    return list_devices(executable, timeout=2, cancelled=cancelled)  # a hang, without the wait


def _listings(tmp_path: Path) -> list[dict[str, object]]:
    return [event for event in _events_if_any(tmp_path) if event["event"] == "list"]


def _events_if_any(tmp_path: Path) -> list[dict[str, object]]:
    log = tmp_path / "fake.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_each_build_lists_its_cards_once_per_check(
    tmp_path: Path, engine_root: Path, with_devices: None
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True, env={"FAKE_REPORT_BACKEND": "cpu"})
    prepare([old, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))
    assert not (tmp_path / "fake.log").exists()  # a first install: nothing listed or started

    prepare([new, ESPEAK_NG], engine_root, check=audiocpp.self_check(engine_root, True))

    listed = [e for e in _events(tmp_path) if e["event"] == "list"]
    assert len(listed) == 1  # the new build, once, though the failed check ran twice
    assert len(_checks(tmp_path)) == 4  # (new, old) on the card, twice


def test_a_stopped_engine_is_never_called_crashing(tmp_path: Path, fake_server: Path) -> None:
    with KokoroServer(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        engine._consecutive_restarts = audiocpp._MAX_RESTARTS  # it had crashed on this text
        engine.abort()  # and then the user stopped it
        assert engine._process is not None
        engine._process.wait(timeout=10)
        with pytest.raises(Cancelled):  # not "keeps crashing", which would blame the build
            engine.synthesize("Anyone there?", tmp_path / "x.wav")


def test_a_cpu_run_hides_the_graphics_card(tmp_path: Path, fake_server: Path) -> None:
    with KokoroServer(tmp_path / "data", executable=fake_server, backend="cpu"):
        pass
    with KokoroServer(
        tmp_path / "data", executable=fake_server, backend="vulkan", device=0, startup_timeout=20
    ):
        pass
    cpu_env, card_env = (start["env"] for start in _starts(tmp_path))
    assert isinstance(cpu_env, dict) and isinstance(card_env, dict)
    # ggml sees no card, and the Vulkan loader finds no driver to load: nothing touches the
    # card (checked on the AMD 860M: no driver loaded, no device set up, busy % unchanged).
    assert cpu_env["GGML_VK_VISIBLE_DEVICES"] == ""
    assert not Path(cpu_env["VK_DRIVER_FILES"]).exists()
    assert not Path(cpu_env["VK_ICD_FILENAMES"]).exists()
    assert "GGML_VK_VISIBLE_DEVICES" not in card_env and "VK_DRIVER_FILES" not in card_env


def test_the_cpu_fallback_hides_the_card_too(
    tmp_path: Path, fake_server: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.backend == "cpu"
    cpu_start = _starts(tmp_path)[-1]["env"]
    assert isinstance(cpu_start, dict) and cpu_start["GGML_VK_VISIBLE_DEVICES"] == ""
