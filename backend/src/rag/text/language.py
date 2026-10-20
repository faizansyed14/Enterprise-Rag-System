"""Language of a page, chunk or query, from its script and common words.

The library is mostly English and Arabic. What matters downstream is (a) which BM25 stemmer to use, (b) whether
the reranker can read the text (RERANKER_LANGUAGES) and (c) which language to answer in. Script ratios settle
Arabic vs Latin reliably; a stop-word ratio separates English from other Latin-script languages.
"""

from __future__ import annotations

import re

from rag.text.normalize import is_arabic_char

_EN_COMMON = frozenset(
    "the of and to in is for on with be as by this that are shall or from at an it not will all any which have "
    "has been their other such these where within must should may can if per when than into".split()
)
_WORD = re.compile(r"[a-z]+")


def detect_language(text: str, *, min_letters: int = 12) -> str:
    """'ar', 'en', 'latin' (another Latin-script language), 'mixed', or 'und' (too little text to say)."""
    if not text:
        return "und"
    sample = text[:6000]
    letters = arabic = latin = 0
    for ch in sample:
        if ch.isalpha():
            letters += 1
            if is_arabic_char(ch):
                arabic += 1
            elif ch.isascii() or "À" <= ch <= "ɏ":
                latin += 1
    if letters < min_letters:
        return "und"
    ar_ratio = arabic / letters
    if ar_ratio >= 0.6:
        return "ar"
    if ar_ratio >= 0.15:
        return "mixed"
    words = _WORD.findall(sample.lower())
    if not words:
        return "und"
    common = sum(1 for w in words if w in _EN_COMMON)
    # Short labels ("Ground Floor Plan") have few function words; treat short Latin text as English.
    if len(words) < 25 or common / len(words) >= 0.04:
        return "en"
    return "latin"


def languages_compatible(language: str, supported: set[str] | None) -> bool:
    """Can a model that supports `supported` read text in `language`? None means every language."""
    if supported is None:
        return True
    if language in {"und"}:
        return True
    if language == "mixed":
        return "ar" in supported and "en" in supported
    return language in supported
