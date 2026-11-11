"""Measure the judge on public-domain books: accuracy, CPU speed and peak memory.

Not part of the app or the test suite (it runs a real model). Usage::

    uv run python scripts/eval_judge.py --server PATH/llama-server --model PATH/model.gguf \\
        --threads 4 --out results.json BOOK.txt [BOOK.txt ...]

Each book is read with Tomecast's own TXT reader. The first ``--chapters`` chapters are
flattened into one run of lines with the chapter titles as lines of their own, as an
untagged book would look, and the judge labels them. The expected labels come from
:func:`gold` (rules written for these Gutenberg texts, plus "either" for lines where both
answers are fine), so the counts below are only as good as those rules; the script prints
every disagreement so they can be checked by eye.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Sequence
from pathlib import Path

from tomecast.ai.judge import JudgeOptions, Stats, Verdict, apply_verdicts, judge_lines
from tomecast.ai.llama import LlamaServer
from tomecast.readers.txt import read_txt

KEEP, DROP, START, EITHER = "keep", "drop", "chapter_start", "either"

_CHAPTER = re.compile(r"^(?:CHAPTER|Chapter) [IVXLC]+\.?\]?$")
_STORY = re.compile(r"^[IVX]+\. [A-Z][A-Z \-\u2019'.,]+$")  # "I. A SCANDAL IN BOHEMIA"
_SECTION = re.compile(r"^[IVX]+\.$")  # a numbered section inside a story
_LISTED = re.compile(r"^.{1,80}\s(?:[ivxlc]+|\d+)$")  # "Mr. and Mrs. Bennet 5"


def flatten(path: Path, chapters: int, limit: int) -> list[str]:
    """The book's first chapters as one run of lines, titles included, at most ``limit``."""
    book = read_txt(path)
    lines: list[str] = []
    for chapter in book.chapters[:chapters]:
        if chapter.title:
            lines.append(chapter.title)
        lines.extend(chapter.paragraphs)
    return lines[:limit]


def gold(lines: Sequence[str]) -> list[str]:
    """The expected label of each line (see the module docstring)."""
    labels: list[str] = []
    in_list = False  # inside a contents page or list of illustrations
    in_picture = False  # inside a "[Illustration: ..." note that runs over several lines
    seen_body = False  # past the first chapter heading
    for line in lines:
        text = line.strip()
        if _CHAPTER.match(text) or (_STORY.match(text) and not in_list):
            # Checked first: a heading can close a picture note ("Chapter I.]").
            in_list = in_picture = False
            seen_body = True
            labels.append(START)
        elif in_picture or text.startswith("[Illustration"):
            # Still open: the note goes on until a line ends with its closing bracket.
            in_picture = not text.endswith("]") if in_picture else text.count("[") > text.count("]")
            # A long line in a picture note (a dedication) may be read or skipped.
            labels.append(EITHER if len(text.split()) > 12 else DROP)
        elif text in {"Contents", "CONTENTS", "PAGE"}:
            in_list = True
            labels.append(DROP)
        elif text.startswith("I. A Scandal in Bohemia II."):  # a contents page in one line
            in_list = False
            labels.append(DROP)
        elif text in {"* * *", "*"}:
            labels.append(DROP)
        elif _SECTION.match(text):
            labels.append(EITHER)  # a section number: kept, or a chapter start, both fine
        elif in_list and _LISTED.match(text):
            labels.append(DROP)
        elif not seen_body and len(text.split()) <= 12:
            labels.append(EITHER)  # title page, printer, dedication: read or skipped
        else:
            in_list = in_list and len(text.split()) <= 12
            labels.append(KEEP)
    return labels


def predicted(
    lines: Sequence[str], verdicts: dict[int, Verdict], options: JudgeOptions
) -> list[str]:
    edit = apply_verdicts(lines, verdicts, options)
    labels = [KEEP] * len(lines)
    for index in edit.dropped:
        labels[index] = DROP
    for index in edit.starts:
        labels[index] = START
    return labels


def score(expected: Sequence[str], got: Sequence[str], lines: Sequence[str]) -> dict[str, object]:
    def counts(label: str) -> dict[str, float]:
        relevant = [i for i, e in enumerate(expected) if e != EITHER]
        tp = sum(1 for i in relevant if expected[i] == label and got[i] == label)
        fp = sum(1 for i in relevant if expected[i] != label and got[i] == label)
        fn = sum(1 for i in relevant if expected[i] == label and got[i] != label)
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
            "recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        }

    body_dropped = [i for i, e in enumerate(expected) if e == KEEP and got[i] == DROP]
    return {
        "lines": len(lines),
        "gold": {label: expected.count(label) for label in (KEEP, DROP, START, EITHER)},
        "drop": counts(DROP),
        "chapter_start": counts(START),
        "body_lines_dropped": len(body_dropped),
        "body_words_dropped": sum(len(lines[i].split()) for i in body_dropped),
        "body_dropped_examples": [lines[i][:80] for i in body_dropped[:10]],
    }


def peak_rss_mb(pid: int | None) -> float:
    if pid is None:
        return 0.0
    for row in Path(f"/proc/{pid}/status").read_text().splitlines():
        if row.startswith("VmHWM:"):
            return int(row.split()[1]) / 1024
    return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("books", nargs="+", type=Path)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--context", type=int, default=16_384)
    parser.add_argument("--chapters", type=int, default=4)
    parser.add_argument("--max-lines", type=int, default=400)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    options = JudgeOptions()
    results: dict[str, object] = {"model": args.model.name, "threads": args.threads}
    start = time.monotonic()
    with LlamaServer(
        args.server, args.model, threads=args.threads, context=args.context, thinking=args.thinking
    ) as server:
        load = time.monotonic() - start
        results["load_seconds"] = round(load, 1)
        books: dict[str, object] = {}
        for path in args.books:
            lines = flatten(path, args.chapters, args.max_lines)
            expected = gold(lines)
            stats = Stats()
            began = time.monotonic()
            verdicts = judge_lines(lines, server, options, stats)
            seconds = time.monotonic() - began
            got = predicted(lines, verdicts, options)
            raw_drops = sum(1 for v in verdicts.values() if v is Verdict.DROP)
            result = score(expected, got, lines)
            result.update(
                seconds=round(seconds, 1),
                requests=stats.requests,
                failed_windows=stats.failed_windows,
                errors=stats.errors,
                prompt_tokens=stats.prompt_tokens,
                generated_tokens=stats.generated_tokens,
                prompt_tok_per_s=round(stats.prompt_tokens / (stats.prompt_ms / 1000), 1)
                if stats.prompt_ms
                else 0,
                gen_tok_per_s=round(stats.generated_tokens / (stats.generated_ms / 1000), 1)
                if stats.generated_ms
                else 0,
                model_drops_before_guard=raw_drops,
                refused_by_guard=raw_drops - got.count(DROP),
            )
            books[path.name] = result
            print(f"== {path.name}: {json.dumps(result, ensure_ascii=False)}")
            for i, (e, g) in enumerate(zip(expected, got, strict=True)):
                if e != g and e != EITHER:
                    print(f"   [{i + 1}] expected {e}, got {g}: {lines[i][:90]!r}")
        results["peak_rss_mb"] = round(peak_rss_mb(server.pid), 0)
        results["books"] = books
    print(json.dumps({k: v for k, v in results.items() if k != "books"}))
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
