"""The premium voice, Ming-3.0-tts on audio.cpp (issue #11), against the fake engine: what it
sends, what it needs, and that no text is lost when its spoken-text check fails."""

import json
import re
from pathlib import Path

import pytest
from epub_factory import NavPoint, Page, build_epub
from fake_engine import write_fake_server

from tomecast.audio import wav_info
from tomecast.convert import MissingAudioError, convert_book, plan_chunks
from tomecast.engines import ming
from tomecast.engines.audiocpp import AUDIOCPP_CPU, AUDIOCPP_VULKAN, EngineError
from tomecast.engines.ming import AutoMing, MingServer
from tomecast.manifest import ASSETS, MING_Q8
from tomecast.readers import read_book

_DEVICES = json.dumps(
    {
        "schema_version": 1,
        "devices": [
            {"backend": "vulkan", "index": 0, "name": "Vulkan0", "type": "gpu",
             "description": "NVIDIA RTX PRO 1000 Blackwell Generation Laptop GPU",
             "memory": {"free_bytes": 7_000_000_000, "total_bytes": 8_000_000_000}},
        ],
    }
)  # fmt: skip

LONG = (
    "Alice was beginning to get very tired of sitting by her sister on the bank, and of "
    "having nothing to do: once or twice she had peeped into the book her sister was reading."
)


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_RATE", "44100")  # Ming's output
    return write_fake_server(tmp_path / "audiocpp_server")


def _events(tmp_path: Path, kind: str) -> list[dict[str, object]]:
    lines = (tmp_path / "fake.log").read_text().splitlines()
    return [event for event in map(json.loads, lines) if event["event"] == kind]


def _voiced(tmp_path: Path) -> list[str]:
    return [str(event["body"]["input"]) for event in _events(tmp_path, "voiced")]  # type: ignore[index]


def _server(tmp_path: Path, script: Path, **kwargs: object) -> MingServer:
    return MingServer(tmp_path / "data", executable=script, startup_timeout=20, **kwargs)  # type: ignore[arg-type]


# --- The pin -------------------------------------------------------------------------------


def test_the_model_is_pinned_and_downloaded_only_when_chosen() -> None:
    assert MING_Q8.opt_in
    assert MING_Q8 in ASSETS
    assert re.fullmatch(r"[0-9a-f]{64}", MING_Q8.sha256)
    assert MING_Q8.size == 1_742_280_896
    # An exact upstream commit, never a branch that can move.
    assert "/resolve/2eb8d37c7f10672e69c8aa33091baa04c461b66c/" in MING_Q8.url
    assert MING_Q8.url.startswith("https://huggingface.co/audio-cpp/")


def test_a_ming_run_needs_the_engine_and_the_model_but_no_espeak() -> None:
    assert ming.required_assets(True) == (AUDIOCPP_VULKAN, MING_Q8)
    assert ming.required_assets(False) == (AUDIOCPP_CPU, MING_Q8)


# --- What it sends -------------------------------------------------------------------------


def test_voices_a_sentence_with_mings_request_and_config(tmp_path: Path, fake_server: Path) -> None:
    output = tmp_path / "out.wav"
    with _server(tmp_path, fake_server, voice="US_Emma", language="fr") as engine:
        engine.synthesize("Bonjour, Alice.", output)
        assert engine.sample_rate == 44_100
        assert engine.label == "Ming (US_Emma)"

    assert wav_info(output).sample_rate == 44_100
    (start,) = _events(tmp_path, "start")
    config = start["config"]
    assert isinstance(config, dict)
    (model,) = config["models"]
    assert model["id"] == "ming"
    assert model["family"] == "ming3_tts"
    assert model["path"].endswith(MING_Q8.path)
    assert "default_request_options" not in model  # no speed: Ming has none
    (request,) = _events(tmp_path, "voiced")
    # Never a top-level speed (an HTTP 500) or voice (ignored): the voice is an option.
    assert request["body"] == {
        "model": "ming",
        "input": "Bonjour, Alice.",
        "response_format": "wav",
        "options": {"template_name": "ip", "voice_id": "US_Emma", "language": "fr", "seed": 42},
    }


def test_ming_needs_no_espeak(tmp_path: Path, fake_server: Path) -> None:
    with _server(tmp_path, fake_server):
        pass
    environment = _events(tmp_path, "start")[0]["env"]
    assert isinstance(environment, dict)
    assert not any(name.startswith("AUDIOCPP_ESPEAK") for name in environment)


