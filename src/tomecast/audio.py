"""Small helpers for checking the WAV files voice engines produce."""

from __future__ import annotations

import array
import math
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

_FRAMES_PER_READ = 65536


@dataclass(frozen=True, slots=True)
class WavInfo:
    """Basic facts about a WAV file."""

    sample_rate: int
    channels: int
    sample_width: int  # bytes per sample
    frames: int

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def wav_info(path: Path) -> WavInfo:
    """Read a WAV file's header. Raises ``wave.Error`` or ``OSError`` if it isn't valid."""
    with wave.open(str(path), "rb") as wav:
        return WavInfo(wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes())


def peak_level(path: Path) -> float:
    """The loudest RMS level of any 50 ms window, from 0.0 (silence) to 1.0 (full scale).

    Only 16-bit PCM is supported. The file is streamed, so long files are fine.
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected 16-bit audio")
        window = max(1, wav.getframerate() // 20) * wav.getnchannels()
        loudest = 0.0
        while frames := wav.readframes(_FRAMES_PER_READ):
            samples = array.array("h", frames)
            if sys.byteorder == "big":
                samples.byteswap()  # WAV data is little-endian
            for start in range(0, len(samples), window):
                part = samples[start : start + window]
                rms = math.sqrt(sum(s * s for s in part) / len(part))
                loudest = max(loudest, rms / 32768)
        return loudest


def write_silence(path: Path, seconds: float, sample_rate: int) -> None:
    """Write a mono 16-bit WAV of silence (used by tests and as a placeholder)."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * round(seconds * sample_rate))
