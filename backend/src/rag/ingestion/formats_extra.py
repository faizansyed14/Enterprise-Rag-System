"""More native readers, ported from the earlier code base: RTF, OpenDocument text and Primavera P6 schedules (.xer).

None needs LibreOffice. Archive members are read with a size cap and XML that declares entities or a DOCTYPE is refused,
so a hostile file cannot expand into memory.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from rag.errors import PermanentError
from rag.ingestion.formats import (
    PSEUDO_PAGE_CHARS,
    TABLE_MARKER,
    _pseudo_pages,
    _table_text,
    decode_text,
)
from rag.plugins.capabilities import ParsedDocument, ParsedPage, ParsedTable
from rag.text.language import detect_language
from rag.text.normalize import clean_text

MAX_MEMBER_BYTES = 64 * 1024 * 1024

# ---- RTF ---------------------------------------------------------------------------------------------------------------

_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "footnote", "themedata",
             "colorschememapping", "latentstyles", "datastore", "listtable", "listoverridetable", "rsidtbl", "generator",
             "xmlnstbl", "fldinst", "bkmkstart", "bkmkend", "object", "shpinst", "nonshppict"}
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)", re.S)


def rtf_to_text(data: str) -> str:
    out: list[str] = []
    stack: list[bool] = []  # True: the group is a destination whose text is not content
    skip = False
    unicode_skip = 0
    for m in _RTF_TOKEN.finditer(data):
        word, num, hexcode, symbol, brace, literal = m.groups()
        if brace == "{":
            stack.append(skip)
        elif brace == "}":
            skip = stack.pop() if stack else False
        elif word is not None:
            if word in _RTF_SKIP:
                skip = True
            elif skip:
                continue
            elif word in {"par", "sect", "page"}:
                out.append("\n\n")
            elif word == "line":
                out.append("\n")
            elif word == "tab":
                out.append("\t")
            elif word == "u" and num is not None:
                code = int(num)
                out.append(chr(code + 65536 if code < 0 else code))
                unicode_skip = 1
            elif word == "cell":
                out.append(" | ")
            elif word == "row":
                out.append("\n")
        elif hexcode is not None:
            if not skip:
                out.append(bytes([int(hexcode, 16)]).decode("cp1252", errors="replace"))
        elif symbol is not None:
            if symbol == "*":
                skip = True  # an optional destination: ignore the whole group
            elif not skip and symbol in "\\{}":
                out.append(symbol)
            elif not skip and symbol == "~":
                out.append(" ")
        elif literal is not None and not skip:
            piece = literal.replace("\r", "").replace("\n", "")
            if unicode_skip:
                piece, unicode_skip = piece[unicode_skip:], 0
            out.append(piece)
    return "".join(out)


def read_rtf(data: bytes) -> ParsedDocument:
    if not data.lstrip().startswith(b"{\\rtf"):
        raise PermanentError("not an RTF file")
    text = clean_text(rtf_to_text(data[:MAX_MEMBER_BYTES].decode("latin-1")))
    return ParsedDocument(pages=_pseudo_pages(text), format="rtf")


# ---- OpenDocument text -------------------------------------------------------------------------------------------------

_T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TB = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_O = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"


def _odf_text(node: Any) -> str:
    parts: list[str] = []

    def walk(n: Any) -> None:
        if n.text:
            parts.append(n.text)
        for child in n:
            if child.tag == f"{_T}s":
                parts.append(" " * int(child.get(f"{_T}c", "1")))
            elif child.tag == f"{_T}tab":
                parts.append("\t")
            elif child.tag == f"{_T}line-break":
                parts.append("\n")
            else:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return "".join(parts).strip()


def _safe_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise PermanentError(f"archive member too large: {name}")
    raw = zf.read(info)
    head = raw[:4096].lower()
    if name.endswith(".xml") and (b"<!entity" in head or b"<!doctype" in head):
        raise PermanentError("XML with a DOCTYPE or entity declaration refused")
    return raw


def read_odt(data: bytes) -> ParsedDocument:
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            raw = _safe_member(zf, "content.xml")
            meta = _safe_member(zf, "meta.xml") if "meta.xml" in zf.namelist() else b""
    except (zipfile.BadZipFile, KeyError) as exc:
        raise PermanentError(f"not a readable OpenDocument file: {type(exc).__name__}") from exc
    root = ET.fromstring(raw)  # noqa: S314 - checked above
    body = root.find(f"{_O}body/{_O}text")
    lines: list[str] = []
    tables: list[ParsedTable] = []
    for node in body if body is not None else []:
        if node.tag == f"{_T}h":
            level = int(node.get(f"{_T}outline-level", "1") or 1)
            if text := _odf_text(node):
                lines.append("#" * min(level, 6) + " " + text)
        elif node.tag == f"{_T}p":
            if text := _odf_text(node):
                lines.append(text)
        elif node.tag == f"{_T}list":
            lines.extend("- " + t for t in (_odf_text(p) for p in node.iter(f"{_T}p")) if t)
        elif node.tag == f"{_TB}table":
            rows = [[_odf_text(c) for c in tr.findall(f"{_TB}table-cell")] for tr in node.iter(f"{_TB}table-row")]
            rows = [r for r in rows if any(r)]
            if rows:
                tables.append(ParsedTable(rows=rows))
                lines.append(TABLE_MARKER.format(len(tables) - 1))
    title = None
    if meta and (m := re.search(rb"<dc:title>(.*?)</dc:title>", meta, re.S)):
        title = m.group(1).decode("utf-8", "replace").strip() or None
    text = clean_text("\n".join(lines))
    if len(text) > PSEUDO_PAGE_CHARS * 1.5 and not tables:
        pages = _pseudo_pages(text)
    else:
        pages = [ParsedPage(number=1, text=text, method="native", tables=tables, language=detect_language(text))]
    return ParsedDocument(pages=pages, format="odt", title=title, properties={"title": title} if title else {})


# ---- Primavera P6 schedules ----------------------------------------------------------------------------------------------

XER_KEEP = {  # the columns of each table that say something to a person; the rest are internal keys and flags
    "TASK": ["task_code", "task_name", "status_code", "phys_complete_pct", "target_start_date", "target_end_date",
             "act_start_date", "act_end_date", "total_float_hr_cnt", "remain_drtn_hr_cnt", "task_type"],
    "PROJWBS": ["wbs_short_name", "wbs_name"],
    "RSRC": ["rsrc_short_name", "rsrc_name", "rsrc_type"],
    "CALENDAR": ["clndr_name", "clndr_type"],
    "PROJECT": ["proj_short_name", "plan_start_date", "scd_end_date"],
}
XER_ROWS_PER_PAGE = 120


def _xer_tables(text: str) -> dict[str, tuple[list[str], list[list[str]]]]:
    tables: dict[str, tuple[list[str], list[list[str]]]] = {}
    name = ""
    fields: list[str] = []
    rows: list[list[str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        tag = parts[0]
        if tag == "%T":
            if name and rows:
                tables[name] = (fields, rows)
            name, fields, rows = (parts[1] if len(parts) > 1 else "TABLE"), [], []
        elif tag == "%F":
            fields = parts[1:]
        elif tag == "%R":
            rows.append((parts[1:] + [""] * len(fields))[: len(fields)])
    if name and rows:
        tables[name] = (fields, rows)
    return tables


def read_xer(data: bytes) -> ParsedDocument:
    """A P6 export is a set of tab-separated tables (%T name, %F fields, %R rows). Each table becomes pages of rows with
    their column names; links between activities are written with the activity codes, not the internal ids."""
    tables = _xer_tables(decode_text(data))
    if not tables:
        raise PermanentError("no tables found in the XER file")
    code_by_id: dict[str, str] = {}
    if "TASK" in tables:
        f, r = tables["TASK"]
        if "task_id" in f and "task_code" in f:
            i_id, i_code = f.index("task_id"), f.index("task_code")
            code_by_id = {row[i_id]: row[i_code] for row in r}
    pages: list[ParsedPage] = []
    for table, (f, r) in tables.items():
        if table == "TASKPRED" and code_by_id and {"task_id", "pred_task_id"} <= set(f):
            i_s, i_p = f.index("task_id"), f.index("pred_task_id")
            i_t = f.index("pred_type") if "pred_type" in f else None
            i_l = f.index("lag_hr_cnt") if "lag_hr_cnt" in f else None
            header = ["predecessor", "successor", "relationship", "lag_hours"]
            body = [[code_by_id.get(row[i_p], row[i_p]), code_by_id.get(row[i_s], row[i_s]),
                     row[i_t] if i_t is not None else "", row[i_l] if i_l is not None else ""] for row in r]
        elif table in XER_KEEP:
            keep = [c for c in XER_KEEP[table] if c in f]
            idx = [f.index(c) for c in keep if any(row[f.index(c)] for row in r)]
            header = [f[i] for i in idx]
            body = [[row[i] for i in idx] for row in r]
        else:
            continue  # internal tables (settings, user-defined values, rate tables ...) add noise, not answers
        for start in range(0, len(body), XER_ROWS_PER_PAGE):
            part = body[start: start + XER_ROWS_PER_PAGE]
            label = f"XER {table} rows {start + 1}-{start + len(part)}"
            table_obj = ParsedTable(rows=[header, *part], header_rows=1)
            pages.append(ParsedPage(number=len(pages) + 1, text=f"{label}\n" + TABLE_MARKER.format(0), method="native",
                                    tables=[table_obj], label=label,
                                    language=detect_language(_table_text([table_obj]))))
    if not pages:
        raise PermanentError("the XER file has no activity, WBS, resource or relationship tables")
    return ParsedDocument(pages=pages, format="xer")
