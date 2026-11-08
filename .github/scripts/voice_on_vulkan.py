"""Voice a few sentences with the engine's Vulkan build, and check them against the CPU build.

Usage: GGML_VK_VISIBLE_DEVICES=0 python voice_on_vulkan.py WORK_FOLDER

Run it with the Python Tomecast is installed in, with both engine builds, eSpeak NG and Kokoro
already in Tomecast's data folder. CI has no graphics card, so it runs this in a container
with Mesa's software Vulkan driver (lavapipe): that way the Vulkan build's own shaders really
run, which starting it and listing its devices doesn't prove. ggml skips CPU-type Vulkan
devices like lavapipe unless GGML_VK_VISIBLE_DEVICES names them.

For each sentence it checks that:

* the engine reports the Vulkan backend (Tomecast's own check, so it fails rather than
  quietly running on the CPU);
* the audio passes the same checks a conversion uses (length for the text, not silence);
* it lasts about as long as the CPU build's audio for the same text, voice and seed. Kokoro
  predicts each sound's length itself, so broken maths on the graphics card shows up there.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Literal

from tomecast.audio import wav_info
from tomecast.convert import audio_problem
from tomecast.downloads import data_dir
from tomecast.engines.audiocpp import KokoroServer

SENTENCES = [
    "Alice was beginning to get very tired of sitting by her sister on the bank.",
    "“Curiouser and curiouser!” cried Alice.",
    # No closing punctuation, like the last fragment of a chapter.
    "The first question of course was, how to get dry again",
]
MAX_LENGTH_DIFFERENCE = 0.10  # Vulkan and CPU audio lengths may differ by this fraction
STARTUP_TIMEOUT = 900.0  # lavapipe compiles every shader on the CPU at start-up


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def voice(folder: Path, backend: Literal["cpu", "vulkan"]) -> list[float]:
    """Voice every sentence with ``backend``; return each one's length in seconds."""
    folder.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    lengths = []
    with KokoroServer(
        data_dir(),
        backend=backend,
        device=0 if backend == "vulkan" else None,
        startup_timeout=STARTUP_TIMEOUT,
        request_timeout=STARTUP_TIMEOUT,
    ) as engine:
        print(f"{backend}: engine started in {time.monotonic() - started:.1f} s")
        for index, text in enumerate(SENTENCES):
            wav = folder / f"{index}.wav"
            engine.synthesize(text, wav)
            problem = audio_problem(wav, text)
            if problem:
                fail(f"{backend} audio for {text!r}: {problem}")
            lengths.append(wav_info(wav).seconds)
            print(f"  {lengths[-1]:5.2f} s  {text}")
    return lengths


def main() -> None:
    work = Path(sys.argv[1])
    on_vulkan = voice(work / "vulkan", "vulkan")
    on_cpu = voice(work / "cpu", "cpu")
    for text, vulkan, cpu in zip(SENTENCES, on_vulkan, on_cpu, strict=True):
        if abs(vulkan - cpu) > MAX_LENGTH_DIFFERENCE * cpu:
            fail(f"{text!r} lasts {vulkan:.2f} s on Vulkan but {cpu:.2f} s on the CPU")
    print(f"OK: {len(SENTENCES)} sentences voiced on Vulkan, matching the CPU build's lengths")


if __name__ == "__main__":
    main()
