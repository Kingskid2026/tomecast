"""Tests for reading scanned PDF pages: finding them, drawing them, checking and keeping text.

No model runs here: a fake runner stands in for PaddleOCR-VL. The text is from O. Henry's
"The Gift of the Magi" (1905), which is in the public domain.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from pdf_factory import PAGE_HEIGHT, PAGE_WIDTH, PageBuilder, build_pdf

from tomecast.book import Band
from tomecast.llama_ocr import (
    PROMPT,
    LlamaOcrConfig,
    OcrServerError,
    parse_reply,
    request_body,
    server_command,
)
from tomecast.readers.scanned import (
    MAX_PIXELS,
    OcrResult,
    OcrRunner,
    PageImage,
    ScannedPage,
    estimate_seconds,
    page_blocks,
    page_problem,
    read_scanned,
    render_page,
    textless_pages,
    unknown_word_share,
)

_KNOWN_TEXT = (
    "one dollar and eighty seven cents that was all sixty of it in pennies she had been "
    "saving every penny could for months with this result there is clearly nothing to do "
    "but flop down on the shabby little couch howl so did"
)
KNOWN = frozenset(_KNOWN_TEXT.split())

PAGE_TEXT = """THE GIFT OF THE MAGI

One dollar and eighty-seven cents. That was all. And sixty cents of it was in pen-
nies.

There was clearly nothing to do but flop down on the shabby little couch and howl.
So Della did it.

