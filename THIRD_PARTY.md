# Third-party engines and models

Tomecast's own code is licensed under [Apache 2.0](LICENSE). Tomecast does **not** include the engines and models below. It downloads them from their official sources onto your computer when a feature needs them, and each one keeps its own license.

This list reflects the current design ([docs/architecture.md](docs/architecture.md)) and is updated as features land.

## Voice models

| Model | Used for | License | Source |
|---|---|---|---|
| Kokoro 82M | Default voice, any computer | Apache 2.0 | [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), GGUF from [audio-cpp](https://huggingface.co/audio-cpp/audio.cpp-gguf) |
| Ming-3.0-tts 0.5B | Premium voice, graphics card | MIT | [inclusionAI/Ming-3.0-tts](https://github.com/inclusionAI/Ming-3.0-tts), GGUF from [audio-cpp](https://huggingface.co/audio-cpp/Ming-3.0-TTS-0.5B-GGUF) |

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
| audio.cpp | Running the voice models | Apache 2.0 | [0xShug0/audio.cpp](https://github.com/0xShug0/audio.cpp) |
| llama.cpp | Running the text and OCR models | MIT | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) |
| ONNX Runtime | Running Docling Heron | MIT | [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) |
| pypdfium2 / PDFium | Reading PDFs | Apache 2.0 or BSD-3-Clause / BSD-3-Clause | [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) |
| FFmpeg (via imageio-ffmpeg) | Building M4B/MP3 files; runs as a separate program | LGPL/GPL | [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) |
| eSpeak NG 1.52 (from the espeakng-loader wheel) | Turning text into sounds for Kokoro, needed for every sentence. Downloaded as a runtime file and used by audio.cpp, not imported by Tomecast | GPL-3.0-or-later | [espeak-ng](https://github.com/espeak-ng/espeak-ng), [espeakng-loader](https://pypi.org/project/espeakng-loader/) |
| uv | Installing Tomecast | Apache 2.0 or MIT | [astral-sh/uv](https://github.com/astral-sh/uv) |
