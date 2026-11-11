"""Apostrophe-like marks that eSpeak NG reads out by name, made into plain apostrophes.

Names and transliterations use several letters that look like an apostrophe: U+02BC for a
glottal stop or as the apostrophe itself ("Kʼicheʼ", some ebooks' "donʼt"), U+02BE and
U+02BF for the hamza and ayin of transliterated Arabic ("Qurʾān", "ʿAli"), U+02B9 and U+02BA
for the Russian soft and hard signs ("Ilʹich", "obʺyom"), and a few more. eSpeak NG 1.52
reads each of these out by name, and spells the word it sits in letter by letter: "Hawaiʼi"
was "H A W A I ejective I", and "Ilʹich" "I L letter-two-B-nine I C H". The ʻokina (U+02BB)
and the ordinary and curly apostrophes are read as intended (all four give həwˈaɪiː).

So :func:`speakable` turns the troublesome marks into an ASCII apostrophe, which reads best
(measured with eSpeak's own phonemes, no audio): "Il'ich" gives ˈɪlɪtʃ where "Ilich" gives
ˈɪlɪx, and "Mar'ya" gives mˈɑːɹjə where "Marya" gives mˈɛɹɪə. Nothing else changes, so no
word is lost; the text keeps the same letters, just a different apostrophe.
"""

from __future__ import annotations

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

_TO_APOSTROPHE = str.maketrans(dict.fromkeys(APOSTROPHE_LIKE, "'"))


def speakable(text: str) -> str:
    """``text`` with every apostrophe-like mark eSpeak would read out by name turned into an
    ASCII apostrophe: ``"Qurʾān"`` -> ``"Qur'ān"``. Everything else is left as it is."""
    return text.translate(_TO_APOSTROPHE)
