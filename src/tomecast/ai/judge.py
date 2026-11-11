"""Judgment calls by a small local model: which lines to drop and where chapters start.

The model never writes book text. It sees numbered lines and answers with **line numbers
only**, as JSON that llama.cpp forces into a fixed schema::

    {"drop": [3, 17], "chapter_start": [5]}

Every line it doesn't name is kept. The app then makes the edits itself
(:func:`apply_verdicts`) and checks the result (:func:`check_edit`): every kept line must be
byte-for-byte the line it was, in the same order, and the only lines missing must be the
ones dropped. So a model mistake can at worst drop a whole line it named, and the guards
below limit which lines it may drop:

- **Only short lines can be dropped.** A drop on a line longer than
  :attr:`JudgeOptions.max_drop_words` words is refused (the line is kept) and reported. Page
  numbers, running headers, contents entries and footnote markers are short; a paragraph of
  the story is not. So the worst a confused model can do is drop a short line.
- **Only short lines can start a chapter**, and a blank line can't.
- **Anything unexpected keeps the text.** A reply that isn't valid JSON, names a line that
  doesn't exist or fails twice leaves that window of lines exactly as it was.

Long lines are shortened in the prompt (:attr:`JudgeOptions.preview_chars`), which keeps the
prompt small, but the edits are always made on the full, original lines.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from tomecast.book import Book, Chapter


class Verdict(StrEnum):
    """What to do with one line."""

    KEEP = "keep"
    DROP = "drop"
    CHAPTER_START = "chapter_start"  # the line is a chapter heading: a new chapter starts here


class JudgeOutputError(ValueError):
    """The model's reply wasn't the JSON the schema asks for."""


class EditCheckError(AssertionError):
    """An edit would change or lose text it must keep. This is a bug, never a model mistake."""


@dataclass(frozen=True, slots=True)
class Reply:
    """What a model returned for one request, with llama.cpp's timings when it reports them."""

    text: str
    prompt_tokens: int = 0
    generated_tokens: int = 0
    prompt_ms: float = 0.0
    generated_ms: float = 0.0


class JsonModel(Protocol):
    """A model that answers a prompt with JSON matching a schema (llama.cpp enforces it)."""

    def generate_json(
        self, system: str, prompt: str, schema: Mapping[str, object], max_tokens: int
    ) -> Reply: ...


@dataclass(frozen=True, slots=True)
class JudgeOptions:
    """Limits on what the model may do, and how lines are shown to it."""

    max_drop_words: int = 12  # a drop on a longer line is refused
    max_heading_words: int = 12  # a chapter start on a longer line is refused
    preview_chars: int = 160  # longer lines are shortened in the prompt (never in the book)
    window_lines: int = 120  # lines per request
    window_chars: int = 12_000  # and at most this many prompt characters of lines per request
    retries: int = 1  # a bad reply is asked again this many times, then the window is kept


@dataclass(frozen=True, slots=True)
class Refusal:
    """A verdict the guards refused, so the line was kept as it was."""

    line: int  # 0-based index into the lines judged
    verdict: Verdict
    reason: str


@dataclass(frozen=True, slots=True)
class Section:
    """A run of kept lines. ``heading`` is the line that started it, if a chapter start did."""

    heading: str | None
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Edit:
    """The edits for one sequence of lines, as made by the app (never by the model)."""

    original: tuple[str, ...]
    sections: tuple[Section, ...]
    dropped: tuple[int, ...]  # 0-based, ascending
    starts: tuple[int, ...]  # 0-based, ascending: the lines that became headings
    refused: tuple[Refusal, ...] = ()


@dataclass(slots=True)
class Stats:
    """Totals over every request, for the speed tests."""

    requests: int = 0
    failed_windows: int = 0
    prompt_tokens: int = 0
    generated_tokens: int = 0
    prompt_ms: float = 0.0
    generated_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def add(self, reply: Reply) -> None:
        self.requests += 1
        self.prompt_tokens += reply.prompt_tokens
        self.generated_tokens += reply.generated_tokens
        self.prompt_ms += reply.prompt_ms
        self.generated_ms += reply.generated_ms


