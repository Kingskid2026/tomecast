"""Apostrophe-like marks that eSpeak NG reads out by name, made into plain apostrophes.

Names and transliterations use several letters that look like an apostrophe: U+02BC for a
glottal stop or as the apostrophe itself ("Kʼicheʼ", some ebooks' "donʼt"), U+02BE and
U+02BF for the hamza and ayin of transliterated Arabic ("Qurʾān", "ʿAli"), U+02B9 and U+02BA
for the Russian soft and hard signs ("Ilʹich", "obʺyom"), and a few more. eSpeak NG 1.52
reads each of these out by name, and spells the word it sits in letter by letter: "Hawaiʼi"
was "H A W A I ejective I", and "Ilʹich" "I L letter-two-B-nine I C H". The ʻokina (U+02BB)
and the ordinary and curly apostrophes are read as intended (all four give həwˈaɪiː).

So :func:`speakable` turns these marks into an ASCII apostrophe where they touch a letter
(inside a word or at its edge). Measured with eSpeak's own phonemes (no audio), an
apostrophe usually reads as well as dropping the mark or better: "Il'ich" gives ˈɪlɪtʃ where
"Ilich" gives ˈɪlɪx, "Mar'ya" mˈɑːɹjə where "Marya" gives mˈɛɹɪə, and Olga, Kazan or Gogol
read the same either way. It is worse after a single letter at the start of a word, which
eSpeak then reads as a letter name: "K'iche'" is kˈeɪˈɪtʃ ("kay-itch") where "Kiche" is
kˈɪtʃ, and "L'vov" ˈɛlvˈɑːv where "Lvov" is ləvˈɑːv. Dropping the mark there would break
the far commoner apostrophes of a book that uses U+02BC for them ("I'm" is aɪm but "Im" is
ˈɪm; "O'Brien" and "d'Artagnan" lose a letter name too), and a letter name is still far
better than the whole word spelled out, so it stays an apostrophe.

A mark standing on its own, as in a transliteration table ("ʾ  hamza"), is left as it is:
it is the only thing to say there, and an apostrophe would say nothing at all. So is a mark
after a lone capital letter at the end of a word, a label such as "Aʹ" (A prime) or
"Part Dʼ": as "D'" the English rules would read it as a Roman numeral ("five hundred").
Nothing else changes, and each mark becomes exactly one apostrophe, so the text keeps its
length.
"""

from __future__ import annotations

import re

# Each of these is read out by name, and spells its word out, in eSpeak NG 1.52 (en-us).
APOSTROPHE_LIKE = (
    "ʹ"  # ʹ modifier letter prime: the Russian soft sign in transliteration
    "ʺ"  # ʺ modifier letter double prime: the Russian hard sign
    "ʼ"  # ʼ modifier letter apostrophe: a glottal stop or an ejective, or an apostrophe
    "ʽ"  # ʽ modifier letter reversed comma: rough breathing, Armenian aspiration
    "ʾ"  # ʾ modifier letter right half ring: the Arabic hamza or Hebrew aleph
    "ʿ"  # ʿ modifier letter left half ring: the Arabic ayin
    "ˮ"  # ˮ modifier letter double apostrophe (Nenets)
    "Ꞌ"  # Ꞌ latin capital letter saltillo: a glottal stop in some Mexican languages
    "ꞌ"  # ꞌ latin small letter saltillo
)

_MARKS = re.compile(f"[{APOSTROPHE_LIKE}]+")


def _is_letter(character: str) -> bool:
    return character.isalpha() and character not in APOSTROPHE_LIKE


def _lone_capital(text: str, index: int) -> bool:
    """True if ``text[index]`` is a capital letter that is a word on its own so far."""
    return text[index].isupper() and (index == 0 or not _is_letter(text[index - 1]))


def speakable(text: str) -> str:
    """``text`` with every apostrophe-like mark eSpeak would read out by name turned into an
    ASCII apostrophe where it touches a letter: ``"Qurʾān"`` -> ``"Qur'ān"``,
    ``"Siobhanʼs"`` -> ``"Siobhan's"``. A mark (or a run of them) with no letter on either
    side is left as it is, and so is one whose only letter is a lone capital before it
    (``"Aʹ"``); everything else is too. The result is as long as ``text``."""

    def replace(match: re.Match[str]) -> str:
        start, end = match.span()
        before = start > 0 and _is_letter(text[start - 1])
        after = end < len(text) and _is_letter(text[end])
        if not (before or after) or (not after and _lone_capital(text, start - 1)):
            return match.group()
        return "'" * (end - start)

    return _MARKS.sub(replace, text)
