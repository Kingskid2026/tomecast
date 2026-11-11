"""Every engine and model Tomecast downloads, pinned by URL, size and SHA-256.

This is the one place to change a pin. It holds data only, so a pull request that moves to a
new engine build or model changes this file and nothing else (plus a note in the changelog
and THIRD_PARTY.md if something new is added). Users only ever get what is pinned here:
never "latest".

To move to a new audio.cpp build (see docs/architecture.md, "Engines and models"): publish it
with .github/workflows/engine-build.yml, then change ``AUDIOCPP_VERSION``/``AUDIOCPP_BUILD``
and the two SHA-256s and sizes below from the release's ``SHA256SUMS``. Users' computers keep
their current build until the new one passes a self-check (:func:`tomecast.downloads.prepare`).

Which of these a run needs is decided elsewhere (``required_assets`` in
:mod:`tomecast.engines.audiocpp`). A voice that is downloaded only when the user chooses it
(the premium Ming-3.0-tts voice, about 1.74 GB) is pinned here too, with ``opt_in=True``.
"""

from __future__ import annotations

from tomecast.downloads import Asset

AUDIOCPP_VERSION = "0.9.4"
# Tomecast's own build of that release, published as a release of the Tomecast repo by
# .github/workflows/engine-build.yml. A new build of the same source gets a new -tcN tag.
AUDIOCPP_BUILD = f"audiocpp-v{AUDIOCPP_VERSION}-tc1"
_ENGINE_DOWNLOADS = "https://github.com/Kingskid2026/tomecast/releases/download"


def _engine_build(variant: str, label: str, *, sha256: str, size: int) -> Asset:
    """One of the engine tarballs (``cpu``, ``vulkan``) published with :data:`AUDIOCPP_BUILD`."""
    return Asset(
        id=f"audiocpp-{variant}",
        name=f"audio.cpp {AUDIOCPP_VERSION} voice engine ({label})",
        url=f"{_ENGINE_DOWNLOADS}/{AUDIOCPP_BUILD}/{AUDIOCPP_BUILD}-linux-x86_64-{variant}.tar.gz",
        sha256=sha256,
        size=size,
        path=f"engines/audiocpp-{variant}",
        archive="tar.gz",
        # Tomecast 0.1.x put upstream's build of 0.9.4 here.
        replaces=(f"engines/audiocpp-0.9.4-{variant}",),
    )


AUDIOCPP_CPU = _engine_build(
    "cpu",
    "CPU",
    sha256="fb4465a856fbae3843ef506706b5dc145300b95f55dc2b2ae068196f570200db",
    size=27_434_594,
)

# The same audio.cpp, built with Vulkan. It also contains the CPU backend, so a machine that
# uses the graphics card downloads only this one. It loads the system's own Vulkan loader
# (libvulkan.so.1) and graphics driver (NVIDIA's own, or Mesa for AMD and Intel), so it only
# starts where a Vulkan loader is installed; Tomecast never manages drivers.
AUDIOCPP_VULKAN = _engine_build(
    "vulkan",
    "graphics card",
    sha256="8466f9c628d1635e52ccf30e9a8f98dfc1cb7b76093252d762c22f87a0664742",
    size=44_609_493,
)

ESPEAK_NG = Asset(
    id="espeak-ng",
    name="eSpeak NG 1.52 pronunciation rules",
    url=(
        "https://files.pythonhosted.org/packages/de/1e/"
        "25ec5ab07528c0fbb215a61800a38eca05c8a99445515a02d7fa5debcb32/"
        "espeakng_loader-0.2.4-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    ),
    sha256="08721baf27d13d461f6be6eed9a65277e70d68234ff484fd8b9897b222cdcb6d",
    size=10_078_484,
    path="engines/espeak-ng",
    archive="zip",
    replaces=("engines/espeak-ng-1.52",),  # where Tomecast 0.1.x put this same wheel
)

KOKORO_Q8 = Asset(
    id="kokoro-82m-q8_0",
    name="Kokoro voice model (82M, 8-bit)",
    url=(
        "https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/"
        "fd2fe4c3195bc410f8b817bf7c7519fd822686fb/Kokoro-82M-GGUF/kokoro-82m-q8_0.gguf"
    ),
    sha256="9a4ca6176460cdc8c7845b804c0bbe85a4791ee3ffff568a3fdadbb708c63f47",
    size=189_550_976,
    path="models/kokoro-82m-q8_0.gguf",
)

# The optional AI pass for PDF page layout (issue #12): Docling Heron, the official ONNX
# export of docling-project/docling-layout-heron (Apache 2.0), FP32. Downloaded only when the
# user turns the AI pass on, and only needed with the "layout" extra (onnxruntime). Not in
# ASSETS yet: `tomecast doctor` and the hardware scan don't know about the AI pass.
HERON_LAYOUT = Asset(
    id="docling-layout-heron",
    name="Docling Heron page layout model (43M, FP32 ONNX)",
    url=(
        "https://huggingface.co/docling-project/docling-layout-heron-onnx/resolve/"
        "40bde044036bb181c130ddf6c51792187268748f/model.onnx"
    ),
    sha256="59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a",
    size=171_220_471,
    path="models/docling-layout-heron.onnx",
    opt_in=True,
)

# Everything pinned, in the order `tomecast doctor` lists it.
ASSETS: tuple[Asset, ...] = (AUDIOCPP_CPU, AUDIOCPP_VULKAN, ESPEAK_NG, KOKORO_Q8)
