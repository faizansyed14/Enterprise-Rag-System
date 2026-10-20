"""Rules-first metadata: every field comes from the most reliable place that has it, with its confidence and source.

Order of trust (highest first, per field): source system columns (SharePoint document number...), ISO 19650
file name, drawing title block, document content (first page headings and labels), file name patterns, folder path,
document properties. A small LLM (LLM_ENRICH) fills only what is still missing or weak, and its answers carry a
lower confidence so a rule always wins over a guess.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rag.metadata.file_naming import parse_filename
from rag.metadata.identifiers import extract_identifiers, identifier_key, spec_display, spec_key
from rag.metadata.path_rules import PathRules
from rag.metadata.title_block import parse_title_block
from rag.metadata.vocabularies import (
    doc_type_from_text,
)
from rag.text.language import detect_language

log = logging.getLogger(__name__)

FIELDS = ("doc_number", "title", "doc_type", "project", "spec_section")

_CONTENT_TYPES = [
    (re.compile(r"(?i)\brequest\s+for\s+information\b|طلب\s+معلومات"), "rfi"),
    (re.compile(r"(?i)\b(?:material\s+)?submittal\b|\bmaterial\s+approval\b|اعتماد\s+مواد"), "submittal"),
    (re.compile(r"(?i)\bminutes\s+of\s+(?:the\s+)?meeting\b|\bmeeting\s+minutes\b|محضر\s+اجتماع"), "minutes"),
    (re.compile(r"(?i)\b(?:change|variation)\s+order\b|أمر\s+تغيير"), "change_order"),
    (re.compile(r"(?i)\b(?:tax\s+)?invoice\b|فاتورة"), "invoice"),
    (re.compile(r"(?i)\btransmittal\b"), "transmittal"),
    (re.compile(r"(?i)\bnon[-\s]?conformance\s+report\b"), "inspection"),
    (re.compile(r"(?i)\bmethod\s+statement\b"), "method_statement"),
    (re.compile(r"(?i)\bsite\s+instruction\b|تعليمات\s+الموقع"), "site_instruction"),
    (re.compile(r"(?i)\bbill\s+of\s+quantities\b|جدول\s+الكميات"), "boq"),
]
_SECTION_HEADING = re.compile(r"(?im)^\s*section\s+(\d{2}\s?\d{2}\s?\d{2}(?:\.\d{2})?)\s*[-–:]?\s*(.*)$")
_NUMBER_LABEL = re.compile(
    r"(?i)\b(rfi|request\s+for\s+information|submittal|change\s+order|variation\s+order|site\s+instruction|"
    r"invoice|transmittal|ncr|co|vo|si)\s*(?:no\.?|number|#|ref\.?)\s*[:.]?\s*([A-Z0-9][A-Z0-9/-]{0,20})"
)
_LABEL_CODES = {"rfi": "RFI", "request for information": "RFI", "submittal": "SUB", "change order": "CO",
                "variation order": "VO", "site instruction": "SI", "invoice": "INV", "transmittal": "TRN",
                "ncr": "NCR", "co": "CO", "vo": "VO", "si": "SI"}
_NEXT_LABEL = re.compile(r"\s+(?=(?:Issue\s+)?Date\s*:|From\s*:|To\s*:|Rev(?:ision)?\s*:|Ref\s*:|Subject\s*:|"
                         r"Client\s*:|Contractor\s*:|Consultant\s*:|Location\s*:|Doc(?:ument)?\s+No)")
_PROJECT_LABEL = re.compile(r"(?im)^\s*(?:project(?:\s+name)?|اسم\s+المشروع|المشروع)\s*[:\-]\s*(.{3,80})$")
_SUBJECT = re.compile(r"(?im)^\s*(?:subject|re|title|الموضوع)\s*[:\-]\s*(.{3,160})$")
_BAD_TITLES = re.compile(r"(?i)^(?:microsoft\s+word\s*-|untitled|document\d*$|page\s+\d+|slide\s+\d+)")


_TITLE_JUNK = re.compile(r"(?i)\.{4,}|^arxiv:|^https?://|^page\s+\d|^(?:table of )?contents?$|^abstract$|^untitled|^\W*\d+\W*$")


def _title_like_lines(text: str, count: int) -> list[str]:
    """The first lines of a page that read like titles or labels (short, not sentences). A line such as
    "Submittals: thirty entries, SUB-019 Rev 0 still under review." is narrative about other things."""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line.split()) > 12 or re.search(r"[.!?] \S", line):
            break
        out.append(line)
        if len(out) >= count:
            break
    return out


def plausible_title(text: str | None) -> bool:
    """A document title read off a page: not a table-of-contents line, an arXiv stamp, a URL or a lone number."""
    text = (text or "").strip()
    return 4 <= len(text) <= 200 and not _TITLE_JUNK.search(text) and sum(ch.isalpha() for ch in text) >= 4


@dataclass
class FieldValue:
    value: Any
    confidence: float
    source: str


@dataclass
class DocMetadata:
    fields: dict[str, FieldValue] = field(default_factory=dict)
    identifiers: list[str] = field(default_factory=list)
    language: str = "und"

    def offer(self, name: str, value: Any, confidence: float, source: str) -> None:
        if value is None or value == "":
            return
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return
        current = self.fields.get(name)
        if current is None or confidence > current.confidence:
            self.fields[name] = FieldValue(value, round(confidence, 3), source)

    def get(self, name: str, default: Any = None) -> Any:
        fv = self.fields.get(name)
        return fv.value if fv else default

    def confidence(self, name: str) -> float:
        fv = self.fields.get(name)
        return fv.confidence if fv else 0.0

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, fv in self.fields.items():
            value = fv.value.isoformat() if isinstance(fv.value, date) else fv.value
            out[name] = {"value": value, "confidence": fv.confidence, "source": fv.source}
        return out

    @property
    def doc_number_key(self) -> str | None:
        number = self.get("doc_number")
        return identifier_key(number) if number else None


@dataclass
class PageInput:
    number: int
    text: str
    is_drawing: bool = False
    title_block: dict[str, str] = field(default_factory=dict)


class MetadataExtractor:
    def __init__(self, path_rules: PathRules | None = None, *, dayfirst: bool = True) -> None:
        self.path_rules = path_rules or PathRules()
        self.dayfirst = dayfirst

    def extract(
        self,
        *,
        path: str,
        source_fields: dict[str, Any] | None = None,
        pages: list[PageInput],
        properties: dict[str, str] | None = None,
    ) -> DocMetadata:
        md = DocMetadata()
        text_sample = "\n".join(p.text for p in pages[:3])[:20000]
        md.language = detect_language("\n".join(p.text for p in pages[:10])[:20000])

        # 1. Fields from the source system (SharePoint columns, upload form) are authoritative.
        for name, value in (source_fields or {}).items():
            if name in FIELDS and value:
                md.offer(name, value, 0.98, "source")

        # 2. File name.
        nf = parse_filename(path)
        name_conf_scale = 1.0 if nf.iso else 0.95
        for name in ("doc_number", "title", "doc_type", "project", "spec_section"):
            value = getattr(nf, name)
            if value:
                md.offer(name, value, nf.confidence.get(name, 0.5) * name_conf_scale, "filename")

        # 3. Drawing title block (first drawing sheet).
        for page in pages[:3]:
            block = page.title_block or (parse_title_block(page.text) if page.is_drawing else {})
            if not block:
                continue
            md.offer("doc_number", block.get("doc_number"), 0.9, "title_block")
            md.offer("title", block.get("title"), 0.88, "title_block")
            md.offer("project", block.get("project"), 0.75, "title_block")
            if page.is_drawing:
                md.offer("doc_type", "drawing", 0.85, "title_block")
            break

        # 4. Content of the first pages.
        self._from_content(md, text_sample)

        # 5. Folder path.
        for name, (value, conf) in self.path_rules.apply(path).items():
            if name in FIELDS:
                md.offer(name, value, conf, "path")

        # 6. Document properties (PDF/Office title) -- often stale ("Microsoft Word - draft.doc"), so low trust. The title
        #    the layout analysis read off the first page (largest type, or the outline's title) is worth more.
        prop_title = (properties or {}).get("title", "")
        if prop_title and not _BAD_TITLES.match(prop_title) and len(prop_title) > 3:
            md.offer("title", prop_title, 0.45, "properties")
        guess = (properties or {}).get("title_guess", "")
        if plausible_title(guess) and (len(guess.split()) >= 2 or not prop_title):  # one word is a stray heading, not a title
            md.offer("title", guess, 0.72, "layout")

        # Derived values.
        spec = md.get("spec_section")
        if spec:
            key = spec_key(spec)
            if key:
                md.fields["spec_section"].value = spec_display(key)
                md.offer("doc_type", "specification", 0.7, "spec_section")
        if not md.get("title"):
            first_line = next((ln.strip() for p in pages[:1] for ln in p.text.split("\n") if 3 < len(ln.strip()) < 120), "")
            md.offer("title", first_line or nf.title or path.rsplit("/", 1)[-1], 0.3, "first_line")

        ids = extract_identifiers(text_sample, limit=60)
        md.identifiers = [i.key for i in ids]
        if md.doc_number_key and md.doc_number_key not in md.identifiers:
            md.identifiers.insert(0, md.doc_number_key)
        return md

    def _from_content(self, md: DocMetadata, text: str) -> None:
        head = text[:3000]
        # The type of a document is what its title says, not any word in its first paragraphs ("Submittals" in a
        # project summary does not make the summary a submittal).
        title_zone = "\n".join(_title_like_lines(head, 3))[:400]
        for pattern, doc_type in _CONTENT_TYPES:
            if pattern.search(title_zone):
                md.offer("doc_type", doc_type, 0.75, "content")
                break
        if m := _SECTION_HEADING.search(head):
            key = spec_key(m.group(1))
            if key:
                md.offer("spec_section", spec_display(key), 0.85, "content")
                md.offer("doc_number", spec_display(key), 0.7, "content")
                title = m.group(2).strip()
                if not title:
                    after = head[m.end():].strip().split("\n", 1)[0]
                    title = after.strip()
                if 3 < len(title) < 120:
                    md.offer("title", title.title() if title.isupper() else title, 0.7, "content")
        if m := _NUMBER_LABEL.search(head):
            label = re.sub(r"\s+", " ", m.group(1).lower())
            code = _LABEL_CODES.get(label, label.upper())
            number = m.group(2).strip("-/")
            if any(ch.isdigit() for ch in number):
                value = number if number.upper().startswith(code) else f"{code}-{number}"
                md.offer("doc_number", value.upper(), 0.7, "content")
        first_lines = "\n".join(_title_like_lines(head, 2))
        for ident in extract_identifiers(first_lines, limit=3):
            if ident.kind in {"prefixed", "iso"}:
                md.offer("doc_number", ident.raw.upper().replace(" ", "-"), 0.65, "content")
                break
        if m := _PROJECT_LABEL.search(head):
            value = _NEXT_LABEL.split(m.group(1).strip())[0]
            md.offer("project", value.strip(" ,;-"), 0.6, "content")
        if m := _SUBJECT.search(head):
            md.offer("title", m.group(1).strip(), 0.6, "content")
        if not md.get("doc_type") and (t := doc_type_from_text(title_zone)):
            md.offer("doc_type", t, 0.35, "content")


ENRICH_PROMPT = """You label construction project documents. Read the start of the document and return JSON only:
{"title": str|null, "doc_type": one of [%s]|null,
 "doc_number": str|null, "confidence": number between 0 and 1}
