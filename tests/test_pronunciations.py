from pathlib import Path

import pytest

from tomecast import pronunciations
from tomecast.pronunciations import (
    Pronunciation,
    Pronunciations,
    is_phonetic,
    parse,
    phonetic_lines,
    phonetic_notation,
    respelling,
    without_phonetic,
)


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


# eSpeak reads these out symbol by symbol: Siobhan written in plain IPA was heard as
# "Esch small capi stress V open all lengthened N". Each value's only phonetic symbol is the
# one its id names, so each range of the rule is pinned by its own case.
IPA = [
    pytest.param("ʃɪˈvɔːn", id="plain IPA"),
    pytest.param("/ʃɪˈvɔːn/", id="IPA in slashes"),
    pytest.param("[ʃɪˈvɔːn]", id="IPA in brackets, as Wikipedia writes it"),
    pytest.param("[ ʃɪˈvɔːn ]", id="brackets with spaces"),
    pytest.param("shi-vawn (ʃɪˈvɔːn)", id="IPA after a respelling"),
    pytest.param("ɡal", id="IPA g, not the letter g"),
    pytest.param("ʃiv-awn", id="one IPA letter"),
    pytest.param("shi-ˈvawn", id="primary stress mark only"),
    pytest.param("ˌshi-vawn", id="secondary stress mark only"),
    pytest.param("shi-vawːn", id="length mark only"),
    pytest.param("shi-vawˑn", id="half-length mark only"),
    pytest.param("tʰom", id="aspiration mark"),
    pytest.param("kaˀta", id="glottal stop modifier"),
    pytest.param("kaˁta", id="reversed glottal stop modifier"),
    pytest.param("ka˞ta", id="rhotic hook"),
    pytest.param("tˠa", id="velarised"),
    pytest.param("maˤ", id="pharyngealised"),
    pytest.param("ma˥", id="tone letter"),
    pytest.param("t͡sa", id="tie bar above"),
    pytest.param("t͜sa", id="tie bar below"),
    pytest.param("drift-ᵻd", id="phonetic extensions, as eSpeak writes them"),
    pytest.param("kiθ", id="theta"),
    pytest.param("βa-ka", id="beta"),
    pytest.param("χa-ka", id="chi"),
]


@pytest.mark.parametrize("say", IPA)
def test_ipa_is_skipped_with_advice(say: str) -> None:
    assert phonetic_notation(say) == "IPA"
    found, warnings = parse(f"Siobhan → {say}\nHermione → her-my-oh-nee\n", "x.txt")
    assert found.entries == (Pronunciation("Hermione", "her-my-oh-nee"),)
    assert warnings == [
        f"x.txt, line 1: '{say}' looks like IPA, and the voice can't use phonetic symbols, "
        "so the line is skipped; write a respelling for 'Siobhan' instead, the way it sounds "
        "in plain letters, like shi-vawn"
    ]


@pytest.mark.parametrize("say", ["[[S@v'O:n]]", "shi-[[vA:n]]"])
def test_espeak_markup_is_skipped_with_advice(say: str) -> None:
    assert phonetic_notation(say) == "markup"
    found, warnings = parse(f"Siobhan → {say}\n", "x.txt")
    assert len(found) == 0
    assert warnings == [
        f"x.txt, line 1: '{say}' is eSpeak phoneme markup, which the voice can't use, so the "
        "line is skipped; write a respelling for 'Siobhan' instead, the way it sounds in plain "
        "letters, like shi-vawn"
    ]


@pytest.mark.parametrize(
    ("say", "notation"),
    [
        ("/shi-vawn/", "slashes"),
        ("/SH IH V AO N/", "slashes"),  # ARPAbet: plain letters, but not a respelling
        ("//", "slashes"),
        ("[shi-vawn]", "brackets"),
        ("[sic]", "brackets"),
        ("[]", "brackets"),
    ],
)
def test_plain_letters_in_slashes_or_brackets_say_what_to_leave_out(
    say: str, notation: str
) -> None:
    assert phonetic_notation(say) == notation
    found, warnings = parse(f"Siobhan → {say}\n", "x.txt")
    assert len(found) == 0
    assert warnings == [
        f"x.txt, line 1: '{say}' is in {notation}, the way phonetic spellings are written, so "
        f"the line is skipped; leave the {notation} out and write a respelling for 'Siobhan' "
        "instead, the way it sounds in plain letters, like shi-vawn"
    ]


