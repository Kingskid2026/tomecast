"""The graphics card running out of memory (#49): told apart from other failures, and the book
carries on on the CPU with the same voice, from the piece that failed, losing nothing.

Kokoro runs out per request (HTTP 500, the server stays up); Ming runs out while it loads.
Both are played by the fake audio.cpp server, so nothing here needs a graphics card.
"""

import json
from pathlib import Path

import pytest
from fake_engine import write_fake_server

from tomecast.audio import wav_info
from tomecast.book import Book, Chapter
from tomecast.convert import convert_book, plan_chunks
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import (
    OUT_OF_MEMORY_NOTE,
    RAN_OUT_OF_MEMORY_NOTE,
    AutoKokoro,
    EngineError,
    GpuOutOfMemory,
    KokoroServer,
    out_of_graphics_memory,
)
from tomecast.speed import engine_speed, known_cpu_speed, remember_speed, speed_key

_DEVICES = json.dumps(
    {
        "devices": [
            {"backend": "vulkan", "index": 0, "name": "Vulkan0", "type": "gpu",
             "description": "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",
             "memory": {"free_bytes": 1_500_000_000, "total_bytes": 8_000_000_000}},
        ]
    }
)  # fmt: skip


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_DEVICES", _DEVICES)
    return write_fake_server(tmp_path / "audiocpp_server")


def _events(tmp_path: Path, kind: str) -> list[dict[str, object]]:
    lines = (tmp_path / "fake.log").read_text().splitlines()
    return [event for line in lines if (event := json.loads(line))["event"] == kind]


def _backends_started(tmp_path: Path) -> list[str]:
    return [event["config"]["backend"] for event in _events(tmp_path, "start")]  # type: ignore[index]


def _voiced(tmp_path: Path) -> list[tuple[str, str]]:
    """(text, backend) for each piece the engine voiced, in order."""
    return [
        (event["body"]["input"], str(event["backend"]))  # type: ignore[index]
        for event in _events(tmp_path, "voiced")
    ]


def _auto(tmp_path: Path, server: Path, **kwargs: object) -> AutoKokoro:
    return AutoKokoro(tmp_path / "data", executable=server, startup_timeout=20, **kwargs)  # type: ignore[arg-type]


# --- Telling it apart -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        # Kokoro's per-request failure (the 500's message contains "failed to allocate").
        "the voice engine failed (HTTP 500): ggml_gallocr_reserve_n: failed to allocate "
        "Vulkan0 buffer of size 268435456",
        # Ming's start-up failure, and the other ways ggml and Vulkan say the same.
        "ggml_vulkan: vk::Device::allocateMemory: ErrorOutOfDeviceMemory",
        "vkAllocateMemory returned VK_ERROR_OUT_OF_DEVICE_MEMORY",
        "ggml_vulkan: Device memory allocation of size 1744830464 failed.",
        # A warning ggml carried on after, then the real failure.
        "ggml_vulkan: Failed to allocate pinned memory (ErrorOutOfDeviceMemory)\n"
        "ggml_vulkan: Device memory allocation of size 1744830464 failed.",
    ],
)
def test_running_out_of_graphics_memory_is_recognised(message: str) -> None:
    assert out_of_graphics_memory(message)


@pytest.mark.parametrize(
    "message",
    [
        "",
        "the voice engine failed (HTTP 500): something broke",
        "the voice engine failed (HTTP 500): Kokoro phoneme string exceeds 510 symbols",
        "model failed to load",
        # ggml recovers from this one by using ordinary memory.
        "ggml_vulkan: Failed to allocate pinned memory (vk::Device::allocateMemory: "
        "ErrorOutOfDeviceMemory)",
        # The computer's own memory: the CPU needs that just the same.
        "ggml_gallocr_reserve_n: failed to allocate CPU buffer of size 1024",
        "ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size 1024",
        # A normal start-up line that talks about memory.
        "ggml_vulkan: 0 = NVIDIA RTX PRO 1000 | uma: 0 | fp16: 1 | shared memory: 49152",
    ],
)
def test_other_failures_are_not_taken_for_running_out_of_memory(message: str) -> None:
    assert not out_of_graphics_memory(message)


