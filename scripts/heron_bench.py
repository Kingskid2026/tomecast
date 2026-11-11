"""Measure Docling Heron on CPU: speed per page, FP32 against INT8, and peak memory (#12).

    uv run python -I scripts/heron_bench.py compare FP32.onnx INT8.onnx [INT8b.onnx] \\
        --pdf BOOK.pdf --pages 20 --threads 4
    uv run python -I scripts/heron_bench.py memory MODEL.onnx --pdf BOOK.pdf
    uv run python -I scripts/heron_bench.py read FP32.onnx --pdf BOOK.pdf

``compare`` renders each page once, runs every model on it (after a warm-up), and reports
the time per page and, for each INT8 file against FP32: how many boxes match (IoU 0.5 or
more), how many of those have the same label, their mean IoU, and how many of the page's
words get the same label (the number that matters: it's what decides what gets read).
``memory`` runs one model on a few pages in this process and prints its peak memory (run it
once per model). ``read`` reads whole PDFs with the AI pass and with the rules alone and
compares what each leaves out. Run under ``taskset -c 0-3`` to keep to four CPUs.
"""

from __future__ import annotations

import argparse
import resource
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


def _pages(pdf: Path, count: int) -> list[int]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    total = len(document)
    document.close()
    if count <= 0 or count >= total:
        return list(range(total))
    step = total / count
    return sorted({int(i * step) for i in range(count)})


def _word_labels(chars: Any, regions: Any) -> list[str]:
    from tomecast.readers.pdf_layout import _choose, page_words

    words = page_words(chars)
    labels = []
    for word in words:
        index = _choose(word.box(), word.size, regions)
        labels.append(regions[index].label.value if index >= 0 else "orphan")
    return labels


def compare(args: argparse.Namespace) -> None:
    import pypdfium2 as pdfium

    from tomecast.readers.heron import HeronModel, iou, to_regions
    from tomecast.readers.pdf import _page_chars

    models = [HeronModel(path, threads=args.threads) for path in args.models]
    names = [path.name for path in args.models]
    times: dict[str, list[float]] = {name: [] for name in names}
    render_times: list[float] = []
    stats = {name: Counter[str]() for name in names[1:]}
    ious: dict[str, list[float]] = {name: [] for name in names[1:]}
    labels_seen: Counter[str] = Counter()
    warmed = False
    for pdf in args.pdf:
        document = pdfium.PdfDocument(pdf)
        for index in _pages(pdf, args.pages):
            page = document[index]
            started = time.perf_counter()
            pixels = models[0].render(page)
            render_times.append(time.perf_counter() - started)
            if not warmed:
                for model in models:
                    model.run(pixels)
                warmed = True
            outputs = []
            for name, model in zip(names, models, strict=True):
                started = time.perf_counter()
                raw = model.run(pixels)
                times[name].append(time.perf_counter() - started)
                outputs.append(to_regions(page, *raw))
            textpage = page.get_textpage()
            chars, _ = _page_chars(textpage)
            textpage.close()
            reference = outputs[0]
            labels_seen.update(region.label.value for region in reference)
            reference_words = _word_labels(chars, reference)
            for name, regions in zip(names[1:], outputs[1:], strict=True):
                counts = stats[name]
                counts["fp32 boxes"] += len(reference)
                counts["int8 boxes"] += len(regions)
                for region in reference:
                    best = max(regions, key=lambda other: iou(region, other), default=None)
                    overlap = iou(region, best) if best is not None else 0.0
                    if best is not None and overlap >= 0.5:
                        counts["matched"] += 1
                        counts["same label"] += best.label is region.label
                        ious[name].append(overlap)
                words = _word_labels(chars, regions)
                counts["words"] += len(words)
                counts["same word label"] += sum(
                    a == b for a, b in zip(reference_words, words, strict=True)
                )
            page.close()
        document.close()

    pages = len(render_times)
    print(f"pages: {pages}, threads: {args.threads}")
    print(f"render to 640x640: median {1000 * statistics.median(render_times):.1f} ms")
    for name in names:
        values = times[name]
        print(
            f"{name}: median {1000 * statistics.median(values):.0f} ms/page, "
            f"mean {1000 * statistics.mean(values):.0f}, "
            f"min {1000 * min(values):.0f}, max {1000 * max(values):.0f}"
        )
    print("FP32 labels:", dict(labels_seen.most_common()))
    for name in names[1:]:
        counts = stats[name]
        print(
            f"{name} vs FP32: boxes {counts['int8 boxes']} vs {counts['fp32 boxes']}, "
            f"matched {counts['matched']}/{counts['fp32 boxes']} (IoU>=0.5), "
            f"same label {counts['same label']}/{counts['matched']}, "
            f"mean IoU {statistics.mean(ious[name]) if ious[name] else 0:.3f}, "
            f"words with the same label {counts['same word label']}/{counts['words']} "
            f"({100 * counts['same word label'] / max(1, counts['words']):.2f}%)"
        )


def memory(args: argparse.Namespace) -> None:
    import numpy  # noqa: F401  (counted in the baseline)
    import onnxruntime  # noqa: F401
    import pypdfium2 as pdfium

    from tomecast.readers.heron import HeronModel

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    model = HeronModel(args.models[0], threads=args.threads)
    loaded = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    for pdf in args.pdf:
        document = pdfium.PdfDocument(pdf)
        for index in _pages(pdf, args.pages):
            page = document[index]
            model.detect(page, *page.get_size())
            page.close()
        document.close()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(
        f"{args.models[0].name}: peak {peak:.0f} MB (python + numpy + onnxruntime "
        f"{before:.0f} MB, after loading {loaded:.0f} MB, model + running "
        f"{peak - before:.0f} MB)"
    )


def read(args: argparse.Namespace) -> None:
    from tomecast.cleanup import clean
    from tomecast.readers.heron import HeronModel
    from tomecast.readers.pdf import read_pdf
    from tomecast.readers.pdf_layout import HeronLayout

    for pdf in args.pdf:
        started = time.perf_counter()
        rules = clean(read_pdf(pdf))
        rules_time = time.perf_counter() - started
        layout = HeronLayout(HeronModel(args.models[0], threads=args.threads))
        started = time.perf_counter()
        model = clean(read_pdf(pdf, layout=layout))
        model_time = time.perf_counter() - started
        pages = len(layout.pages) + len(layout.fallbacks)

        def words(result: Any) -> int:
            return sum(
                len(paragraph.split())
                for index in result.kept
                for paragraph in result.book.chapters[index].spoken_paragraphs()
            )

        print(f"== {pdf.name}: {pages} pages with text")
        print(f"rules: {rules_time:.1f} s, {words(rules)} words spoken; left out:", rules.summary())
        print(
            f"heron: {model_time:.1f} s ({1000 * layout.seconds / max(1, pages):.0f} ms/page "
            f"in the AI pass), {words(model)} words spoken; left out:",
            model.summary(),
        )
        print(f"pages left to the rules: {len(layout.fallbacks)}")
        for page, reason in layout.fallbacks[:5]:
            print(f"  page {page + 1}: {reason}")
        if args.show:
            for removal in model.removed:
                if removal.rule in ("page-numbers", "footnotes", "captions", "tables"):
                    print(f"  heron {removal.rule} p{removal.page}: {removal.text[:70]!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("compare", "memory", "read"))
    parser.add_argument("models", type=Path, nargs="+")
    parser.add_argument("--pdf", type=Path, action="append", default=[])
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    {"compare": compare, "memory": memory, "read": read}[args.mode](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
