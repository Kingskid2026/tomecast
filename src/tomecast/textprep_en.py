"""English rules for :mod:`tomecast.textprep`: say numbers, dates, abbreviations and so on.

Everything here is deterministic and works on one paragraph (or heading) at a time. Each rule
only ever *replaces* a match with words that say the same thing, so no word of the book can
disappear; :func:`tomecast.textprep.words_lost` checks that as a safety net.

Other languages plug in by providing their own object with the same two methods
(``heading`` and ``body``); see :data:`tomecast.textprep.RULES`.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from tomecast.chunking import ABBREVIATIONS

# --- Number words ---------------------------------------------------------------------------

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = ("_ _ twenty thirty forty fifty sixty seventy eighty ninety").split()
_SCALES = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (10**3, "thousand"))
_ORDINAL_WORDS = {
    "one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
    "nine": "ninth", "twelve": "twelfth",
}  # fmt: skip
# Bigger numbers than this are read digit by digit.
_MAX_CARDINAL = 10**15


def cardinal(number: int) -> str:
    """``123`` -> ``"one hundred twenty-three"``. Works up to a quadrillion."""
    if number < 0:
        return "minus " + cardinal(-number)
    if number < 20:
        return _ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if number < 1000:
        hundreds, rest = divmod(number, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {cardinal(rest)}" if rest else "")
    for size, name in _SCALES:
        if number >= size:
            count, rest = divmod(number, size)
            return f"{cardinal(count)} {name}" + (f" {cardinal(rest)}" if rest else "")
    raise AssertionError("unreachable")  # pragma: no cover


def ordinal_word(word: str) -> str:
    """``"two"`` -> ``"second"``, ``"twenty"`` -> ``"twentieth"``, ``"four"`` -> ``"fourth"``."""
    if word in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[word]
    if word.endswith("y"):
        return word[:-1] + "ieth"
    return word + "th"


def ordinal(number: int) -> str:
    """``21`` -> ``"twenty-first"``."""
    text = cardinal(number)
    cut = max(text.rfind(" "), text.rfind("-")) + 1
    return text[:cut] + ordinal_word(text[cut:])


def digit_words(digits: str) -> str:
    """``"305"`` -> ``"three zero five"``: each digit on its own."""
    return " ".join(_ONES[int(d)] for d in digits if d.isdigit())


def spoken_number(whole: str, fraction: str | None = None) -> str:
    """Say a number written with digits: ``("1,250", "5")`` -> "one thousand two hundred fifty
    point five". Leading zeros ("007") and huge numbers are read digit by digit."""
    digits = whole.replace(",", "")
    leading_zero = len(digits) > 1 and digits.startswith("0")
    if leading_zero or int(digits) >= _MAX_CARDINAL:
        words = digit_words(digits)
    else:
        words = cardinal(int(digits))
    if fraction:
        words += " point " + digit_words(fraction)
    return words


def year(number: int) -> str:
    """Say a year: 1865 -> "eighteen sixty-five", 1905 -> "nineteen oh five", 1900 ->
    "nineteen hundred", 2005 -> "two thousand five", 2019 -> "twenty nineteen"."""
    if number == 1000 or 2000 <= number <= 2009:
        return cardinal(number)
    if 1001 <= number <= 2099:
        first, last = divmod(number, 100)
        if last == 0:
            return f"{cardinal(first)} hundred"
        if last < 10:
            return f"{cardinal(first)} oh {_ONES[last]}"
        return f"{cardinal(first)} {cardinal(last)}"
    return cardinal(number)


def _plural_tens(number: int) -> str:
    """``60`` -> ``"sixties"``, ``10`` -> ``"tens"``."""
    word = cardinal(number)
    return word[:-1] + "ies" if word.endswith("y") else word + "s"


# --- Roman numerals -------------------------------------------------------------------------

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
# Only well-formed numerals count: "IIII" and "VX" are not numbers.
ROMAN_PATTERN = r"(?=[IVXLCDM])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"


def roman_value(numeral: str) -> int:
    """``"XIV"`` -> 14."""
    total = 0
    for index, letter in enumerate(numeral):
        value = _ROMAN_VALUES[letter]
        if index + 1 < len(numeral) and value < _ROMAN_VALUES[numeral[index + 1]]:
            total -= value
        else:
            total += value
    return total


# --- Helpers --------------------------------------------------------------------------------

_CLOSERS = "\"')]\u201d\u2019»"
_OPENERS = "\"'([\u201c\u2018«"
# Put where a sentence starts while the rules run (see _mark_sentence_starts), and taken out
# at the end. It's a private-use character, so it can't be mistaken for the book's text.
_MARK = "\ue000"
_NUMBER = r"\d{1,3}(?:,\d{3})+(?!\d)|\d+"
# A letter, a currency sign, a slash or "1." directly before a number means it isn't a
# free-standing one (it is part of a word, a price, a fraction or a decimal).
_NOT_AFTER = r"(?<![\w$£€¥/])(?<!\d\.)"

MONTHS = (
    "January February March April May June July August September October November December"
).split()
_MONTH_ABBREVIATIONS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Sept": 9,
    "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip
_MONTH_FULL = "|".join(MONTHS)
_MONTH_SHORT = "|".join(sorted(_MONTH_ABBREVIATIONS, key=len, reverse=True))
# Quantities: "in 1500 men" is a number, not a year.
_QUANTITY_WORDS = frozenset(
    (
        "years year men women people dollars pounds miles feet words times days hours minutes "
        "seconds units kilometers meters percent soldiers copies pages votes cases members "
        "students children dead killed acres tons"
    ).split()
)