SYSTEM_PROMPT = """\
You help turn a book into an audiobook. You get lines of text taken from a book, each \
starting with its number in square brackets. Long lines are shortened and end in "…": \
those are always the book's own text.

Pick out the lines a narrator would not read aloud, and the lines that start a chapter.

"drop" lines are not part of the text to read:
- page numbers, alone or with the book or chapter title (running headers and footers)
- table of contents entries and lists of illustrations, including chapter titles listed there
- a footnote number or marker alone on a line, like "1" or "*" or "[2]"
- notes like "[Illustration]", scanner or transcriber notes, and decorative lines like "* * *"

"chapter_start" lines are headings that begin a new chapter in the text itself, like \
"CHAPTER III.", "Chapter 3", "III. THE STORM" or "BOOK TWO". A chapter title listed in a \
table of contents is "drop", not "chapter_start".

Every other line is kept: all sentences of the story, dialogue, poems, letters, quotes, \
dedications and headings inside a chapter. When unsure, keep the line.

Answer with JSON: the numbers of the lines to drop, and the numbers of the lines that \
start a chapter. Lines you don't list are kept.

Example lines:
[1] CONTENTS
[2] I. The Arrival 1
[3] II. The Storm 14
[4] CHAPTER I.
[5] THE ARRIVAL
[6] It was late in the evening when the coach at last drew up at the inn, and the …
[7] 2
[8] THE SILENT HOUSE
[9] “Is anyone there?” she called.
Example answer: {"drop": [1, 2, 3, 7, 8], "chapter_start": [4]}"""


def schema(count: int) -> dict[str, object]:
    """The JSON schema for a reply about ``count`` lines, numbered from 1.

    llama.cpp turns it into a grammar, so the model can only name line numbers that exist.
    """
    numbers: dict[str, object] = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": count},
        "maxItems": count,
    }
    return {
        "type": "object",
        "properties": {"drop": numbers, "chapter_start": numbers},
        "required": ["drop", "chapter_start"],
        "additionalProperties": False,
    }


def preview(line: str, limit: int) -> str:
    """How a line is shown to the model: as it is, or shortened to about ``limit`` characters."""
    flat = " ".join(line.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit].rsplit(" ", 1)[0] or flat[:limit]
    return f"{cut} …"


def prompt(lines: Sequence[str], options: JudgeOptions) -> str:
    """The user message: the lines, numbered from 1."""
    numbered = "\n".join(
        f"[{number}] {preview(line, options.preview_chars)}"
        for number, line in enumerate(lines, start=1)
    )
    return f"Lines 1 to {len(lines)}:\n{numbered}"


def max_tokens(count: int) -> int:
    """A cap on the reply's length: enough to list every line number twice, plus the keys."""
    digits = len(str(count))
    return 32 + 2 * count * (digits + 2)