def test_spaces_around_a_value_dont_hide_its_slashes_or_brackets() -> None:
    assert phonetic_notation("  /shi-vawn/ ") == "slashes"
    assert phonetic_notation("\t[shi-vawn]  ") == "brackets"
    assert phonetic_notation(" [[S@v'O:n]] ") == "markup"
    assert phonetic_notation("  shi-vawn  ") is None


RESPELLINGS = [
    pytest.param("seer-sha", id="hyphenated respelling"),
    pytest.param("SEER-sha", id="capitals for stress"),
    pytest.param("her-MY-oh-nee", id="capitals in the middle"),
    pytest.param("Hawaiʻi", id="okina U+02BB"),
    pytest.param("ha-WAI-ʻee", id="okina in a respelling"),
    pytest.param("Hawaiʼi", id="modifier apostrophe U+02BC, said as an apostrophe"),
    pytest.param("Hawai'i", id="plain apostrophe"),
    pytest.param("Hawai‘i", id="left quotation mark as okina"),
    pytest.param("Hawai’i", id="right quotation mark as apostrophe"),
    pytest.param("shi'vawn", id="apostrophe where IPA would put stress"),
    pytest.param("pé-ña", id="accented Latin letters"),
    pytest.param("Bjørn Łukasz", id="o with stroke and l with stroke"),
    pytest.param("Æðelstan Þór", id="ash, eth and thorn"),
    pytest.param("ʿa-LEE Qurʾān", id="half rings from transliterated Arabic"),
    pytest.param("Ilʹich", id="prime from transliterated Russian"),
    pytest.param("ay slash bee", id="the word slash"),
    pytest.param("/", id="a lone slash"),
    pytest.param("a/b/c", id="slashes inside a respelling"),
    pytest.param("ay [bee] see", id="brackets inside a respelling"),
    pytest.param("[ay] bee [see]", id="two bracketed pieces, not one pair"),
    pytest.param("/ay/ bee /see/", id="two slashed pieces, not one pair"),
    pytest.param("C#-ish", id="a hash inside a word"),
]


@pytest.mark.parametrize("say", RESPELLINGS)
def test_respellings_and_the_letters_names_use_are_kept(say: str) -> None:
    assert not is_phonetic(say)
    found, warnings = parse(f"Siobhan → {say}\n")
    assert warnings == []
    assert found.entries == (Pronunciation("Siobhan", say),)


def test_the_skipped_phonetic_lines_can_be_found() -> None:
    text = "# mine\nSiobhan → ʃɪˈvɔːn  # wiki\nZorp → zawrp\nno arrow\nsiobhan → /shi-vawn/\n"
    assert phonetic_lines(text) == (
        Pronunciation("Siobhan", "ʃɪˈvɔːn"),
        Pronunciation("siobhan", "/shi-vawn/"),
    )


def test_only_the_say_it_as_side_is_checked() -> None:
    # Ewe and Akan spell names with ɔ and ɛ: a book's name can hold them.
    found, warnings = parse("Kɔfi → KO-fee\n")
    assert warnings == []
    assert found.apply("Kɔfi waved.") == "ko-fee waved."


def test_a_list_with_phonetic_lines_keeps_its_other_fixes(tmp_path: Path) -> None:
    """A list saved before this check (or written in an editor) still loads: only the
    phonetic lines are skipped, each with its own note."""
    data, book = tmp_path / "data", tmp_path / "book.epub"
    data.mkdir()
    pronunciations.global_list_path(data).write_text(
        "Siobhan → ʃɪˈvɔːn\nHermione → her-MY-oh-nee\n", encoding="utf-8"
    )
    pronunciations.book_list_path(book).write_text(
        "Galadriel → [ɡəˈlædriəl]\nZorp → ZAWRP\n", encoding="utf-8"
    )
    found, warnings = pronunciations.load_for_book(data, book)
    assert found.entries == (
        Pronunciation("Hermione", "her-MY-oh-nee"),
        Pronunciation("Zorp", "ZAWRP"),
    )
    assert [w.split(":")[0] for w in warnings] == [
        "pronunciations.txt, line 1",
        "book.pronunciations.txt, line 1",
    ]
    assert found.apply("Siobhan met Galadriel.") == "Siobhan met Galadriel."