Use null when the text does not say. Do not invent numbers."""


async def fill_gaps_with_llm(chat: Any, md: DocMetadata, text: str, *, threshold: float = 0.5) -> list[str]:
    """Ask LLM_ENRICH for fields that are missing or weaker than `threshold`. Returns the fields it filled."""
    from rag.metadata.vocabularies import DOC_TYPES
    from rag.plugins.capabilities import ChatMessage

    wanted = [f for f in ("title", "doc_type", "doc_number") if md.confidence(f) < threshold]
    if not wanted or chat is None or not text.strip():
        return []
    prompt = ENRICH_PROMPT % ", ".join(DOC_TYPES)
    result = await chat.complete(
        [ChatMessage("system", prompt), ChatMessage("user", text[:6000])], max_tokens=300, temperature=0.0,
        json_mode=True,
    )
    try:
        data = json.loads(_json_block(result.content))
    except (ValueError, TypeError):
        log.warning("enrichment model returned non-JSON output")
        return []
    llm_conf = float(data.get("confidence") or 0.5)
    filled = []
    for name in wanted:
        value = data.get(name)
        if name == "doc_type" and value not in DOC_TYPES:
            continue
        if value:
            before = md.confidence(name)
            md.offer(name, value, min(0.6, llm_conf * 0.6), "llm")
            if md.confidence(name) > before:
                filled.append(name)
    return filled


def _json_block(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    return text[start: end + 1] if start >= 0 and end > start else text
