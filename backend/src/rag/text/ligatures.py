"""Recover letters that a PDF font lost: "A ributes" -> "Attributes", "e ciency" -> "efficiency", "estion" -> "Question".

Fonts that cannot say which Unicode letters a glyph stands for (ligatures such as ff, fi, fl, ffi, tt, Qu in academic and
brochure PDFs) make pdfium emit the raw glyph code, a control character. Text cleaning used to turn those into spaces, which
splits and spoils the words: keyword search cannot find "efficient" in "e cient". Here the codes are kept as private-use
placeholders until the whole document has been read, then each code is resolved by experiment: for every candidate (ff, fi,
fl, ffi, ffl, tt, ti, ... Qu) count the words the insertion turns into real dictionary words, and take the candidate that
explains the code. No font parsing is needed, so it works for any encoding, and it is conservative: a code that no candidate
explains is removed (joining its neighbours) rather than guessed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

PLACEHOLDER_BASE = 0xE000  # control character c (1..31) is kept as chr(0xE000 + c)
_PLACEHOLDER = re.compile("[-]")
# TeX's standard (OT1) positions of the f-ligatures; the experiment confirms or overrides them per document.
DEFAULT_CODES = {0x0B: "ff", 0x0C: "fi", 0x0D: "fl", 0x0E: "ffi", 0x0F: "ffl"}
CANDIDATES = ("ff", "fi", "fl", "ffi", "ffl", "tt", "ti", "tf", "ft", "ct", "st", "Th", "Qu", "Tw", "fj", "fb", "fh", "fk",
              "ffb", "ffh", "ffj", "ffk", "th", "ch", "ck", "ll", "ss")
_WORD_AROUND = re.compile("([A-Za-z]*)([-])([A-Za-z]*)")
_SPACE_RUN = re.compile(r"[ \t]{2,}")
MIN_EVIDENCE = 2   # distinct words a candidate must complete
MIN_SHARE = 0.25   # ... and the share of the distinct words around the code that this must be
MIN_STUB = 2       # letters that must remain around the code: with none, any dictionary word "completes" itself


@lru_cache(maxsize=1)
def _vocabulary() -> dict[str, float]:
    """Word -> cost (lower is more frequent). The list holds many rare fragments ("nal", "cient"), so a bare membership test
    cannot tell a word from a stub; the cost can."""
    try:
        import wordninja

        return {w: c for w, c in wordninja.DEFAULT_LANGUAGE_MODEL._wordcost.items() if len(w) >= 3 and w.isalpha()}
    except Exception:  # noqa: BLE001 - without a vocabulary nothing can be verified, so nothing is guessed
        return {}


def _completes_word(vocab: dict[str, float], left: str, candidate: str, right: str) -> bool:
    """Does inserting the candidate turn the broken word into a real, more common one ("e"+"ffi"+"ciency")?"""
    joined = (left + candidate + right).lower()
    if len(joined) < 3 or joined not in vocab:
        return False
    stub = (left + right).lower()
    return stub not in vocab or vocab[stub] > vocab[joined]


def protect(char: str) -> str:
    """The placeholder for a control character (used by the text cleaner)."""
    return chr(PLACEHOLDER_BASE + ord(char))


def has_placeholders(text: str) -> bool:
    return _PLACEHOLDER.search(text) is not None


def infer_mapping(texts: list[str]) -> dict[str, str]:
    """For every placeholder in the document, the letters it stands for ('' when no candidate is convincing)."""
    vocab = _vocabulary()
    # distinct words around each code: a name repeated on every page ("He?iachchi" for Hettiachchi) is one word, not twenty
    contexts: dict[str, set[tuple[str, str]]] = defaultdict(set)
    seen: set[str] = set()
    for text in texts:
        for left, code, right in _WORD_AROUND.findall(text):
            seen.add(code)
            if _PLACEHOLDER.search(left) or _PLACEHOLDER.search(right):
                continue
            if len(left) + len(right) >= MIN_STUB:
                contexts[code].add((left, right))
    mapping: dict[str, str] = {}
    for code in seen:
        default = DEFAULT_CODES.get(ord(code) - PLACEHOLDER_BASE)
        ordered = ([default] if default else []) + [c for c in CANDIDATES if c != default]
        scores: dict[str, int] = {}
        best, best_score = "", 0
        for candidate in ordered:
            score = sum(1 for left, right in contexts.get(code, ()) if _completes_word(vocab, left, candidate, right))
            scores[candidate] = score
            if score > best_score:  # ties keep the earlier candidate: the TeX default first
                best, best_score = candidate, score
        distinct = max(1, len(contexts.get(code, ())))
        # two words that agree, or one word that only one candidate can explain ("A?ributes" in a document that sets its
        # headings in a font of its own)
        lone = best_score == 1 and sum(1 for v in scores.values() if v) == 1 and distinct <= 6
        if (best_score >= MIN_EVIDENCE and best_score >= MIN_SHARE * distinct) or lone:
            mapping[code] = best
        elif default and not vocab:
            mapping[code] = default
    return mapping


def apply_mapping(text: str, mapping: dict[str, str]) -> str:
    """Replace placeholders; a code that was not resolved is dropped between letters (the word stays whole) and next to
    whitespace, and becomes a single space between anything else."""
    if not has_placeholders(text):
        return text

    def resolve(match: re.Match[str]) -> str:
        code = match.group(0)
        if code in mapping:
            return mapping[code]
        before = text[match.start() - 1] if match.start() else " "
        after = text[match.end()] if match.end() < len(text) else " "
        if before.isspace() or after.isspace() or (before.isalpha() and after.isalpha()):
            return ""
        return " "

    fixed = _PLACEHOLDER.sub(resolve, text)
    # a dropped code can leave two spaces side by side (the cleaner had already collapsed runs of them)
    return "\n".join(_SPACE_RUN.sub(" ", line).strip() for line in fixed.split("\n"))


def repair_document(texts: list[str]) -> list[str]:
    """Resolve the placeholders of a whole document (its pages share the fonts, so the evidence is pooled)."""
    if not any(has_placeholders(t) for t in texts):
        return texts
    mapping = infer_mapping(texts)
    return [apply_mapping(t, mapping) for t in texts]
