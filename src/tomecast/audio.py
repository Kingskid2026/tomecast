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


@dataclass(frozen=True, slots=True)
class WavData:
    """What a WAV file's audio holds, read to its end."""

    # Whole frames that are really there: fewer than the header says if the file was cut
    # short (a power cut can leave a file whose header was written but not all its data).
    frames: int
    peak: float  # see peak_level


def read_wav(path: Path) -> WavData:
    """Read all of a 16-bit PCM WAV's audio: how many frames are there, and how loud it is.
    Raises ``wave.Error``, ``EOFError`` or ``OSError`` if it can't be read, and
    ``ValueError`` if it isn't 16-bit. The file is streamed, so long files are fine."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected 16-bit audio")
        frame_size = 2 * wav.getnchannels()
        window = max(1, wav.getframerate() // 20) * wav.getnchannels()
        loudest = 0.0
        found = 0
        while frames := wav.readframes(_FRAMES_PER_READ):
            # Only the last read of a file that was cut short can end part-way through a frame.
            frames = frames[: len(frames) - len(frames) % frame_size]
            found += len(frames) // frame_size
            samples = array.array("h", frames)
            if sys.byteorder == "big":
                samples.byteswap()  # WAV data is little-endian
            for start in range(0, len(samples), window):
                part = samples[start : start + window]
                rms = math.sqrt(sum(s * s for s in part) / len(part))
                loudest = max(loudest, rms / 32768)
        return WavData(found, loudest)


def peak_level(path: Path) -> float:
    """The loudest RMS level of any 50 ms window, from 0.0 (silence) to 1.0 (full scale).

    Only 16-bit PCM is supported. The file is streamed, so long files are fine.
    """
    return read_wav(path).peak


def write_silence(path: Path, seconds: float, sample_rate: int) -> None:
    """Write a mono 16-bit WAV of silence (used by tests and as a placeholder)."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * round(seconds * sample_rate))


def concatenate_wavs(parts: list[Path], output: Path, gap_seconds: float = 0.0) -> None:
    """Join mono 16-bit WAVs with the same sample rate into one file, streaming."""
    if not parts:
        raise ValueError("nothing to concatenate")
    first = wav_info(parts[0])
    with wave.open(str(output), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(first.sample_rate)
        silence = b"\x00\x00" * round(gap_seconds * first.sample_rate)
        for index, part in enumerate(parts):
            with wave.open(str(part), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
                    1,
                    2,
                    first.sample_rate,
                ):
                    raise ValueError(f"{part.name}: format differs from {parts[0].name}")
                while frames := wav.readframes(_FRAMES_PER_READ):
                    out.writeframes(frames)
            if index < len(parts) - 1:
                out.writeframes(silence)
