"""What the judge downloads: llama.cpp's official CPU build and the model files, pinned.

Data only, like :mod:`tomecast.manifest`. These aren't in ``manifest.ASSETS`` yet, so
``tomecast doctor`` and a normal conversion never fetch them; they move there when the AI
pass is wired into the window and the terminal command.

- **llama.cpp:** the official prebuilt ``ubuntu-x64`` CPU build from ggml-org's releases. Its
  programs need glibc 2.34 and the system's libstdc++ from GCC 12 or newer (``GLIBCXX_3.4.30``,
  checked with ``objdump -T``), the same glibc floor as Tomecast's window. It holds CPU
  kernels for every x86-64 level and picks the best one at run time. LFM3 needs b11960 or
  newer; the NVFP4 file (not used) needs b12088. The SHA-256 is the digest GitHub publishes
  for the release asset.
- **LFM3-1.7B QAD-Q4_0:** Liquid AI's own file, trained to recover 4-bit losses (QAD). LFM
  Open License v1.0: commercial use is only licensed for a person or company with **under
  US$10M annual revenue**; non-commercial use has no cap. Downloaded by the user, on first
  use, so Tomecast never redistributes it.
- **MiniCPM5-2B Q4_K_M:** OpenBMB's own GGUF, Apache 2.0, the backup.
"""

from __future__ import annotations

from tomecast.downloads import Asset

LLAMA_CPP_BUILD = "b12282"

LLAMA_CPP_CPU = Asset(
    id="llama-cpp-cpu",
    name=f"llama.cpp {LLAMA_CPP_BUILD} (CPU)",
    url=(
        f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_BUILD}/"
        f"llama-{LLAMA_CPP_BUILD}-bin-ubuntu-x64.tar.gz"
    ),
    sha256="26f7e67260769ea02db1972b0536cc881b5f13fc30a1caecd8cb3f070c9d5200",
    size=23_283_478,
    path="engines/llama-cpp-cpu",
    archive="tar.gz",
    opt_in=True,
)

# The program inside the archive, relative to the extracted build folder.
LLAMA_SERVER = f"llama-{LLAMA_CPP_BUILD}/llama-server"

LFM3_QAD_Q4_0 = Asset(
    id="lfm3-1.7b-qad-q4_0",
    name="LFM3-1.7B judge model (4-bit, QAD)",
    url=(
        "https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF/resolve/"
        "cd0c74c575b5c416f3011b5b4c298077f26ca8c2/LFM3-1.7B-QAD-Q4_0.gguf"
    ),
    sha256="43a7d09a0e9cba91b63eb7052488b9564569bddfd65754d2e426465e572a7452",
    size=1_049_615_680,
    path="models/LFM3-1.7B-QAD-Q4_0.gguf",
    opt_in=True,
)

MINICPM5_Q4_K_M = Asset(
    id="minicpm5-2b-q4_k_m",
    name="MiniCPM5-2B judge model (4-bit, backup)",
    url=(
        "https://huggingface.co/openbmb/MiniCPM5-2B-GGUF/resolve/"
        "8ad02b786ab81e33d465e1c80562640c55ab2877/MiniCPM5-2B-Q4_K_M.gguf"
    ),
    sha256="ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd",
    size=1_561_318_368,
    path="models/MiniCPM5-2B-Q4_K_M.gguf",
    opt_in=True,
)

JUDGE_ASSETS: tuple[Asset, ...] = (LLAMA_CPP_CPU, LFM3_QAD_Q4_0)