def _month_number(name: str) -> int:
    name = name.rstrip(".")
    if name in MONTHS:
        return MONTHS.index(name) + 1
    return _MONTH_ABBREVIATIONS[name]


def _match_case(original: str, spoken: str) -> str:
    """Say ``spoken`` in capitals if the abbreviation it replaces was in capitals."""
    return spoken.upper() if len(original) > 1 and original.isupper() else spoken


def _after_is_capital(text: str, end: int) -> bool:
    """True if the next word after ``end`` starts with a capital letter."""
    rest = text[end:].lstrip()
    rest = rest.lstrip(_OPENERS)
    return bool(rest) and rest[0].isupper()


def _before_word(text: str, start: int) -> str:
    """The word right before ``start`` ("" if none), without surrounding punctuation."""
    found = re.search(r"([^\W\d_]+)[^\w]*$", text[:start])
    return found.group(1) if found else ""


def _sentence_end_period(text: str, end: int) -> str:
    """``"."`` if an abbreviation whose own period ended at ``end`` also ends a sentence.

    "He left at 3 p.m." must keep its full stop once "p.m." is spelled out, or the chunker
    would run this sentence into the next one.
    """
    index = end
    while index < len(text) and text[index] in _CLOSERS:
        index += 1
    if index >= len(text):
        return "."
    if not text[index].isspace():
        return ""
    while index < len(text) and (text[index].isspace() or text[index] in _OPENERS):
        index += 1
    if index >= len(text):
        return "."
    return "." if text[index].isupper() else ""


def _valid_day(day: int) -> bool:
    return 1 <= day <= 31


def _year_text(value: str) -> str:
    return year(int(value))


# --- Dates and times ------------------------------------------------------------------------

_ISO_DATE = re.compile(rf"{_NOT_AFTER}(\d{{4}})-(\d{{2}})-(\d{{2}})(?![\w-])")
_MONTH_FIRST = re.compile(
    rf"\b(?P<month>{_MONTH_FULL}|(?:{_MONTH_SHORT})\b\.?)\s+(?P<day>\d{{1,2}})"
    rf"(?:st|nd|rd|th)?\b(?![:\d]|[.,]\d)(?:,?\s+(?P<year>\d{{4}})\b(?![,.]\d))?"
)
_MONTH_YEAR = re.compile(
    rf"\b(?P<month>{_MONTH_FULL}|(?:{_MONTH_SHORT})\b\.?)\s+(?P<year>\d{{4}})\b"
)
# Days listed before the last one: "2nd, 3rd and 4th May", "2 or 3 May", "3-4 May". At most
# six, so a long run of small numbers (an index, a table) costs the same at every number.
_LISTED_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_DAY_SEPARATOR = r"\s*,\s*(?:(?:and|or|&)\s+)?|\s+(?:and|or|&)\s+|\s?[-\u2013]\s?"
_DAY_FIRST = re.compile(
    rf"{_NOT_AFTER}(?P<days>(?:{_LISTED_DAY}(?:{_DAY_SEPARATOR})){{0,6}})"
    rf"(?P<day>\d{{1,2}})(?P<suffix>st|nd|rd|th)?(?:\s+of)?\s+"
    rf"(?P<month>{_MONTH_FULL}|(?:{_MONTH_SHORT})\.)(?:,?\s+(?P<year>\d{{4}})\b(?![,.]\d))?"
)
_DAY_LIST_ITEM = re.compile(r"(\d{1,2}(?:st|nd|rd|th)?)")
_DASHES = ("-", "\u2013")
# Words that already give "21 May" its article. Only words that can't be anything else right
# before a date count: "that" ("he said that 21 May was fine"), "her" ("I told her 21 May")
# and "last" in "at last" are often something else, so those dates get their "the" as usual.
_DATE_DETERMINERS = frozenset(
    "the every each this next last same my your his our their its".split()
)
_WORDS_BEFORE = re.compile(r"(?:\b(?P<previous>[^\W\d_]+)\s+)?\b(?P<word>[^\W\d_]+)\s+$")


def _has_determiner(text: str, start: int) -> bool:
    """True if the word right before ``start`` already gives a date its article."""
    found = _WORDS_BEFORE.search(text, max(0, start - 40), start)
    if found is None:
        return False
    word, previous = found["word"].lower(), (found["previous"] or "").lower()
    if word not in _DATE_DETERMINERS:
        return False
    return not (word == "last" and previous in {"at", "long"})


def _spoken_day_list(days: str, last: int, last_suffixed: bool) -> str | None:
    """The days listed before the last one, said as ordinals: ``"2nd and "`` -> ``"second and
    "``, ``"3-"`` -> ``"third to "``. ``None`` if they aren't a list of days, as in "of the
    12, 4 May have gone"."""
    if not days:
        return ""
    parts = _DAY_LIST_ITEM.split(days)
    items, separators = parts[1::2], parts[2::2]
    numbers = [int(item.rstrip("stndrh")) for item in items]
    if not all(_valid_day(number) for number in numbers):
        return None
    dashed = [separator.strip() in _DASHES for separator in separators]
    if any(dashed):
        if len(items) != 1 or numbers[0] >= last:
            return None  # a range is two days, the first one earlier: "3-4 May"
    else:
        suffixed = [not item.isdigit() for item in items] + [last_suffixed]
        joined = any(
            word in {"and", "or", "&"}
            for separator in separators
            for word in separator.replace(",", " ").split()
        )
        # "2nd, 3rd May" and "2 and 3 May" are lists; "12, 4 May" is a count and a date.
        if not (all(suffixed) or (joined and not any(suffixed))):
            return None
    spoken = []
    for number, separator, dash in zip(numbers, separators, dashed, strict=True):
        spoken.append(ordinal(number) + (" to " if dash else separator))
    return "".join(spoken)


