"""A plain-language account of what the privacy shield found in a document, for the upload list and the document page.

Built from the same detector that masks the requests, so what it reports is what is hidden from cloud models.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from rag.privacy.shield import ALL_KINDS, Vault, find

NAMES = {
    "account": ("bank account number", "bank account numbers"),
    "iban": ("IBAN", "IBANs"),
    "card": ("card number", "card numbers"),
    "swift": ("SWIFT/BIC code", "SWIFT/BIC codes"),
    "ssn": ("social security number", "social security numbers"),
    "emirates_id": ("Emirates ID", "Emirates IDs"),
    "passport": ("passport number", "passport numbers"),
    "email": ("email address", "email addresses"),
    "phone": ("phone number", "phone numbers"),
    "payment_ref": ("payment reference", "payment references"),
    "secret": ("password or key", "passwords or keys"),
    "tax_id": ("tax or licence number", "tax or licence numbers"),
    "long_number": ("long number", "long numbers"),
    "custom": ("value from your own rules", "values from your own rules"),
}


def phrase(found: dict[str, int]) -> str:
    """'1 bank account number and 1 IBAN'"""
    parts = [f"{n} {NAMES.get(kind, (kind, kind + 's'))[0 if n == 1 else 1]}" for kind, n in found.items() if n]
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else ""


def table_text(tables: Sequence[dict[str, Any]] | None) -> str:
    return "\n".join(" | ".join(str(c) for c in row) for t in tables or [] for row in t.get("rows", []))


def scan_pages(
    pages: Iterable[tuple[int, str, Sequence[dict[str, Any]] | None]], kinds: Sequence[str] = ALL_KINDS
) -> dict[str, Any]:
    """Counts of what the shield finds in stored pages (text and table cells): totals and per page."""
    total: Counter[str] = Counter()
    per_page: dict[int, dict[str, int]] = {}
    for number, text, tables in pages:
        found = Counter(f.kind for f in find(f"{text or ''}\n{table_text(tables)}", kinds))
        if found:
            per_page[number] = dict(found)
            total.update(found)
    return {"found": dict(total), "pages": per_page, "phrase": phrase(dict(total))}


def model_view(chunks: Iterable[tuple[int, str, str]], kinds: Sequence[str] = ALL_KINDS) -> dict[str, Any]:
    """What a cloud model would receive from a document's chunks (page, header, text): the masked text of those that
    hold a value."""
    total: Counter[str] = Counter()
    pages: dict[int, dict[str, Any]] = {}
    count = 0
    for page, header, body in chunks:
        count += 1
        raw = f"{header}\n{body}"
        found = Counter(f.kind for f in find(raw, kinds))
        if not found:
            continue
        total.update(found)
        entry = pages.setdefault(page, {"page": page, "found": Counter(), "model_view": []})
        entry["found"].update(found)
        entry["model_view"].append(Vault(kinds).mask(raw))
    return {
        "found": dict(total),
        "phrase": phrase(dict(total)),
        "chunks": count,
        "pages": [
            {"page": p["page"], "found": dict(p["found"]), "model_view": "\n\n".join(p["model_view"])}
            for p in sorted(pages.values(), key=lambda p: p["page"])
        ],
    }
