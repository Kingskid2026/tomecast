"""The two pronunciation lists the Names screen edits: the book's own, and the global one.

Each list is kept as its file's text. A change is applied to the text with
:func:`tomecast.pronunciations.edit` (so the user's other lines and comments stay as they
were) and saved at once. Before a change, the file is read again, so lines the user added
in an editor while the window was open are kept too.

A change that can't be saved (the book's folder is read-only, say) stays in the text for
this session, and :attr:`FixLists.unsaved` says so: the window then hands the conversion
these lists rather than reading the files again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tomecast.pronunciations import (
    Notation,
    Pronunciation,
    Pronunciations,
    edit,
    matches,
    parse,
    phonetic_lines,
    phonetic_notation,
    read_list,
    same_written,
    save,
)

Scope = Literal["book", "all"]  # this book's list, or the one for every book
SCOPES: tuple[Scope, ...] = ("book", "all")

PHONETIC = (
    "That looks like IPA. This voice can't use phonetic symbols, so type the word the way it "
    "sounds in plain letters, like shiv-AWN."
)
MARKUP = (
    "This voice can't use eSpeak's [[ ]] phoneme markup, so type the word the way it sounds "
    "in plain letters, like shiv-AWN."
)
LEAVE_OUT = "Leave out the {}: type just the way the word sounds, in plain letters, like shiv-AWN."
NOTATIONS: dict[Notation, str] = {
    "IPA": PHONETIC,
    "markup": MARKUP,
    "slashes": LEAVE_OUT.format("slashes"),
    "brackets": LEAVE_OUT.format("brackets"),
}
COMMENT = "Leave out the “#”: in a pronunciation list it starts a comment."


class FixNotSaved(Exception):
    """A change that couldn't be saved. It's kept for this session; the message says so."""


class BadFix(ValueError):
    """A respelling a pronunciation list can't hold. The message says why."""


@dataclass(frozen=True, slots=True)
class Fix:
    """One fix, as its list has it."""

    word: str  # the left side ("Gryphon")
    say: str  # the right side, capitals and all ("GRIFF-on")
    scope: Scope


def _read(path: Path) -> str:
    """A list file's text; a missing file is an empty list.

    Raises:
        OSError, UnicodeDecodeError: if it exists but can't be read.
    """
    try:
        return read_list(path)
    except FileNotFoundError:
        return ""


def _reason(error: BaseException) -> str:
    if isinstance(error, OSError) and error.strerror:
        return error.strerror.lower()
    if isinstance(error, UnicodeDecodeError):
        return "it isn't UTF-8 text"
    return str(error)


@dataclass
class ListFile:
    """One list file and its text, as last read or changed."""

    path: Path
    text: str = ""
    problem: str | None = None  # why it couldn't be read: Tomecast then never writes it
    unsaved: bool = False  # holds a change that couldn't be saved

    @classmethod
    def read(cls, path: Path) -> ListFile:
        try:
            return cls(path, _read(path))
        except (OSError, UnicodeDecodeError) as error:
            return cls(path, "", problem=_reason(error))

    def entries(self) -> tuple[Pronunciation, ...]:
        return parse(self.text)[0].entries

    def phonetic(self) -> tuple[Pronunciation, ...]:
        """The lines skipped because they are phonetic spellings (they show as no fix)."""
        return phonetic_lines(self.text)

    def latest(self) -> dict[str, str]:
        """Left side -> right side, one per left side (a later line wins, as when converting)."""
        found: dict[str, str] = {}
        for entry in self.entries():
            key = " ".join(entry.written.split())
            found.pop(key, None)  # so the order is that of the lines that count
            found[key] = entry.spoken
        return found

    def change(self, written: str, spoken: str | None, *, keep_on_failure: bool = True) -> None:
        """Set the fix for ``written`` (or remove it, with ``None``) and save the file.

        Raises:
            FixNotSaved: if the file couldn't be read or written. With ``keep_on_failure``
                the change is kept for this session; without, nothing changes.
        """
        before = (self.text, self.problem, self.unsaved)
        base = self.text
        if self.problem is None and not self.unsaved:
            try:
                base = _read(self.path)  # keeps lines added in an editor meanwhile
            except (OSError, UnicodeDecodeError) as error:
                self.problem = _reason(error)
        self.text = edit(base, written, spoken)
        failure: str | None = None
        if self.problem is not None:
            failure = f"Tomecast couldn't read {self.path} ({self.problem}), so it won't change it."
        else:
            try:
                save(self.path, self.text)
            except OSError as error:
                failure = (
                    f"Tomecast couldn't save {self.path.name} in {self.path.parent} "
                    f"({_reason(error)})."
                )
        if failure is None:
            self.unsaved = False
            return
        if keep_on_failure:
            self.unsaved = True
            raise FixNotSaved(f"{failure} The change works until you close Tomecast.")
        self.text, self.problem, self.unsaved = before
        raise FixNotSaved(failure)


