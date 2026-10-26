"""Format detection and the native readers for everything that is not a PDF.

Office files are read directly (python-docx, openpyxl, python-pptx) -- never converted to PDF first -- so Arabic text,
table structure and headings survive exactly. E-mail attachments and zip members become child items of their own.
Legacy binary Office formats (.doc/.xls/.ppt/.rtf/.od*) are converted with LibreOffice when it is installed.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import filetype

from rag.errors import PermanentError
from rag.plugins.capabilities import OCRResult, ParsedDocument, ParsedPage, ParsedTable, TextLine
from rag.text.language import detect_language
from rag.text.normalize import clean_text

log = logging.getLogger(__name__)

PSEUDO_PAGE_CHARS = 4000
MAX_SHEET_ROWS = 1_000_000
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_TOTAL_BYTES = 4 * 1024**3
MAX_ZIP_RATIO = 200
TABLE_MARKER = "[[TABLE:{}]]"
TABLE_MARKER_RE = re.compile(r"\[\[TABLE:(\d+)\]\]")

# Text and source code, read as text. (No env files: they hold secrets and are not documents.)
TEXT_EXTENSIONS = {"txt", "md", "markdown", "rst", "adoc", "csv", "tsv", "json", "xml", "log", "ini", "cfg", "conf",
                   "toml", "properties", "yaml", "yml", "sql", "sh", "bash", "bat", "cmd", "ps1", "py", "js", "jsx",
                   "mjs", "ts", "tsx", "java", "kt", "scala", "c", "h", "cpp", "cc", "hpp", "cs", "go", "rs", "rb",
                   "php", "swift", "lua", "pl", "r", "css", "scss", "less", "vue", "svelte", "gradle", "tex"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif"}
LEGACY_OFFICE = {"doc": "docx", "xls": "xlsx", "ppt": "pptx", "ods": "xlsx", "odp": "pptx"}  # need LibreOffice
NATIVE_EXTRA = {"rtf", "odt", "xer"}  # read natively (rag.ingestion.formats_extra)
CAD_EXTENSIONS = {"dwg", "dxf"}  # AutoCAD drawings (rag.ingestion.cad)
SUPPORTED = {"pdf", "docx", "xlsx", "xlsm", "pptx", "eml", "msg", "html", "htm", "zip", *TEXT_EXTENSIONS,
             *IMAGE_EXTENSIONS, *LEGACY_OFFICE, *NATIVE_EXTRA, *CAD_EXTENSIONS}


def extension(name: str) -> str:
    suffix = PurePosixPath(name.lower()).suffix
    return suffix[1:] if suffix else ""


_SKIP_REASONS = {
    "dwg": "AutoCAD drawings (.dwg) are not processed",
    "dxf": "AutoCAD drawings (.dxf) are not processed",
    "jpg": "JPG/JPEG images are not processed yet",
    "jpeg": "JPG/JPEG images are not processed yet",
    "png": "PNG images are not processed yet",
}


def skip_reason(name: str, skipped: frozenset[str]) -> str | None:
    """Why a file of a type listed in SKIP_EXTENSIONS is left out, or None when it is processed."""
    ext = extension(name)
    if ext and ext in skipped:
        return _SKIP_REASONS.get(ext, f".{ext} files are not processed (SKIP_EXTENSIONS)")
    return None


def detect_format(data: bytes, name: str) -> str:
    ext = extension(name)
    head = data[:8]
    if head.startswith(b"%PDF") or (b"%PDF-" in data[:1024]):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist()[:2000])
        except zipfile.BadZipFile as exc:
            raise PermanentError("corrupt zip container") from exc
        if "[Content_Types].xml" in names:
            if any(n.startswith("word/") for n in names):
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("ppt/") for n in names):
                return "pptx"
        if "mimetype" in names and (ext in LEGACY_OFFICE or ext == "odt"):
            return ext  # OpenDocument
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE compound file: legacy Office or Outlook .msg
        return ext if ext in {"doc", "xls", "ppt", "msg"} else "doc"
    from rag.ingestion.cad import is_dwg, is_dxf

    if is_dwg(head):
        return "dwg"
    if ext == "dxf" or is_dxf(data[:64]):  # before the text check: a DXF file is text
        return "dxf"
    kind = filetype.guess(data[:8192])
    if kind is not None and kind.extension in IMAGE_EXTENSIONS | {"jpg"}:
        return "image"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in {"html", "htm"} or data[:512].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if ext == "eml" or (ext not in TEXT_EXTENSIONS and re.match(rb"(?i)^(received|from|return-path|message-id|"
                                                                 rb"mime-version|date|subject):", data[:200])):
        return "email"
    if ext == "xer" or data.startswith(b"ERMHDR"):
        return "xer"
    if ext == "rtf" or data.startswith(b"{\\rtf"):
        return "rtf"
    if ext in TEXT_EXTENSIONS or _looks_like_text(data):
        return "text"
    raise PermanentError(f"unsupported file type: {ext or 'unknown'}")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if not sample:
        return True
    if b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    try:
        decode_text(sample)
    except UnicodeDecodeError:
        return False
    return True


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp1256")  # Arabic Windows code page
        if re.search(r"[؀-ۿ]", text):
            return text
    except UnicodeDecodeError:
        pass
    return data.decode("latin-1")


def _pseudo_pages(text: str, label: str = "Section") -> list[ParsedPage]:
    """Split long text without fixed pages into ~4,000-character parts at paragraph boundaries."""
    text = clean_text(text)
    if not text:
        return [ParsedPage(number=1, text="", method="native", label=f"{label} 1")]
    pages: list[ParsedPage] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > PSEUDO_PAGE_CHARS and current:
            pages.append(ParsedPage(number=len(pages) + 1, text="\n\n".join(current), method="native"))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        pages.append(ParsedPage(number=len(pages) + 1, text="\n\n".join(current), method="native"))
    for page in pages:
        page.label = f"{label} {page.number}"
        page.language = detect_language(page.text)
    return pages


# ---- DOCX ------------------------------------------------------------------------------------------------------------


def read_docx(data: bytes) -> ParsedDocument:
    import docx
    from docx.oxml.ns import qn

    document = docx.Document(io.BytesIO(data))
    props = document.core_properties
    properties = {k: str(v) for k, v in {"title": props.title, "subject": props.subject, "author": props.author,
                                         "created": props.created, "modified": props.modified}.items() if v}
    header_lines: list[str] = []
    for section in document.sections[:1]:
        for part in (section.header, section.footer):
            header_lines += [p.text for p in part.paragraphs if p.text.strip()]
            for table in part.tables:
                header_lines += [" | ".join(c.text.strip() for c in row.cells if c.text.strip()) for row in table.rows]
    if header_lines:
        properties["header"] = "\n".join(header_lines)[:2000]

    pages: list[dict[str, Any]] = [{"lines": [], "tables": []}]
    has_breaks = False

    def new_page() -> None:
        nonlocal has_breaks
        has_breaks = True
        if pages[-1]["lines"] or pages[-1]["tables"]:
            pages.append({"lines": [], "tables": []})

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            breaks = child.findall(".//" + qn("w:br"))
            if any(b.get(qn("w:type")) == "page" for b in breaks) or child.findall(".//" + qn("w:lastRenderedPageBreak")):
                new_page()
            paragraph = docx.text.paragraph.Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            level = re.match(r"(?i)heading\s*(\d)", style)
            if level:
                text = "#" * int(level.group(1)) + " " + text
            elif style.lower() == "title":
                text = "# " + text
            elif style.lower().startswith("list"):
                text = "- " + text
            pages[-1]["lines"].append(text)
        elif child.tag == qn("w:tbl"):
            table = docx.table.Table(child, document)
            rows: list[list[str]] = []
            for row in table.rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = clean_text(cell.text)
                    if not cells or value != cells[-1] or not value:  # merged cells repeat their text
                        cells.append(value)
                if any(cells):
                    rows.append(cells)
            if rows:
                pages[-1]["tables"].append(ParsedTable(rows=rows))
                pages[-1]["lines"].append(TABLE_MARKER.format(len(pages[-1]["tables"]) - 1))

    parsed: list[ParsedPage] = []
    if not has_breaks and sum(len("\n".join(p["lines"])) for p in pages) > PSEUDO_PAGE_CHARS * 1.5:
        # No page information in the file: split into sections so citations point somewhere useful.
        merged_lines = [line for p in pages for line in p["lines"]]
        tables = [t for p in pages for t in p["tables"]]
        pages = _split_lines(merged_lines, tables)
    for i, page in enumerate(pages, start=1):
        text = clean_text("\n".join(page["lines"]))
        if not text and not page["tables"]:
            continue
        parsed.append(ParsedPage(number=len(parsed) + 1, text=text, method="native", tables=page["tables"],
                                 label=None if has_breaks else f"Section {i}",
                                 language=detect_language(text + _table_text(page["tables"]))))
    if not parsed:
        parsed = [ParsedPage(number=1, text="", method="native")]
    return ParsedDocument(pages=parsed, format="docx", title=properties.get("title"), properties=properties)


def _split_lines(lines: list[str], tables: list[ParsedTable]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = [{"lines": [], "tables": []}]
    size = 0
    for line in lines:
        is_heading = line.startswith("#")
        if size > PSEUDO_PAGE_CHARS and (is_heading or size > PSEUDO_PAGE_CHARS * 1.5):
            pages.append({"lines": [], "tables": []})
            size = 0
        m = TABLE_MARKER_RE.fullmatch(line)
        if m:
            pages[-1]["tables"].append(tables[int(m.group(1))])
            line = TABLE_MARKER.format(len(pages[-1]["tables"]) - 1)  # noqa: PLW2901 - renumber per page
        pages[-1]["lines"].append(line)
        size += len(line) + 1
    return pages


def _table_text(tables: list[ParsedTable]) -> str:
    return " " + " ".join(c for t in tables for r in t.rows[:20] for c in r)


# ---- XLSX ------------------------------------------------------------------------------------------------------------


ROWS_PER_PAGE = 2_000  # a sheet is cut into pages of this many rows: each cites its own row range and repeats the header
HEADER_SCAN = 15       # the header row is looked for among the first rows of a sheet (titles and notes often come first)


# a cell that is plainly a value (an id of six or more digits, a date) is not a column name: a row of "label | value | label |
# value" pairs (the top of a tax form) is a form, not a header
_VALUE_CELL = re.compile(r"\d{6,}|\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}")


def _has_value_cell(cells: list[str]) -> bool:
    return any(_VALUE_CELL.search(c) for c in cells if c.strip())


def _looks_like_header(cells: list[str], width: int) -> bool:
    """A header row is mostly filled and mostly labels: a title line has one cell, a data row has numbers."""
    filled = [c for c in cells if c.strip()]
    if len(filled) < max(2, int(0.6 * width)) or _has_value_cell(filled):
        return False
    numeric = sum(1 for c in filled if re.fullmatch(r"[-+()\d.,%/: ]+", c.strip()))
    return numeric <= len(filled) // 3


def _split_header(rows: list[tuple[int, list[str]]]) -> tuple[list[str], list[str], int]:
    """(title lines above the header, header cells, how many leading rows are used up)."""
    width = max(len(cells) for _, cells in rows)
    for index, (_, cells) in enumerate(rows):
        if _looks_like_header(cells, width):
            titles = [" ".join(c for c in above if c.strip()) for _, above in rows[:index]]
            return [t for t in titles if t], cells, index + 1
    first = rows[0][1]
    labels = [c for c in first if c.strip()]
    numeric = sum(1 for c in labels if re.fullmatch(r"[-+()\d.,%/: ]+", c.strip()))
    if labels and numeric <= len(labels) // 2 and not _has_value_cell(labels):
        return [], first, 1  # a sparse row of labels is still the header ('pvc', 'fedrigoni', 'quantity')
    return [], [], 0  # figures or dates: the sheet has no header, and its first row is data


def read_xlsx(data: bytes) -> ParsedDocument:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pages: list[ParsedPage] = []
    warnings: list[str] = []

    def add_page(sheet: str, titles: list[str], header: list[str], body: list[tuple[int, list[str]]], *,
                 first: bool, single: bool) -> None:
        width = max([len(header)] + [len(cells) for _, cells in body])
        rows = ([header + [""] * (width - len(header))] if header else []) + [
            cells + [""] * (width - len(cells)) for _, cells in body
        ]
        table = ParsedTable(rows=rows, header_rows=1 if header else 0)  # no header: the cells are kept without column names
        # a sheet that fits on one page keeps the plain label; longer ones say which rows each page holds
        label = f"Sheet: {sheet}" if single or not body else f"Sheet: {sheet}, rows {body[0][0]:,}-{body[-1][0]:,}"
        text = "\n".join([label, *(titles if first else []), TABLE_MARKER.format(0)])
        pages.append(
            ParsedPage(
                number=len(pages) + 1,
                text=text,
                method="native",
                tables=[table],
                label=label,
                language=detect_language(_table_text([table])),
            )
        )

    def read_sheet(sheet: Any) -> None:
        pending: list[tuple[int, list[str]]] = []  # the first rows, held until the header is found
        header: list[str] | None = None
        titles: list[str] = []
        segment: list[tuple[int, list[str]]] = []
        emitted = 0

        def take(item: tuple[int, list[str]]) -> None:
            nonlocal emitted
            segment.append(item)
            if len(segment) >= ROWS_PER_PAGE:
                add_page(sheet.title, titles, header or [], segment, first=emitted == 0, single=False)
                emitted += 1
                segment.clear()

        for number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if number > MAX_SHEET_ROWS:
                warnings.append(f"sheet {sheet.title}: only the first {MAX_SHEET_ROWS:,} rows were read")
                break
            cells = ["" if v is None else _cell(v) for v in row]
            while cells and not cells[-1]:
                cells.pop()
            if not any(cells):
                continue
            if header is None:
                pending.append((number, cells))
                if len(pending) < HEADER_SCAN:
                    continue
                titles, header, used = _split_header(pending)
                for item in pending[used:]:
                    take(item)
                pending = []
                continue
            take((number, cells))
        if header is None and pending:
            titles, header, used = _split_header(pending)
            for item in pending[used:]:
                take(item)
        if header is not None and (segment or emitted == 0):
            add_page(sheet.title, titles, header, segment, first=emitted == 0, single=emitted == 0)

    try:
        for sheet in workbook.worksheets:
            read_sheet(sheet)
    finally:
        workbook.close()
    props = workbook.properties
    title = getattr(props, "title", None)
    return ParsedDocument(
        pages=pages or [ParsedPage(number=1, text="", method="native")],
        format="xlsx",
        title=title,
        properties={"title": title} if title else {},
        warnings=warnings,
    )


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.10g}"
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10] if getattr(value, "hour", 0) == 0 else value.isoformat()
    return clean_text(str(value))


def read_csv(data: bytes) -> ParsedDocument:
    import csv

    text = decode_text(data)
    dialect = csv.excel_tab if text.count("\t") > text.count(",") else csv.excel
    rows = [[clean_text(c) for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    rows = [r for r in rows if any(r)][:MAX_SHEET_ROWS]
    table = ParsedTable(rows=rows)
    return ParsedDocument(
        pages=[
            ParsedPage(
                number=1,
                text=TABLE_MARKER.format(0),
                method="native",
                tables=[table],
                language=detect_language(_table_text([table])),
            )
        ],
        format="text",
    )


# ---- PPTX ------------------------------------------------------------------------------------------------------------


def read_pptx(data: bytes) -> ParsedDocument:
    from pptx import Presentation

    deck = Presentation(io.BytesIO(data))
    pages: list[ParsedPage] = []
    for number, slide in enumerate(deck.slides, start=1):
        lines: list[str] = []
        tables: list[ParsedTable] = []
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        if title:
            lines.append("# " + title)
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        lines.append(("- " if para.level else "") + text)
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[clean_text(c.text) for c in row.cells] for row in shape.table.rows]
                if rows:
                    tables.append(ParsedTable(rows=rows))
                    lines.append(TABLE_MARKER.format(len(tables) - 1))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append("Notes: " + notes)
        text = clean_text("\n".join(lines))
        pages.append(
            ParsedPage(
                number=number, text=text, method="native", tables=tables, label=f"Slide {number}", language=detect_language(text)
            )
        )
    title = deck.core_properties.title or None
    return ParsedDocument(
        pages=pages or [ParsedPage(number=1, text="", method="native")],
        format="pptx",
        title=title,
        properties={"title": title} if title else {},
    )


# ---- e-mail, HTML, text ------------------------------------------------------------------------------------------------


def html_to_text(html: str) -> tuple[str, list[ParsedTable]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "head", "svg"]):
        tag.decompose()
    tables: list[ParsedTable] = []
    for table in soup.find_all("table"):
        rows = [[clean_text(cell.get_text(" ")) for cell in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if rows:
            tables.append(ParsedTable(rows=rows))
            table.replace_with(soup.new_string("\n" + TABLE_MARKER.format(len(tables) - 1) + "\n"))
    for level in range(1, 7):
        for heading in soup.find_all(f"h{level}"):
            heading.replace_with(soup.new_string("\n" + "#" * level + " " + heading.get_text(" ").strip() + "\n"))
    text = soup.get_text("\n")
    return clean_text(text), tables


def read_html(data: bytes) -> ParsedDocument:
    text, tables = html_to_text(decode_text(data))
    page = ParsedPage(number=1, text=text, method="native", tables=tables, language=detect_language(text))
    if len(text) > PSEUDO_PAGE_CHARS * 1.5 and not tables:
        return ParsedDocument(pages=_pseudo_pages(text), format="html")
    return ParsedDocument(pages=[page], format="html")


def read_email(data: bytes) -> ParsedDocument:
    message = email.message_from_bytes(data, policy=email.policy.default)
    headers = [f"{h}: {message[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if message[h]]
    body_text = ""
    tables: list[ParsedTable] = []
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        body_text = plain.get_content()
    else:
        html = message.get_body(preferencelist=("html",))
        if html is not None:
            body_text, tables = html_to_text(html.get_content())
    attachments: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        name = part.get_filename() or "attachment.bin"
        payload = part.get_payload(decode=True)
        if payload:
            attachments.append((name, payload))
    text = clean_text("\n".join(headers) + "\n\n" + body_text)
    subject = str(message["Subject"] or "") or None
    return ParsedDocument(
        pages=[ParsedPage(number=1, text=text, method="native", tables=tables, language=detect_language(body_text or text))],
        format="email",
        title=subject,
        properties={"title": subject or ""},
        attachments=attachments,
    )


def read_msg(data: bytes) -> ParsedDocument:
    try:
        import extract_msg
    except ImportError as exc:
        raise PermanentError("Outlook .msg files need the 'msg' extra (extract-msg)") from exc
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "message.msg"
        path.write_bytes(data)
        msg = extract_msg.Message(str(path))
        try:
            headers = [f"From: {msg.sender}", f"To: {msg.to}", f"Date: {msg.date}", f"Subject: {msg.subject}"]
            attachments = [
                (a.longFilename or a.shortFilename or "attachment.bin", a.data)
                for a in msg.attachments
                if isinstance(getattr(a, "data", None), bytes)
            ]
            text = clean_text("\n".join(headers) + "\n\n" + (msg.body or ""))
            return ParsedDocument(
                pages=[ParsedPage(number=1, text=text, method="native", language=detect_language(text))],
                format="email",
                title=msg.subject,
                properties={"title": msg.subject or ""},
                attachments=attachments,
            )
        finally:
            msg.close()


def read_text(data: bytes, name: str) -> ParsedDocument:
    if extension(name) in {"csv", "tsv"}:
        return read_csv(data)
    return ParsedDocument(pages=_pseudo_pages(decode_text(data), "Part"), format="text")


def read_zip(data: bytes) -> ParsedDocument:
    """A zip archive has no text of its own; its members become child items (with zip-bomb limits)."""
    attachments: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_MEMBERS:
            raise PermanentError(f"zip has {len(infos)} files; the limit is {MAX_ZIP_MEMBERS}")
        total = sum(i.file_size for i in infos)
        if total > MAX_ZIP_TOTAL_BYTES:
            raise PermanentError("zip expands beyond the size limit")
        for info in infos:
            if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
                raise PermanentError("zip member has a suspicious compression ratio")
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                continue
            attachments.append((str(name), zf.read(info)))
    listing = "\n".join(f"- {n}" for n, _ in attachments[:500])
    return ParsedDocument(
        pages=[ParsedPage(number=1, text="Archive contents:\n" + listing, method="native")], format="zip", attachments=attachments
    )


def convert_legacy(data: bytes, ext: str, timeout_s: int = 180) -> tuple[bytes, str]:
    target = LEGACY_OFFICE.get(ext, "docx")
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        raise PermanentError(f".{ext} files need LibreOffice in the worker image (or save them as .{target})")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"input.{ext}"
        src.write_bytes(data)
        # Every conversion gets its own LibreOffice profile: two conversions at once share (and lock) the default profile, and
        # the second one then fails or produces nothing. HOME is also pointed at the temporary folder (the worker's is read-only).
        profile = Path(tmp) / "profile"
        cmd = [
            soffice,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--norestore",
            "--convert-to",
            target,
            "--outdir",
            tmp,
            str(src),
        ]
        env = {**os.environ, "HOME": tmp}
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout_s, env=env)  # noqa: S603 - fixed argv
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PermanentError(f"LibreOffice could not convert the .{ext} file") from exc
        out = Path(tmp) / f"input.{target}"
        if not out.is_file():
            raise PermanentError(
                f"LibreOffice could not open the .{ext} file: it is probably password-protected or damaged "
                "(remove the password, or save it again as .xlsx, and upload it again)"
            )
        return out.read_bytes(), target


async def read_image(data: bytes, ocr: Any) -> ParsedDocument:
    from PIL import Image, ImageSequence

    Image.MAX_IMAGE_PIXELS = 200_000_000
    pages: list[ParsedPage] = []
    image = Image.open(io.BytesIO(data))
    frames = [frame.convert("RGB").copy() for frame in ImageSequence.Iterator(image)][:500]
    for number, frame in enumerate(frames, start=1):
        width, height = frame.size
        if width * height > 25_000_000:
            frame.thumbnail((5000, 5000))
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        result: OCRResult = await ocr.recognize(buffer.getvalue()) if ocr else OCRResult(text="")
        pages.append(
            ParsedPage(
                number=number,
                text=clean_text(result.text),
                method="ocr" if ocr else "empty",
                lines=[TextLine(t.text, t.box, t.confidence) for t in result.lines],
                width=float(width),
                height=float(height),
                language=detect_language(result.text),
                quality={"ocr_confidence": result.confidence},
            )
        )
    return ParsedDocument(pages=pages, format="image")


async def read_cad(data: bytes, kind: str, pdf_parser: Any) -> ParsedDocument:
    """An AutoCAD drawing: text and title block from the file, each sheet drawn, and explained by the vision model the PDF
    reader was given (with its daily page budget and its privacy rule; none when the file is read with local OCR only)."""
    from rag.ingestion import cad, cad_analysis

    options = getattr(pdf_parser, "cad", None)  # set when CAD_VISION is usable: the sheets get their own, fuller analysis
    analyse = options is not None and options.model is not None and options.max_sheets > 0
    parsed, images, hires = await asyncio.to_thread(
        cad.read_full, data, kind, analysis_sheets=options.max_sheets if analyse else 0,
        analysis_px=options.render_px if analyse else 6000, analysis_colour=options.colour if analyse else True,
        preview_colour=getattr(getattr(pdf_parser, "settings", None), "cad_colour", True))  # CAD_COLOUR: viewer and analysis
    take_budget = getattr(pdf_parser, "take_budget", None)
    protect_kinds = getattr(pdf_parser, "protect_kinds", None)
    analysed = (await cad_analysis.analyze_sheets(parsed, hires, options, take_budget=take_budget, protect_kinds=protect_kinds)
                if analyse else set())
    await cad.describe_sheets(parsed, images, getattr(pdf_parser, "vision", None), take_budget=take_budget,
                              protect_kinds=protect_kinds, skip=analysed)
    parsed.previews = images
    return parsed


async def parse_document(data: bytes, name: str, *, pdf_parser: Any, ocr: Any) -> ParsedDocument:
    """Read any supported file into pages. PDFs go to the PARSER role; everything else is read natively here."""
    kind = detect_format(data, name)
    if kind in LEGACY_OFFICE:
        converted, target = await asyncio.to_thread(convert_legacy, data, extension(name) or kind)
        return await parse_document(converted, f"converted.{target}", pdf_parser=pdf_parser, ocr=ocr)
    if kind == "pdf":
        return await pdf_parser.parse_pdf(data, name)
    if kind == "image":
        return await read_image(data, ocr)
    if kind in CAD_EXTENSIONS:
        return await read_cad(data, kind, pdf_parser)
    from rag.ingestion import formats_extra

    readers = {
        "docx": read_docx,
        "xlsx": read_xlsx,
        "pptx": read_pptx,
        "html": read_html,
        "email": read_email,
        "msg": read_msg,
        "zip": read_zip,
        "rtf": formats_extra.read_rtf,
        "odt": formats_extra.read_odt,
        "xer": formats_extra.read_xer,
    }
    if kind == "text":
        return await asyncio.to_thread(read_text, data, name)
    reader = readers.get(kind)
    if reader is None:
        raise PermanentError(f"unsupported file type: {kind}")
    try:
        return await asyncio.to_thread(reader, data)
    except PermanentError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise PermanentError(f"could not read {kind} file: {type(exc).__name__}: {exc}") from exc
