"""The pronunciation lists as the Names screen edits them (tomecast.gui.fixes), on real files."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tomecast import pronunciations
from tomecast.gui.fixes import PHONETIC, BadFix, Fix, FixLists, FixNotSaved, ListFile

BOOK_LIST = """\
# Alice: checked by ear
Gryphon → GRIFF-on   # not "gry-fon"
"""
GLOBAL_LIST = """\
# Every book
Hermione → her-MY-oh-nee
"""


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    """A book with its own list, and a data folder with the global one."""
    books, data = tmp_path / "books", tmp_path / "data"
    books.mkdir()
    data.mkdir()
    (books / "alice.epub").write_bytes(b"not read here")
    (books / "alice.pronunciations.txt").write_text(BOOK_LIST, encoding="utf-8")
    (data / "pronunciations.txt").write_text(GLOBAL_LIST, encoding="utf-8")
    return books / "alice.epub", data


def _lists(book: Path, data: Path) -> FixLists:
    return FixLists(
        ListFile.read(pronunciations.book_list_path(book)),
        ListFile.read(pronunciations.global_list_path(data)),
    )


def _book_text(book: Path) -> str:
    return pronunciations.book_list_path(book).read_text(encoding="utf-8")


def _global_text(data: Path) -> str:
    return pronunciations.global_list_path(data).read_text(encoding="utf-8")


@pytest.fixture
def read_only(tmp_path: Path) -> Iterator[None]:
    if os.geteuid() == 0:
        pytest.skip("root can write to read-only folders")
    yield


def test_the_lists_show_each_fix_once_with_its_list(folders: tuple[Path, Path]) -> None:
    lists = _lists(*folders)
    assert lists.fixes() == [
        Fix("Gryphon", "GRIFF-on", "book"),
        Fix("Hermione", "her-MY-oh-nee", "all"),
    ]
    assert lists.fix_for("Gryphon") == Fix("Gryphon", "GRIFF-on", "book")
    assert lists.fix_for("Alice") is None
    assert lists.combined().apply("The Gryphon met Hermione.") == "The griff-on met her-my-oh-nee."


def test_a_new_fix_goes_in_the_books_list_and_keeps_the_users_lines(
    folders: tuple[Path, Path],
) -> None:
    book, data = folders
    lists = _lists(book, data)
    lists.set_for_name("Dinah", "  DIE-nuh ")
    assert _book_text(book) == BOOK_LIST + "Dinah → DIE-nuh\n"
    assert _global_text(data) == GLOBAL_LIST
    assert lists.fix_for("Dinah") == Fix("Dinah", "DIE-nuh", "book")
    assert not lists.unsaved
    # Exactly what a conversion (and the command line) reads from the files.
    from_files, warnings = pronunciations.load_for_book(data, book)
    assert warnings == []
    assert lists.combined() == from_files


def test_changing_and_clearing_a_fix_edits_the_list_that_has_it(
    folders: tuple[Path, Path],
) -> None:
    book, data = folders
    lists = _lists(book, data)
    lists.set_for_name("Gryphon", "GRIF-un")
    assert _book_text(book) == BOOK_LIST.replace("GRIFF-on", "GRIF-un")  # comment kept
    lists.set_for_name("Hermione", "her-MY-uh-nee")  # a global fix stays global
    assert _global_text(data) == GLOBAL_LIST.replace("oh-nee", "uh-nee")
    lists.set_for_name("Gryphon", "")
    assert _book_text(book) == "# Alice: checked by ear\n"
    assert lists.fix_for("Gryphon") is None


def test_an_unchanged_fix_is_not_written_again(folders: tuple[Path, Path]) -> None:
    book, data = folders
    lists = _lists(book, data)
    path = pronunciations.book_list_path(book)
    before = path.stat().st_mtime_ns
    os.utime(path, ns=(before - 10**9, before - 10**9))
    lists.set_for_name("Gryphon", "GRIFF-on")
    lists.set_for_name("Alice", "")  # no fix to remove
    assert path.stat().st_mtime_ns == before - 10**9


@pytest.mark.parametrize(
    ("say", "message"),
    [
        ("/ˈɡrɪfən/", PHONETIC),
        ("ˈdaɪnə", PHONETIC),  # plain IPA
        ("[ˈdaɪnə]", PHONETIC),  # as Wikipedia writes it
        ("[[d'aIn@]]", PHONETIC),  # eSpeak's phoneme markup
        ("DIE-nˈuh", PHONETIC),  # a stress mark is enough
        ("die-nuː", PHONETIC),  # and so is a length mark
        ("griff #on", "Leave out the “#”"),
        ("#griff", "Leave out the “#”"),
    ],
)
def test_a_fix_the_list_cant_hold_is_refused(
    folders: tuple[Path, Path], say: str, message: str
) -> None:
    book, data = folders
    lists = _lists(book, data)
    with pytest.raises(BadFix, match=re.escape(message)):
        lists.set_for_name("Dinah", say)
    assert _book_text(book) == BOOK_LIST
    assert lists.fix_for("Dinah") is None


@pytest.mark.parametrize("say", ["ha-WAI-ʻee", "Hawaiʼi", "Hawai'i", "DEE-nah", "Dína", "Ðina"])
def test_names_marks_and_accents_are_saved(folders: tuple[Path, Path], say: str) -> None:
    book, data = folders
    lists = _lists(book, data)
    lists.set_for_name("Dinah", say)
    assert lists.fix_for("Dinah") == Fix("Dinah", say, "book")
    assert _book_text(book) == f"{BOOK_LIST}Dinah → {say}\n"


def test_an_ipa_line_already_in_a_list_is_skipped_and_a_new_fix_replaces_it(
    folders: tuple[Path, Path],
) -> None:
    book, data = folders
    path = pronunciations.book_list_path(book)
    path.write_text(f"{BOOK_LIST}Dinah → ˈdaɪnə   # from Wikipedia\n", encoding="utf-8")
    lists = _lists(book, data)
    assert lists.fix_for("Dinah") is None  # no fix: said as written, as when converting
    assert lists.combined().apply("Dinah") == "Dinah"
    lists.set_for_name("Dinah", "DIE-nuh")
    assert _book_text(book) == f"{BOOK_LIST}Dinah → DIE-nuh   # from Wikipedia\n"
    assert lists.fix_for("Dinah") == Fix("Dinah", "DIE-nuh", "book")


def test_a_respelling_with_a_hash_inside_a_word_is_fine(folders: tuple[Path, Path]) -> None:
    book, data = folders
    lists = _lists(book, data)
    lists.set_for_name("Dinah", "C#-ish")
    assert lists.fix_for("Dinah") == Fix("Dinah", "C#-ish", "book")
    assert pronunciations.load_for_book(data, book)[0].apply("Dinah") == "C#-ish"


def test_moving_a_fix_between_the_lists(folders: tuple[Path, Path]) -> None:
    book, data = folders
    lists = _lists(book, data)
    lists.move("book", "Gryphon")
    assert _book_text(book) == "# Alice: checked by ear\n"
    assert _global_text(data) == GLOBAL_LIST + "Gryphon → GRIFF-on\n"
    assert lists.fix_for("Gryphon") == Fix("Gryphon", "GRIFF-on", "all")
    lists.move("all", "Hermione")
    assert _global_text(data) == "# Every book\nGryphon → GRIFF-on\n"
    assert _book_text(book).endswith("Hermione → her-MY-oh-nee\n")
    assert lists.fixes() == [
        Fix("Hermione", "her-MY-oh-nee", "book"),
        Fix("Gryphon", "GRIFF-on", "all"),
    ]
    lists.remove("all", "Gryphon")
    assert _global_text(data) == "# Every book\n"
    assert lists.fix_for("Gryphon") is None


def test_lines_added_in_an_editor_meanwhile_are_kept(folders: tuple[Path, Path]) -> None:
    book, data = folders
    lists = _lists(book, data)
    path = pronunciations.book_list_path(book)
    path.write_text(BOOK_LIST + "Dodo → DOH-doh\n", encoding="utf-8")  # by hand, after loading
    lists.set_for_name("Dinah", "DIE-nuh")
    assert _book_text(book) == BOOK_LIST + "Dodo → DOH-doh\nDinah → DIE-nuh\n"
    assert lists.fix_for("Dodo") == Fix("Dodo", "DOH-doh", "book")


def test_a_read_only_book_folder_keeps_the_fix_for_the_session(
    folders: tuple[Path, Path], read_only: None
) -> None:
    book, data = folders
    lists = _lists(book, data)
    book.parent.chmod(0o555)
    try:
        with pytest.raises(
            FixNotSaved, match=re.escape("couldn't save alice.pronunciations.txt")
        ) as caught:
            lists.set_for_name("Dinah", "DIE-nuh")
        assert "until you close Tomecast" in str(caught.value)
        assert _book_text(book) == BOOK_LIST  # the file is untouched
        assert lists.unsaved
        assert lists.combined().apply("Dinah") == "die-nuh"  # but the fix works now
        # Moving to the book's list can't be saved either: then nothing moves at all.
        with pytest.raises(FixNotSaved):
            lists.move("all", "Hermione")
        assert lists.fix_for("Hermione") == Fix("Hermione", "her-MY-oh-nee", "all")
        assert _global_text(data) == GLOBAL_LIST
    finally:
        book.parent.chmod(0o755)
    lists.set_for_name("Dodo", "DOH-doh")  # writable again: everything so far is saved
    assert _book_text(book) == BOOK_LIST + "Dinah → DIE-nuh\nDodo → DOH-doh\n"
    assert not lists.unsaved


def test_a_list_that_cant_be_read_is_never_overwritten(folders: tuple[Path, Path]) -> None:
    book, data = folders
    path = pronunciations.book_list_path(book)
    path.write_bytes(b"Gryphon \xff\xfe broken\n")
    lists = _lists(book, data)
    assert lists.fixes() == [Fix("Hermione", "her-MY-oh-nee", "all")]
    with pytest.raises(FixNotSaved, match="couldn't read"):
        lists.set_for_name("Dinah", "DIE-nuh")
    assert path.read_bytes() == b"Gryphon \xff\xfe broken\n"
    assert lists.combined().apply("Dinah") == "die-nuh"


def test_a_fix_for_a_lowercase_entry_is_the_one_that_applies(folders: tuple[Path, Path]) -> None:
    book, data = folders
    pronunciations.global_list_path(data).write_text("dinah → dee-nah\n", encoding="utf-8")
    lists = _lists(book, data)
    assert lists.fix_for("Dinah") == Fix("dinah", "dee-nah", "all")
    lists.set_for_name("Dinah", "DIE-nuh")  # changes that line, keeping its left side
    assert _global_text(data) == "dinah → DIE-nuh\n"
    assert _book_text(book) == BOOK_LIST
