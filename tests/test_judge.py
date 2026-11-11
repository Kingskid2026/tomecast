"""The judge: reading the model's reply, the guards, the edits and the check that proves no
kept line changed. No model runs here: a fake one answers with canned JSON."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest

from tomecast.ai.judge import (
    SYSTEM_PROMPT,
    Edit,
    EditCheckError,
    JudgeOptions,
    JudgeOutputError,
    Reply,
    Section,
    Stats,
    Verdict,
    apply_verdicts,
    chapters_from,
    check_chapters,
    check_edit,
    judge_book,
    judge_lines,
    looks_like_heading,
    looks_like_sentence,
    max_tokens,
    parse_reply,
    preview,
    prompt,
    schema,
    windows,
)
from tomecast.book import Book, Chapter

# Public domain: O. Henry, "The Gift of the Magi" (1905), with the junk a PDF or a scanned
# text leaves behind.
STORY = (
    "THE GIFT OF THE MAGI",  # 0 running header
    "One dollar and eighty-seven cents. That was all. And sixty cents of it was in pennies.",
    "12",  # 2 page number
    "Pennies saved one and two at a time by bulldozing the grocer and the vegetable man and "
    "the butcher until one's cheeks burned with the silent imputation of parsimony that such "
    "close dealing implied.",
    "*",  # 4 footnote marker
    "CHAPTER II.",  # 5 chapter heading
    "There was clearly nothing to do but flop down on the shabby little couch and howl.",
    "So Della did it.",
)


class FakeModel:
    """Answers each request with the next canned reply, and records the requests."""

    def __init__(self, replies: Sequence[str | Mapping[str, list[int]]]) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.prompts: list[str] = []
        self.schemas: list[Mapping[str, object]] = []

    def generate_json(
        self, system: str, prompt: str, schema: Mapping[str, object], max_tokens: int
    ) -> Reply:
        assert system == SYSTEM_PROMPT
        assert max_tokens > 0
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return Reply(self.replies.pop(0), prompt_tokens=10, generated_tokens=3)


def spoken(edit: Edit) -> list[str]:
    out: list[str] = []
    for section in edit.sections:
        if section.heading is not None:
            out.append(section.heading)
        out.extend(section.lines)
    return out


# --- the reply ---


def test_parse_reply_reads_line_numbers_as_zero_based_verdicts() -> None:
    verdicts = parse_reply('{"drop": [1, 3, 5], "chapter_start": [6]}', 8)
    assert verdicts == {0: Verdict.DROP, 2: Verdict.DROP, 4: Verdict.DROP, 5: Verdict.CHAPTER_START}


def test_parse_reply_with_nothing_named_keeps_everything() -> None:
    assert parse_reply('{"drop": [], "chapter_start": []}', 3) == {}


def test_a_line_named_twice_is_a_chapter_start_not_a_drop() -> None:
    assert parse_reply('{"drop": [2, 2], "chapter_start": [2]}', 3) == {1: Verdict.CHAPTER_START}


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"drop": [1]}',  # a key missing
        '{"drop": [], "chapter_start": [], "rewrite": "x"}',  # an extra key
        '{"drop": [0], "chapter_start": []}',  # numbers start at 1
        '{"drop": [4], "chapter_start": []}',  # only 3 lines
        '{"drop": ["2"], "chapter_start": []}',
        '{"drop": [true], "chapter_start": []}',
        '{"drop": [1.5], "chapter_start": []}',
        '{"drop": 2, "chapter_start": []}',
        "[1, 2]",
    ],
)
def test_parse_reply_refuses_anything_but_the_schema(text: str) -> None:
    with pytest.raises(JudgeOutputError):
        parse_reply(text, 3)


def test_schema_only_allows_line_numbers_that_exist() -> None:
    shape = schema(7)
    properties = shape["properties"]
    assert isinstance(properties, dict)
    items = properties["drop"]["items"]
    assert items == {"type": "integer", "minimum": 1, "maximum": 7}
    assert shape["additionalProperties"] is False
    assert shape["required"] == ["drop", "chapter_start"]


def test_max_tokens_is_enough_to_name_every_line_in_both_lists() -> None:
    every = json.dumps({"drop": list(range(1, 121)), "chapter_start": list(range(1, 121))})
    assert max_tokens(120) >= len(every) // 2  # a token is at least one character, often 2-3


# --- the prompt ---


def test_prompt_numbers_lines_from_one_and_shortens_long_ones() -> None:
    text = prompt(STORY, JudgeOptions(preview_chars=60))
    lines = text.splitlines()
    assert lines[0] == f"Lines 1 to {len(STORY)}:"
    assert lines[1] == "[1] THE GIFT OF THE MAGI"
    assert lines[3] == "[3] 12"
    assert lines[4].startswith("[4] Pennies saved one and two") and lines[4].endswith(" …")
    assert len(lines[4]) < 70


def test_preview_collapses_whitespace_and_never_cuts_short_lines() -> None:
    assert preview("  CHAPTER\tII. ", 160) == "CHAPTER II."
    assert preview("word " * 100, 20) == "word word word word …"
    assert preview("x" * 30, 10) == "x" * 10 + " …"


def test_windows_cover_every_line_once_in_order() -> None:
    lines = [f"line {i}" for i in range(250)]
    spans = windows(lines, JudgeOptions(window_lines=100))
    assert [len(span) for span in spans] == [100, 100, 50]
    assert [i for span in spans for i in span] == list(range(250))


def test_windows_also_split_by_size() -> None:
    lines = ["x" * 150] * 10
    spans = windows(lines, JudgeOptions(window_lines=100, window_chars=500))
    assert all(len(span) <= 3 for span in spans)
    assert [i for span in spans for i in span] == list(range(10))


# --- the edits and the check ---


def test_apply_drops_only_named_lines_and_keeps_the_rest_byte_for_byte() -> None:
    verdicts = {0: Verdict.DROP, 2: Verdict.DROP, 4: Verdict.DROP, 5: Verdict.CHAPTER_START}
    edit = apply_verdicts(STORY, verdicts)
    assert edit.dropped == (0, 2, 4)
    assert edit.starts == (5,)
    assert edit.sections == (
        Section(None, (STORY[1], STORY[3])),
        Section("CHAPTER II.", (STORY[6], STORY[7])),
    )
    kept = [line for i, line in enumerate(STORY) if i not in {0, 2, 4}]
    assert spoken(edit) == kept


def test_a_drop_on_a_long_line_is_refused_and_the_line_kept() -> None:
    edit = apply_verdicts(STORY, {0: Verdict.DROP, 1: Verdict.DROP, 3: Verdict.DROP})
    assert edit.dropped == (0,)  # 5 words
    assert [r.line for r in edit.refused] == [1, 3]  # 16 and 31 words
    assert all("too long to drop" in r.reason for r in edit.refused)
    assert STORY[1] in spoken(edit) and STORY[3] in spoken(edit)


def test_the_drop_limit_counts_words() -> None:
    options = JudgeOptions(max_drop_words=3)
    edit = apply_verdicts(STORY, {0: Verdict.DROP, 7: Verdict.DROP}, options)
    assert edit.dropped == ()  # "THE GIFT OF THE MAGI" is 5 words, "So Della did it." is 4
    edit = apply_verdicts(STORY, {2: Verdict.DROP, 4: Verdict.DROP}, options)
    assert edit.dropped == (2, 4)


def test_long_or_blank_lines_cant_start_a_chapter() -> None:
    lines = ("Para one.", "", STORY[3])
    edit = apply_verdicts(lines, {1: Verdict.CHAPTER_START, 2: Verdict.CHAPTER_START})
    assert edit.starts == ()
    assert len(edit.refused) == 2
    assert spoken(edit) == list(lines)


def test_a_chapter_start_on_the_first_line_of_a_titled_chapter_is_refused() -> None:
    edit = apply_verdicts(STORY[5:], {0: Verdict.CHAPTER_START}, first_is_heading=True)
    assert edit.starts == ()
    assert edit.refused[0].reason == "the chapter already starts here"
    edit = apply_verdicts(STORY[5:], {0: Verdict.CHAPTER_START})
    assert edit.starts == (0,)
    assert edit.sections == (Section("CHAPTER II.", STORY[6:]),)


def test_check_edit_catches_a_changed_word() -> None:
    edit = apply_verdicts(STORY, {2: Verdict.DROP})
    changed = Section(None, (STORY[0], STORY[1].replace("dollar", "dollars"), *STORY[3:]))
    bad = Edit(edit.original, (changed,), edit.dropped, edit.starts)
    with pytest.raises(EditCheckError, match="changed"):
        check_edit(bad)


def test_check_edit_catches_a_lost_line() -> None:
    edit = apply_verdicts(STORY, {2: Verdict.DROP})
    short = Section(None, (STORY[0], STORY[1], *STORY[4:]))  # STORY[3] missing
    with pytest.raises(EditCheckError):
        check_edit(Edit(edit.original, (short,), edit.dropped, edit.starts))


def test_check_edit_catches_reordered_lines() -> None:
    swapped = Section(None, (STORY[1], STORY[0], *STORY[2:]))
    with pytest.raises(EditCheckError, match="changed"):
        check_edit(Edit(STORY, (swapped,), (), ()))


def test_check_edit_catches_invisible_changes() -> None:
    lines = ("café au lait",)
    decomposed = Section(None, ("café au lait",))  # looks the same, isn't the same bytes
    with pytest.raises(EditCheckError):
        check_edit(Edit(lines, (decomposed,), (), ()))
    spaced = Section(None, ("café  au lait",))
    with pytest.raises(EditCheckError):
        check_edit(Edit(lines, (spaced,), (), ()))


def test_check_edit_catches_a_long_line_dropped_past_the_guard() -> None:
    rest = Section(None, tuple(line for i, line in enumerate(STORY) if i != 3))
    with pytest.raises(EditCheckError, match="too long"):
        check_edit(Edit(STORY, (rest,), (3,), ()))


def test_check_edit_catches_a_dropped_index_that_doesnt_exist() -> None:
    with pytest.raises(EditCheckError):
        check_edit(Edit(STORY, (Section(None, STORY),), (99,), ()))


# --- asking the model ---


def test_judge_lines_maps_window_numbers_back_to_the_whole_text() -> None:
    lines = [f"line {i}" for i in range(5)]
    model = FakeModel([{"drop": [2], "chapter_start": []}, {"drop": [1], "chapter_start": [2]}])
    verdicts = judge_lines(lines, model, JudgeOptions(window_lines=3))
    assert verdicts == {1: Verdict.DROP, 3: Verdict.DROP, 4: Verdict.CHAPTER_START}
    assert model.prompts[1].startswith("Lines 1 to 2:\n[1] line 3\n[2] line 4")


def test_a_bad_reply_is_asked_again_then_the_window_is_kept() -> None:
    lines = ["a", "b"]
    model = FakeModel(["oops", '{"drop": [1], "chapter_start": []}'])
    stats = Stats()
    assert judge_lines(lines, model, stats=stats) == {0: Verdict.DROP}
    assert stats.requests == 2 and stats.failed_windows == 0 and len(stats.errors) == 1

    model = FakeModel(["oops", '{"drop": [9], "chapter_start": []}'])
    stats = Stats()
    assert judge_lines(lines, model, stats=stats) == {}
    assert stats.failed_windows == 1
    assert stats.prompt_tokens == 20 and stats.generated_tokens == 6


# --- whole books ---


def test_judge_book_splits_chapters_and_proves_every_kept_line_is_spoken() -> None:
    book = Book(
        title="The Gift of the Magi",
        authors=("O. Henry",),
        language="en",
        chapters=(Chapter("The Gift of the Magi", STORY),),
    )
    model = FakeModel([{"drop": [1, 3, 5], "chapter_start": [6]}])
    judged = judge_book(book, model)
    assert [c.title for c in judged.book.chapters] == ["The Gift of the Magi", "CHAPTER II."]
    assert judged.book.chapters[0].paragraphs == (STORY[1], STORY[3])
    assert judged.book.chapters[1].paragraphs == (STORY[6], STORY[7])
    assert judged.dropped == 3 and judged.refused == 0
    every_word = " ".join(c for ch in judged.book.chapters for c in ch.spoken_paragraphs())
    for i, line in enumerate(STORY):
        if i not in {0, 2, 4}:
            assert line in every_word


def test_judge_book_keeps_a_title_whose_lines_were_all_dropped() -> None:
    chapter = Chapter("Contents", ("The Gift 1", "The Combs 9", "CHAPTER I.", "Text."))
    model = FakeModel([{"drop": [1, 2], "chapter_start": [3]}])
    judged = judge_book(Book("T", (), None, (chapter,)), model)
    assert [(c.title, c.paragraphs) for c in judged.book.chapters] == [
        ("Contents", ()),
        ("CHAPTER I.", ("Text.",)),
    ]


def test_judge_book_with_an_untitled_chapter_starting_with_a_heading() -> None:
    chapter = Chapter("", ("CHAPTER I.", "Text."))
    model = FakeModel([{"drop": [], "chapter_start": [1]}])
    judged = judge_book(Book("T", (), None, (chapter,)), model)
    assert [(c.title, c.paragraphs) for c in judged.book.chapters] == [("CHAPTER I.", ("Text.",))]


def test_check_chapters_catches_a_lost_title() -> None:
    chapter = Chapter("Contents", ("The Gift 1", "Text."))
    edit = apply_verdicts(chapter.paragraphs, {0: Verdict.DROP})
    assert chapters_from(chapter, edit) == [Chapter("Contents", ("Text.",))]
    with pytest.raises(EditCheckError):
        check_chapters(chapter, edit, [Chapter("", ("Text.",))])


def test_a_model_that_drops_everything_only_loses_short_lines() -> None:
    """The worst case: the model names every line. Only lines short enough to be junk go."""
    chapter = Chapter("", STORY)
    model = FakeModel([{"drop": list(range(1, len(STORY) + 1)), "chapter_start": []}])
    judged = judge_book(Book("T", (), None, (chapter,)), model)
    kept = [p for c in judged.book.chapters for p in c.spoken_paragraphs()]
    # Every sentence stays, and so does the chapter heading.
    assert kept == [STORY[1], STORY[3], "CHAPTER II.", STORY[6], STORY[7]]
    assert [line for line in STORY if line not in kept] == [STORY[0], "12", "*"]
    assert judged.refused == 5


@pytest.mark.parametrize(
    ("line", "heading"),
    [
        ("CHAPTER II.", True),
        ("I. A SCANDAL IN BOHEMIA", True),
        ("Part Two", True),
        ("12", False),  # a page number
        ("THE GIFT OF THE MAGI", False),  # a running header
        ("Heading to Chapter I. 1", False),
    ],
)
def test_looks_like_heading(line: str, heading: bool) -> None:
    assert looks_like_heading(line) is heading


@pytest.mark.parametrize(
    ("line", "sentence"),
    [
        ("“Yes.”", True),
        ("So Della did it.", True),
        ("“Is anyone there?” she called.", True),
        ("Well…", True),
        ("_She stopped._", True),
        ("(See the note.)", True),
        ("“But your client—”", True),
        ("“Precisely so. But how—”", True),
        ("“Very truly yours,", True),
        ("I took the paper from him and read as follows:", True),
        ("“He came down to see the place”", True),  # dialogue, even with no full stop
        ("“I’m the tallest” 9", False),  # a list of illustrations: ends in a page number
        ("[Illustration:", False),
        ("Heading to Chapter XXX. 209", False),
        ("Frontispiece iv", False),
        ("12", False),
        ("THE GIFT OF THE MAGI", False),
        ("CHAPTER II.", False),  # no lowercase: a heading
        ("PREFACE.", False),
        ("Mr. and Mrs. Bennet 5", False),
        ("[Illustration: The arrival.]", False),
        ("[_Copyright 1894 by George Allen._]]", False),
        ("*", False),
        ("", False),
    ],
)
def test_looks_like_sentence(line: str, sentence: bool) -> None:
    assert looks_like_sentence(line) is sentence


def test_a_short_sentence_is_never_dropped_unless_the_guard_is_off() -> None:
    edit = apply_verdicts(STORY, {7: Verdict.DROP})
    assert edit.dropped == () and edit.refused[0].reason == "it looks like a sentence"
    edit = apply_verdicts(STORY, {7: Verdict.DROP}, JudgeOptions(keep_sentences=False))
    assert edit.dropped == (7,)
