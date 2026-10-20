"""Cross-references between documents ("refer to drawing A-102", "response to RFI-023").

The agent's `open_reference` tool follows these, and the document page lists what a document refers to and what
refers to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag.metadata.identifiers import extract_identifiers

_RESPONDS = re.compile(r"(?i)\b(?:response\s+to|reply\s+to|in\s+reply\s+to|answer\s+to|ردا\s+على|رداً\s+على)\b")


@dataclass(frozen=True)
class Reference:
    target: str  # identifier key
    kind: str  # refers_to | responds_to
    page_no: int
    context: str


def find_references(pages: list[tuple[int, str]], own_keys: set[str], *, per_document: int = 200) -> list[Reference]:
    found: dict[tuple[str, str], Reference] = {}
    for page_no, text in pages:
        if not text:
            continue
        for ident in extract_identifiers(text, limit=200):
            if ident.key in own_keys:
                continue
            idx = text.find(ident.raw)
            start = max(0, idx - 90) if idx >= 0 else 0
            context = text[start: (idx + len(ident.raw) + 90) if idx >= 0 else 180].replace("\n", " ").strip()
            before = text[max(0, idx - 60): idx] if idx >= 0 else ""
            before = re.split(r"[.;:\n]", before)[-1]  # only the clause the identifier is in
            kind = "refers_to"
            if _RESPONDS.search(before):
                kind = "responds_to"
            key = (ident.key, kind)
            if key not in found:
                found[key] = Reference(ident.key, kind, page_no, context[:300])
            if len(found) >= per_document:
                return list(found.values())
    return list(found.values())