[15]"""


def _scan_pdf(tmp_path: Path) -> Path:
    """Page 1: text. Page 2: a full-page picture. Page 3: blank. Page 4: a small logo only."""
    text = PageBuilder().paragraph("One dollar and eighty-seven cents. That was all.")
    scan = PageBuilder().picture(0, 0, PAGE_WIDTH, PAGE_HEIGHT)
    blank = PageBuilder()
    logo = PageBuilder().picture(100, 100, 50, 50)
    return build_pdf(tmp_path / "mixed.pdf", [text.page, scan.page, blank.page, logo.page])


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.replace("-\n", ""))


# --- Finding and drawing ---------------------------------------------------------------------


def test_only_picture_pages_without_text_are_found(tmp_path: Path) -> None:
    document = pdfium.PdfDocument(_scan_pdf(tmp_path))
    try:
        assert textless_pages(document) == [1]
    finally:
        document.close()


def test_a_page_is_drawn_within_the_pixel_limit_and_keeps_its_shape(tmp_path: Path) -> None:
    document = pdfium.PdfDocument(_scan_pdf(tmp_path))
    try:
        image = render_page(document, 1)
        small = render_page(document, 1, max_pixels=40_000)
    finally:
        document.close()
    assert image.page == 1
    assert image.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert 0.95 * MAX_PIXELS <= image.width * image.height <= MAX_PIXELS
    assert image.height / image.width == pytest.approx(PAGE_HEIGHT / PAGE_WIDTH, rel=0.01)
    assert small.width * small.height <= 40_000


# --- Checking what came back -----------------------------------------------------------------


def test_names_count_as_words_but_nonsense_does_not() -> None:
    assert unknown_word_share("Della had been saving every penny", KNOWN) == 0.0
    assert unknown_word_share("Della hqd bccn saving", KNOWN) == 0.5
    assert unknown_word_share("", KNOWN) == 0.0


@pytest.mark.parametrize(
    ("result", "problem"),
    [
        (OcrResult("  \n", 1.0), "no text"),
        (OcrResult("One dollar", 1.0, truncated=True), "cut off at the token limit"),
        (OcrResult("so did\n" * 5, 1.0), "a line repeated 4 times"),
        (OcrResult("Onc dcllar and eiqhty sevcn ccnts", 1.0), "67% of its words aren't words"),
    ],
)
def test_broken_pages_are_spotted(result: OcrResult, problem: str) -> None:
    assert page_problem(result, KNOWN) == problem


def test_a_good_page_has_no_problem() -> None:
    assert page_problem(OcrResult(PAGE_TEXT, 1.0), KNOWN) is None


# --- Turning text into blocks ----------------------------------------------------------------


def test_header_and_page_number_become_bands_and_paragraphs_stay_whole() -> None:
    blocks = page_blocks(ScannedPage(14, PAGE_TEXT, 1.0, reread=False, problem=None))
    assert [block.band for block in blocks] == [Band.TOP, None, None, Band.BOTTOM]
    assert blocks[0].lines == ("THE GIFT OF THE MAGI",)
    assert blocks[1].lines == (
        "One dollar and eighty-seven cents. That was all. And sixty cents of it was in pen-",
        "nies.",
    )
    assert blocks[1].continues and not blocks[2].continues
    assert blocks[3].lines == ("[15]",)
    assert {block.page for block in blocks} == {14}


def test_no_word_the_model_read_is_lost() -> None:
    for text in (PAGE_TEXT, "Just one line", "12\n\nA page with its number on top.", "\n\n"):
        blocks = page_blocks(ScannedPage(0, text, 1.0, reread=False, problem=None))
        kept = "\n".join("\n".join(block.lines) for block in blocks)
        assert _words(kept) == _words(text)


def test_a_one_line_page_is_not_mistaken_for_a_header() -> None:
    blocks = page_blocks(ScannedPage(0, "THE END", 1.0, reread=False, problem=None))
    assert [(block.lines, block.band) for block in blocks] == [(("THE END",), None)]


# --- Reading with the light model, then the full one -----------------------------------------


class _FakeRunner:
    def __init__(self, name: str, texts: dict[int, str], log: list[str]) -> None:
        self.name, self.texts, self.log = name, texts, log

    def read(self, image: PageImage) -> OcrResult:
        self.log.append(f"{self.name} reads {image.page}")
        return OcrResult(self.texts[image.page], 2.0)


def _factory(name: str, texts: dict[int, str], log: list[str]):  # type: ignore[no-untyped-def]
    @contextmanager
    def open_runner() -> Iterator[OcrRunner]:
        log.append(f"{name} opens")
        yield _FakeRunner(name, texts, log)
        log.append(f"{name} closes")

    return open_runner


def _two_scans(tmp_path: Path) -> Path:
    pages = [PageBuilder().picture(0, 0, PAGE_WIDTH, PAGE_HEIGHT).page for _ in range(2)]
    return build_pdf(tmp_path / "scan.pdf", pages)


def test_only_broken_pages_are_reread_and_one_model_is_loaded_at_a_time(tmp_path: Path) -> None:
    log: list[str] = []
    progress: list[tuple[int, int]] = []
    light = _factory("light", {0: PAGE_TEXT, 1: "Onc dcllar eiqhty"}, log)
    full = _factory("full", {1: "One dollar and eighty-seven cents."}, log)
    pages = read_scanned(
        _two_scans(tmp_path), light, full, KNOWN, progress=lambda *done: progress.append(done)
    )
    assert log == [
        "light opens", "light reads 0", "light reads 1", "light closes",
        "full opens", "full reads 1", "full closes",
    ]  # fmt: skip
    assert progress == [(1, 2), (2, 2)]
    assert [(page.page, page.reread, page.problem, page.seconds) for page in pages] == [
        (0, False, None, 2.0),
        (1, True, None, 4.0),
    ]
    assert pages[1].text == "One dollar and eighty-seven cents."


def test_without_a_full_model_a_broken_page_keeps_its_problem(tmp_path: Path) -> None:
    log: list[str] = []
    light = _factory("light", {0: PAGE_TEXT, 1: ""}, log)
    pages = read_scanned(_two_scans(tmp_path), light, None, KNOWN)
    assert pages[1].problem == "no text" and not pages[1].reread


def test_an_empty_reread_does_not_replace_text(tmp_path: Path) -> None:
    log: list[str] = []
    light = _factory("light", {0: PAGE_TEXT, 1: "Onc dcllar eiqhty"}, log)
    full = _factory("full", {1: ""}, log)
    pages = read_scanned(_two_scans(tmp_path), light, full, KNOWN)
    assert pages[1].text == "Onc dcllar eiqhty" and pages[1].reread
    assert pages[1].problem == "67% of its words aren't words"


def test_the_estimate_counts_the_rereads() -> None:
    assert estimate_seconds(300, 40.0, reread_share=0.0) == 12_000.0
    assert estimate_seconds(300, 40.0, reread_share=0.1, full_slowdown=1.5) == pytest.approx(
        13_800.0
    )


# --- The llama.cpp runner's plumbing (no server is started) ----------------------------------


def _config(gpu_layers: int = 0) -> LlamaOcrConfig:
    return LlamaOcrConfig(Path("llama-server"), Path("m.gguf"), Path("v.gguf"), 4, gpu_layers)


def test_the_cpu_command_keeps_everything_off_the_graphics_card() -> None:
    command = server_command(_config(), 8123)
    assert command[:5] == ["llama-server", "--model", "m.gguf", "--mmproj", "v.gguf"]
    assert command[5:7] == ["--host", "127.0.0.1"] and "8123" in command
    assert "--device" in command and "--no-mmproj-offload" in command
    assert "--device" not in server_command(_config(gpu_layers=99), 8123)


def test_the_request_sends_the_page_as_a_png_with_the_ocr_prompt() -> None:
    body = request_body(PageImage(0, 1, 1, b"\x89PNG"), 512)
    content = body["messages"][0]["content"]
    assert content[0]["image_url"]["url"] == "data:image/png;base64,iVBORw=="
    assert content[1] == {"type": "text", "text": PROMPT}
    assert body["temperature"] == 0 and body["max_tokens"] == 512


def test_a_reply_cut_off_at_the_limit_is_marked() -> None:
    reply = {"choices": [{"message": {"content": "One dollar"}, "finish_reason": "length"}]}
    assert parse_reply(reply, 3.0) == OcrResult("One dollar", 3.0, truncated=True)
    reply["choices"][0]["finish_reason"] = "stop"
    assert not parse_reply(reply, 3.0).truncated
    with pytest.raises(OcrServerError):
        parse_reply({"choices": []}, 1.0)