def test_a_request_that_runs_out_of_graphics_memory_says_so_and_leaves_the_server_up(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "0")
    server = KokoroServer(tmp_path / "data", executable=fake_server, backend="vulkan", device=0)
    with server, pytest.raises(GpuOutOfMemory, match="failed to allocate Vulkan0 buffer"):
        server.synthesize("Hello there.", tmp_path / "out.wav")
    assert _backends_started(tmp_path) == ["vulkan"]  # not restarted as if it had crashed


def test_another_failure_on_the_card_is_a_plain_engine_error(
    tmp_path: Path, fake_server: Path
) -> None:
    server = KokoroServer(tmp_path / "data", executable=fake_server, backend="vulkan", device=0)
    with server, pytest.raises(EngineError, match="something broke") as error:
        server.synthesize("EXPLODE", tmp_path / "out.wav")
    assert not isinstance(error.value, GpuOutOfMemory)  # not every 500 is out of memory


def test_failing_to_allocate_on_the_cpu_is_a_plain_engine_error(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "0")
    monkeypatch.setenv("FAKE_OOM_CPU_TOO", "1")
    with (
        _auto(tmp_path, fake_server, use_gpu=False) as engine,
        pytest.raises(EngineError, match="failed to allocate") as error,
    ):
        engine.synthesize("Hello there.", tmp_path / "out.wav")
    assert not isinstance(error.value, GpuOutOfMemory)  # nothing to fall back to
    assert _backends_started(tmp_path) == ["cpu"]


# --- Kokoro: out of memory partway through ----------------------------------------------------


def test_the_cpu_carries_on_from_the_piece_that_failed_with_the_same_voice(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "2")  # another program takes the memory
    texts = [f"Sentence number {n}." for n in range(1, 6)]
    with _auto(tmp_path, fake_server, voice="bf_emma", speed=1.2, seed=7) as engine:
        assert (engine.backend, engine.warning, engine.out_of_memory) == ("vulkan", None, False)
        for index, text in enumerate(texts):
            engine.synthesize(text, tmp_path / f"{index}.wav")
            assert wav_info(tmp_path / f"{index}.wav").frames > 0

        assert engine.backend == "cpu"
        assert engine.warning == RAN_OUT_OF_MEMORY_NOTE
        assert engine.fell_back and engine.out_of_memory
        assert (engine.device_label, engine.gpu_description) == ("CPU", "")
        assert engine._consecutive_restarts == 0  # not a crash

    failed = [event["body"]["input"] for event in _events(tmp_path, "out of memory")]  # type: ignore[index]
    assert failed == ["Sentence number 3."]
    assert _voiced(tmp_path) == [
        ("Sentence number 1.", "vulkan"),
        ("Sentence number 2.", "vulkan"),
        ("Sentence number 3.", "cpu"),  # the one that failed, voiced again: nothing skipped
        ("Sentence number 4.", "cpu"),  # and it stays on the CPU
        ("Sentence number 5.", "cpu"),
    ]
    starts = _events(tmp_path, "start")
    assert _backends_started(tmp_path) == ["vulkan", "cpu"]
    assert starts[0]["exe"] == starts[1]["exe"]  # the same build
    cpu_env = starts[1]["env"]
    assert isinstance(cpu_env, dict) and cpu_env["GGML_VK_VISIBLE_DEVICES"] == ""  # card hidden
    bodies = [event["body"] for event in _events(tmp_path, "voiced")]
    assert {(b["voice"], b["speed"], b["options"]["seed"]) for b in bodies} == {  # type: ignore[index]
        ("bf_emma", 1.2, 7)
    }


_UNFINISHED = "The last page ends mid-sentence and"
_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")  # no digits: text prep
# would spell those out, and this compares the book's own characters