def parse_reply(text: str, count: int) -> dict[int, Verdict]:
    """Read a reply about ``count`` lines into verdicts, by 0-based line index.

    Only lines the model named are in the result; the rest are kept. A line named both as
    "drop" and as "chapter_start" is treated as a chapter start (it's kept, as a heading).

    Raises:
        JudgeOutputError: if the reply isn't the expected JSON, or names a line that doesn't
            exist.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise JudgeOutputError(f"the reply isn't JSON: {error}") from None
    if not isinstance(payload, dict) or set(payload) != {"drop", "chapter_start"}:
        raise JudgeOutputError("the reply must have exactly the keys drop and chapter_start")
    verdicts: dict[int, Verdict] = {}
    for key, verdict in (("drop", Verdict.DROP), ("chapter_start", Verdict.CHAPTER_START)):
        numbers = payload[key]
        if not isinstance(numbers, list):
            raise JudgeOutputError(f"{key} must be a list of line numbers")
        for number in numbers:
            # bool is an int in Python; JSON true/false are not line numbers.
            if not isinstance(number, int) or isinstance(number, bool):
                raise JudgeOutputError(f"{key} has {number!r}, which isn't a line number")
            if not 1 <= number <= count:
                raise JudgeOutputError(f"{key} names line {number}, but there are {count}")
            verdicts[number - 1] = verdict  # chapter_start comes second, so it wins
    return verdicts


def words(line: str) -> int:
    """How many words a line has (runs of non-space characters)."""
    return len(line.split())


def guard(
    lines: Sequence[str], verdicts: Mapping[int, Verdict], options: JudgeOptions
) -> tuple[dict[int, Verdict], list[Refusal]]:
    """Refuse the verdicts that could lose real text or make a bad chapter. Refused lines
    are kept as they are."""
    allowed: dict[int, Verdict] = {}
    refused: list[Refusal] = []
    for index in sorted(verdicts):
        verdict = verdicts[index]
        line = lines[index]
        count = words(line)
        if verdict is Verdict.DROP and count > options.max_drop_words:
            refused.append(Refusal(index, verdict, f"{count} words is too long to drop"))
        elif verdict is Verdict.CHAPTER_START and not line.strip():
            refused.append(Refusal(index, verdict, "a blank line can't be a heading"))
        elif verdict is Verdict.CHAPTER_START and count > options.max_heading_words:
            refused.append(Refusal(index, verdict, f"{count} words is too long for a heading"))
        elif verdict is not Verdict.KEEP:
            allowed[index] = verdict
    return allowed, refused


def apply_verdicts(
    lines: Sequence[str],
    verdicts: Mapping[int, Verdict],
    options: JudgeOptions | None = None,
    *,
    first_is_heading: bool = False,
) -> Edit:
    """Make the edits: leave out dropped lines and split at chapter starts.

    The verdicts go through :func:`guard` first, and the result through :func:`check_edit`,
    so this never returns an edit that changes a kept line. ``first_is_heading`` says the
    lines already have a heading (a chapter title), so a chapter start on the first line
    would only repeat it and is refused.
    """
    options = options or JudgeOptions()
    original = tuple(lines)
    allowed, refused = guard(original, verdicts, options)
    if first_is_heading and allowed.get(0) is Verdict.CHAPTER_START:
        del allowed[0]
        refused.append(Refusal(0, Verdict.CHAPTER_START, "the chapter already starts here"))
    sections: list[Section] = []
    heading: str | None = None
    run: list[str] = []
    for index, line in enumerate(original):
        verdict = allowed.get(index, Verdict.KEEP)
        if verdict is Verdict.DROP:
            continue
        if verdict is Verdict.CHAPTER_START:
            if heading is not None or run:
                sections.append(Section(heading, tuple(run)))
            heading, run = line, []
        else:
            run.append(line)
    if heading is not None or run:
        sections.append(Section(heading, tuple(run)))
    edit = Edit(
        original=original,
        sections=tuple(sections),
        dropped=tuple(i for i in sorted(allowed) if allowed[i] is Verdict.DROP),
        starts=tuple(i for i in sorted(allowed) if allowed[i] is Verdict.CHAPTER_START),
        refused=tuple(sorted(refused, key=lambda refusal: refusal.line)),
    )
    check_edit(edit, options)
    return edit


def _same(a: str, b: str) -> bool:
    """Byte-for-byte equality (lone surrogates included, so nothing is normalised away)."""
    return a.encode("utf-8", "surrogatepass") == b.encode("utf-8", "surrogatepass")


def check_edit(edit: Edit, options: JudgeOptions | None = None) -> None:
    """Prove the edit only removed whole dropped lines, and changed nothing it kept.

    Rebuilds the text from the sections (each heading, then its lines) and compares it, line
    by line and byte by byte, with the original minus the dropped lines. Also checks that
    every dropped line was short enough to drop, and that every word of the original is
    either kept or in a dropped line.

    Raises:
        EditCheckError: if any of that doesn't hold.
    """
    options = options or JudgeOptions()
    dropped = set(edit.dropped)
    if len(dropped) != len(edit.dropped) or not dropped <= set(range(len(edit.original))):
        raise EditCheckError("the dropped lines must be distinct lines of the original")
    expected = [line for index, line in enumerate(edit.original) if index not in dropped]
    rebuilt: list[str] = []
    for section in edit.sections:
        if section.heading is not None:
            rebuilt.append(section.heading)
        rebuilt.extend(section.lines)
    if len(rebuilt) != len(expected):
        raise EditCheckError(
            f"{len(expected)} lines should be kept, but the edit has {len(rebuilt)}"
        )
    for position, (got, want) in enumerate(zip(rebuilt, expected, strict=True)):
        if not _same(got, want):
            raise EditCheckError(f"kept line {position + 1} changed: {want!r} became {got!r}")
    for index in edit.dropped:
        if words(edit.original[index]) > options.max_drop_words:
            raise EditCheckError(f"line {index + 1} is too long to have been dropped")
    before = [word for line in edit.original for word in line.split()]
    after = [word for line in rebuilt for word in line.split()]
    gone = [word for index in edit.dropped for word in edit.original[index].split()]
    if len(after) + len(gone) != len(before):
        raise EditCheckError("words were lost outside the dropped lines")


def windows(lines: Sequence[str], options: JudgeOptions) -> list[range]:
    """Split the lines into requests of at most ``window_lines`` lines and about
    ``window_chars`` characters (always at least one line each)."""
    spans: list[range] = []
    start = 0
    size = 0
    for index, line in enumerate(lines):
        shown = len(preview(line, options.preview_chars)) + 8
        if index > start and (
            index - start >= options.window_lines or size + shown > options.window_chars
        ):
            spans.append(range(start, index))
            start, size = index, 0
        size += shown
    if start < len(lines):
        spans.append(range(start, len(lines)))
    return spans


def judge_lines(
    lines: Sequence[str],
    model: JsonModel,
    options: JudgeOptions | None = None,
    stats: Stats | None = None,
) -> dict[int, Verdict]:
    """Ask the model about every line, a window at a time. Returns its verdicts by 0-based
    index (unguarded: :func:`apply_verdicts` applies the guards). A window whose replies
    keep failing gets no verdicts, so all its lines are kept."""
    options = options or JudgeOptions()
    stats = stats if stats is not None else Stats()
    verdicts: dict[int, Verdict] = {}
    for span in windows(lines, options):
        chunk = [lines[index] for index in span]
        for attempt in range(options.retries + 1):
            reply = model.generate_json(
                SYSTEM_PROMPT, prompt(chunk, options), schema(len(chunk)), max_tokens(len(chunk))
            )
            stats.add(reply)
            try:
                found = parse_reply(reply.text, len(chunk))
            except JudgeOutputError as error:
                stats.errors.append(f"lines {span.start + 1}-{span.stop}: {error}")
                if attempt == options.retries:
                    stats.failed_windows += 1
                continue
            verdicts.update({span.start + index: verdict for index, verdict in found.items()})
            break
    return verdicts


@dataclass(frozen=True, slots=True)
class JudgedBook:
    """A book after the judge, with each chapter's edit (in the original chapter order)."""

    book: Book
    edits: tuple[Edit, ...]

    @property
    def dropped(self) -> int:
        return sum(len(edit.dropped) for edit in self.edits)

    @property
    def refused(self) -> int:
        return sum(len(edit.refused) for edit in self.edits)


