"""Tables that run on over several pages.

A long register, bill of quantities or comparison table prints its column headings on the first page only. Read page by page,
the first data row of every later page then looks like a heading, and each row is labelled with the previous row's values
("Amodei (2016): Liang (2016) | Speech: CV") instead of the column names ("Paper: Liang (2016) | Domain: CV"). A question that
names a column then finds nothing to match.

`link_continued_tables` recognises a table as the continuation of the previous page's table (it starts at the top of its page,
the previous one ends at the bottom of its page, same number of columns, same horizontal extent, and its first row is not a copy
of the heading) and puts the heading on top of it. Works on plain `ParsedTable`s, so it is testable without a PDF.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from rag.plugins.capabilities import ParsedTable

TOP_ZONE = 0.30        # a continuation starts in the top 30% of its page ...
BOTTOM_ZONE = 0.68     # ... and the table it continues ended in the bottom 32% of the previous one
X_TOLERANCE = 0.05     # same left and right edge, as a fraction of the page width
REPEAT_SHARE = 0.6     # the first row repeats the heading when this share of its cells equal the heading's

_NORMALISE = re.compile(r"[\W_]+")


def _key(cell: str) -> str:
    return _NORMALISE.sub("", (cell or "").lower())


def _columns(table: ParsedTable) -> int:
    return max((len(r) for r in table.rows), default=0)


def repeats_heading(first_row: Sequence[str], heading: Sequence[str]) -> bool:
    """Is this row a second printing of the heading (page break with a repeated header row)?"""
    pairs = [(_key(a), _key(b)) for a, b in zip(first_row, heading, strict=False) if _key(a) or _key(b)]
    if not pairs:
        return False
    return sum(1 for a, b in pairs if a == b) / len(pairs) >= REPEAT_SHARE


def _has_digit(row: Sequence[str]) -> bool:
    return any(ch.isdigit() for cell in row for ch in (cell or ""))


def _starts_new_table(previous: ParsedTable, first_row: Sequence[str]) -> bool:
    """A first row of plain labels (no digit, nothing long) under data rows that mostly carry digits is a heading of its own:
    a different table that happens to have the same layout."""
    if _has_digit(first_row) or any(len((c or "").strip()) > 40 for c in first_row):
        return False
    body = previous.rows[previous.header_rows:]
    return bool(body) and sum(1 for r in body if _has_digit(r)) / len(body) >= 0.6


def is_continuation(previous: ParsedTable, current: ParsedTable) -> bool:
    if previous.bbox is None or current.bbox is None or previous.header_rows < 1:
        return False
    if previous.bbox[3] < BOTTOM_ZONE or current.bbox[1] > TOP_ZONE:
        return False
    if _columns(previous) != _columns(current) or _columns(current) < 2:
        return False
    if abs(previous.bbox[0] - current.bbox[0]) > X_TOLERANCE or abs(previous.bbox[2] - current.bbox[2]) > X_TOLERANCE:
        return False
    heading = previous.rows[: previous.header_rows]
    if repeats_heading(current.rows[0], heading[-1]):
        return False  # the page prints its own heading; nothing to add
    return not _starts_new_table(previous, current.rows[0])


def link_continued_tables(pages: Sequence[Sequence[ParsedTable]]) -> int:
    """Give every continued table the heading of the table it continues. Returns how many were continued.

    `pages` holds each page's tables, top to bottom. Tables are changed in place (rows get the heading on top and
    `header_rows` is set), so a chain of three pages passes the heading on page by page."""
    linked = 0
    previous: ParsedTable | None = None
    for tables in pages:
        ordered = sorted(tables, key=lambda t: t.bbox[1] if t.bbox else 0.0)
        if ordered and previous is not None and is_continuation(previous, ordered[0]):
            heading = [list(r) for r in previous.rows[: previous.header_rows]]
            ordered[0].rows = heading + ordered[0].rows
            ordered[0].header_rows = len(heading)
            linked += 1
        previous = ordered[-1] if ordered else None
    return linked


def interleave(lines: Sequence[dict], tops: Sequence[float]) -> str:
    """The page's text lines with a table marker at the height of each table, so that a caption stays above its own table.

    `lines` are dicts with `text`, `top` and `x0` (pdfplumber's text lines outside the tables); `tops` is the top of each table,
    in the order the tables are stored. Without markers the chunker puts every table after all of the page's text, and on a page
    of "Table 10 caption, table, Table 11 caption, table" each caption ends up with the wrong table or none."""
    from rag.ingestion.formats import TABLE_MARKER

    pending = sorted(range(len(tops)), key=lambda i: tops[i])
    out: list[str] = []
    for line in sorted(lines, key=lambda ln: (ln["top"], ln.get("x0", 0.0))):
        while pending and tops[pending[0]] <= line["top"]:
            out.append(TABLE_MARKER.format(pending.pop(0)))
        out.append(line["text"])
    out.extend(TABLE_MARKER.format(i) for i in pending)
    return "\n".join(out)
