"""Dates in documents and questions. Day-first by default (DD/MM/YYYY, usual in the Gulf, UK and Europe)."""

from __future__ import annotations

import re
from datetime import date, datetime

from dateutil import parser as dateparser

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|"
    "september|october|november|december"
)
DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b"),
    re.compile(rf"(?i)\b(\d{{1,2}})(?:st|nd|rd|th)?[\s-]+({_MONTHS})\.?[\s,-]+(\d{{4}})\b"),
    re.compile(rf"(?i)\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"),
]
_LABELLED = re.compile(r"(?i)\b(?:date(?:d)?|issue\s*date|date\s*of\s*issue|التاريخ)\s*[:.]?\s*(.{6,40})")


def parse_date(text: str, *, dayfirst: bool = True) -> date | None:
    text = text.strip()
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            value = dateparser.parse(m.group(0), dayfirst=dayfirst and not pattern.pattern.startswith(r"\b(\d{4})"),
                                     fuzzy=True)
        except (ValueError, OverflowError):
            continue
        if value and 1950 <= value.year <= 2100:
            return value.date()
    return None


def find_document_date(text: str, *, dayfirst: bool = True) -> date | None:
    """The labelled date ('Date: 12/03/2024') if there is one, else the first date in the text."""
    for m in _LABELLED.finditer(text or ""):
        if found := parse_date(m.group(1), dayfirst=dayfirst):
            return found
    return parse_date((text or "")[:4000], dayfirst=dayfirst)


def to_iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None