def chapters_from(chapter: Chapter, edit: Edit) -> list[Chapter]:
    """The chapters one chapter becomes: the first keeps its title, and each chapter start
    begins a new chapter titled by its heading line."""
    result: list[Chapter] = []
    for position, section in enumerate(edit.sections):
        if section.heading is None:
            if position == 0 and (chapter.title or section.lines):
                result.append(Chapter(chapter.title, section.lines))
            continue
        if position == 0 and chapter.title:
            # The chapter's own title comes before the first heading; it stays, on its own.
            result.append(Chapter(chapter.title, ()))
        result.append(Chapter(section.heading, section.lines))
    if not edit.sections and chapter.title:
        result.append(Chapter(chapter.title, ()))
    return result


def check_chapters(before: Chapter, edit: Edit, after: Sequence[Chapter]) -> None:
    """Prove the chapters made from ``before`` speak exactly its text minus the dropped lines.

    Raises:
        EditCheckError: if a kept line or the title changed, moved or went missing.
    """
    expected = ([before.title] if before.title else []) + [
        line for index, line in enumerate(edit.original) if index not in set(edit.dropped)
    ]
    spoken = [text for chapter in after for text in chapter.spoken_paragraphs()]
    if len(spoken) != len(expected) or not all(
        _same(got, want) for got, want in zip(spoken, expected, strict=True)
    ):
        raise EditCheckError(f"the chapters made from {before.title!r} don't match its text")


def judge_book(
    book: Book,
    model: JsonModel,
    options: JudgeOptions | None = None,
    stats: Stats | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> JudgedBook:
    """Run the judge over every chapter's paragraphs and rebuild the book from the edits.

    Each chapter is judged on its own, its paragraphs as the numbered lines. Every result is
    checked (:func:`check_edit` and :func:`check_chapters`) before it's used.
    """
    options = options or JudgeOptions()
    chapters: list[Chapter] = []
    edits: list[Edit] = []
    for number, chapter in enumerate(book.chapters):
        verdicts = judge_lines(chapter.paragraphs, model, options, stats)
        edit = apply_verdicts(
            chapter.paragraphs, verdicts, options, first_is_heading=bool(chapter.title)
        )
        made = chapters_from(chapter, edit)
        check_chapters(chapter, edit, made)
        chapters.extend(made)
        edits.append(edit)
        if progress is not None:
            progress(number + 1, len(book.chapters))
    judged = Book(
        title=book.title,
        authors=book.authors,
        language=book.language,
        chapters=tuple(chapters),
        cover=book.cover,
        raw=book.raw,
    )
    return JudgedBook(judged, tuple(edits))
