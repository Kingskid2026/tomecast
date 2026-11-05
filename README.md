# Tomecast

**Turn ebooks into audiobooks on Linux, with natural-sounding voices that run entirely on your own computer.**

[![CI](https://github.com/Kingskid2026/tomecast/actions/workflows/ci.yml/badge.svg)](https://github.com/Kingskid2026/tomecast/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

> [!NOTE]
> **Early development.** Tomecast can already turn **EPUB** books into **M4B** audiobooks with chapters, using the Kokoro voice, from the command line. PDFs and other formats, smarter cleanup, the desktop window and the premium voice are on the [roadmap](https://github.com/Kingskid2026/tomecast/milestones). The voice engine currently needs a recent Linux (glibc 2.43+, see below).

## Try it

```sh
uv tool install tomecast                      # or: pipx install tomecast
tomecast convert book.epub                    # writes book.m4b next to the book
tomecast convert book.epub --voice bm_george --speed 1.1
tomecast voices                               # list the 54 built-in voices
```

The first conversion downloads the voice engine, eSpeak NG and the Kokoro model (about 264 MB, after asking). On an 8-core laptop CPU, a 13-minute story took about 3 minutes to make.

## What it does (and will do)

- **Reads the book, not the page.** Chapters come out as chapters. Page numbers, running headers and footers, footnote markers, copyright pages, indexes and "read the next book" previews are skipped.
- **Every sentence gets spoken.** Text is voiced in small, checked pieces, so the end of a page or chapter is never silently dropped.
- **Human-sounding, fully local voices.** [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) runs fast on any CPU. [Ming-3.0-tts](https://github.com/inclusionAI/Ming-3.0-tts) is an optional premium voice for computers with a graphics card. No cloud, no account, no subscription.
- **Many formats in:** EPUB, PDF (including scanned PDFs, with an optional AI add-on), DOCX, FB2, HTML and TXT.
- **Standard files out:** one M4B audiobook with chapter markers and cover art, or one MP3 per chapter. Play them in any audiobook app.
- **Fits the machine it's on.** On first launch, Tomecast checks the hardware and picks the right settings for a CPU-only laptop, an NVIDIA card or an AMD card. You can still switch the graphics card off.
- **Pause and resume.** Long books pick up where they left off after a crash or reboot.
- **Pronunciation fixes.** A word list for names the voice gets wrong, a finder for unusual names before you convert, and a button to hear any word right away.
- **One-line install.** It sets up its own engines and models, with no `sudo`.

## How it works

```mermaid
flowchart LR
    A[Ebook file] --> B[Reader<br/>chapters & paragraphs]
    B --> C[Cleanup<br/>rules + optional AI]
    C --> D[Text prep<br/>pronunciations, numbers]
    D --> E[Voice<br/>Kokoro or Ming]
    E --> F[Checks<br/>nothing missing]
    F --> G[M4B or MP3s]
```

The full design, including why each model and quantization was chosen, is in [docs/architecture.md](docs/architecture.md).

## Install (planned for v0.1.0)

```sh
curl -fsSL https://raw.githubusercontent.com/Kingskid2026/tomecast/main/install.sh | sh
```

The script is [short and readable](install.sh). It installs [uv](https://docs.astral.sh/uv/) if it's missing, then installs Tomecast from [PyPI](https://pypi.org/project/tomecast/) into your home folder. On first launch, Tomecast downloads only the engines and models your machine needs, and asks before downloading.

| Your system | Support |
|---|---|
| Distros with glibc 2.43 or newer, like Arch-based ones and openSUSE Tumbleweed (check with `ldd --version`) | Everything |
| Ubuntu 22.04 and 24.04, Debian 12 and 13, current Fedora | Installs, but the voice engine needs a newer system. Planned in [#18](https://github.com/Kingskid2026/tomecast/issues/18) |
| Alpine, NixOS, ARM computers | Not supported yet |

## Privacy

Everything runs locally. Tomecast uses the internet only to download engines and models (from their official sources, checked against pinned checksums) and for a once-a-day update check that you can turn off. There is no telemetry.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), and please follow the [Code of Conduct](CODE_OF_CONDUCT.md). To report a security problem, see [SECURITY.md](SECURITY.md).

## Credits and license

Tomecast's own code is licensed under the [Apache License 2.0](LICENSE). It builds on many open models and engines, each under its own license. They're listed in [THIRD_PARTY.md](THIRD_PARTY.md).
