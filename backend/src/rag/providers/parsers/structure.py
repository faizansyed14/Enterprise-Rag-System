"""Document structure from a PDF's own typography: running headers/footers, headings and a title.

Plain text extraction throws the type away, so "3.2.2 Signs of an Incident" looks like any other line and every page of
a standard starts with the same "NIST SP 800-61 ... This publication is available free of charge from ..." banner. Here
the font size and weight of every line are compared with the document's body text:

  * lines that open or close most pages (after masking digits) are page furniture, not content; they are removed from
    the page text, and a lone number among them is kept as the printed page label;
  * lines set larger than the body text, or bold at body size and short, are headings; they get Markdown "#" markers
    (level from the size rank, or from the dotted number "3.2.2"), which the chunker already understands as section
    boundaries;
  * the largest heading on page 1 is the title.

Everything here works on plain data (lists of `StyledLine`), so it is testable without a PDF.
"""

from __future__ import annotations

import ctypes
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

_DIGITS = re.compile(r"\d+")
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+\S")
_PAGE_NUMBER = re.compile(r"^(?:page\s+)?(\d{1,5})(?:\s*(?:of|/)\s*\d{1,5})?$", re.I)
_SENTENCE_END = re.compile(r"[.!?;,]$")
_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.I)
MAX_HEADING_WORDS = 16
MAX_HEADING_CHARS = 140


@dataclass
class StyledLine:
    text: str
    size: float = 0.0
    bold: bool = False


@dataclass
class Structure:
    body_size: float = 0.0
    furniture: set[str] = field(default_factory=set)   # digit-masked keys of repeated header/footer lines
    headings: dict[str, int] = field(default_factory=dict)  # exact line text -> level (1 = top)
    title: str = ""
    labels: dict[int, str] = field(default_factory=dict)  # page index -> printed page number


def key(text: str) -> str:
    return _DIGITS.sub("#", re.sub(r"\s+", " ", text.strip().lower()))


_ONLY_NUMBERS = re.compile(r"^[#\W_]+$")


def is_furniture(text: str, furniture: set[str]) -> bool:
    """Is this line a repeating header or footer, possibly fused with numbers from its neighbour?

    Text extraction joins pieces that share a baseline, so a footer ("54 Chapter 8 Paying Tax Through Withholding") can arrive
    with the last cell of the table above it in front ("13930 54 Chapter 8 ..."). The digit-masked key then differs from the
    footer's and the line would survive, and, being set in the footer's bold type, become a heading. A line that is a known
    furniture line plus nothing but numbers and punctuation on either side is furniture."""
    k = key(text)
    if k in furniture:
        return True
    if len(k) < 20:
        return False
    for f in furniture:
        if len(f) < 15:
            continue
        if k.endswith(" " + f):
            rest = k[: -len(f)]
        elif k.startswith(f + " "):
            rest = k[len(f):]
        else:
            continue
        if rest.strip() and all(_ONLY_NUMBERS.match(tok) for tok in rest.split()):
            return True
    return False


def page_styled_lines(textpage: object, raw_text: str, *, samples: int = 5) -> list[StyledLine]:
    """Lines of a pdfium text page with the size and weight of the type they are set in."""
    import pypdfium2.raw as pdfium_c

    from rag.text.normalize import clean_text

    ptr = getattr(textpage, "raw", None)
    if ptr is None:
        return [StyledLine(clean_text(line, keep_glyph_codes=True)) for line in raw_text.splitlines() if line.strip()]
    out: list[StyledLine] = []
    pos = 0
    font = ctypes.create_string_buffer(96)
    flags = ctypes.c_int(0)
    for line in raw_text.split("\r\n"):
        start = pos
        pos += len(line) + 2
        if not line.strip():
            continue
        text = clean_text(line, keep_glyph_codes=True)
        if not text:
            continue
        idx = [start + i for i, ch in enumerate(line) if not ch.isspace()]
        picks = idx if len(idx) <= samples else [idx[round(i * (len(idx) - 1) / (samples - 1))] for i in range(samples)]
        sizes, bolds = [], 0
        for i in picks:
            size = pdfium_c.FPDFText_GetFontSize(ptr, i)
            if size <= 0:
                continue
            sizes.append(size)
            weight = pdfium_c.FPDFText_GetFontWeight(ptr, i)
            bold = weight >= 600
            if not bold and pdfium_c.FPDFText_GetFontInfo(ptr, i, font, 96, ctypes.byref(flags)) > 0:
                bold = bool(_BOLD_FONT.search(font.value.decode("latin-1", "ignore")))
            bolds += bold
        if sizes:
            out.append(StyledLine(text, round(statistics.median(sizes) * 2) / 2, bolds >= max(1.0, 0.8 * len(sizes))))
        else:
            out.append(StyledLine(text))
    return out


