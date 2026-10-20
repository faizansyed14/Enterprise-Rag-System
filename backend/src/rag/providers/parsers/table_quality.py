"""Repair, or reject, tables that rule-based extraction has shredded.

Ruled-table extraction works for plain grids. Tables with merged cells, shaded bands or hairlines between text lines come out as
a wide grid in which most cells are empty: one real column is spread over several, and every wrapped line of a cell is a row of its
own. Labelling such rows with their column names gives text that is worse than the page text it replaced.

  tidy(rows)      drops columns that are empty throughout and joins neighbouring columns that are never both filled in the same
                  row (fragments of one column cut by a stray vertical rule). No text is lost.
  is_usable(rows) False when, even after that, most cells are empty or most rows hold a single cell: the page's own text
                  (reading order from pdfium) then serves better than the table.

Plain data in and out, so it is testable without a PDF.
"""

from __future__ import annotations

from collections.abc import Sequence

MIN_FILL = 0.34        # a usable table has at least this share of its cells filled ...
MAX_FRAGMENT_ROWS = 0.6  # ... and no more than this share of rows holding a single cell


def _filled(cell: str | None) -> bool:
    return bool((cell or "").strip())


def _width(rows: Sequence[Sequence[str]]) -> int:
    return max((len(r) for r in rows), default=0)


def _padded(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    width = _width(rows)
    return [list(r) + [""] * (width - len(r)) for r in rows]


def fill(rows: Sequence[Sequence[str]]) -> float:
    cells = sum(len(r) for r in rows)
    return sum(1 for r in rows for c in r if _filled(c)) / cells if cells else 0.0


def tidy(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    grid = _padded(rows)
    if not grid:
        return grid
    keep = [j for j in range(len(grid[0])) if any(_filled(r[j]) for r in grid)]
    grid = [[r[j] for j in keep] for r in grid]
    j = 0
    while grid and j < len(grid[0]) - 1:
        if all(not (_filled(r[j]) and _filled(r[j + 1])) for r in grid):
            for r in grid:
                r[j] = (r[j].strip() + " " + r[j + 1].strip()).strip()
                del r[j + 1]
            continue  # the joined column may join the next one as well
        j += 1
    return grid


def is_usable(rows: Sequence[Sequence[str]]) -> bool:
    if len(rows) < 2 or _width(rows) < 2:
        return False
    singles = sum(1 for r in rows if sum(1 for c in r if _filled(c)) <= 1) / len(rows)
    return fill(rows) >= MIN_FILL and singles <= MAX_FRAGMENT_ROWS
