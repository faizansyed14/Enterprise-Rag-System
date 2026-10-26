"""Structure-aware, token-sized chunking.

- Sized in the embedder's own tokens: ~380 text tokens plus a ~64-token header, ~15% overlap (CHUNK_* settings).
- Follows document structure: SECTION / PART / 1.01 / markdown headings start new chunks and form the section path
  ("SECTION 03 30 00 > PART 3 EXECUTION > 3.02 PLACING"), so every chunk knows where it sits.
- Never crosses a page, so every citation points at one exact page.
- Tables become labelled rows ("Item: B-12 | Description: Rebar 16mm | Qty: 420") grouped to fit, so each row group
  is self-describing and a row is never separated from its column names.
- Drawing sheets get a title-block chunk (number, title, scale) that exact questions hit directly.
- Every chunk carries a header -- Project | Doc number | Title | Section path -- in both the dense and the sparse
  text, so "the C40 note on A-101" matches even when the note itself never says A-101.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from rag.ingestion.formats import TABLE_MARKER, TABLE_MARKER_RE
from rag.metadata.identifiers import identifier_keys, label_keys
from rag.text.language import detect_language
from rag.text.normalize import clean_text
from rag.text.tokens import TokenCounter

CHUNK_NAMESPACE = uuid.UUID("6f1c2a7e-3b9d-4f59-9a51-2d6c0e7f4b13")

_BR = re.compile(r"(?i)<br\s*/?>")
_HTML_TABLE = re.compile(r"(?is)<table\b.*?</table>")
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_BOLD_WHOLE = re.compile(r"^(\*\*|__)(.+?)\1$")


def _pipe_cells(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") else body
    return [c.strip() for c in body.split("|")]


def _explode_breaks(cells: list[str]) -> list[list[str]]:
    """Cells like "TYPE 2<br/>AREA<br/>TOTAL" beside "SQ.M<br/>74.96<br/>89.55" are several rows written on one line."""
    parts = [_BR.split(c) for c in cells]
    sizes = {len(p) for p, c in zip(parts, cells, strict=True) if c}
    if len(sizes) == 1 and sizes != {1} and all(len(p) in (1, *sizes) for p in parts):
        n = sizes.pop()
        return [[p[i].strip() if len(p) == n else "" for p in parts] for i in range(n)]
    return [[_BR.sub(" ", c).strip() for c in cells]]


def _pipe_table(lines: list[str]) -> dict[str, Any] | None:
    rows: list[list[str]] = []
    for line in lines:
        cells = _pipe_cells(line)
        if any(cells) and all(_SEPARATOR_CELL.match(c) for c in cells if c):
            continue  # the |---|---| line under the header
        rows.extend(r for r in _explode_breaks(cells) if any(r))
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    return {"rows": rows, "header_rows": 1}


def _html_table(html: str) -> dict[str, Any] | None:
    from bs4 import BeautifulSoup

    rows: list[list[str]] = []
    header_rows = 0
    for tr in BeautifulSoup(html, "lxml").find_all("tr"):
        cells = tr.find_all(["td", "th"])
        row = [clean_text(c.get_text(" ")) for c in cells]
        if not any(row):
            continue
        if header_rows == len(rows) and all(c.name == "th" for c in cells):
            header_rows += 1
        rows.append(row)
    if len(rows) < 2 or max(len(r) for r in rows) < 2:
        return None
    return {"rows": rows, "header_rows": header_rows or 1}


def inline_tables(text: str, tables: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Parsers such as LlamaParse return Markdown pipe tables and HTML tables inside the page text. Turn them into
    table markers + structured tables, so rows are chunked with their column names instead of as loose text."""
    tables = list(tables)

    def marker(table: dict[str, Any] | None, original: str) -> str:
        if table is None:
            return original
        tables.append(table)
        return "\n" + TABLE_MARKER.format(len(tables) - 1) + "\n"

    text = _HTML_TABLE.sub(lambda m: marker(_html_table(m.group(0)), m.group(0)), text)
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if block:
            out.append(marker(_pipe_table(block), "\n".join(block)).strip("\n") if len(block) > 1 else block[0])
            block.clear()

    for line in text.split("\n"):
        if line.strip().startswith("|") and line.count("|") >= 2:
            block.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out), tables

