"""Metadata from file names: ISO 19650 container names first, then the common informal patterns.

    PRJ1-ABC-ZZ-01-DR-A-1001_S2_P03.pdf   -> doc A-1001 series, type drawing, role A (architectural), rev P03
    A-101 Rev C - Ground Floor Plan.pdf   -> doc A-101, rev C, title "Ground Floor Plan"
    RFI-023 Response from Engineer.pdf    -> doc RFI-23, type rfi
    03 30 00 Cast-in-Place Concrete.docx  -> spec section 03 30 00, type specification
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from rag.metadata.identifiers import extract_identifiers, spec_display, spec_key
from rag.metadata.vocabularies import SPEC_DIVISIONS, doc_type_from_code, doc_type_from_text

ISO_TYPE_CODES = {
    "AF": "animation", "BQ": "boq", "CA": "calculation", "CM": "model", "CO": "correspondence", "CP": "report",
    "DB": "report", "DR": "drawing", "FN": "correspondence", "HS": "report", "IE": "report", "MI": "minutes",
    "MR": "model", "MS": "method_statement", "M2": "model", "M3": "model", "PP": "report", "PR": "schedule",
    "RD": "report", "RI": "rfi", "RP": "report", "SA": "schedule", "SH": "schedule", "SN": "report",
    "SP": "specification", "SU": "survey", "VS": "report",
}
ISO_NAME = re.compile(
    r"^(?P<project>[A-Z0-9]{2,8})-(?P<originator>[A-Z0-9]{2,8})-(?P<volume>[A-Z0-9]{1,4})-(?P<level>[A-Z0-9]{1,4})-"
    r"(?P<type>[A-Z0-9]{2})-(?P<role>[A-Z]{1,2})-(?P<number>\d{3,8})"
    r"(?:[-_ ](?P<status>S[0-7]|A[1-7]|B[1-7]|CR))?(?:[-_ ](?P<revision>[PC]\d{2}(?:\.\d{1,2})?))?(?=$|[-_ .])",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[_]+|\s+-\s+|\s{2,}")
# "2026-08-12_103.eml", "20260812 Minutes": a leading date is a date, not a specification section or a document number
_LEADING_DATE = re.compile(r"^(?:19|20)\d{2}[-_. ]?(?:0[1-9]|1[0-2])[-_. ]?(?:0[1-9]|[12]\d|3[01])(?!\d)[-_. ]*")


@dataclass
class NameFields:
    doc_number: str | None = None
    title: str | None = None
    doc_type: str | None = None
    project: str | None = None
    spec_section: str | None = None
    iso: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)

    def set(self, name: str, value: str | None, confidence: float) -> None:
        if value and (getattr(self, name) is None or confidence > self.confidence.get(name, 0)):
            setattr(self, name, value)
            self.confidence[name] = confidence


def _clean_title(text: str) -> str:
    text = _SEPARATORS.sub(" ", text).strip(" -_.()[]")
    return re.sub(r"\s+", " ", text)


def parse_filename(path: str) -> NameFields:
    name = PurePosixPath(path.replace("\\", "/")).name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    fields = NameFields()

    iso = ISO_NAME.match(stem.upper())
    if iso:
        g = {k: v for k, v in iso.groupdict().items() if v}
        fields.iso = g
        number = "-".join(stem[: iso.end()].split("-")[:7])
        number = re.split(r"[_ ]", number)[0]
        fields.set("doc_number", number.upper(), 0.95)
        fields.set("project", g["project"], 0.8)
        fields.set("doc_type", ISO_TYPE_CODES.get(g["type"].upper()), 0.9)
        rest = _clean_title(stem[iso.end():])
        if rest:
            fields.set("title", rest, 0.7)
        return fields

    working = _LEADING_DATE.sub("", stem) or stem

    # Spec section at the start ("03 30 00 Cast-in-Place Concrete", "033000_Concrete").
    spec = re.match(r"^(\d{2}[ .-]?\d{2}[ .-]?\d{2}(?:\.\d{2})?)(?![0-9])", working)
    if spec:
        key = spec_key(re.sub(r"[_-]", " ", spec.group(1)))
        if key and key.removeprefix("SPEC-")[:2] in SPEC_DIVISIONS:
            fields.set("spec_section", spec_display(key), 0.9)
            fields.set("doc_number", spec_display(key), 0.8)
            fields.set("doc_type", "specification", 0.85)
            title = _clean_title(working[spec.end():])
            if title:
                fields.set("title", title, 0.8)
            return fields

    ids = extract_identifiers(working.replace("_", " "))
    if ids:
        first = ids[0]
        fields.set("doc_number", first.raw.upper().replace(" ", "-") if first.kind == "prefixed" else first.raw.upper(), 0.8)
        prefix = re.match(r"[A-Z]+", first.key)
        if prefix:
            fields.set("doc_type", doc_type_from_code(prefix.group(0)), 0.75)
            if first.kind == "drawing":
                fields.set("doc_type", "drawing", 0.6)
        idx = working.upper().find(first.raw.upper())
        title = _clean_title(working[idx + len(first.raw):] if idx >= 0 else working)
        if title:
            fields.set("title", title, 0.75)
    else:
        fields.set("title", _clean_title(working), 0.5)

    if fields.doc_type is None:
        fields.set("doc_type", doc_type_from_text(working.replace("_", " ")), 0.5)
    return fields
