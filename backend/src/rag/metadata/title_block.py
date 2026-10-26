"""Drawing title blocks: label/value pairs in the corner of a sheet.

The page router passes the text of the title-block region (bottom-right by default; the whole page for small
sheets). Values can sit on the same line as their label or on the next line, in English or Arabic.
"""

from __future__ import annotations

import re

from rag.metadata.identifiers import extract_identifiers

LABELS: dict[str, list[str]] = {
    "doc_number": [r"drawing\s*(?:no|number|nr)\.?", r"dwg\.?\s*(?:no|number)\.?", r"drg\.?\s*(?:no|number)\.?",
                   r"sheet\s*(?:no|number)\.?", r"doc(?:ument)?\.?\s*(?:no|number)\.?", r"رقم\s*(?:المخطط|اللوحة|الرسم)"],
    "revision": [r"rev(?:ision)?\.?(?:\s*no\.?)?", r"(?:رقم\s*)?المراجعة", r"الإصدار"],
    "title": [r"drawing\s*title", r"sheet\s*title", r"title", r"عنوان\s*(?:المخطط|اللوحة)", r"العنوان"],
    "project": [r"project\s*(?:name|title)?", r"اسم\s*المشروع", r"المشروع"],
    "project_number": [r"project\s*(?:no|number)\.?", r"job\s*(?:no|number)\.?", r"رقم\s*المشروع"],
    "date": [r"(?:issue\s*)?date", r"التاريخ"],
    "scale": [r"scale", r"المقياس"],
    "status": [r"status", r"purpose\s*of\s*issue", r"suitability", r"الحالة"],
    "drawn_by": [r"drawn\s*(?:by)?", r"رسم"],
    "checked_by": [r"checked\s*(?:by)?", r"تدقيق"],
    "approved_by": [r"approved\s*(?:by)?", r"اعتماد"],
}
_ALL_LABELS = re.compile(
    r"(?i)(?<![\w])(?:" + "|".join(p for pats in LABELS.values() for p in pats) + r")\s*[:.#]?",
)
_VALUE_END = re.compile(r"\s{3,}|\t|$")


def _value_after(text: str, start: int) -> str:
    line_end = text.find("\n", start)
    line = text[start: line_end if line_end >= 0 else len(text)]
    # Stop at the next label on the same line ("DRAWING NO: A-101   REV: C").
    nxt = _ALL_LABELS.search(line)
    if nxt and nxt.start() > 0:
        line = line[: nxt.start()]
    value = _VALUE_END.split(line.strip(" :.#-"), maxsplit=1)[0].strip(" :.#-")
    if value or line_end < 0:
        return value
    # Value on the following non-empty line.
    for candidate in text[line_end + 1:].split("\n")[:2]:
        following = candidate.strip()
        if following and not _ALL_LABELS.match(following):
            return _VALUE_END.split(following, maxsplit=1)[0].strip(" :.#-")
    return ""


def parse_title_block(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not text:
        return fields
    for name, patterns in LABELS.items():
        if name in fields:
            continue
        for pattern in patterns:
            for m in re.finditer(rf"(?i)(?<![\w]){pattern}\s*[:.#]?", text):
                # "project" must not swallow "project no".
                if name == "project" and re.match(r"(?i)\s*(?:no|number)\b", text[m.end():]):
                    continue
                value = _value_after(text, m.end())
                if value and len(value) <= 160:
                    fields[name] = value
                    break
            if name in fields:
                break
    for unused in ("revision", "status", "date"):  # recognised only so that a value stops at the next label
        fields.pop(unused, None)
    if "doc_number" in fields:
        ids = extract_identifiers(fields["doc_number"], query=True)
        if ids:
            fields["doc_number"] = ids[0].raw.upper()
    return fields