_SECTION = re.compile(r"^(?:#\s+)?(SECTION\s+\d{2}\s?\d{2}\s?\d{2}(?:\.\d{2})?\b.*)$", re.I)
_PART = re.compile(r"^(?:##?\s+)?(PART\s+\d+\b.*)$", re.I)
_ARTICLE = re.compile(r"^(\d{1,2}\.\d{1,2}\s+[A-Z][^\n]{1,80})$")
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+[A-Z؀-ۿ][^\n]{1,70})$")
_MD = re.compile(r"^(#{1,6})\s+(.+)$")
# "Table 11: year-end statement", "Figure 3 - Layout": the caption of what follows. Only counted when a
# table comes right after it.
_CAPTION = re.compile(
    r"^(?:Table|Figure|Fig\.|Chart|Exhibit|Schedule|Appendix|Annex)\s*[A-Z]?\d+(?:\.\d+)*[A-Za-z]?\s*[:.\-–—]\s*\S", re.I
)
# a heading that carries a value (an address with @, a number of four or more digits) is also content
_HEADING_HOLDS_VALUES = re.compile(r"@|\d{4,}")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟;])\s+")


@dataclass
class PageForChunking:
    number: int
    text: str
    tables: list[dict[str, Any]] = field(default_factory=list)  # {"rows": [[...]], "header_rows": 1}
    label: str | None = None
    is_drawing: bool = False
    title_block: dict[str, str] = field(default_factory=dict)
    language: str | None = None


@dataclass
class HeaderFields:
    project: str = ""
    doc_number: str = ""
    title: str = ""
    doc_number_key: str | None = None


@dataclass
class Chunk:
    chunk_no: int
    page_start: int
    page_end: int
    section_path: str
    kind: str
    header: str
    text: str
    token_count: int
    language: str
    identifiers: list[str]
    id: uuid.UUID | None = None
    content_hash: str = ""

    @property
    def dense_text(self) -> str:
        return f"{self.header}\n{self.text}" if self.header else self.text


def chunk_id(version_id: uuid.UUID | str, chunk_no: int) -> uuid.UUID:
    """Deterministic: re-chunking the same version yields the same ids, so re-runs overwrite instead of duplicating."""
    return uuid.uuid5(CHUNK_NAMESPACE, f"{version_id}:{chunk_no}")


@dataclass
class _Block:
    page: int
    kind: str  # text | table | title_block
    text: str
    path: str
    tokens: int


@dataclass
class _Draft:
    """A chunk before its header and id are made, so small neighbours can still be merged."""

    page_start: int
    page_end: int
    kind: str
    path: str
    text: str
    tokens: int


