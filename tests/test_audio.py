import array
import math
import wave
from pathlib import Path

import pytest

from tomecast.audio import peak_level, wav_info, write_silence


def _write_tone(path: Path, seconds: float, sample_rate: int = 24000, volume: float = 0.5) -> None:
    frames = array.array(
        "h",
        (
            round(volume * 32767 * math.sin(2 * math.pi * 440 * i / sample_rate))
            for i in range(round(seconds * sample_rate))
        ),
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames.tobytes())


def test_wav_info_reads_the_header(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    _write_tone(path, seconds=0.5)
    info = wav_info(path)
    assert (info.sample_rate, info.channels, info.sample_width) == (24000, 1, 2)
    assert info.seconds == pytest.approx(0.5)


def test_silence_has_no_level(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    write_silence(path, seconds=1.0, sample_rate=24000)
    assert peak_level(path) == 0.0


def test_a_tone_has_a_clear_level(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    _write_tone(path, seconds=0.3, volume=0.5)
    assert peak_level(path) == pytest.approx(0.5 / math.sqrt(2), rel=0.05)