def _iso_dates(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        y, m, d = (int(g) for g in match.groups())
        if not (1 <= m <= 12 and _valid_day(d)):
            return match.group(0)
        return f"{MONTHS[m - 1]} {ordinal(d)}, {year(y)}"

    return _ISO_DATE.sub(replace, text)


def _dates(text: str) -> str:
    def month_first(match: re.Match[str]) -> str:
        day = int(match["day"])
        if not _valid_day(day):
            return match.group(0)
        month = MONTHS[_month_number(match["month"]) - 1]
        spoken = f"{month} {ordinal(day)}"
        return spoken + (f", {_year_text(match['year'])}" if match["year"] else "")

    def month_year(match: re.Match[str]) -> str:
        month = MONTHS[_month_number(match["month"]) - 1]
        return f"{month} {_year_text(match['year'])}"

    def day_first(match: re.Match[str]) -> str:
        day = int(match["day"])
        if not _valid_day(day):
            return match.group(0)
        kept, start = "", match.start()
        listed = _spoken_day_list(match["days"], day, bool(match["suffix"]))
        if listed is None:  # only the last day is part of the date
            kept, listed, start = match["days"], "", match.start("day")
        month = MONTHS[_month_number(match["month"]) - 1]
        spoken = f"{listed}{ordinal(day)} of {month}"
        # "21 May" and "21st of May" are said "the twenty-first of May", but "the 21 May"
        # and "every 4th of July" already have their article.
        if not _has_determiner(match.string, start):
            spoken = "the " + spoken
        return kept + spoken + (f", {_year_text(match['year'])}" if match["year"] else "")

    text = _DAY_FIRST.sub(day_first, text)
    text = _MONTH_FIRST.sub(month_first, text)
    return _MONTH_YEAR.sub(month_year, text)


_CLOCK_WITH_MERIDIEM = re.compile(
    r"(?<![\w:.])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s?(?P<ap>[ap])\.?\s?m\b(?P<dot>\.?)", re.I
)
_CLOCK = re.compile(r"(?<![\w:.])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?![\w:]|\.\d)")


def _minutes(minute: int) -> str:
    return f"oh {_ONES[minute]}" if minute < 10 else cardinal(minute)


def _times(text: str) -> str:
    def with_meridiem(match: re.Match[str]) -> str:
        hour = int(match["h"])
        minute = int(match["m"]) if match["m"] else 0
        if not (1 <= hour <= 12 and minute < 60):
            return match.group(0)
        spoken = cardinal(hour)
        if minute:
            spoken += " " + _minutes(minute)
        spoken += " ay em" if match["ap"].lower() == "a" else " pee em"
        return spoken + (_sentence_end_period(match.string, match.end()) if match["dot"] else "")

    def plain(match: re.Match[str]) -> str:
        hour, minute = int(match["h"]), int(match["m"])
        if minute == 0:
            return f"{cardinal(hour)} o'clock" if hour <= 12 else f"{cardinal(hour)} hundred"
        return f"{cardinal(hour)} {_minutes(minute)}"

    return _CLOCK.sub(plain, _CLOCK_WITH_MERIDIEM.sub(with_meridiem, text))


# --- Money and percentages ------------------------------------------------------------------

# symbol: (one, many, one cent, many cents); the last two are None where there are no cents
_CURRENCIES = {
    "$": ("dollar", "dollars", "cent", "cents"),
    "£": ("pound", "pounds", "penny", "pence"),
    "€": ("euro", "euros", "cent", "cents"),
    "¥": ("yen", "yen", None, None),
}
_CURRENCY = re.compile(
    rf"(?<![\w$£€¥])(?P<sym>[$£€¥])\s?(?P<whole>{_NUMBER})"
    r"(?:\.(?P<frac>\d+))?(?:\s?(?P<scale>(?i:hundred|thousand|million|billion|trillion))\b)?"
)
_PERCENT = re.compile(rf"(?<![\w.,/])(?P<whole>{_NUMBER})(?:\.(?P<frac>\d+))?\s?%")


# "an $8 flat" is "an eight-dollar flat": after a/an, a whole amount describing the next
# word takes the singular, hyphenated form. These next words mean it's a plain amount instead.
_NOT_A_NOUN = frozenset(
    "a an the and or but per each for to of in on at by from with than so if as is was were "
    "be been are this that it".split()
)
_ARTICLE_BEFORE = re.compile(r"\b(?:a|an)\s+$", re.IGNORECASE)
_WORD_AFTER = re.compile(r"\s+([a-z][a-z'\u2019-]*)")  # \u2019: a curly apostrophe


def _describes_next_word(text: str, match: re.Match[str]) -> bool:
    if not _ARTICLE_BEFORE.search(text[: match.start()]):
        return False
    after = _WORD_AFTER.match(text, match.end())
    return after is not None and after.group(1).lower() not in _NOT_A_NOUN


def _currency(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        one, many, cent_one, cent_many = _CURRENCIES[match["sym"]]
        whole, fraction, scale = match["whole"], match["frac"], match["scale"]
        if not scale and not fraction and _describes_next_word(text, match):
            return f"{spoken_number(whole)}-{one}"
        if scale:
            return f"{spoken_number(whole, fraction)} {scale.lower()} {many}"
        digits = whole.replace(",", "")
        if fraction and len(fraction) <= 2 and cent_one is not None and len(digits) < 15:
            cents = int(fraction.ljust(2, "0"))
            dollars = int(digits)
            parts = []
            if dollars or not cents:
                parts.append(f"{cardinal(dollars)} {one if dollars == 1 else many}")
            if cents:
                parts.append(f"{cardinal(cents)} {cent_one if cents == 1 else cent_many}")
            return " and ".join(parts)
        spoken = spoken_number(whole, fraction)
        is_one = digits == "1" and not fraction
        return f"{spoken} {one if is_one else many}"

    return _CURRENCY.sub(replace, text)


def _percentages(text: str) -> str:
    return _PERCENT.sub(lambda m: spoken_number(m["whole"], m["frac"]) + " percent", text)


# --- Abbreviations --------------------------------------------------------------------------

_TITLES = {
    "Mr": "Mister", "Mrs": "Missus", "Ms": "Mizz", "Dr": "Doctor", "Prof": "Professor",
    "Rev": "Reverend", "Hon": "Honorable", "Gen": "General", "Col": "Colonel",
    "Capt": "Captain", "Lt": "Lieutenant", "Sgt": "Sergeant", "Cpl": "Corporal",
    "Pvt": "Private", "Maj": "Major", "Gov": "Governor", "Sen": "Senator",
    "Rep": "Representative", "Cmdr": "Commander", "Adm": "Admiral", "Supt": "Superintendent",
    "Fr": "Father", "Msgr": "Monsignor", "Mt": "Mount", "Ft": "Fort",
}  # fmt: skip
_ALWAYS_TITLES = frozenset({"Mr", "Mrs", "Ms", "Dr"})  # read as titles even before a lowercase word
_TITLE = re.compile(rf"\b(?P<abbr>{'|'.join(_TITLES)}|{'|'.join(a.upper() for a in _TITLES)})\.")
_STREETS = {"Ave": "Avenue", "Blvd": "Boulevard", "Rd": "Road", "Hwy": "Highway", "Ln": "Lane"}
_STREET = re.compile(rf"\b(?P<abbr>{'|'.join(_STREETS)})\.")
_SAINT = re.compile(r"\b(?P<abbr>St|ST)\.")
_SUFFIX = re.compile(r"\b(?P<abbr>Jr|Sr|Inc|Ltd|Corp)\.")
_SUFFIXES = {"Jr": "Junior", "Sr": "Senior", "Inc": "Incorporated", "Ltd": "Limited",
             "Corp": "Corporation"}  # fmt: skip
_CAPITAL_WORD_BEFORE = re.compile(r"(?:[A-Z][a-z]+|\d+(?:st|nd|rd|th))\s*$")


def _titles(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        abbr = match["abbr"]
        key = abbr.capitalize()
        following_capital = _after_is_capital(match.string, match.end())
        # "Elm Dr." is a street; a "Dr." with no capital after it and no street before it
        # is a doctor.
        is_street = key == "Dr" and not following_capital
        if is_street and _CAPITAL_WORD_BEFORE.search(match.string[: match.start()]):
            return _match_case(abbr, "Drive") + _sentence_end_period(match.string, match.end())
        if key not in _ALWAYS_TITLES and not following_capital:
            return match.group(0)
        return _match_case(abbr, _TITLES[key])

    return _TITLE.sub(replace, text)


def _streets_and_saints(text: str) -> str:
    def saint_or_street(match: re.Match[str]) -> str:
        abbr = match["abbr"]
        before = match.string[: match.start()]
        if _after_is_capital(match.string, match.end()):
            return _match_case(abbr, "Saint")
        if _CAPITAL_WORD_BEFORE.search(before):
            return _match_case(abbr, "Street") + _sentence_end_period(match.string, match.end())
        return match.group(0)

    def street(match: re.Match[str]) -> str:
        if not _CAPITAL_WORD_BEFORE.search(match.string[: match.start()]):
            return match.group(0)
        spoken = _STREETS[match["abbr"]]
        return spoken + _sentence_end_period(match.string, match.end())

    def suffix(match: re.Match[str]) -> str:
        key = match["abbr"]
        if not _CAPITAL_WORD_BEFORE.search(match.string[: match.start()].rstrip(", ")):
            return match.group(0)
        return _SUFFIXES[key] + _sentence_end_period(match.string, match.end())

    text = _SAINT.sub(saint_or_street, text)
    text = _STREET.sub(street, text)
    return _SUFFIX.sub(suffix, text)


# (pattern, replacement, whether the abbreviation's period may end a sentence)
_LATIN: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (re.compile(r"(?<![\w.])e\.g\."), "for example", False),
    (re.compile(r"(?<![\w.])i\.e\."), "that is", False),
    (re.compile(r"(?<![\w.])etc\."), "et cetera", True),
    (re.compile(r"(?<![\w.])et al\."), "and others", True),
    (re.compile(r"(?<![\w.])cf\."), "compare", False),
    (re.compile(r"(?<![\w.])viz\."), "namely", False),
    (re.compile(r"(?<![\w.])N\.B\."), "note well", False),
    (re.compile(r"(?<![\w.])a\.k\.a\."), "also known as", False),
    (re.compile(r"(?<![\w.])approx\."), "approximately", False),
    (re.compile(r"(?<![\w.])vs\.?(?![\w])"), "versus", False),
    (re.compile(r"(?<![\w.])(?:B\.C\.E\.|B\.C\.)"), "bee see", True),
    (re.compile(r"(?<![\w.])(?:A\.D\.)"), "ay dee", True),
    (re.compile(r"(?<![\w.])C\.E\."), "see ee", True),
)
_REFERENCES = (
    (re.compile(r"\bNos\.\s?(?=\d)"), "numbers "),
    (re.compile(r"\bNo\.\s?(?=\d)"), "number "),
    (re.compile(r"\bpp\.\s?(?=\d)"), "pages "),
    (re.compile(r"(?<![\w.])p\.\s?(?=\d)"), "page "),
    (re.compile(r"\b[Ff]igs\.\s?(?=\d)"), "figures "),
    (re.compile(r"\b[Ff]ig\.\s?(?=\d)"), "figure "),
    (re.compile(r"\b[Vv]ols\.\s?(?=\d)"), "volumes "),
    (re.compile(r"\b[Vv]ol\.\s?(?=\d)"), "volume "),
    (re.compile(r"\b[Cc]hap\.\s?(?=\d)"), "chapter "),
    (re.compile(r"(?<![\w.])ch\.\s?(?=\d)"), "chapter "),
    (re.compile(r"(?<![\w.])(?:ca|c)\.\s?(?=\d{3,4}\b)"), "circa "),
)


def _latin_and_references(text: str) -> str:
    for pattern, spoken in _REFERENCES:
        text = pattern.sub(spoken, text)
    for pattern, spoken, can_end in _LATIN:

        def replace(match: re.Match[str], spoken: str = spoken, can_end: bool = can_end) -> str:
            if not match.group(0).endswith("."):
                return spoken
            return spoken + (_sentence_end_period(match.string, match.end()) if can_end else "")

        text = pattern.sub(replace, text)
    return text


def _symbols(text: str) -> str:
    text = re.sub(r"(?<=\s)&(?=\s)", "and", text)
    return re.sub(r"§\s?(?=\d)", "section ", text)


# --- Roman numerals -------------------------------------------------------------------------

# Words that number their sections: any numeral after them is a chapter-style number.
_STRUCTURE_KEYWORDS = (
    "Chapter|Chap\\.?|Book|Part|Act|Scene|Volume|Vol\\.?|Section|Appendix|Canto|Article|"
    "Psalm|Plate|Table|Figure|Fig\\.?|Epilogue|Prologue"
)
# Words that sometimes take a numeral ("World War II", "Type I"); only unambiguous ones count.
_LABEL_KEYWORDS = (
    "War|Bowl|Level|Phase|Stage|Class|Type|Grade|Group|Round|Step|Rule|Unit|Division|"
    "Series|Season|Episode"
)
_CARDINAL_KEYWORDS = f"{_STRUCTURE_KEYWORDS}|{_LABEL_KEYWORDS}"
_KEYWORD_EXPANSIONS = {"chap": "chapter", "chap.": "chapter", "vol": "volume", "vol.": "volume",
                       "fig": "figure", "fig.": "figure"}  # fmt: skip
# A numeral is a whole word: "L'vov" and "I'm" start with a numeral's letter but are not one,
# while "Henry V's" (a possessive) is. The lookbehind stops the numeral from shrinking to
# nothing instead.
_NOT_A_CONTRACTION = r"(?<=[IVXLCDM])(?!['\u2019](?!s\b)[^\W\d_])"
_AFTER_KEYWORD = re.compile(
    rf"\b(?P<kw>(?:{_CARDINAL_KEYWORDS})|(?i:{_CARDINAL_KEYWORDS}))\s+(?P<num>{ROMAN_PATTERN})\b"
    + _NOT_A_CONTRACTION
)
_ROYALS = frozenset(
    (
        "Henry Louis Edward George Richard Charles Elizabeth Victoria William James John Philip "
        "Peter Catherine Napoleon Ferdinand Alexander Frederick Pius Leo Paul Gregory Benedict "
        "Innocent Clement Urban Nicholas Alfonso Carlos Juan Ivan Mary Anne Edwin"
    ).split()
)
_AFTER_NAME = re.compile(
    rf"\b(?P<name>[A-Z][a-z]+)\s+(?P<num>{ROMAN_PATTERN})\b(?!\.\d)" + _NOT_A_CONTRACTION
)
_HEADING_ROMAN = re.compile(
    rf"^(?P<lead>\s*)(?P<num>{ROMAN_PATTERN})(?P<rest>(?:[.:)\u2014\u2013-].*)?)$"
)


def _roman_numerals(text: str) -> str:
    def after_keyword(match: re.Match[str]) -> str:
        keyword, numeral = match["kw"], match["num"]
        capitalised = keyword[:1].isupper()
        if not capitalised and len(numeral) < 2:
            return match.group(0)  # "act I" in running prose is the pronoun
        structural = re.fullmatch(_STRUCTURE_KEYWORDS, keyword, re.IGNORECASE) is not None
        if not structural and len(numeral) < 2 and numeral != "I":
            return match.group(0)  # "Group C" and "Level X" stay as written
        spoken_keyword = _KEYWORD_EXPANSIONS.get(keyword.lower(), keyword)
        if spoken_keyword != keyword:
            spoken_keyword = (
                spoken_keyword.capitalize() if keyword[:1].isupper() else spoken_keyword
            )
        return f"{spoken_keyword} {cardinal(roman_value(numeral))}"

    def after_name(match: re.Match[str]) -> str:
        name, numeral = match["name"], match["num"]
        value = roman_value(numeral)
        if len(numeral) < 2 and not (numeral == "V" and name in _ROYALS):
            return match.group(0)  # "Malcolm X", "Vitamin C" and the pronoun "I" stay as written
        if value < 2 or value > 25:
            return match.group(0)
        return f"{name} the {ordinal(value)}"

    text = _AFTER_KEYWORD.sub(after_keyword, text)
    return _AFTER_NAME.sub(after_name, text)


def _roman_heading(text: str) -> str:
    """A heading that is just a numeral ("XIV", "XIV. The Return") starts with the number."""
    match = _HEADING_ROMAN.match(text)
    if match is None:
        return text
    value = roman_value(match["num"])
    if value > 200:
        return text  # "MIX" and "CD" are words, not chapter numbers
    return f"{match['lead']}{cardinal(value)}{match['rest']}"


# --- Numbers --------------------------------------------------------------------------------

_DECADE = re.compile(r"(?<![\w.,/$])(?P<century>1\d|20)(?P<decade>\d)0['\u2019]?s\b")
_SHORT_DECADE = re.compile(r"(?<![\w.,/$])['\u2019]?(?P<decade>[1-9])0['\u2019]?s\b")
_ORDINAL = re.compile(rf"{_NOT_AFTER}(?P<n>{_NUMBER})(?i:st|nd|rd|th)\b")
_YEAR_RANGE = re.compile(
    rf"{_NOT_AFTER}(?P<a>1\d\d\d|20\d\d)\s?[\u2013-]\s?(?P<b>\d{{4}}|\d{{2}})(?![\w]|[.,]\d)"
)
_YEAR = re.compile(rf"{_NOT_AFTER}(?P<y>1\d\d\d|20\d\d)(?![\w%]|[.,]\d|/)")
_YEAR_BEFORE = frozenset(
    (
        "in since until till by from during before after circa year early late mid between "
        "through throughout"
    ).split()
)
_YEAR_AFTER_OF = frozenset(
    (
        "war panic summer winter spring autumn fall class treaty battle revolution edict act year"
    ).split()
)
_YEAR_LINKS = frozenset({"to", "and", "through", "until", "till", "or", "\u2013", "-"})
_ERA_AFTER = re.compile(r"\s*(?:B\.?C\.?E?|A\.?D\.?|C\.?E\.?|BCE|CE|AD|BC)\b")
_NEXT_WORD = re.compile(r"\s+([^\W\d_]+)")
_PHONE = re.compile(
    r"(?<![\w-])(?:\+?1[-. ])?(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?![\w-])"
    r"|(?<![\w-])\d{3}-\d{4}(?![\w-])"
)
_MIXED_FRACTION = re.compile(r"(?<![\w./])(?P<w>\d+)\s(?P<n>\d+)/(?P<d>\d+)(?![\w/]|\.\d)")
_FRACTION = re.compile(r"(?<![\w./])(?P<n>\d+)/(?P<d>\d+)(?![\w/]|\.\d)")
_VULGAR = {
    "½": (1, 2), "⅓": (1, 3), "⅔": (2, 3), "¼": (1, 4), "¾": (3, 4),
    "⅛": (1, 8), "⅜": (3, 8), "⅝": (5, 8), "⅞": (7, 8),
}  # fmt: skip
_VULGAR_FRACTION = re.compile(rf"(?:(?P<w>\d+)\s?)?(?P<f>[{''.join(_VULGAR)}])")
_SLASHED = re.compile(r"(?<![\w/.$])(?P<a>\d+)/(?P<b>\d+)(?:/(?P<c>\d+))?(?![\w/]|\.\d)")
_RANGE = re.compile(
    rf"{_NOT_AFTER}(?P<a>{_NUMBER})\s?[\u2013-]\s?(?P<b>{_NUMBER})(?![\w]|[.,]\d|-\d)"
)
_MINUS = re.compile(r"(?<![\w)\].,])[-\u2212](?=\d)")
_PLAIN_NUMBER = re.compile(rf"{_NOT_AFTER}(?P<whole>{_NUMBER})(?:\.(?P<frac>\d+))?")


def _decades(text: str) -> str:
    def full(match: re.Match[str]) -> str:
        century, decade = int(match["century"]), int(match["decade"])
        if decade == 0:
            return "two thousands" if century == 20 else f"{cardinal(century)} hundreds"
        return f"{cardinal(century)} {_plural_tens(decade * 10)}"

    def short(match: re.Match[str]) -> str:
        return _plural_tens(int(match["decade"]) * 10)

    text = _DECADE.sub(full, text)
    return _SHORT_DECADE.sub(short, text)


def _next_word(text: str, end: int) -> str:
    found = _NEXT_WORD.match(text, end)
    return found.group(1).lower() if found else ""


def _is_year_context(text: str, match: re.Match[str]) -> bool:
    before = text[: match.start()]
    previous = _before_word(text, match.start())
    spaced = before.endswith(" ") or not before
    if _next_word(text, match.end()) in _QUANTITY_WORDS:
        return False
    if _ERA_AFTER.match(text, match.end()):
        return True
    if not spaced:
        return False
    if previous.lower() in _YEAR_BEFORE and re.search(r"[^\W\d_]\s*$", before):
        return True
    of_context = re.search(r"([^\W\d_]+)\s+of\s*$", before)
    return bool(of_context and of_context.group(1).lower() in _YEAR_AFTER_OF)


def _years(text: str) -> str:
    def year_range(match: re.Match[str]) -> str:
        first = int(match["a"])
        second_text = match["b"]
        if _next_word(match.string, match.end()) in _QUANTITY_WORDS:
            return match.group(0)
        if len(second_text) == 4:
            second = int(second_text)
            if not (1000 <= second <= 2099 and second > first):
                return match.group(0)
            return f"{year(first)} to {year(second)}"
        short = int(second_text)
        if short <= first % 100:
            return match.group(0)
        spoken = f"oh {_ONES[short]}" if short < 10 else cardinal(short)
        return f"{year(first)} to {spoken}"

    text = _YEAR_RANGE.sub(year_range, text)

    # Single years, left to right: the second year of "between 1914 and 1918" counts too.
    pieces: list[str] = []
    cursor = 0
    last_year_end = -1
    for match in _YEAR.finditer(text):
        linked = last_year_end >= 0 and (
            text[last_year_end : match.start()].strip().lower() in _YEAR_LINKS
        )
        if linked or _is_year_context(text, match):
            pieces.append(text[cursor : match.start()])
            pieces.append(year(int(match["y"])))
            cursor = match.end()
            last_year_end = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def _ordinals(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return ordinal(int(match["n"].replace(",", "")))

    return _ORDINAL.sub(replace, text)


def _fraction_words(numerator: int, denominator: int) -> str:
    if denominator == 2:
        return "one half" if numerator == 1 else f"{cardinal(numerator)} halves"
    if denominator == 4:
        return f"{cardinal(numerator)} {'quarter' if numerator == 1 else 'quarters'}"
    return f"{cardinal(numerator)} {ordinal(denominator)}{'' if numerator == 1 else 's'}"


def _fractions(text: str) -> str:
    def vulgar(match: re.Match[str]) -> str:
        numerator, denominator = _VULGAR[match["f"]]
        spoken = _fraction_words(numerator, denominator)
        if match["w"]:
            joined = "a half" if (numerator, denominator) == (1, 2) else spoken
            return f"{spoken_number(match['w'])} and {joined}"
        return spoken

    def mixed(match: re.Match[str]) -> str:
        numerator, denominator = int(match["n"]), int(match["d"])
        if not (0 < numerator < denominator <= 10):
            return match.group(0)
        joined = (
            "a half"
            if (numerator, denominator) == (1, 2)
            else _fraction_words(numerator, denominator)
        )
        return f"{spoken_number(match['w'])} and {joined}"

    def simple(match: re.Match[str]) -> str:
        numerator, denominator = int(match["n"]), int(match["d"])
        if not (0 < numerator < denominator <= 10):
            return match.group(0)
        return _fraction_words(numerator, denominator)

    text = _VULGAR_FRACTION.sub(vulgar, text)
    text = _MIXED_FRACTION.sub(mixed, text)
    return _FRACTION.sub(simple, text)


def _slashed_numbers(text: str) -> str:
    """12/25 and 12/25/2020 are dates (month first); any other ``a/b`` says "a slash b"."""

    def replace(match: re.Match[str]) -> str:
        first, second, third = match["a"], match["b"], match["c"]
        month, day = int(first), int(second)
        is_date = len(first) <= 2 and len(second) <= 2 and 1 <= month <= 12 and _valid_day(day)
        if is_date and third is None:
            return f"{MONTHS[month - 1]} {ordinal(day)}"
        if is_date and third is not None and len(third) == 4:
            return f"{MONTHS[month - 1]} {ordinal(day)}, {year(int(third))}"
        parts = [spoken_number(part) for part in (first, second, third) if part is not None]
        return " slash ".join(parts)

    return _SLASHED.sub(replace, text)


def _phone_numbers(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        groups = re.findall(r"\d+", match.group(0))
        return ", ".join(digit_words(group) for group in groups)

    return _PHONE.sub(replace, text)


def _ranges(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        first, second = match["a"], match["b"]
        if first.startswith("0") or second.startswith("0"):
            return match.group(0)
        if int(first.replace(",", "")) >= int(second.replace(",", "")):
            return match.group(0)
        return f"{spoken_number(first)} to {spoken_number(second)}"

    return _RANGE.sub(replace, text)


def _numbers(text: str) -> str:
    text = _MINUS.sub("minus ", text)

    def replace(match: re.Match[str]) -> str:
        spoken = spoken_number(match["whole"], match["frac"])
        following = match.string[match.end() : match.end() + 1]
        # "5km" is "five km", not "fivekm".
        return spoken + " " if following.isalpha() else spoken

    return _PLAIN_NUMBER.sub(replace, text)


# --- Sentence starts ------------------------------------------------------------------------

# The chunker only ends a sentence before a word that doesn't start in lowercase, so "He
# left. 5 men came." must become "He left. Five men came.", not "...left. five men came.",
# or the two sentences run together. Sentence starts are found once, on the text as written,
# and marked; after all the rules have run, a lowercase letter right after a mark (always
# one the rules wrote: "five", "minus", "number", "the") is made a capital.
#
# Only a clear sentence end counts: "!" or "?", or a period after an ordinary lowercase word.
# Anything else ("Rom. 8:28", "On Fri. 5 May", "the U.S. $5 billion", "vv. 3-5", "at 3 a.m.
# 5 men", "He counted... 3", '"Stop!" 5 men cried') keeps the number in lowercase, so the
# sentence stays whole, as it always was.

# A run of marks is matched from its first mark only, so a long run costs the same as a short.
_SENTENCE_END = re.compile(
    rf"(?<![.!?\u2026])(?P<marks>[.!?\u2026]+)(?P<closers>[{re.escape(_CLOSERS)}]*)\s+"
)
# Signs that may come before the number a sentence starts with: "-5", "$5", "~5", "'70s".
_SIGNS = "-\u2212+~\u00b1\u2248$£€¥#§\u2019"
# Lowercase abbreviations that look like ordinary words (besides chunking.ABBREVIATIONS):
# references, units and grammar terms.
_NOT_SENTENCE_ENDS = frozenset(
    (
        "art arts sec secs sect sects para paras cap caps ver vers ref refs fol fols ibid cit "
        "loc seq ser suppl supp app illus esp incl misc viz circ abt inst ult prox orig repr "
        "trans lit abbr adj adv pron fem masc neut plur obs def min mins gal gals doz deg degs "
        "lat bce"
    ).split()
)
# At least three lowercase letters with a vowel ("y" counts after the first letter): "left",
# "fly", "gym". Not "Rom", "vv", "lbs", "yds".
_ORDINARY_WORD = re.compile(r"(?=[a-z]{3})(?:[a-z]*[aeiou][a-z]*|[a-z]+y[a-z]*)")
_LOWERCASE_ROMAN = re.compile(ROMAN_PATTERN.lower())  # "iii.", "xiv."
_MARKED = re.compile(rf"{_MARK}([^\w\s{_MARK}]*)([a-z]?)")


def _clearly_ends_sentence(text: str, match: re.Match[str]) -> bool:
    """True if the ``.``, ``!`` or ``?`` of ``match`` (from :data:`_SENTENCE_END`) ends a
    sentence for sure."""
    marks = match["marks"]
    if set(marks) <= set("!?"):
        return not match["closers"]  # '"Stop!" 5 men cried.' is one sentence
    if marks != ".":
        return False  # an ellipsis or a dot leader often doesn't end the sentence
    before = text[max(0, match.start() - 40) : match.start()].split()
    word = before[-1].strip(_OPENERS + _CLOSERS) if before else ""
    if not _ORDINARY_WORD.fullmatch(word):
        return False
    if word in ABBREVIATIONS or word in _NOT_SENTENCE_ENDS or _LOWERCASE_ROMAN.fullmatch(word):
        return False
    return not (word == "long" and "lat." in before)  # "lat. 51 N, long. 0 W"


def _mark_sentence_starts(text: str) -> str:
    """Put :data:`_MARK` where a sentence clearly starts after another one, if it doesn't
    start in lowercase. The paragraph's own start needs no mark: it is a sentence start
    whatever its case."""
    pieces: list[str] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        start = index = match.end()
        while index < len(text) and text[index] in _OPENERS + _SIGNS:
            index += 1
        first = text[index : index + 1]
        if not first.isalnum() or first.islower() or not _clearly_ends_sentence(text, match):
            continue
        pieces += [text[cursor:start], _MARK]
        cursor = start
    pieces.append(text[cursor:])
    return "".join(pieces)


def _capitalise_sentence_starts(text: str) -> str:
    """Capitalise a lowercase letter right after each :data:`_MARK` (past any signs), and
    take the marks out. Only ``a``-``z`` change, so the book's own letters never do."""
    return _MARKED.sub(lambda match: match[1] + match[2].upper(), text)


_STEPS: tuple[Callable[[str], str], ...] = (
    _iso_dates,
    _dates,
    _times,
    _currency,
    _percentages,
    _titles,
    _streets_and_saints,
    _latin_and_references,
    _symbols,
    _roman_numerals,
    _decades,
    _years,
    _ordinals,
    _fractions,
    _slashed_numbers,
    _phone_numbers,
    _ranges,
    _numbers,
)

# Lowercase letter-runs that the rules above legitimately turn into other words, so the
# "no word is lost" check doesn't flag them.
CONSUMED_WORDS = frozenset(
    {key.lower() for key in (*_TITLES, *_STREETS, *_SUFFIXES)}
    | {a.lower() for a in _MONTH_ABBREVIATIONS}
    | {
        "st", "nd", "rd", "th", "e", "g", "i", "etc", "et", "al", "cf", "viz", "n", "b", "a",
        "k", "approx", "vs", "c", "d", "m", "p", "pp", "no", "nos", "fig", "figs", "vol",
        "vols", "ch", "chap", "ca", "am", "pm", "ad", "bc", "ce", "bce", "s",
    }
)  # fmt: skip


class EnglishRules:
    """Text prep for English books."""

    language = "en"
    consumed_words = CONSUMED_WORDS

    def heading(self, text: str) -> str:
        """Prepare a chapter title: a bare numeral becomes a number, then the usual rules."""
        return self.body(_roman_heading(text))

    def body(self, text: str) -> str:
        """Prepare running text."""
        marked = _MARK not in text  # it never is in a real book; if so, nothing is marked
        if marked:
            text = _mark_sentence_starts(text)
        for step in _STEPS:
            text = step(text)
        return _capitalise_sentence_starts(text) if marked else text