@pytest.mark.parametrize(
    ("written", "text", "spoken"),
    [
        ("Qur\u02bean", "The Qur\u02bean.", "The kur-ahn."),  # the same mark both sides
        ("Qur\u02bean", "The Qur'an.", "The kur-ahn."),  # the list's mark, the book's '
        ("Qur'an", "The Qur\u02bean.", "The kur-ahn."),  # the other way round
        ("Siobhan", "Siobhan\u02bcs hat", "shi-vawn's hat"),  # a possessive with U+02BC
        ("siobhan", "SIOBHAN\u02bcS HAT", "shi-vawn'S HAT"),
        ("\u02be", "\u02be is hamza", "kur-ahn is hamza"),  # a lone mark matches itself
    ],
)
def test_apostrophe_like_marks_match_as_apostrophes_on_both_sides(
    written: str, text: str, spoken: str
) -> None:
    say = "shi-vawn" if written.lower() == "siobhan" else "kur-AHN"
    assert fixes((written, say)).apply(text) == spoken


def test_marks_count_as_apostrophes_when_comparing_left_sides() -> None:
    assert pronunciations.matches("Qur\u02bean", "Qur'an")
    assert pronunciations.matches("qur'an", "Qur\u02bean")
    assert pronunciations.same_written("Qur\u02bean", "Qur'an")
    assert fixes(("Qur\u02bean", "x")).written_words() == {"qur'an"}
    # A line for the same left side is found and changed whichever way it was written...
    assert pronunciations.edit("Qur\u02bean → x\n", "Qur'an", "y") == "Qur\u02bean → y\n"
    # ...and a new line keeps the spelling it was given.
    assert pronunciations.edit("", "Qur\u02bean", "kur-AHN") == "Qur\u02bean → kur-AHN\n"


def test_the_skipped_lines_for_a_word_can_be_taken_out() -> None:
    text = (
        "# mine\n"
        "dinah → /\u02c8da\u026an\u0259/  # wiki\n"
        "Zorp → zawrp\n"
        "Dinah → \u02c8di\u02d0n\u0259\n"
        "Dinah → DIE-nuh\n"
        "Dina → \u02c8di\u02d0n\u0259\n"
    )
    assert without_phonetic(text, "Dinah") == (
        "# mine\nZorp → zawrp\nDinah → DIE-nuh\nDina → \u02c8di\u02d0n\u0259\n"
    )


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


def test_a_later_capitalised_entry_beats_an_earlier_any_case_one() -> None:
    # The global list has "alice", the book's own list has "Alice": the book's wins where
    # its spelling matches, and the any-case entry still covers the other spellings.
    combined = fixes(("alice", "global")).then(fixes(("Alice", "book")))
    assert combined.apply("Alice, ALICE and alice.") == "book, book and global."


def test_a_later_any_case_entry_beats_an_earlier_capitalised_one() -> None:
    combined = fixes(("Alice", "global")).then(fixes(("alice", "book")))
    assert combined.apply("Alice, ALICE and alice.") == "book, book and book."


def test_an_entry_written_in_capitals_beats_another_entrys_all_caps_form() -> None:
    fix = fixes(("HERMIONE", "heading"), ("Hermione", "name"))
    assert fix.apply("HERMIONE met Hermione.") == "heading met name."


def test_the_longest_entry_wins_whichever_case_rule_it_has() -> None:
    fix = fixes(("new", "nyoo"), ("New Zealand", "noo zee-lund"))
    assert fix.apply("New Zealand, new shoes, NEW ZEALAND") == (
        "noo zee-lund, nyoo shoes, noo zee-lund"
    )


def test_a_capitalised_entry_never_matches_lowercase_even_inside_a_longer_match() -> None:
    fix = fixes(("Mark", "marc"), ("mark twain", "twain"))
    assert fix.apply("Mark and mark and Mark Twain.") == "marc and mark and twain."


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


def test_a_byte_order_mark_doesnt_cost_the_first_fix(tmp_path: Path) -> None:
    """Some editors save "UTF-8 with BOM": the mark is not part of the first left side."""
    path = tmp_path / "list.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "Zorp → zawrp\nAda -> ay-da\n".encode())
    found, warnings = pronunciations.load(path)
    assert warnings == []
    assert found.entries == (Pronunciation("Zorp", "zawrp"), Pronunciation("Ada", "ay-da"))
    assert found.apply("Zorp ran. Ada ran.") == "zawrp ran. ay-da ran."
    assert pronunciations.read_list(path) == "Zorp → zawrp\nAda -> ay-da\n"


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


# --- editing a list file (the window's Names screen) --------------------------------------------

HAND_MADE = """\
# Fixes for this book.
Hermione  →  her-MY-oh-nee   # checked with tomecast preview
Lothlórien -> loth-LOR-ee-en

# Places
New  Zealand → noo zee-lund
"""


