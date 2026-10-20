"""Document numbers and codes: drawing numbers, ISO 19650 names, spec sections, RFIs, submittals, change orders...

People write the same identifier many ways (A-101, A101, a 101; RFI #023, RFI-23, RFI No. 23; 03 30 00 and 033000).
`identifier_key` folds them to one key, so the exact-match retrieval leg and the BM25 analyzer find a document
however the question spells its number.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

PREFIXES = (
    "RFI", "SUB", "SUBMITTAL", "CO", "PCO", "CCD", "COR", "VO", "VOR", "NCR", "TQ", "TRN", "TR", "TRANSMITTAL",
    "INV", "PO", "CT", "MOM", "DWG", "SK", "ASI", "CR", "EOT", "SI", "RFC", "MIR", "WIR", "IR", "DN", "PR", "LTR",
    "MS", "ITP", "MAR", "MTL", "BOQ", "IPC", "PC", "RN",
)
_PREFIX_RE = "|".join(sorted(PREFIXES, key=len, reverse=True))

# ISO 19650: Project-Originator-Volume-Level-Type-Role-Number (5 to 8 segments).
ISO_RE = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{2,8}(?:-[A-Z0-9]{1,8}){4,9}(?![A-Z0-9])")
_ISO_SUFFIX = re.compile(r"^(?:S[0-7]|A[1-7]|B[1-7]|CR|[PC]\d{2}(?:\.\d{1,2})?)$")
PREFIXED_RE = re.compile(
    rf"\b(?:{_PREFIX_RE})(?:\s*[-_#.:]\s*|\s+(?:NO\.?|NUMBER|#)?\s*|(?=\d))\d{{1,6}}(?:[-.]\d{{1,4}})*[A-Z]?\b",
    re.IGNORECASE,
)
DRAWING_RE = re.compile(r"\b[A-Z]{1,3}[-_.]?\d{1,3}(?:[-.]\d{1,3})?[A-Z]?\b")
DRAWING_STRICT_RE = re.compile(r"\b[A-Z]{1,3}[-_.]?\d{2,4}(?:[.-]\d{1,3})?[A-Z]?\b")
SPEC_RE = re.compile(r"\b(\d{2})[ .]?(\d{2})[ .]?(\d{2})(?:\.(\d{2}))?\b")
SPEC_CONTEXT_RE = re.compile(r"(?i)\b(?:section|spec(?:ification)?|division)\s*[:#]?\s*(\d{2}[ .]?\d{2}[ .]?\d{2}(?:\.\d{2})?)")
SPACED_SPEC_RE = re.compile(r"\b\d{2} \d{2} \d{2}(?:\.\d{2})?\b")
GENERIC_CODE_RE = re.compile(r"\b[A-Z]{2,6}-\d{2,8}(?:-\d{1,6})*\b")

# Captions and numbered parts of a document ("Table 11", "Figure 3", "Appendix 2"). A question naming one is an exact lookup.
LABEL_RE = re.compile(
    r"\b(TABLE|FIGURE|FIG|CHART|APPENDIX|ANNEX|SCHEDULE|EXHIBIT)\.?\s*[-.:#]?\s*([A-Z]?\d{1,3}(?:\.\d{1,3})*[A-Z]?)\b",
    re.IGNORECASE,
)

_NOISE = re.compile(r"\b(?:NO\.?|NUMBER|NUM\.?)\s*|#", re.IGNORECASE)
_PARTS = re.compile(r"[A-Z]+|\d+")
_STOP_CODES = frozenset({"ISO", "PDF", "DWG", "REV", "PAGE", "NO", "AM", "PM", "UTC", "GMT", "COVID"})


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).upper()


def spec_key(raw: str) -> str | None:
    m = SPEC_RE.fullmatch(_fold(raw).strip())
    if not m:
        return None
    key = f"SPEC-{m.group(1)}{m.group(2)}{m.group(3)}"
    return key + (f".{m.group(4)}" if m.group(4) else "")


def identifier_key(raw: str) -> str:
    """Canonical key: upper-case letter runs and digit runs (leading zeros dropped), joined with '-'."""
    text = _fold(raw).strip()
    spec = spec_key(text)
    if spec:
        return spec
    text = _NOISE.sub(" ", text)
    parts = _PARTS.findall(text)
    return "-".join(p.lstrip("0") or "0" if p.isdigit() else p for p in parts)


def spec_display(key: str) -> str:
    digits = key.removeprefix("SPEC-")
    base, _, sub = digits.partition(".")
    out = f"{base[0:2]} {base[2:4]} {base[4:6]}"
    return out + (f".{sub}" if sub else "")


def label_key(kind: str, number: str) -> str:
    kind = kind.upper()
    kind = "FIGURE" if kind == "FIG" else kind
    return f"{kind}-{number.upper().lstrip('0') or '0'}"


def label_keys(text: str) -> list[str]:
    """Labels such as TABLE-11 in a caption or section title. Used for a chunk's heading path, not for its body: a sentence that
    says "see Table 3" must not make its chunk look like Table 3."""
    seen: dict[str, None] = {}
    for m in LABEL_RE.finditer(unicodedata.normalize("NFKC", text or "")):
        seen[label_key(m.group(1), m.group(2))] = None
    return list(seen)


@dataclass(frozen=True)
class Identifier:
    raw: str
    key: str
    kind: str  # iso | prefixed | drawing | spec | code


def extract_identifiers(text: str, *, query: bool = False, limit: int = 40) -> list[Identifier]:
    """Identifiers in `text`, most specific patterns first, de-duplicated by key.

    Queries are matched case-insensitively ("a101", "rfi 23"); documents only match upper-case drawing-style
    codes so ordinary words never become identifiers.
    """
    if not text:
        return []
    folded = _fold(text) if query else unicodedata.normalize("NFKC", text)
    found: dict[str, Identifier] = {}
    taken: list[tuple[int, int]] = []

    def add(raw: str, kind: str, span: tuple[int, int], key: str | None = None) -> None:
        # A shorter pattern inside an identifier already found (ZZ-01 inside an ISO name) is not a new identifier.
        if any(span[0] < end and start < span[1] for start, end in taken):
            return
        key = key or identifier_key(raw)
        if not key or key in found or len(found) >= limit:
            return
        head = key.split("-", 1)[0]
        if head in _STOP_CODES and kind in {"drawing", "code"}:
            return
        if not any(ch.isdigit() for ch in key):
            return
        found[key] = Identifier(raw=raw.strip(), key=key, kind=kind)
        taken.append(span)

    for m in ISO_RE.finditer(folded):
        segments = m.group(0).split("-")
        while len(segments) > 5 and _ISO_SUFFIX.match(segments[-1]):
            segments.pop()  # status code appended to the number
        token = "-".join(segments)
        if sum(ch.isalpha() for ch in token) >= 2 and sum(ch.isdigit() for ch in token) >= 2:
            add(token, "iso", m.span())
    for m in PREFIXED_RE.finditer(folded):
        add(m.group(0), "prefixed", m.span())
    for m in SPEC_CONTEXT_RE.finditer(folded):
        key = spec_key(m.group(1))
        if key:
            add(m.group(1), "spec", m.span(1), key)
    for m in SPACED_SPEC_RE.finditer(folded):
        key = spec_key(m.group(0))
        if key and _plausible_spec(key):
            add(m.group(0), "spec", m.span(), key)
    for m in GENERIC_CODE_RE.finditer(folded):
        add(m.group(0), "code", m.span())
    if query:
        for m in LABEL_RE.finditer(folded):
            add(m.group(0), "label", m.span(), label_key(m.group(1), m.group(2)))
    drawing_re = DRAWING_RE if query else DRAWING_STRICT_RE
    for m in drawing_re.finditer(folded):
        token = m.group(0)
        if query or any(sep in token for sep in "-_.") or sum(ch.isdigit() for ch in token) >= 3:
            add(token, "drawing", m.span())
    return list(found.values())


def _plausible_spec(key: str) -> bool:
    digits = key.removeprefix("SPEC-")[:6]
    division = int(digits[:2])
    return 0 <= division <= 49


def identifier_keys(text: str, *, query: bool = False, limit: int = 40) -> list[str]:
    return [i.key for i in extract_identifiers(text, query=query, limit=limit)]