def _body_size(pages: list[list[StyledLine]]) -> float:
    weight: Counter[float] = Counter()
    for lines in pages:
        for ln in lines:
            if ln.size > 0 and len(ln.text) >= 25:
                weight[ln.size] += len(ln.text)
    if not weight:
        for lines in pages:
            for ln in lines:
                if ln.size > 0:
                    weight[ln.size] += len(ln.text)
    return weight.most_common(1)[0][0] if weight else 0.0


def _furniture(pages: list[list[StyledLine]], edge: int = 3) -> set[str]:
    n = len(pages)
    if n < 4:
        return set()
    seen: Counter[str] = Counter()
    for lines in pages:
        keys = {key(ln.text) for ln in (lines[:edge] + lines[-edge:]) if 0 < len(ln.text) <= 160}
        seen.update(keys)
    need = max(3, int(n * 0.35))
    return {k for k, c in seen.items() if c >= need and not _is_content_like(k)}


def _is_content_like(masked: str) -> bool:
    """A line that repeats on many pages is furniture, unless it is a long sentence that merely recurs."""
    return len(masked.split()) > 22


def _heading_level(text: str, size: float, bold: bool, body: float, size_levels: list[float]) -> int:
    numbered = _NUMBERED.match(text)
    if numbered and size >= body * 0.95:
        return min(4, numbered.group(1).count(".") + 1)
    if size > body * 1.1 and size in size_levels:
        return min(4, size_levels.index(size) + 1)
    return min(5, len(size_levels) + 1)


def analyse(pages: list[list[StyledLine]], *, drawing_pages: set[int] | None = None) -> Structure:
    """Decide body size, furniture, headings, title and printed page numbers for a whole document."""
    drawing_pages = drawing_pages or set()
    text_pages = [ln for i, ln in enumerate(pages) if i not in drawing_pages]
    result = Structure(body_size=_body_size(text_pages))
    if result.body_size <= 0:
        return result
    body = result.body_size
    result.furniture = _furniture(text_pages)
    for index, lines in enumerate(pages):
        for ln in lines:
            if is_furniture(ln.text, result.furniture):
                match = _PAGE_NUMBER.match(ln.text.strip())
                if match and index not in result.labels:
                    result.labels[index] = match.group(1)
    # candidate headings
    found: list[tuple[int, StyledLine]] = []
    for index, lines in enumerate(pages):
        if index in drawing_pages:
            continue
        for ln in lines:
            text = ln.text
            if ln.size <= 0 or is_furniture(text, result.furniture) or len(text) > MAX_HEADING_CHARS:
                continue
            if len(text.split()) > MAX_HEADING_WORDS or sum(ch.isalpha() for ch in text) < 3 or _PAGE_NUMBER.match(text):
                continue
            if text[0].islower():
                continue  # a heading does not start in lower case; this is the continuation of a paragraph line
            if text.endswith(":") and len(text.split()) >= 5:
                continue  # "Overall, our survey makes the following contributions:" introduces a list; it is not a heading
            larger = ln.size >= body * 1.12 and ln.size >= body + 0.9
            bold_short = ln.bold and ln.size >= body * 0.95 and len(text.split()) <= 12 and not _SENTENCE_END.search(text)
            if larger or bold_short:
                found.append((index, ln))
    sizes = sorted({ln.size for _, ln in found if ln.size >= body * 1.12}, reverse=True)[:4]
    for _, ln in found:
        result.headings[ln.text] = _heading_level(ln.text, ln.size, ln.bold, body, sizes)
    # title: the largest type on page 1 (consecutive lines of that size form one title)
    first = [ln for ln in (pages[0] if pages else []) if not is_furniture(ln.text, result.furniture) and ln.size > 0]
    if first:
        top = max(ln.size for ln in first)
        if top >= body * 1.3:
            parts: list[str] = []
            for ln in first:
                if ln.size == top:
                    parts.append(ln.text)
                elif parts:
                    break
            result.title = re.sub(r"\s+", " ", " ".join(parts)).strip()[:200]
    return result