def test_fewer_flow_steps_only_with_an_engine_build_that_reads_them(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _server(tmp_path, fake_server):
        pass
    monkeypatch.setattr(ming, "AUDIOCPP_MING_FLOW_STEPS", True)  # the -tc2 build
    with _server(tmp_path, fake_server):
        pass
    before, after = (start["env"] for start in _events(tmp_path, "start"))
    assert isinstance(before, dict)
    assert isinstance(after, dict)
    assert "AUDIOCPP_MING_FLOW_STEPS" not in before
    assert after["AUDIOCPP_MING_FLOW_STEPS"] == "5"
    assert after["AUDIOCPP_MING_FLOW_SCHEDULE"] == "uniform"


def test_the_flow_steps_are_part_of_the_work_folder_key(monkeypatch: pytest.MonkeyPatch) -> None:
    default = ming.work_key("UK_George")
    monkeypatch.setattr(ming, "AUDIOCPP_MING_FLOW_STEPS", True)
    assert ming.work_key("UK_George") != default
    assert ming.work_key("UK_George") != ming.work_key("US_Emma")


def test_unknown_voices_languages_and_speeds_are_refused(tmp_path: Path, fake_server: Path) -> None:
    # The server would take any voice id silently, so it's checked here.
    with pytest.raises(EngineError, match="unknown premium voice"):
        _server(tmp_path, fake_server, voice="af_heart")
    with pytest.raises(EngineError, match="doesn't speak"):
        _server(tmp_path, fake_server, language="it")
    with pytest.raises(EngineError, match=r"between 0\.5 and 2\.0"):
        _server(tmp_path, fake_server, speed=2.5)


def test_all_of_the_speed_is_the_packagers_stretch(tmp_path: Path, fake_server: Path) -> None:
    engine = _server(tmp_path, fake_server, speed=1.75)
    assert (engine.voice_speed, engine.tempo) == (1.0, 1.75)
    engine.speed = 0.5
    assert engine.tempo == 0.5
    with pytest.raises(EngineError):
        engine.speed = 3.0


# --- The spoken-text check -----------------------------------------------------------------


def test_a_chunk_that_fails_the_check_is_voiced_again_in_checked_pieces(
    tmp_path: Path, fake_server: Path
) -> None:
    checked: list[str] = []

    def skips_long_text(wav: Path, text: str) -> str:
        checked.append(text)
        return "skipped 'and of having'" if len(text) > 100 else ""

    output = tmp_path / "out.wav"
    with _server(tmp_path, fake_server, verify=skips_long_text) as engine:
        engine.synthesize(LONG, output)

    voiced = _voiced(tmp_path)
    assert voiced[0] == LONG  # tried whole first
    pieces = voiced[1:]
    assert len(pieces) >= 2
    assert all(len(piece) <= 100 for piece in pieces)
    assert " ".join(pieces) == LONG  # nothing lost in the split
    assert checked == voiced  # every piece was checked
    # The joined audio holds every piece.
    assert wav_info(output).seconds >= sum(len(p) for p in pieces) / 15 - 0.01


def test_a_chunk_that_keeps_failing_fails_loudly(tmp_path: Path, fake_server: Path) -> None:
    with (
        _server(tmp_path, fake_server, verify=lambda wav, text: "skipped a word") as engine,
        pytest.raises(EngineError, match="left out words"),
    ):
        engine.synthesize(LONG, tmp_path / "out.wav")


def test_every_sentence_of_a_book_is_voiced_by_ming(tmp_path: Path, fake_server: Path) -> None:
    """The same guarantees as Kokoro: size-capped chunks, the trailing fragment kept, and a
    book whose check never passes is never shipped."""
    path = build_epub(
        tmp_path / "story.epub",
        [
            Page("one.xhtml", f"<h1>Chapter One</h1><p>{LONG} {LONG}</p>"),
            Page("two.xhtml", "<h1>Chapter Two</h1><p>It ended without a period</p>"),
        ],
        toc=[NavPoint("Chapter One", "one.xhtml"), NavPoint("Chapter Two", "two.xhtml")],
        title="A Story",
    )
    book = read_book(path)
    with _server(tmp_path, fake_server, verify=lambda wav, text: "") as engine:
        result = convert_book(book, engine, tmp_path / "story.m4b", workdir=tmp_path / "work")
    assert result.output.is_file()
    voiced = " ".join(_voiced(tmp_path))
    assert "It ended without a period" in voiced
    for chunks in plan_chunks(book, ming.MAX_CHARS, list(range(len(book.chapters)))):
        for chunk in chunks:
            assert len(chunk.text) <= ming.MAX_CHARS
            assert chunk.text in voiced
    wavs = list((tmp_path / "work" / "chunks").glob("*.wav"))
    assert wavs
    assert {wav_info(wav).sample_rate for wav in wavs} == {44_100}

    with (
        _server(tmp_path, fake_server, verify=lambda wav, text: "skipped") as engine,
        pytest.raises(MissingAudioError),
    ):
        convert_book(book, engine, tmp_path / "again.m4b", workdir=tmp_path / "work-2")
    assert not (tmp_path / "again.m4b").exists()


# --- Hardware ------------------------------------------------------------------------------


def test_auto_ming_uses_the_card_like_kokoro(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DEVICES", _DEVICES)
    with AutoMing(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.backend == "vulkan"
        assert engine.device_label == "NVIDIA RTX PRO 1000"
        assert engine.warning is None
    config = _events(tmp_path, "start")[0]["config"]
    assert isinstance(config, dict)
    assert config["models"][0]["id"] == "ming"


def test_auto_ming_falls_back_to_the_cpu_like_kokoro(
    tmp_path: Path, fake_server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DEVICES", _DEVICES)
    monkeypatch.setenv("FAKE_CRASH_VULKAN", "1")
    with AutoMing(tmp_path / "data", executable=fake_server, startup_timeout=20) as engine:
        assert engine.backend == "cpu"
        assert engine.fell_back
        assert engine.warning
        assert engine.voice == ming.DEFAULT_VOICE  # still Ming, on the same build
    starts = _events(tmp_path, "start")
    assert [start["config"]["models"][0]["id"] for start in starts] == ["ming", "ming"]  # type: ignore[index]


def test_ming_checks_a_new_engine_build_with_its_own_voice(tmp_path: Path) -> None:
    check = ming.ming_check(tmp_path / "data", use_gpu=False)
    assert check.name == "Ming"
    assert check.uses == frozenset({AUDIOCPP_CPU.slot})
