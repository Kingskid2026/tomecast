import dataclasses
import hashlib
import json
import platform
import tarfile
from pathlib import Path

import pytest
from fake_engine import write_fake_server

from tomecast.audio import wav_info
from tomecast.downloads import (
    UPDATE_FAILED_NOTE,
    Asset,
    Prepared,
    build_folder,
    build_in_use,
    failure,
    prepare,
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


def test_an_engine_that_dies_without_a_word_says_how_it_ended(tmp_path: Path) -> None:
    program = tmp_path / "audiocpp_server"
    program.write_text("#!/bin/sh\nkill -SEGV $$\n")  # a damaged build: crashes at once
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
    # The new one on the card, then the old one on the card (it worked, so the new one failed).
    assert _checks(tmp_path) == [(new.sha256[:12], "vulkan"), (old.sha256[:12], "vulkan")]
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


def test_when_the_card_fails_with_every_build_the_new_one_is_checked_on_the_cpu(
    tmp_path: Path, engine_root: Path, with_devices: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _engine_build(tmp_path, "old", gpu=True)
    new = _engine_build(tmp_path, "new", gpu=True)
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")  # a broken driver: no build can use the card

    prepared = _update(engine_root, old, new, gpu=True)

    assert prepared.switched == (new,)  # the driver is the problem, not the update
    assert _checks(tmp_path) == [
        (new.sha256[:12], "vulkan"),
        (old.sha256[:12], "vulkan"),
        (new.sha256[:12], "cpu"),
    ]


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