def _long_book() -> Book:
    def paragraph(chapter: str, count: int) -> str:
        return " ".join(f"The {chapter} chapter says {_WORDS[n]} out loud." for n in range(count))

    return Book(
        title="A Long Book",
        authors=("Ada Writer",),
        language="en",
        chapters=(
            Chapter("Chapter One", (paragraph("first", 6), paragraph("first", 3))),
            Chapter("Chapter Two", (paragraph("second", 5),)),
            Chapter("Chapter Three", (paragraph("third", 4), _UNFINISHED)),
        ),
    )


def test_no_text_is_lost_when_the_card_runs_out_of_memory_mid_book(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "4")
    book = _long_book()
    output = tmp_path / "book.m4b"

    with _auto(tmp_path, fake_server) as engine:
        result = convert_book(book, engine, output, workdir=tmp_path / "work")
        assert engine.out_of_memory and engine.backend == "cpu"

    planned = [c.text for chapter in plan_chunks(book, engine.max_chars) for c in chapter]
    voiced = _voiced(tmp_path)
    # Every piece voiced exactly once, in order: the one that ran out of memory on the card
    # was voiced on the CPU, and nothing before or after it was skipped or repeated.
    assert [text for text, _ in voiced] == planned
    assert [backend for _, backend in voiced] == ["vulkan"] * 4 + ["cpu"] * (len(planned) - 4)
    spoken = "".join("".join(text.split()) for text, _ in voiced)
    source = "".join(
        "".join(p.split()) for chapter in book.chapters for p in chapter.spoken_paragraphs()
    )
    assert spoken == source  # every character of the book, in order
    assert voiced[-1][0] == _UNFINISHED
    assert _backends_started(tmp_path) == ["vulkan", "cpu"]  # never back to the card mid-book
    assert len(list((tmp_path / "work" / "chunks").glob("*.wav"))) == len(planned)
    assert [chapter.title for chapter in result.chapters] == [c.title for c in book.chapters]
    assert output.is_file()


def test_the_next_book_tries_the_card_again(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "0")
    engine = _auto(tmp_path, fake_server)
    with engine:
        engine.synthesize("First book.", tmp_path / "1.wav")
        assert engine.backend == "cpu"

    monkeypatch.delenv("FAKE_OOM_VULKAN_AFTER")  # the other program has finished
    with engine:
        assert (engine.backend, engine.warning, engine.out_of_memory) == ("vulkan", None, False)
        assert not engine.fell_back
        engine.synthesize("Second book.", tmp_path / "2.wav")

    assert _backends_started(tmp_path) == ["vulkan", "cpu", "vulkan"]
    assert _voiced(tmp_path)[-1] == ("Second book.", "vulkan")