def check(written: str, say: str) -> None:
    """Make sure ``written → say`` can be saved as a line and read back the same.

    Raises:
        BadFix: for a phonetic spelling (IPA, eSpeak markup, or a value in slashes or
            brackets: see :func:`tomecast.pronunciations.phonetic_notation`), a "#" that
            would start a comment, or anything else that wouldn't read back as typed.
    """
    notation = phonetic_notation(say)
    if notation is not None:
        raise BadFix(NOTATIONS[notation])
    fixes, warnings = parse(f"{written} → {say}")
    if warnings or fixes.entries != (Pronunciation(written, say),):
        raise BadFix(COMMENT if "#" in say else f"Tomecast can't save “{say}” as a fix.")


class FixLists:
    """The book's list and the global list, as the Names screen shows and changes them."""

    def __init__(self, book: ListFile, every: ListFile) -> None:
        self._files: dict[Scope, ListFile] = {"book": book, "all": every}
        self._combined: Pronunciations | None = None

    @classmethod
    def empty(cls) -> FixLists:
        """No book: nothing to show, nothing that can be saved."""
        return cls(ListFile(Path(), problem="no book"), ListFile(Path(), problem="no book"))

    def file(self, scope: Scope) -> ListFile:
        return self._files[scope]

    @property
    def unsaved(self) -> bool:
        """True if a change couldn't be saved, so the files don't hold every fix."""
        return any(f.unsaved for f in self._files.values())

    def combined(self) -> Pronunciations:
        """Every fix for this book: the global list, then the book's, which wins (exactly as
        :func:`tomecast.pronunciations.load_for_book` reads the files)."""
        if self._combined is None:
            every = Pronunciations(self._files["all"].entries())
            self._combined = every.then(Pronunciations(self._files["book"].entries()))
        return self._combined

    def fixes(self) -> list[Fix]:
        """One per left side: this book's fixes, then the global ones it doesn't override,
        each in its list's order."""
        book = self._files["book"].latest()
        every = self._files["all"].latest()
        return [Fix(w, s, "book") for w, s in book.items()] + [
            Fix(w, s, "all") for w, s in every.items() if w not in book
        ]

    def fix_for(self, word: str) -> Fix | None:
        """The fix that applies to ``word``: the book's list first, then the global one."""
        for scope in SCOPES:
            for written, spoken in reversed(self._files[scope].latest().items()):
                if matches(written, word):
                    return Fix(written, spoken, scope)
        return None

    def set_for_name(self, word: str, say: str) -> None:
        """Save ``say`` as the fix for ``word`` (a name the finder listed); an empty ``say``
        removes its fix. A fix that exists is changed in its own list. Otherwise a line
        for ``word`` that is skipped as a phonetic spelling (IPA saved before Tomecast
        refused it, say) is given the respelling, in whichever list has it, so its warning
        goes away; failing that, the new fix goes in the book's list.

        Raises:
            BadFix: if ``say`` can't be saved (nothing changes).
            FixNotSaved: if the file couldn't be written (the change is kept for now).
        """
        say = say.strip()
        current = self.fix_for(word)
        if not say:
            if current is not None:
                self._change(current.scope, current.word, None)
            return
        if current is not None and current.say == say:
            return
        target = current or self._phonetic_for(word)
        check(target.word if target else word, say)
        if target is not None:
            self._change(target.scope, target.word, say)
        else:
            self._change("book", word, say)

    def _phonetic_for(self, word: str) -> Fix | None:
        """A line for ``word`` skipped as a phonetic spelling: the book's list first, then the
        global one, matched the way a fix matches a word (so ``siobhan`` covers *Siobhan*)."""
        for scope in SCOPES:
            for entry in reversed(self._files[scope].phonetic()):
                if matches(entry.written, word):
                    return Fix(entry.written, entry.spoken, scope)
        return None

    def remove(self, scope: Scope, word: str) -> None:
        """Remove the fix for ``word`` from one list.

        Raises:
            FixNotSaved: if the file couldn't be written (it's removed for now).
        """
        if self._find(scope, word) is not None:
            self._change(scope, word, None)

    def move(self, scope: Scope, word: str) -> None:
        """Move the fix for ``word`` from one list to the other (this book's ↔ every book's).

        Raises:
            FixNotSaved: if either file couldn't be written. When the first write fails,
                nothing changes; when the second does, the fix is moved for this session
                and the file it came from still has it.
        """
        found = self._find(scope, word)
        if found is None:
            return
        other: Scope = "all" if scope == "book" else "book"
        self._change(other, found.word, found.say, keep_on_failure=False)
        self._change(scope, found.word, None)

    def _find(self, scope: Scope, word: str) -> Fix | None:
        for written, spoken in self._files[scope].latest().items():
            if same_written(written, word):
                return Fix(written, spoken, scope)
        return None

    def _change(
        self, scope: Scope, written: str, spoken: str | None, *, keep_on_failure: bool = True
    ) -> None:
        self._combined = None
        self._files[scope].change(written, spoken, keep_on_failure=keep_on_failure)