class Chunker:
    def __init__(
        self,
        counter: TokenCounter,
        *,
        chunk_tokens: int = 380,
        header_tokens: int = 64,
        overlap: float = 0.15,
        min_tokens: int = 48,
    ) -> None:
        self.counter = counter
        self.chunk_tokens = chunk_tokens
        self.header_tokens = header_tokens
        self.overlap = overlap
        self.min_tokens = min_tokens

    # ---- header -------------------------------------------------------------------------------------------------------
    def header(self, fields: HeaderFields, section_path: str) -> str:
        parts = [p for p in (fields.project, fields.doc_number, fields.title, section_path) if p]
        text = " | ".join(parts)
        if self.counter.count(text) <= self.header_tokens:
            return text
        # Trim the title, then the section path, until it fits.
        title = fields.title
        path = section_path
        while (
            self.counter.count(" | ".join(p for p in (fields.project, fields.doc_number, title, path) if p)) > self.header_tokens
        ):
            if len(title) > 40:
                title = title[: int(len(title) * 0.8)].rstrip() + "…"
            elif len(path) > 30:
                path = "…" + path[-int(len(path) * 0.8) :]
            else:
                break
        return " | ".join(p for p in (fields.project, fields.doc_number, title, path) if p)

    # ---- blocks ---------------------------------------------------------------------------------------------------------
    @staticmethod
    def _is_structured(pages: list[PageForChunking]) -> bool:
        """The parser already marked the document's headings (Markdown '#'): then only those define sections, and the
        guesses made for plain text (numbered lines, short capitalised lines) are not used."""
        marked = 0
        for page in pages:
            marked += sum(1 for line in page.text.split("\n") if _MD.match(line.strip()))
            if marked >= 2:
                return True
        return False

    def _blocks(self, pages: list[PageForChunking]) -> list[_Block]:
        structured = self._is_structured(pages)
        blocks: list[_Block] = []
        path: list[tuple[float, str, int]] = []  # (effective level, heading, level as written)
        # Headings that follow each other with no text between ("## TOWER 3" then "## LEVEL 01") are one label, not
        # siblings: the second nests under the first, and later headings of that level on the page keep the first.
        run = {"open": False, "keep": 0, "level": -1}

        def current_path() -> str:
            return " > ".join(h for _, h, _ in path)

        def push_heading(level: int, heading: str) -> None:
            text = heading.strip()[:90]
            if run["open"] and path and level <= path[-1][2]:  # not deeper than the heading above it
                run["keep"], run["level"] = len(path), level
                path.append((path[-1][0] + 0.1, text, level))
            elif run["keep"] and level == run["level"] and path[0][2] < level:
                del path[run["keep"] :]
                path.append((path[-1][0] + 0.1, text, level))
            else:
                run["keep"] = 0
                while path and path[-1][0] >= level:
                    path.pop()
                path.append((float(level), text, level))
            run["open"] = True

        for page in pages:
            run.update(open=False, keep=0, level=-1)
            page_text, page_tables = inline_tables(page.text, page.tables)
            if page.is_drawing and page.title_block:
                tb = page.title_block
                parts = [f"{k.replace('_', ' ').title()}: {v}" for k, v in tb.items() if v]
                text = "Drawing sheet title block - " + "; ".join(parts)
                blocks.append(_Block(page.number, "title_block", text, page.label or "Title block", self.counter.count(text)))
            used_tables: set[int] = set()
            paragraph: list[str] = []

            def flush(page_no: int = page.number, lines: list[str] = paragraph) -> None:
                if lines:
                    text = " ".join(lines).strip()
                    if text:
                        blocks.append(_Block(page_no, "text", text, current_path(), self.counter.count(text)))
                    lines.clear()

            page_lines = page_text.split("\n")
            for position, raw in enumerate(page_lines):
                line = raw.strip()
                if not line:
                    if not page.is_drawing:
                        flush()
                    continue
                if page.is_drawing and not TABLE_MARKER_RE.fullmatch(line):
                    # The words of a drawing are labels scattered over the sheet ("BEDROOM", "4000 x 3600"), not prose
                    # with headings: keep them together as one text run instead of one chunk per label.
                    paragraph.append(line)
                    continue
                marker = TABLE_MARKER_RE.fullmatch(line)
                if marker:
                    flush()
                    run["open"] = False
                    index = int(marker.group(1))
                    if index < len(page_tables):
                        used_tables.add(index)
                        blocks.extend(self._table_blocks(page.number, page_tables[index], current_path()))
                    continue
                if line.startswith("Sheet: ") and len(line) <= 120 and not page.is_drawing:
                    flush()  # a spreadsheet's sheet name titles every passage below it, not only the first one
                    push_heading(2, line)
                    continue
                if len(line) <= 160 and _CAPTION.match(line) and self._table_follows(page_lines, position):
                    flush()  # the caption is the title of the table under it, in any file format
                    push_heading(4, line)
                    continue
                level, heading = self._heading(line, structured=structured)
                if level:
                    flush()
                    push_heading(level, heading)
                    if len(heading) > 90 or _HEADING_HOLDS_VALUES.search(heading):
                        # forms print a bold label and its value on one line ("Licence No.: 123456789012345 info@x.ae"); the
                        # title keeps 90 characters, so such a line is content too and must stay in the text
                        paragraph.append(heading)
                    continue
                if line.startswith(("- ", "• ")) or re.match(r"^[A-Z]\.\s|^\d+\.\s|^[a-z]\)\s", line):
                    flush()  # list items and lettered paragraphs start their own block
                run["open"] = False
                paragraph.append(line)
            flush()
            for index, table in enumerate(page_tables):
                if index not in used_tables:
                    blocks.extend(self._table_blocks(page.number, table, current_path()))
        return blocks

    @staticmethod
    def _table_follows(lines: list[str], position: int) -> bool:
        """Is the next non-empty line a table marker?"""
        for later in lines[position + 1 : position + 3]:
            if later.strip():
                return bool(TABLE_MARKER_RE.fullmatch(later.strip()))
        return False

    @staticmethod
    def _heading(line: str, *, structured: bool = False) -> tuple[int, str]:
        if len(line) > 120 or line.startswith(("|", "<")):
            return 0, ""  # a long line, or a table row / tag the all-caps rule below would mistake for a heading
        if m := _MD.match(line):
            bold = _BOLD_WHOLE.match(m.group(2).strip())
            return len(m.group(1)), bold.group(2) if bold else m.group(2)
        if m := _SECTION.match(line):
            return 1, m.group(1)
        if m := _PART.match(line):
            return 2, m.group(1)
        if structured:
            return 0, ""
        if m := _ARTICLE.match(line):
            return 3, m.group(1)
        if _NUMBERED.match(line) and not line.endswith((".", ",", ";", ":")) and len(line.split()) <= 10:
            return 3, line
        letters = [c for c in line if c.isalpha()]
        if (
            len(line.split()) <= 8
            and len(letters) >= 4
            and line.isupper()
            and not line.endswith((".", ","))
            and not re.search(r"\d{3,}", line)
        ):
            return 4, line
        return 0, ""

    def _table_blocks(self, page: int, table: dict[str, Any], path: str) -> list[_Block]:
        rows: list[list[str]] = table.get("rows") or []
        if not rows:
            return []
        header_rows = max(0, min(int(table.get("header_rows", 1) or 0), len(rows) - 1))
        headers = [" ".join(col).strip() for col in zip(*rows[:header_rows], strict=False)] if header_rows else []
        body = rows[header_rows:] if header_rows else rows
        lines: list[str] = []
        for row in body:
            cells = []
            for i, value in enumerate(row):
                value = (value or "").strip()  # noqa: PLW2901
                if not value:
                    continue
                name = headers[i] if i < len(headers) and headers[i] else ""
                cells.append(f"{name}: {value}" if name else value)
            if cells:
                lines.append(" | ".join(cells))
        blocks: list[_Block] = []
        group: list[str] = []
        size = 0
        limit = self.chunk_tokens
        for line in lines:
            tokens = self.counter.count(line)
            if group and size + tokens > limit:
                text = "\n".join(group)
                blocks.append(_Block(page, "table", text, path, size))
                group, size = [], 0
            group.append(line)
            size += tokens
        if group:
            blocks.append(_Block(page, "table", "\n".join(group), path, size))
        return blocks

    # ---- packing --------------------------------------------------------------------------------------------------------
    def _split_long(self, block: _Block) -> list[_Block]:
        if block.tokens <= self.chunk_tokens:
            return [block]
        pieces: list[_Block] = []
        sentences = _SENTENCE_SPLIT.split(block.text)
        current: list[str] = []
        size = 0
        for sentence in sentences:
            tokens = self.counter.count(sentence)
            if tokens > self.chunk_tokens:
                words = sentence.split()
                step = max(1, int(len(words) * self.chunk_tokens / max(tokens, 1)))
                for i in range(0, len(words), step):
                    piece = " ".join(words[i : i + step])
                    pieces.append(_Block(block.page, block.kind, piece, block.path, self.counter.count(piece)))
                continue
            if current and size + tokens > self.chunk_tokens:
                text = " ".join(current)
                pieces.append(_Block(block.page, block.kind, text, block.path, size))
                current, size = [], 0
            current.append(sentence)
            size += tokens
        if current:
            pieces.append(_Block(block.page, block.kind, " ".join(current), block.path, size))
        return pieces

    def _overlap_tail(self, text: str) -> str:
        budget = int(self.chunk_tokens * self.overlap)
        if budget <= 0:
            return ""
        sentences = _SENTENCE_SPLIT.split(text)
        tail: list[str] = []
        size = 0
        for sentence in reversed(sentences):
            tokens = self.counter.count(sentence)
            if size + tokens > budget:
                break
            tail.insert(0, sentence)
            size += tokens
        return " ".join(tail)

    def chunk(
        self, pages: list[PageForChunking], fields: HeaderFields, *, version_id: uuid.UUID | str | None = None
    ) -> list[Chunk]:
        blocks = [piece for block in self._blocks(pages) for piece in self._split_long(block)]
        drafts: list[_Draft] = []
        current: list[_Block] = []
        size = 0

        def emit() -> None:
            nonlocal current, size
            if not current:
                return
            text = "\n".join(b.text for b in current).strip()
            if text:
                kinds = {b.kind for b in current}
                kind = kinds.pop() if len(kinds) == 1 else ("table" if "table" in kinds else "text")
                drafts.append(
                    _Draft(
                        current[0].page,
                        current[-1].page,
                        kind,
                        _common_path([b.path for b in current]),
                        text,
                        self.counter.count(text),
                    )
                )
            current, size = [], 0

        caption_tokens = 40
        for block in blocks:
            hard = bool(current) and (
                block.page != current[-1].page
                or _major_change(block.path, current[-1].path)
                or block.kind == "title_block"
                or current[-1].kind == "title_block"
            )
            if current and not hard:
                last = current[-1]
                caption = block.kind == "table" and last.kind == "text" and size <= caption_tokens
                if (block.kind != last.kind and not caption) or (
                    block.path != last.path and (size >= self.chunk_tokens * 0.3 or block.kind == "table")
                ):
                    hard = True
            if current and (hard or size + block.tokens > self.chunk_tokens):
                carry = "" if hard or block.kind != "text" or current[-1].kind != "text" else self._overlap_tail(current[-1].text)
                emit()
                if carry and carry != block.text:
                    current.append(_Block(block.page, "text", carry, block.path, self.counter.count(carry)))
                    size = current[0].tokens
            current.append(block)
            size += block.tokens
        emit()

        chunks: list[Chunk] = []
        for draft in self._merge_small(drafts):
            header = self.header(fields, draft.path)
            ids = identifier_keys(header + "\n" + draft.text, limit=30)
            ids += [k for k in label_keys(draft.path) if k not in ids]  # "Table 11" in this chunk's caption
            if fields.doc_number_key and fields.doc_number_key not in ids:
                ids.insert(0, fields.doc_number_key)
            chunks.append(
                Chunk(
                    chunk_no=len(chunks),
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                    section_path=draft.path,
                    kind=draft.kind,
                    header=header,
                    text=draft.text,
                    token_count=draft.tokens,
                    language=detect_language(draft.text),
                    identifiers=ids,
                )
            )
        for chunk in chunks:
            if version_id is not None:
                chunk.id = chunk_id(version_id, chunk.chunk_no)
            chunk.content_hash = hashlib.sha256(chunk.dense_text.encode()).hexdigest()
        return chunks

    def _merge_small(self, drafts: list[_Draft]) -> list[_Draft]:
        """A chunk of a few words (a one-line section, a caption, the tail of a page) says too little to be found and costs
        as much to store and search as a full one. Fold it into its neighbour on the same page when they fit together and
        belong to the same part of the document; the merged chunk keeps the shared part of their section paths."""
        merged: list[_Draft] = []
        for draft in drafts:
            prev = merged[-1] if merged else None
            if prev and self._can_merge(prev, draft):
                prev.text = f"{prev.text}\n{draft.text}"
                prev.tokens += draft.tokens
                prev.page_end = draft.page_end
                prev.path = _common_path([prev.path, draft.path])
            else:
                merged.append(draft)
        return merged

    def _can_merge(self, a: _Draft, b: _Draft) -> bool:
        if a.kind != "text" or b.kind != "text" or a.page_end != b.page_start:
            return False
        if min(a.tokens, b.tokens) >= self.min_tokens or a.tokens + b.tokens > self.chunk_tokens:
            return False
        related = not _major_change(a.path, b.path) or a.path.startswith(b.path) or b.path.startswith(a.path)
        return related or min(a.tokens, b.tokens) < self.min_tokens // 2


def _parts(path: str) -> list[str]:
    return path.split(" > ") if path else []


def _major_change(a: str, b: str) -> bool:
    """SECTION or PART (the first two levels) differ."""
    return _parts(a)[:2] != _parts(b)[:2]


def _common_path(paths: list[str]) -> str:
    split = [_parts(p) for p in paths]
    common: list[str] = []
    for level in zip(*split, strict=False):
        if all(x == level[0] for x in level):
            common.append(level[0])
        else:
            break
    return " > ".join(common)