def test_an_engine_that_stops_saying_it_ran_out_isnt_restarted_on_the_card(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")
    monkeypatch.setenv(
        "FAKE_DIE_SAYING", "terminate called after throwing an instance of "
        "'vk::OutOfDeviceMemoryError'\n  what():  vk::Device::allocateMemory: "
        "ErrorOutOfDeviceMemory",
    )  # fmt: skip
    with _auto(tmp_path, fake_server) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        engine.synthesize("Second.", tmp_path / "2.wav")  # dies on the card, done on the CPU
        assert engine.warning == RAN_OUT_OF_MEMORY_NOTE
        assert engine._consecutive_restarts == 0  # never counted towards "keeps crashing"

    assert _backends_started(tmp_path) == ["vulkan", "cpu"]
    assert _voiced(tmp_path) == [("First.", "vulkan"), ("Second.", "cpu")]

    plain = KokoroServer(tmp_path / "data", executable=fake_server, backend="vulkan", device=0)
    with plain:
        plain.synthesize("First.", tmp_path / "1.wav")
        with pytest.raises(GpuOutOfMemory, match="ran out of memory and the voice engine stopped"):
            plain.synthesize("Second.", tmp_path / "2.wav")


def test_an_ordinary_crash_on_the_card_is_still_restarted_on_the_card(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")
    # A warning ggml carries on after is in the log, but it's not why the engine stopped.
    monkeypatch.setenv(
        "FAKE_OOM_LOG", "ggml_vulkan: Failed to allocate pinned memory (ErrorOutOfDeviceMemory)"
    )
    with _auto(tmp_path, fake_server) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        engine.synthesize("Second.", tmp_path / "2.wav")
        assert (engine.backend, engine.out_of_memory, engine.warning) == ("vulkan", False, None)

    assert _backends_started(tmp_path) == ["vulkan", "vulkan"]


# --- Ming: out of memory as it loads ----------------------------------------------------------


def test_running_out_of_memory_at_start_up_is_gpu_out_of_memory(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_START", "1")
    server = KokoroServer(
        tmp_path / "data", executable=fake_server, backend="vulkan", device=0, startup_timeout=20
    )
    with pytest.raises(GpuOutOfMemory, match="ran out of memory while the voice engine started"):
        server.start()
    assert server._process is None  # cleaned up


def test_a_voice_that_runs_out_of_memory_as_it_loads_starts_on_the_cpu(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_START", "1")
    with _auto(tmp_path, fake_server, voice="bm_george") as engine:
        assert engine.backend == "cpu"
        assert engine.warning == OUT_OF_MEMORY_NOTE
        assert engine.fell_back and engine.out_of_memory
        engine.synthesize("It still speaks.", tmp_path / "out.wav")

    starts = _events(tmp_path, "start")
    assert _backends_started(tmp_path) == ["vulkan", "cpu"]
    assert starts[0]["exe"] == starts[1]["exe"]
    assert _voiced(tmp_path) == [("It still speaks.", "cpu")]
    assert _events(tmp_path, "voiced")[0]["body"]["voice"] == "bm_george"  # type: ignore[index]


def test_a_start_that_hangs_after_running_out_of_memory_falls_back_too(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_START", "1")
    monkeypatch.setenv("FAKE_OOM_HANG", "1")  # it says so, then never serves
    engine = AutoKokoro(tmp_path / "data", executable=fake_server, startup_timeout=3)
    with engine:
        assert (engine.backend, engine.warning) == ("cpu", OUT_OF_MEMORY_NOTE)


def test_a_start_up_failure_that_isnt_memory_keeps_the_general_note(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with _auto(tmp_path, fake_server) as engine:
        assert engine.warning == audiocpp.CPU_FALLBACK_NOTE
        assert engine.fell_back and not engine.out_of_memory


# --- The speed test ----------------------------------------------------------------------------


def test_the_speed_test_reports_the_cpu_when_the_card_runs_out_of_memory(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_OOM_VULKAN_AFTER", "1")  # the warm-up works, then the memory goes
    root = tmp_path / "data"
    measuring: list[bool] = []
    with _auto(tmp_path, fake_server) as engine:
        speed = engine_speed(root, engine, measuring=lambda: measuring.append(True))
        assert engine.backend == "cpu"

    assert speed is not None and speed > 0
    assert measuring == [True]  # said once
    saved = json.loads((root / "speeds.json").read_text())["speeds"]
    assert list(saved) == [speed_key(fake_server.parent, backend="cpu", gpu_description="",
                                     threads=engine.threads)]  # fmt: skip
    assert saved[next(iter(saved))]["speedup"] == speed  # the CPU's alone, nothing for the card


def test_the_cpu_speed_for_the_new_estimate(tmp_path: Path, fake_server: Path) -> None:
    root = tmp_path / "data"
    engine = _auto(tmp_path, fake_server)
    assert known_cpu_speed(root, engine) is None
    key = speed_key(fake_server.parent, backend="cpu", gpu_description="", threads=engine.threads)
    remember_speed(root, key, 3.1)
    assert known_cpu_speed(root, engine) == 3.1