def _plain(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def mark_outline_headings(page_texts: list[str], entries: list[tuple[int, str, int]]) -> tuple[list[str], int]:
    """Put Markdown heading markers on the lines of the page text that an outline names.

    `entries` are (level, title, first page). The title comes from a different parse of the same PDF, so it can differ
    from the line (case, a glyph or two); a line matches when the two are the same after stripping punctuation and case,
    or nearly (fuzzy ratio). A heading wrapped over two lines is joined. Returns the new texts and how many headings were
    found, so the caller can tell whether the outline fits this text at all."""
    from rapidfuzz import fuzz

    lines_by_page = [t.split("\n") for t in page_texts]
    found = 0
    for level, title, start in entries:
        want = _plain(title)
        if len(want) < 3 or not 1 <= start <= len(lines_by_page):
            continue
        for page_index in (start - 1, start):  # the heading sits on its first page; tolerate an off-by-one
            if page_index >= len(lines_by_page):
                break
            lines = lines_by_page[page_index]
            hit = None
            for i, line in enumerate(lines):
                if line.startswith("#") or len(line) > len(title) * 2 + 24:
                    continue
                for span in (1, 2):
                    if i + span > len(lines):
                        break
                    candidate = " ".join(lines[i: i + span])
                    plain = _plain(candidate)
                    if plain == want or (len(want) >= 8 and fuzz.ratio(plain, want) >= 88):
                        hit = (i, span, candidate)
                        break
                if hit:
                    break
            if hit:
                i, span, candidate = hit
                lines[i: i + span] = ["#" * min(level, 5) + " " + candidate.strip()]
                found += 1
                break
    return ["\n".join(lines) for lines in lines_by_page], found


_SENTENCE_END_OR_COLON = re.compile(r"[.!?:;]$")
MAX_CONTINUATION_CHARS = 90


def apply(text: str, structure: Structure, *, headings: bool = True) -> str:
    """Page text without furniture lines, with Markdown markers on headings (wrapped headings are joined)."""
    out: list[str] = []
    previous_heading: tuple[int, str] | None = None
    open_heading = False  # the line just written was a heading that may continue on the next line
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            previous_heading = None
            open_heading = False
            continue
        if is_furniture(stripped, structure.furniture):
            continue
        level = structure.headings.get(stripped) if headings else None
        if level:
            wrapped = (previous_heading and previous_heading[0] == level and not _SENTENCE_END.search(out[-1])
                       and not _NUMBERED.match(stripped))
            if wrapped:
                out[-1] = f"{out[-1]} {stripped}"
            else:
                out.append("#" * level + " " + stripped)
            previous_heading = (level, stripped)
            open_heading = not _SENTENCE_END_OR_COLON.search(stripped)
        elif open_heading and stripped[0].islower() and len(stripped) <= MAX_CONTINUATION_CHARS:
            # "Table 11: self-contained year-end statement (multiple" / "problems resolved)": the rest of a wrapped heading
            out[-1] = f"{out[-1]} {stripped}"
            open_heading = False
        else:
            out.append(line)
            previous_heading = None
            open_heading = False
    return "\n".join(out)
