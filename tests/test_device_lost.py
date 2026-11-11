"""The graphics card lost partway through a book (``VK_ERROR_DEVICE_LOST``: a GPU reset, or the
driver crashed; #53): told apart from other failures, and, like running out of memory, the book
carries on on the CPU with the same voice, from the piece that failed, losing nothing, and the
build is never blamed for it.

The fake audio.cpp server plays it with the real wording of audio.cpp 0.9.4's Vulkan build, so
nothing here needs a graphics card.
"""

import json
from pathlib import Path

import pytest
from fake_engine import LOST_CRASH, LOST_GGML, LOST_REPLY, write_fake_server

from tomecast.book import Book, Chapter
from tomecast.convert import convert_book, plan_chunks
from tomecast.downloads import Inconclusive
from tomecast.engines import audiocpp
from tomecast.engines.audiocpp import (
    CPU_FALLBACK_NOTE,
    DEVICE_LOST_NOTE,
    AutoKokoro,
    GpuDeviceLost,
    GpuOutOfMemory,
    KokoroServer,
    check_build,
    graphics_card_lost,
    out_of_graphics_memory,
)

_DEVICES = json.dumps(
    {
        "devices": [
            {"backend": "vulkan", "index": 0, "name": "Vulkan0", "type": "gpu",
             "description": "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",
             "memory": {"free_bytes": 6_000_000_000, "total_bytes": 8_000_000_000}},
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
        LOST_REPLY,  # the server's HTTP 500 reply
        f"the voice engine failed (HTTP 500): {LOST_REPLY}",
        LOST_CRASH,  # uncaught, it ends the program
        LOST_GGML,  # ggml's own check, then it exits
        "vk::Queue::submit: ErrorDeviceLost",
        "radv: GPU hang detected; VK_ERROR_DEVICE_LOST",  # Vulkan's C name, from a driver
    ],
)
def test_a_lost_graphics_card_is_recognised(message: str) -> None:
    assert graphics_card_lost(message)
    assert not out_of_graphics_memory(message)  # it's not running out of memory


@pytest.mark.parametrize(
    "message",
    [
        "",
        "the voice engine failed (HTTP 500): something broke",
        "vk::Device::allocateMemory: ErrorOutOfDeviceMemory",
        "ggml_vulkan: 0 = NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU (NVIDIA) | uma: 0",
        "the device is lost in thought",  # plain words aren't Vulkan's
    ],
)
def test_other_failures_are_not_taken_for_a_lost_card(message: str) -> None:
    assert not graphics_card_lost(message)


def test_a_request_on_a_lost_card_says_so(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOST_VULKAN_AFTER", "0")
    server = KokoroServer(tmp_path / "data", executable=fake_server, backend="vulkan", device=0)
    with server, pytest.raises(GpuDeviceLost, match="ErrorDeviceLost") as error:
        server.synthesize("Hello there.", tmp_path / "out.wav")
    assert not isinstance(error.value, GpuOutOfMemory)
    assert _backends_started(tmp_path) == ["vulkan"]  # not restarted as if it had crashed


# --- Partway through a book --------------------------------------------------------------------


def test_the_cpu_carries_on_from_the_piece_that_failed_with_the_same_voice(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOST_VULKAN_AFTER", "2")  # the card is reset after two pieces
    texts = [f"Sentence number {n}." for n in range(1, 6)]
    with _auto(tmp_path, fake_server, voice="bf_emma", speed=1.2, seed=7) as engine:
        assert (engine.backend, engine.warning, engine.device_lost) == ("vulkan", None, False)
        for index, text in enumerate(texts):
            engine.synthesize(text, tmp_path / f"{index}.wav")

        assert engine.backend == "cpu"
        assert engine.warning == DEVICE_LOST_NOTE
        assert engine.fell_back and engine.device_lost and not engine.out_of_memory
        assert (engine.device_label, engine.gpu_description) == ("CPU", "")
        assert engine._consecutive_restarts == 0  # not a crash

    lost = [event["body"]["input"] for event in _events(tmp_path, "lost")]  # type: ignore[index]
    assert lost == ["Sentence number 3."]
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
    bodies = [event["body"] for event in _events(tmp_path, "voiced")]
    assert {(b["voice"], b["speed"], b["options"]["seed"]) for b in bodies} == {  # type: ignore[index]
        ("bf_emma", 1.2, 7)
    }


_UNFINISHED = "The last page ends mid-sentence and"
_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


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


def test_no_text_is_lost_or_repeated_when_the_card_is_lost_mid_book(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOST_VULKAN_AFTER", "4")
    book = _long_book()
    output = tmp_path / "book.m4b"

    with _auto(tmp_path, fake_server) as engine:
        result = convert_book(book, engine, output, workdir=tmp_path / "work")
        assert engine.device_lost and engine.backend == "cpu"

    planned = [c.text for chapter in plan_chunks(book, engine.max_chars) for c in chapter]
    voiced = _voiced(tmp_path)
    # Every piece voiced exactly once, in order: the one the card was lost on was voiced on
    # the CPU, and nothing before or after it was skipped or repeated.
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


def test_an_engine_that_dies_with_the_card_isnt_restarted_on_it_or_counted(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DIE_AFTER", "1")
    monkeypatch.setenv("FAKE_DIE_SAYING", LOST_CRASH)
    with _auto(tmp_path, fake_server) as engine:
        engine.synthesize("First.", tmp_path / "1.wav")
        engine._consecutive_restarts = audiocpp._MAX_RESTARTS  # one more crash would be too many
        engine.synthesize("Second.", tmp_path / "2.wav")  # the card is lost; done on the CPU
        assert engine.warning == DEVICE_LOST_NOTE
        assert engine.device_lost and engine._consecutive_restarts == 0

    assert _backends_started(tmp_path) == ["vulkan", "cpu"]
    assert _voiced(tmp_path) == [("First.", "vulkan"), ("Second.", "cpu")]

    plain = KokoroServer(tmp_path / "data", executable=fake_server, backend="vulkan", device=0)
    with plain:
        plain.synthesize("First.", tmp_path / "1.wav")
        with pytest.raises(GpuDeviceLost, match="was lost"):
            plain.synthesize("Second.", tmp_path / "2.wav")


def test_a_card_lost_as_the_engine_starts_moves_to_the_cpu_blamelessly(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOST_VULKAN_START", "1")
    with _auto(tmp_path, fake_server, voice="bm_george") as engine:
        assert engine.backend == "cpu"
        assert engine.warning == CPU_FALLBACK_NOTE
        assert engine.fell_back and engine.device_lost and not engine.out_of_memory
        engine.synthesize("It still speaks.", tmp_path / "out.wav")
    assert _backends_started(tmp_path) == ["vulkan", "cpu"]
    assert _voiced(tmp_path) == [("It still speaks.", "cpu")]


def test_the_next_book_tries_the_card_again(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LOST_VULKAN_AFTER", "0")
    engine = _auto(tmp_path, fake_server)
    with engine:
        engine.synthesize("First book.", tmp_path / "1.wav")
        assert engine.backend == "cpu"

    monkeypatch.delenv("FAKE_LOST_VULKAN_AFTER")  # the driver has recovered
    with engine:
        assert (engine.backend, engine.warning, engine.device_lost) == ("vulkan", None, False)
        assert not engine.fell_back
    assert _backends_started(tmp_path) == ["vulkan", "cpu", "vulkan"]


# --- The self-check ----------------------------------------------------------------------------


def test_a_self_check_that_loses_the_card_is_a_failure_not_no_verdict(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A new build's shader can hang the card into a reset, so a check that loses it fails;
    # self_check then tries the build in use on the card to tell the build from the card.
    monkeypatch.setenv("FAKE_LOST_VULKAN_AFTER", "0")
    result = check_build(tmp_path / "data", fake_server.parent, None, backend="vulkan", device=0)
    assert isinstance(result, str) and "ErrorDeviceLost" in result
    assert not isinstance(result, Inconclusive)
