"""Text clean-up shared by extraction, chunking, BM25 and citation checks."""

from __future__ import annotations

import re
import unicodedata

_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_ALEF_VARIANTS = str.maketrans({"آ": "ا", "أ": "ا", "إ": "ا", "ٱ": "ا"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# pdfium writes U+0002 where a line-end hyphen was removed: "ob\x02tain" is "obtain", "10\x02year" is "10-year".
_BREAK_HYPHEN_WORD = re.compile(r"(?<=[^\W\d_])\x02(?=[a-zà-ɏ])")
# a real hyphen left at the end of a line inside a word: "perfor-\nmance"
_LINE_END_HYPHEN = re.compile(r"(?<=[a-zà-ɏ]{2})-[ \t]*\r?\n[ \t]*(?=[a-zà-ɏ]{2})")
# control characters pdfium uses for glyphs a font cannot name (not \t \n \r, and not \x02, the removed line-end hyphen)
_GLYPH_CODE = re.compile(r"[\x01\x03-\x08\x0b\x0c\x0e-\x1f]")
# U+000D is where TeX fonts keep the "fl" ligature. pdfium ends real lines with "\r\n", so a CR that is not part of one and
# sits right before a letter ("\rood") is the glyph.
_MIDWORD_CR = re.compile(r"(?<!\r)\r(?=[^\W\d_])")
# Symbol and Wingdings fonts put their glyphs in the private-use area. At the start of a line followed by a space the glyph is a
# bullet; the pieces of tall brackets and braces (U+F8EB-F8FE) carry no text; any other private-use character is unreadable.
_PUA_BULLET = re.compile(r"(?m)^[ 	]*[\uf000-\uf0ff](?=[ 	])")
_PUA_BRACKET_PART = re.compile(r"[\uf8eb-\uf8fe]")
_PUA_OTHER = re.compile(r"[\uf000-\uf8ff]")
_SURROGATES = re.compile(r"[\ud800-\udfff]")
_SPACES = re.compile(r"[ \t\u00a0\u2000-\u200b\u202f\u205f\u3000]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_ARABIC_CHAR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def clean_text(text: str, *, keep_glyph_codes: bool = False) -> str:
    """Make extracted text safe to store and index: NFKC (folds Arabic presentation forms and ligatures back to
    letters), no NUL/control characters (Postgres rejects NUL), no lone surrogates, tidy whitespace.

    `keep_glyph_codes`: PDF text uses control characters for glyphs a font cannot name (ligatures). With this on they are kept
    as private-use placeholders so rag.text.ligatures can work out the letters; without it they become spaces."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _SURROGATES.sub("", text)
    text = _PUA_BULLET.sub("•", text)
    text = _PUA_BRACKET_PART.sub("", text)
    text = _PUA_OTHER.sub(" ", text)
    text = _BREAK_HYPHEN_WORD.sub("", text).replace("\x02", "-")
    text = _LINE_END_HYPHEN.sub("", text)
    if keep_glyph_codes:
        text = _MIDWORD_CR.sub(chr(0xE000 + 0x0D), text)
        text = _GLYPH_CODE.sub(lambda m: chr(0xE000 + ord(m.group(0))), text)
    text = _CONTROL.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _MANY_NEWLINES.sub("\n\n", text).strip()


def normalize_arabic(text: str) -> str:
    text = _ARABIC_DIACRITICS.sub("", text).replace(_TATWEEL, "")
    text = text.translate(_ALEF_VARIANTS)
    return text.replace("ى", "ي").replace("ة", "ه")


def normalize_for_search(text: str) -> str:
    """Lower-case, fold digits and Arabic letter variants so queries and documents meet in the same form."""
    text = unicodedata.normalize("NFKC", text).translate(_DIGITS).lower()
    if _ARABIC_CHAR.search(text):
        text = normalize_arabic(text)
    return text


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_CHAR.search(text))


def is_arabic_char(ch: str) -> bool:
    return bool(_ARABIC_CHAR.match(ch))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
