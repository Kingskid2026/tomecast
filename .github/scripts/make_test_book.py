"""Write a tiny public-domain EPUB for the integration test, plus what the audiobook should hold.

Usage: python make_test_book.py BOOK.epub

Writes BOOK.epub and BOOK.expected.json (the chapter titles the M4B must have, in order).
The text is from Lewis Carroll's *Alice's Adventures in Wonderland* (1865), which is in the
public domain. The book is built with the tests' own EPUB factory, which needs only the
standard library, so this runs with any Python 3.11+.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from epub_factory import NavPoint, Page, build_epub

CHAPTERS = [
    (
        "Chapter I: Down the Rabbit-Hole",
        [
            "Alice was beginning to get very tired of sitting by her sister on the bank, "
            "and of having nothing to do.",
            "Suddenly a White Rabbit with pink eyes ran close by her.",
        ],
    ),
    (
        "Chapter II: The Pool of Tears",
        [
            "“Curiouser and curiouser!” cried Alice.",
            "She was so much surprised that for the moment she quite forgot how to speak "
            "good English.",
        ],
    ),
    (
        "Chapter III: A Caucus-Race and a Long Tale",
        [
            "They were indeed a queer-looking party that assembled on the bank.",
            # No closing punctuation: the last fragment of a chapter must still be spoken.
            "The first question of course was, how to get dry again",
        ],
    ),
]


def main() -> None:
    book = Path(sys.argv[1])
    pages = []
    toc = []
    for number, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        name = f"chapter{number}.xhtml"
        body = f"<h1>{escape(title)}</h1>" + "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
        pages.append(Page(name, body))
        toc.append(NavPoint(title, name))
    build_epub(
        book,
        pages,
        toc=toc,
        title="Alice's Adventures in Wonderland (excerpt)",
        authors=("Lewis Carroll",),
    )
    expected = {"chapters": [title for title, _ in CHAPTERS]}
    book.with_suffix(".expected.json").write_text(json.dumps(expected, indent=2))
    print(f"Wrote {book} with {len(CHAPTERS)} chapters")


if __name__ == "__main__":
    main()
