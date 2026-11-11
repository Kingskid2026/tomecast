# Third-party engines and models

Tomecast's own code is licensed under [Apache 2.0](LICENSE). Tomecast does **not** include the engines and models below. It downloads them onto your computer when a feature needs them, and each one keeps its own license. Everything comes from its official source, except the audio.cpp voice engine, which Tomecast builds itself from audio.cpp's official source (see [below](#audiocpp-builds-distributed-by-tomecast)).

This list reflects the current design ([docs/architecture.md](docs/architecture.md)) and is updated as features land.

## Voice models

| Model | Used for | License | Source |
|---|---|---|---|
| Kokoro 82M | Default voice, any computer | Apache 2.0 | [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), GGUF from [audio-cpp](https://huggingface.co/audio-cpp/audio.cpp-gguf) |
| Ming-3.0-tts 0.5B | Premium voice, graphics card; downloaded only when the user chooses it (`ming-3.0-tts-0.5b-q8_0.gguf`, 1.74 GB, pinned by commit and SHA-256) | MIT | [inclusionAI/Ming-3.0-tts](https://github.com/inclusionAI/Ming-3.0-tts), GGUF from [audio-cpp](https://huggingface.co/audio-cpp/Ming-3.0-TTS-0.5B-GGUF) |

## Optional AI cleanup models

| Model | Used for | License | Source |
|---|---|---|---|
| Docling Heron | PDF page layout | Apache 2.0 | [docling-project/docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron) |
| PaddleOCR-VL-1.6 | Scanned PDF pages | Apache 2.0 | [PaddlePaddle/PaddleOCR-VL-1.6-GGUF](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF) |
| LFM3-1.7B | Spotting front/back matter and chapter starts | LFM Open License v1.0 (free for personal use and for organizations under US$10M annual revenue) | [LiquidAI/LFM3-1.7B-GGUF](https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF) |
| MiniCPM5-2B | Backup for LFM3 | Apache 2.0 | [openbmb/MiniCPM5-2B](https://huggingface.co/OpenBMB/MiniCPM5-2B) |

## Engines and tools

| Component | Used for | License | Source |
|---|---|---|---|
| audio.cpp | Running the voice models: a CPU build, and a Vulkan build for graphics cards (through the installed driver; downloaded only when the graphics card is used) | Apache 2.0 | Built by Tomecast from [0xShug0/audio.cpp](https://github.com/0xShug0/audio.cpp), see [below](#audiocpp-builds-distributed-by-tomecast) |
| llama.cpp | Running the text and OCR models | MIT | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) |
| ONNX Runtime | Running Docling Heron | MIT | [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) |
| python-docx | Reading DOCX documents (it also installs typing-extensions, PSF-2.0) | MIT | [python-openxml/python-docx](https://github.com/python-openxml/python-docx) |
| pypdfium2 5.14+ / PDFium | Reading PDFs. A Python dependency, installed from PyPI; its wheel carries a PDFium build | pypdfium2: Apache 2.0 or BSD-3-Clause. PDFium: BSD-3-Clause. The PDFium build also includes FreeType (FreeType License), ICU (Unicode License v3), libjpeg-turbo, OpenJPEG, libpng, Little CMS, zlib, Abseil and others under permissive licences, listed in the wheel's `licenses` folder | [pypdfium2](https://github.com/pypdfium2-team/pypdfium2), [PDFium](https://pdfium.googlesource.com/pdfium/), [pdfium-binaries](https://github.com/bblanchon/pdfium-binaries) |
| FFmpeg (via imageio-ffmpeg) | Building M4B/MP3 files; runs as a separate program | LGPL/GPL | [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) |
| eSpeak NG 1.52 (from the espeakng-loader wheel) | Turning text into sounds for Kokoro, needed for every sentence. Downloaded as a runtime file and used by audio.cpp, not imported by Tomecast | GPL-3.0-or-later | [espeak-ng](https://github.com/espeak-ng/espeak-ng), [espeakng-loader](https://pypi.org/project/espeakng-loader/) |
| uv | Installing Tomecast | Apache 2.0 or MIT | [astral-sh/uv](https://github.com/astral-sh/uv) |
| Qt 6 / PySide6-Essentials | The desktop window | LGPL-3.0 (installed from PyPI as a separate library, not modified) | [PySide6-Essentials](https://pypi.org/project/PySide6-Essentials/), [qt.io](https://www.qt.io/licensing/open-source-lgpl-obligations) |

## audio.cpp builds distributed by Tomecast

audio.cpp's own Linux binaries need glibc 2.43, which most distros don't have yet ([#18](https://github.com/Kingskid2026/tomecast/issues/18)). So Tomecast builds the same release itself and publishes the result as release assets of this repository, for example [audio.cpp 0.9.4 engine builds](https://github.com/Kingskid2026/tomecast/releases/tag/audiocpp-v0.9.4-tc1). Tomecast then downloads them like any other engine, pinned by SHA-256.

- **What they are:** `audiocpp_server` from audio.cpp's tagged release, built with upstream's own release build options. The other changes are about where it runs: it's built on an older system (manylinux_2_28, glibc 2.28) with the C++ and OpenMP runtimes linked in, and for any CPU with AVX2 instead of for the build machine's own CPU. The CPU build and the Vulkan build come from the same source; the Vulkan build uses the computer's own Vulkan loader and graphics driver and includes no driver.
- **Tomecast's patches:** `-tc1` is unmodified. From `-tc2` on, the source carries Tomecast's own small patches from [`.github/patches/audiocpp/`](.github/patches/audiocpp/), written for Tomecast and not part of upstream audio.cpp. Each is **opt-in**: unless its environment variables are set, the server behaves exactly like upstream's. So far there is one, `0001-ming-flow-steps.patch`, which lets Ming's flow sampler (used by Ming-3.0-tts, and by upstream's Ming3 Audio talker, which Tomecast doesn't load) run fewer flow-matching steps (`AUDIOCPP_MING_FLOW_STEPS`, `AUDIOCPP_MING_FLOW_SCHEDULE`) for about 1.55× the speed. The patches are under audio.cpp's license (Apache 2.0), the changed lines are marked in the code, and each tarball includes them in `patches/` and lists them in `BUILD-INFO.txt`.
- **The current build:** [`audiocpp-v0.9.4-tc1`](https://github.com/Kingskid2026/tomecast/releases/tag/audiocpp-v0.9.4-tc1), audio.cpp v0.9.4 at upstream commit [`f7ac446f`](https://github.com/0xShug0/audio.cpp/commit/f7ac446fb75e594a6913c846bea6c85b637f6ac0), built by [this workflow run](https://github.com/Kingskid2026/tomecast/actions/runs/42525635111) from Tomecast commit [`9033d73`](https://github.com/Kingskid2026/tomecast/commit/9033d73b1615dbaedb3a3026163a23de00da07e5).
- **How:** [`.github/workflows/engine-build.yml`](.github/workflows/engine-build.yml) and [`.github/scripts/build-audiocpp.sh`](.github/scripts/build-audiocpp.sh) do the whole build in GitHub Actions, from pinned sources (audio.cpp by commit, the build image by digest, everything else by SHA-256). Each tarball has a signed build provenance attestation, which `gh attestation verify FILE --repo Kingskid2026/tomecast` checks.
- **License:** audio.cpp is Apache 2.0. Each tarball includes audio.cpp's `LICENSE`, a `licenses/` folder with the licenses of the code compiled into it (ggml, sentencepiece and its bundled libraries, cJSON, cpp-httplib, libyaml, utf8proc, BoringSSL, and for the Vulkan build the Vulkan and SPIR-V headers), and `BUILD-INFO.txt` with the exact source commit, patches and build options. The GCC runtime libraries linked in (libstdc++, libgcc, libgomp) are covered by the GCC Runtime Library Exception.

## Data files we generate

| Item | Used for | License | Source |
|---|---|---|---|
| `src/tomecast/data/common_words.txt` | The name finder's list of ordinary English words, so it doesn't list them as names | Public domain: a list of words (every word that appears in lowercase at least 3 times) counted from 23 public-domain Project Gutenberg books | Rebuild with `scripts/make_wordlist.py`; book list in that script |

## Bundled with the app

These ship inside the Tomecast package itself (`src/tomecast/gui/`).

| Item | Used for | License | Source |
|---|---|---|---|
| Doto (font) | Big numerals and timers | SIL Open Font License 1.1 | See `src/tomecast/gui/fonts/README.md` |
| IBM Plex Sans, IBM Plex Mono (fonts) | Interface text, labels and data | SIL Open Font License 1.1 | See `src/tomecast/gui/fonts/README.md` |
| Luca della Robbia, *Cantoria* (1431–38), photograph | Drop screen art, shown as dots | Public domain | See `src/tomecast/gui/assets/CREDITS.md` |
