"""Run the layout engine on a PDF and return its section outline in a small, picklable form."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any


@dataclass
class OutlineEntry:
    level: int  # 1 = top level
    title: str
    start: int  # 1-based first page
    end: int


@dataclass
class LayoutOutline:
    title: str | None = None
    entries: list[OutlineEntry] = field(default_factory=list)
    source: str = "none"  # detected | bookmarks | hybrid | none


_SPACES = re.compile(r"\s+")


def _flatten(nodes: list[dict[str, Any]], level: int, out: list[OutlineEntry], max_level: int) -> None:
    for node in nodes:
        title = _SPACES.sub(" ", str(node.get("title") or "")).strip()
        start = int(node.get("start_index") or 0)
        end = int(node.get("end_index") or start)
        if title and start > 0 and level <= max_level:
            out.append(OutlineEntry(level, title, start, max(start, end)))
        _flatten(node.get("nodes") or [], level + 1, out, max_level)


def analyse(data: bytes, *, max_level: int = 4) -> LayoutOutline:
    """Outline and title of a PDF. Raises whatever the engine raises; callers treat any failure as 'no outline'."""
    from rag.providers.parsers.layout.main import extract_toc

    result = extract_toc(BytesIO(data), workers=1, use_embedded_toc=True)
    entries: list[OutlineEntry] = []
    _flatten(result.get("structure") or [], 1, entries, max_level)
    title = _SPACES.sub(" ", str(result.get("doc_title") or "")).strip() or None
    return LayoutOutline(title=title, entries=_dedupe(entries), source=str(result.get("toc_source") or "detected"))


def _dedupe(entries: list[OutlineEntry]) -> list[OutlineEntry]:
    """Bookmarks merged with detected sections can list the same heading twice (once per source, at different depths):
    keep one entry per (title, start page), at the shallowest level, in document order."""
    best: dict[tuple[str, int], OutlineEntry] = {}
    for entry in entries:
        key = (re.sub(r"[^\w]+", "", entry.title.lower()), entry.start)
        if key not in best or entry.level < best[key].level:
            best[key] = entry
    kept = {id(e) for e in best.values()}
    return [e for e in entries if id(e) in kept]
