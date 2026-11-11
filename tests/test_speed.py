"""The speed test, the speeds it remembers, and the estimates made from them."""

import json
import math
import struct
import time
import wave
from pathlib import Path

import pytest
from fake_engine import write_fake_server

from tomecast import speed as speed_module
from tomecast.downloads import FAILURE_DAYS, Cancelled, build_folder
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import (
    AUDIOCPP_CPU,
    AUDIOCPP_VULKAN,
    ESPEAK_NG,
    KOKORO_Q8,
    EngineError,
    KokoroServer,
)
from tomecast.speed import (
    SAMPLE,
    WARM_UP,
    WORDS_PER_MINUTE,
    Speeds,
    audio_minutes,
    engine_speed,
    forget_speeds,
    known_speed,
    make_minutes,
    measure,
    probe,
    remember_speed,
    speed_key,
)


class FakeClock:
    """Stands in for the ``time`` module in :mod:`tomecast.speed`, so the speed test's timing
    is exactly what the engine says it took, however busy the computer is."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class TimedEngine:
    """A voice that makes one second of audio per sentence and takes ``seconds`` to do it on
    ``clock`` (the warm-up takes much longer, as a real engine's first request can)."""

    def __init__(self, seconds: float, clock: FakeClock, warm_up: float = 0.3) -> None:
        self.voice = "bf_emma"
        self.speed = 1.25
        self.seconds = seconds
        self.clock = clock
        self.warm_up = warm_up
        self.asked: list[tuple[str, str, float]] = []

    def synthesize(self, text: str, output: Path) -> None:
        self.asked.append((text, self.voice, self.speed))
        self.clock.now += self.warm_up if text == WARM_UP else self.seconds
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(
                b"".join(struct.pack("<h", int(8000 * math.sin(i / 9))) for i in range(24000))
            )


def test_the_speed_test_divides_audio_by_time_and_leaves_out_the_warm_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(speed_module, "time", clock)
    engine = TimedEngine(seconds=0.05, clock=clock)

    speedup = measure(engine)

    # Two sentences, one second of audio each, 0.05 s each: 20x real time. With the 0.3 s
    # warm-up counted it would be 5x. The clock is the engine's, so a busy computer (which
    # once made it 9.7x) can't change it.
    assert len(SAMPLE) == 2
    assert speedup == pytest.approx(20.0)
    assert [text for text, _, _ in engine.asked] == [WARM_UP, *SAMPLE]
    assert {(voice, speed) for _, voice, speed in engine.asked} == {("af_heart", 1.0)}
    assert (engine.voice, engine.speed) == ("bf_emma", 1.25)  # put back afterwards


def test_a_speed_test_that_makes_no_audio_is_an_engine_error(tmp_path: Path) -> None:
    class Silent(TimedEngine):
        def synthesize(self, text: str, output: Path) -> None:
            output.write_bytes(b"not a wav")

    engine = Silent(0, FakeClock())
    with pytest.raises(EngineError, match="speed test"):
        measure(engine)
    assert (engine.voice, engine.speed) == ("bf_emma", 1.25)


def test_speeds_are_remembered_per_build_and_device(tmp_path: Path) -> None:
    root = tmp_path / "data"
    build = tmp_path / "build"
    build.mkdir()
    (build / ".tomecast-sha256").write_text("a" * 64)
    on_cpu = speed_key(build, backend="cpu", gpu_description="", threads=8)
    on_card = speed_key(build, backend="vulkan", gpu_description="NVIDIA RTX", threads=8)
    assert (
        len({on_cpu, on_card, speed_key(build, backend="cpu", gpu_description="", threads=4)}) == 3
    )
    (build / ".tomecast-sha256").write_text("b" * 64)  # a new build: measured again
    assert speed_key(build, backend="cpu", gpu_description="", threads=8) != on_cpu

    remember_speed(root, on_cpu, 4.567)
    remember_speed(root, on_card, 19.8)

    assert known_speed(root, on_cpu) == 4.57
    assert known_speed(root, on_card) == 19.8
    saved = json.loads((root / "speeds.json").read_text())
    assert saved["speeds"][on_cpu]["speedup"] == 4.57
    forget_speeds(root)  # `tomecast scan`
    assert known_speed(root, on_cpu) is None
    forget_speeds(root)  # nothing to forget is fine


def test_a_damaged_speeds_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "speeds.json").write_text("{not json")
    assert known_speed(tmp_path, "anything") is None
    remember_speed(tmp_path, "key", 3.0)
    assert known_speed(tmp_path, "key") == 3.0


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data folder with both engine builds (the fake server), eSpeak and the model."""
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    root = tmp_path / "data"
    for asset in (AUDIOCPP_CPU, AUDIOCPP_VULKAN, ESPEAK_NG):
        folder = build_folder(asset, root)
        folder.mkdir(parents=True)
        (folder / ".tomecast-sha256").write_text(asset.sha256 + "\n")
        if asset is not ESPEAK_NG:
            write_fake_server(folder / "audiocpp_server")
    model = root / KOKORO_Q8.path
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    return root


def _starts(tmp_path: Path) -> list[dict[str, object]]:
    log = tmp_path / "fake.log"
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [event for event in events if event["event"] == "start"]


NVIDIA = "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU"
DEVICES = json.dumps(
    {"devices": [{"backend": "vulkan", "index": 0, "name": "Vulkan0", "description": NVIDIA,
                  "type": "gpu", "memory": {"free_bytes": 7 * 2**30, "total_bytes": 8 * 2**30}}]}
)  # fmt: skip


def test_the_probe_measures_the_cpu_and_the_card_once(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DEVICES", DEVICES)

    first = probe(installed, gpu=True)

    assert first.cpu is not None and first.cpu > 0
    assert first.gpu is not None and first.gpu > 0
    assert first.gpu_description == NVIDIA
    backends = [event["config"]["backend"] for event in _starts(tmp_path)]  # type: ignore[index]
    assert backends == ["cpu", "vulkan"]
    cpu_build = Path(str(_starts(tmp_path)[0]["exe"])).parent
    assert cpu_build == build_folder(AUDIOCPP_CPU, installed)  # the CPU build, for the CPU

    assert probe(installed, gpu=True) == first  # remembered: no engine started again
    assert len(_starts(tmp_path)) == 2


def test_the_probe_leaves_out_a_card_that_doesnt_work(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DEVICES", DEVICES)
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")

    speeds = probe(installed, gpu=True)

    assert speeds.cpu is not None
    assert (speeds.gpu, speeds.gpu_description) == (None, "")


def test_the_probe_needs_nothing_downloaded_and_never_downloads(tmp_path: Path) -> None:
    assert probe(tmp_path / "data", gpu=True) == Speeds()
    assert not (tmp_path / "data").exists()


def test_the_probe_uses_the_card_builds_cpu_side_when_that_is_all_there_is(
    tmp_path: Path, installed: Path
) -> None:
    import shutil

    shutil.rmtree(installed / AUDIOCPP_CPU.path)

    speeds = probe(installed, gpu=False)

    assert speeds.cpu is not None and speeds.gpu is None
    start = _starts(tmp_path)[0]
    assert Path(str(start["exe"])).parent == build_folder(AUDIOCPP_VULKAN, installed)
    assert start["config"]["backend"] == "cpu"  # type: ignore[index]


def test_engine_speed_measures_on_the_running_engine_once(tmp_path: Path, installed: Path) -> None:
    told: list[str] = []
    with KokoroServer(installed, startup_timeout=20) as engine:
        first = engine_speed(installed, engine, measuring=lambda: told.append("measuring"))
        again = engine_speed(installed, engine, measuring=lambda: told.append("measuring"))

    assert first is not None and first == again
    assert told == ["measuring"]
    assert len(_starts(tmp_path)) == 1  # measured on the engine that was already running


def test_engine_speed_never_stops_a_conversion(tmp_path: Path, installed: Path) -> None:
    with KokoroServer(installed, startup_timeout=20) as engine:
        engine.close()  # the engine is gone: the speed test fails
        assert engine_speed(installed, engine) is None


def test_the_estimates() -> None:
    hour = WORDS_PER_MINUTE * 60
    assert audio_minutes(hour) == 60
    assert audio_minutes(hour, speed=2.0) == 30
    assert make_minutes(hour, 1.0, 4.0) == 15
    # Making it stops getting quicker at 1.5x: faster than that is a quick time-stretch.
    assert make_minutes(hour, 3.0, 4.0) == make_minutes(hour, 1.5, 4.0) == 10
    assert make_minutes(hour, 1.0, 0) == 0


def test_the_speed_test_sentences_fit_in_one_request() -> None:
    assert all(len(text) <= audiocpp.MAX_CHARS for text in (WARM_UP, *SAMPLE))


# --- After the review: failed measurements, time limits, the card switched off ---------------


def test_a_card_that_fails_the_speed_test_isnt_started_again_on_every_launch(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DEVICES", DEVICES)
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")

    for _ in range(3):  # three window launches
        assert probe(installed, gpu=True).gpu is None

    on_card = [s for s in _starts(tmp_path) if s["config"]["backend"] == "vulkan"]  # type: ignore[index]
    assert len(on_card) == 1  # remembered as failed: tried again in a few days, or on `scan`
    forget_speeds(installed)
    probe(installed, gpu=True)
    on_card = [s for s in _starts(tmp_path) if s["config"]["backend"] == "vulkan"]  # type: ignore[index]
    assert len(on_card) == 2


def test_a_failed_measurement_is_tried_again_after_a_few_days(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH", "1")
    assert probe(installed, gpu=False).cpu is None
    monkeypatch.delenv("FAKE_CRASH")
    assert probe(installed, gpu=False).cpu is None  # not yet

    later = time.time() + (FAILURE_DAYS + 0.1) * 86400
    monkeypatch.setattr("time.time", lambda: later)
    assert probe(installed, gpu=False).cpu is not None


def test_the_probe_gives_a_hung_engine_short_time_limits(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DELAY", "30")  # every request hangs
    monkeypatch.setattr(speed_module, "PROBE_REQUEST_TIMEOUT", 0.5)
    started = time.monotonic()

    assert probe(installed, gpu=False).cpu is None

    assert time.monotonic() - started < 10
    assert speed_module.PROBE_STARTUP_TIMEOUT <= 30 and speed_module.PROBE_REQUEST_TIMEOUT <= 30


def test_a_probe_stopped_by_the_user_records_nothing(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_START_DELAY", "30")
    stop_at = time.monotonic() + 0.5

    with pytest.raises(Cancelled):
        probe(installed, gpu=False, cancelled=lambda: time.monotonic() > stop_at)

    assert not (installed / "speeds.json").exists()


def test_without_the_card_the_probe_never_touches_it(
    tmp_path: Path, installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[object] = []

    def list_devices(executable: object) -> list[object]:
        listed.append(executable)
        return []

    monkeypatch.setattr(speed_module, "list_devices", list_devices)

    probe(installed, gpu=False)

    assert listed == []
    assert all(s["config"]["backend"] == "cpu" for s in _starts(tmp_path))  # type: ignore[index]


def test_engine_speed_remembers_a_failure_too(tmp_path: Path, installed: Path) -> None:
    with KokoroServer(installed, startup_timeout=20) as engine:
        engine.close()
        assert engine_speed(installed, engine) is None
        told: list[str] = []
        assert engine_speed(installed, engine, measuring=lambda: told.append("x")) is None
        assert told == []  # not measured again


def test_a_failed_measurement_dated_in_the_future_has_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    monkeypatch.setattr("time.time", lambda: now + 10 * 365 * 86400)  # the clock was years ahead
    speed_module.remember_failure(tmp_path, "build/vulkan/card", "it crashed")
    monkeypatch.setattr("time.time", lambda: now)  # put right

    assert not speed_module.failed_recently(tmp_path, "build/vulkan/card")  # measured again
