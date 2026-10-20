"""Put back the spaces some OCR models drop ("Installfire dampersatall" -> "Install fire dampers at all").

The PP-OCR recognition models bundled with RapidOCR were trained mostly on Chinese and often merge English words.
Long letter runs are split with a word-frequency model (wordninja, English Wikipedia unigrams); identifiers
(SI-014, PRJ1-ABC-ZZ-03-DR-S-2001), numbers, short codes (C40) and non-Latin text are left exactly as recognised.
"""

from __future__ import annotations

import re
from functools import lru_cache

_PIECES = re.compile(r"[A-Za-z]+|\d+(?:[.,]\d+)*|[^A-Za-z\d\s]+")
_IDENT = re.compile(r"^[A-Z0-9]{1,8}(?:[-_/][A-Z0-9]{1,8})+[.,;:]?$")
_CODE = re.compile(r"^[A-Z]{1,2}\d{1,4}[A-Z]?[.,;:]?$")
_UNIT_PREFIXES = {"m", "mm", "cm", "km", "kg", "kn", "n", "mpa"}


@lru_cache(maxsize=1)
def _model():
    import wordninja

    return wordninja.DEFAULT_LANGUAGE_MODEL


@lru_cache(maxsize=50_000)
def _split_letters(run: str) -> tuple[str, ...]:
    if len(run) < 6:
        return (run,)
    if re.search(r"[a-z][A-Z]", run):  # "KarimHaddad": cut at the capitals first, then handle each part
        return tuple(w for part in re.split(r"(?<=[a-z])(?=[A-Z])", run) for w in _split_letters(part))
    if len(run) <= 8 and run[0].isupper() and run[1:].islower():
        return (run,)  # a capitalised short word is usually a name ("Haddad"), which the word model would cut up
    return tuple(_model().split(run)) or (run,)


def restore_spaces(line: str) -> str:
    if not line or not re.search(r"[A-Za-z]{6,}|[A-Za-z][\d:.,;]|\d[A-Za-z]{3,}", line):
        return line
    out_tokens: list[str] = []
    for token in line.split(" "):
        if not token or _IDENT.match(token) or _CODE.match(token):
            out_tokens.append(token)
            continue
        pieces = _PIECES.findall(token)
        words: list[str] = []
        for piece in pieces:
            if piece.isalpha():
                words.extend(_split_letters(piece))
            else:
                words.append(piece)
        rebuilt = ""
        for i, word in enumerate(words):
            if i == 0:
                rebuilt = word
                continue
            prev = words[i - 1]
            if not word[0].isalnum():
                rebuilt += word  # punctuation sticks to the previous word
            elif not prev[-1].isalnum():
                # after punctuation: "Project:Marina" -> "Project: Marina", but keep "1.5" and "A-1" intact
                before = words[i - 2] if i >= 2 else ""
                glued = prev in {"-", "/"} or (prev in {".", ","} and before.isdigit())
                hyphenated = prev == "-" and before.isalpha() and word.isalpha()  # "mock-up", "near-miss"
                rebuilt += word if (glued and word[0].isdigit()) or hyphenated else " " + word
            elif prev.isalpha() and word[0].isdigit() and len(prev) <= 3 and (prev.isupper() or prev in _UNIT_PREFIXES):
                rebuilt += word  # C40, M20, m3 (but "to24" is "to 24")
            else:
                rebuilt += " " + word
        out_tokens.append(rebuilt)
    return " ".join(out_tokens)
