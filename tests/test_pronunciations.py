from pathlib import Path

from tomecast import pronunciations
from tomecast.pronunciations import Pronunciation, Pronunciations, parse, respelling


def fixes(*pairs: tuple[str, str]) -> Pronunciations:
    return Pronunciations(tuple(Pronunciation(w, s) for w, s in pairs))


# --- parsing ------------------------------------------------------------------------------------


def test_parse_reads_arrow_lines_and_skips_comments_and_blank_lines() -> None:
    text = """
# my fixes
Hermione → her-MY-oh-nee
Lothlórien -> loth-LOR-ee-en   # arrow can be ASCII

   # indented comment
kernel→colonel
"""
    found, warnings = parse(text)
    assert warnings == []
    assert found.entries == (
        Pronunciation("Hermione", "her-MY-oh-nee"),
        Pronunciation("Lothlórien", "loth-LOR-ee-en"),
        Pronunciation("kernel", "colonel"),
    )


def test_a_hash_inside_a_word_is_not_a_comment() -> None:
    found, warnings = parse("C# → see sharp\n")
    assert warnings == []
    assert found.entries == (Pronunciation("C#", "see sharp"),)


def test_bad_lines_are_skipped_with_a_warning_naming_the_line() -> None:
    found, warnings = parse(
        "good → fine\nno arrow here\n→ nothing before\nnothing after →\n", "x.txt"
    )
    assert found.entries == (Pronunciation("good", "fine"),)
    assert len(warnings) == 3
    assert [w.split(":")[0] for w in warnings] == [
        "x.txt, line 2",
        "x.txt, line 3",
        "x.txt, line 4",
    ]


def test_exact_phonetic_spellings_are_rejected_with_an_explanation() -> None:
    found, warnings = parse("Galadriel → /ɡəˈlædriəl/\nHermione → her-my-oh-nee\n")
    assert found.entries == (Pronunciation("Hermione", "her-my-oh-nee"),)
    assert len(warnings) == 1
    assert "line 1" in warnings[0]
    assert "respelling" in warnings[0]


def test_a_slash_inside_a_respelling_is_fine() -> None:
    found, warnings = parse("a/b → ay slash bee\n")
    assert warnings == []
    assert len(found) == 1


# --- applying -----------------------------------------------------------------------------------


def test_matches_whole_words_only() -> None:
    fix = fixes(("Ron", "rahn"))
    assert fix.apply("Ron and Ronald met Baron Ron.") == "rahn and Ronald met Baron rahn."


def test_lowercase_entries_match_any_case() -> None:
    fix = fixes(("kernel", "colonel"))
    assert fix.apply("The kernel, Kernel and KERNEL.") == "The colonel, colonel and colonel."


def test_capitalised_entries_match_that_spelling_and_all_caps() -> None:
    fix = fixes(("Hermione", "her-my-oh-nee"))
    assert (
        fix.apply("Hermione, HERMIONE and hermione.")
        == "her-my-oh-nee, her-my-oh-nee and hermione."
    )


def test_possessives_keep_their_ending() -> None:
    assert fixes(("Hermione", "her-my-oh-nee")).apply("Hermione's wand") == "her-my-oh-nee's wand"


def test_longer_entries_win_and_multi_word_entries_match_across_spacing() -> None:
    fix = fixes(("New", "nyoo"), ("New Zealand", "noo zee-lund"))
    assert fix.apply("New Zealand and New\nZealand and New York") == (
        "noo zee-lund and noo zee-lund and nyoo York"
    )


def test_later_entries_win_over_earlier_ones() -> None:
    first = fixes(("Rowan", "ro-an"))
    second = fixes(("Rowan", "row-an"))
    assert first.then(second).apply("Rowan") == "row-an"


def test_regex_characters_in_entries_are_taken_literally() -> None:
    fix = fixes(("St. John's", "saint jonz"), ("a+b", "a plus b"))
    assert fix.apply("At St. John's we saw a+b.") == "At saint jonz we saw a plus b."


def test_an_empty_list_changes_nothing() -> None:
    assert Pronunciations().apply("Anything at all") == "Anything at all"
    assert len(Pronunciations()) == 0


def test_stressed_syllables_in_capitals_are_lowercased_before_speaking() -> None:
    assert respelling("her-MY-oh-nee") == "her-my-oh-nee"
    assert respelling("A-OK") == "A-ok"
    assert respelling("NASA") == "nasa"
    assert fixes(("Hermione", "her-MY-oh-nee")).apply("Hermione") == "her-my-oh-nee"


def test_written_words_lists_the_left_sides_in_lowercase() -> None:
    assert fixes(("Hermione", "x"), ("New  Zealand", "y")).written_words() == {
        "hermione",
        "new zealand",
    }


# --- files --------------------------------------------------------------------------------------


def test_list_file_locations(tmp_path: Path) -> None:
    assert pronunciations.global_list_path(tmp_path) == tmp_path / "pronunciations.txt"
    assert pronunciations.book_list_path(tmp_path / "My Book.epub") == (
        tmp_path / "My Book.pronunciations.txt"
    )


def test_load_reads_a_file_and_a_missing_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    path.write_text("Hermione → her-my-oh-nee\n", encoding="utf-8")
    found, warnings = pronunciations.load(path)
    assert len(found) == 1
    assert warnings == []
    assert pronunciations.load(tmp_path / "nope.txt") == (Pronunciations(), [])


def test_load_reports_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "list.txt"
    path.write_bytes(b"\xff\xfe not utf-8 \x80")
    found, warnings = pronunciations.load(path)
    assert len(found) == 0
    assert "couldn't read" in warnings[0]


def test_the_books_own_list_wins_over_the_global_one(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "pronunciations.txt").write_text("Rowan → ro-an\nZed → zed\n", encoding="utf-8")
    book = tmp_path / "story.epub"
    (tmp_path / "story.pronunciations.txt").write_text("Rowan → row-an\n", encoding="utf-8")
    combined, warnings = pronunciations.load_for_book(data, book)
    assert warnings == []
    assert combined.apply("Rowan and Zed") == "row-an and zed"
    only_global, _ = pronunciations.load_for_book(data, None)
    assert only_global.apply("Rowan") == "ro-an"
