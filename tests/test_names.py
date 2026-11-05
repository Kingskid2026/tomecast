from tomecast.book import Book, Chapter
from tomecast.names import Name, common_words, find_book_names, find_names

# A made-up story: the invented words are the "names" to find.
STORY = [
    "Galadriel whispered to Thorin. The boats drifted toward Lothlorien.",
    "“Namarie,” said Galadriel. Thorin nodded, and the table was quiet.",
    "Later, Thorin and Galadriel walked to Lothlorien. Then Galadriel's table was set.",
    "Tomorrow Thorin went home. Table manners mattered little to Bilbo.",
    "Bilbo saw Lothlorien once, and Bilbo wrote of it.",
    "A Hobbit met Mr. Pelennor.",
]


def test_the_common_word_list_holds_ordinary_words_and_no_names() -> None:
    words = common_words()
    assert {"the", "table", "water", "because", "nevertheless"} <= words
    assert not words & {"holmes", "darcy", "dracula", "london", "galadriel"}
    assert 10_000 < len(words) < 100_000
    assert all(word == word.lower() for word in words)


def test_finds_frequent_unusual_capitalised_words_most_frequent_first() -> None:
    found = find_names(STORY)
    assert found == [
        Name("Galadriel", 4),
        Name("Thorin", 4),
        Name("Bilbo", 3),
        Name("Lothlorien", 3),
    ]


def test_min_count_decides_how_often_a_word_must_appear() -> None:
    assert [n.word for n in find_names(STORY, min_count=4)] == ["Galadriel", "Thorin"]
    assert find_names(STORY, min_count=100) == []
    assert {n.word for n in find_names(STORY, min_count=1)} >= {"Hobbit", "Pelennor"}


def test_words_the_book_also_writes_in_lowercase_are_not_names() -> None:
    names = {n.word for n in find_names(STORY, min_count=1)}
    assert "Table" not in names  # "table" appears in lowercase too
    assert "Tomorrow" not in names  # an ordinary word


def test_a_word_that_only_starts_sentences_is_not_a_name() -> None:
    text = ["Zorp is here. Zorp went. Zorp came."]
    assert find_names(text, min_count=1) == []
    assert find_names(["Then Zorp is here. Zorp went. Zorp came."], min_count=1) == [
        Name("Zorp", 3)
    ]


def test_skip_leaves_out_words_that_already_have_a_fix() -> None:
    found = find_names(STORY, skip=["galadriel", "THORIN"])
    assert [n.word for n in found] == ["Bilbo", "Lothlorien"]


def test_possessives_count_for_the_name() -> None:
    text = ["Then Zorp's hat. Then Zorp's coat. Then Zorp left."]
    assert find_names(text, min_count=3) == [Name("Zorp", 3)]


def test_all_caps_words_and_single_letters_are_ignored() -> None:
    text = ["Then NASA flew. Then NASA landed. Then NASA left. Then I A B went."] * 2
    assert find_names(text, min_count=1) == []


def test_a_custom_common_word_list_can_be_given() -> None:
    found = find_names(STORY, common=frozenset({"galadriel"}))
    assert "Galadriel" not in {n.word for n in found}


def test_accented_names_are_found_whole() -> None:
    text = ["Then Lothlórien rose. Then Lothlórien fell. Then Lothlórien slept."]
    assert find_names(text) == [Name("Lothlórien", 3)]


def test_find_book_names_reads_titles_and_paragraphs_of_every_chapter() -> None:
    book = Book(
        title="T",
        authors=(),
        language="en",
        chapters=(
            Chapter("Then Zorp", ("Then Zorp came.",)),
            Chapter("", ("Then Zorp left.",)),
        ),
    )
    assert find_book_names(book, min_count=3) == [Name("Zorp", 3)]
    assert find_book_names(book, min_count=3, skip=["zorp"]) == []


def test_contractions_are_not_names() -> None:
    text = ["I\u2019m here. I\u2019ll go. I\u2019ve gone. Then I\u2019d stay. Then I'm done."]
    assert find_names(text * 3, min_count=1) == []