def test_editing_a_fix_keeps_every_other_line_and_comment() -> None:
    edited = pronunciations.edit(HAND_MADE, "Hermione", "her-MY-uh-nee")
    assert edited == HAND_MADE.replace("her-MY-oh-nee", "her-MY-uh-nee")
    # The left side, the arrow, the spacing and the comment are the user's.
    assert "Hermione  →  her-MY-uh-nee   # checked with tomecast preview\n" in edited


def test_a_new_fix_goes_at_the_end() -> None:
    edited = pronunciations.edit(HAND_MADE, "Gryphon", "GRIFF-on")
    assert edited == HAND_MADE + "Gryphon → GRIFF-on\n"
    assert pronunciations.edit("", "Gryphon", "GRIFF-on") == "Gryphon → GRIFF-on\n"
    # A file without a final newline gets one first; Windows line endings are kept.
    assert pronunciations.edit("Zed → zed", "Gryphon", "x") == "Zed → zed\nGryphon → x\n"
    assert pronunciations.edit("Zed → zed\r\n", "Gryphon", "x") == "Zed → zed\r\nGryphon → x\r\n"


def test_removing_a_fix_removes_only_its_lines() -> None:
    edited = pronunciations.edit(HAND_MADE, "Lothlórien", None)
    assert edited == HAND_MADE.replace("Lothlórien -> loth-LOR-ee-en\n", "")
    # Spacing inside the left side doesn't matter; a word with no fix changes nothing.
    assert "Zealand" not in pronunciations.edit(HAND_MADE, "New Zealand", None)
    assert pronunciations.edit(HAND_MADE, "Gryphon", None) == HAND_MADE
    # A comment that mentions the word is not a fix line.
    text = "# Hermione → not a fix\nHermione → her\n"
    assert pronunciations.edit(text, "Hermione", None) == "# Hermione → not a fix\n"


def test_duplicates_the_last_line_is_changed_and_all_are_removed() -> None:
    text = "Zed → zee\nZed → zed\n"  # the later line wins when converting
    assert pronunciations.edit(text, "Zed", "zehd") == "Zed → zee\nZed → zehd\n"
    assert pronunciations.edit(text, "Zed", None) == ""


def test_an_edited_list_reads_back_as_the_fixes_the_window_shows() -> None:
    edited = pronunciations.edit(HAND_MADE, "Hermione", "her-MY-uh-nee")
    edited = pronunciations.edit(edited, "Gryphon", "GRIFF-on")
    edited = pronunciations.edit(edited, "Lothlórien", None)
    found, warnings = parse(edited)
    assert warnings == []
    assert found.entries == (
        Pronunciation("Hermione", "her-MY-uh-nee"),
        Pronunciation("New  Zealand", "noo zee-lund"),
        Pronunciation("Gryphon", "GRIFF-on"),
    )


def test_matches_follows_the_case_rules() -> None:
    assert pronunciations.matches("siobhan", "Siobhan")
    assert pronunciations.matches("siobhan", "SIOBHAN")
    assert pronunciations.matches("Mark", "Mark")
    assert pronunciations.matches("Mark", "MARK")
    assert not pronunciations.matches("Mark", "mark")
    assert not pronunciations.matches("Ron", "Ronald")
    assert pronunciations.matches("New  Zealand", "New Zealand")


def test_save_writes_the_whole_list_and_keeps_its_permissions(tmp_path: Path) -> None:
    path = tmp_path / "Book.pronunciations.txt"
    pronunciations.save(path, "Zed → zed\n")
    assert path.read_text(encoding="utf-8") == "Zed → zed\n"
    path.chmod(0o640)
    pronunciations.save(path, "Zed → zehd\n")
    assert path.read_text(encoding="utf-8") == "Zed → zehd\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left


def test_save_writes_through_a_link_and_removes_an_empty_list(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "pronunciations.txt"
    real.parent.mkdir()
    real.write_text("Zed → zed\n", encoding="utf-8")
    link = tmp_path / "data" / "pronunciations.txt"
    link.parent.mkdir()
    link.symlink_to(real)
    pronunciations.save(link, "Zed → zehd\n")
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "Zed → zehd\n"

    plain = tmp_path / "Book.pronunciations.txt"
    plain.write_text("Zed → zed\n", encoding="utf-8")
    pronunciations.save(plain, "")
    assert not plain.exists()  # nothing left: no empty file next to the book
    pronunciations.save(tmp_path / "never.txt", "")
    assert not (tmp_path / "never.txt").exists()
