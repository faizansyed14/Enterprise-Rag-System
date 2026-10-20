"""Raster previews for Office pages (Word / Excel / PowerPoint) from stored extract text and tables.

Page numbers match the extract side of the document viewer. PDF and image formats keep their native renderers.
"""

from __future__ import annotations

import io
import re
import textwrap
from pathlib import Path
from typing import Any

TABLE_MARKER_RE = re.compile(r"\[\[TABLE:(\d+)\]\]")

# Formats that can be drawn from stored page text/tables (aligned with extract page_no).
OFFICE_PREVIEW_FORMATS = frozenset({
    "docx", "doc", "odt", "rtf",
    "xlsx", "xls", "xlsm", "ods",
    "pptx", "ppt", "odp",
    # drawn from the stored text as well: plain text and CSV, web pages, e-mail and Primavera exports
    "text", "html", "email", "msg", "xer",
})

# formats whose page is a table only (a CSV) are drawn as a grid, like a spreadsheet
TEXT_FORMATS = frozenset({"text", "html", "email", "msg", "xer"})

SHEET_FORMATS = frozenset({"xlsx", "xls", "xlsm", "ods"})


def _font(size: int, *, bold: bool = False) -> Any:
    from PIL import ImageFont

    names = (
        ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        ("arialbd.ttf" if bold else "arial.ttf"),
        ("segoeui.ttf" if not bold else "segoeuib.ttf"),
    )
    roots = (
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/dejavu"),
        Path("/usr/share/fonts/truetype/liberation"),
        Path("C:/Windows/Fonts"),
    )
    for root in roots:
        for name in names:
            path = root / name
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
    return ImageFont.load_default()


def _measure(draw: Any, text: str, font: Any) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _wrap(text: str, font: Any, draw: Any, max_width: int) -> list[str]:
    if not text:
        return [""]
    # Approximate character width for wrapping when default bitmap fonts are used.
    sample_w, _ = _measure(draw, "ABCDEFGHIJ", font)
    avg = max(sample_w / 10, 6)
    width_chars = max(20, int(max_width / avg))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(textwrap.wrap(paragraph, width=width_chars, replace_whitespace=False,
                                   drop_whitespace=False) or [""])
    return lines


def render_office_page_png(
    *,
    fmt: str,
    text: str,
    tables: list[dict[str, Any]] | None,
    label: str | None,
    scale: float = 1.5,
) -> bytes:
    """Draw one extract page as a PNG. Spreadsheet formats get a grid; others a document sheet."""
    scale = max(0.5, min(float(scale), 3.0))
    tables = tables or []
    only_a_table = fmt in TEXT_FORMATS and tables and not any(
        ln.strip() and not TABLE_MARKER_RE.fullmatch(ln.strip()) for ln in (text or "").splitlines()[1:])
    if fmt in SHEET_FORMATS or only_a_table:
        return _render_sheet(text=text, tables=tables, label=label, scale=scale)
    return _render_document(text=text, tables=tables, label=label, scale=scale)


def _render_sheet(*, text: str, tables: list[dict[str, Any]], label: str | None, scale: float) -> bytes:
    from PIL import Image, ImageDraw

    table = tables[0] if tables else {"rows": [], "header_rows": 0}
    rows: list[list[str]] = [list(map(str, r)) for r in (table.get("rows") or [])]
    header_rows = int(table.get("header_rows") or 0)
    title = (label or "").strip() or next((ln.strip() for ln in (text or "").splitlines()
                                           if ln.strip() and not TABLE_MARKER_RE.fullmatch(ln.strip())), "Sheet")

    # Cap for a readable preview; full data remains on the extract pane.
    max_rows, max_cols = 80, 18
    rows = [r[:max_cols] for r in rows[:max_rows]]
    cols = max((len(r) for r in rows), default=1)
    for r in rows:
        while len(r) < cols:
            r.append("")

    cell_pad_x = int(8 * scale)
    cell_pad_y = int(5 * scale)
    font = _font(int(12 * scale))
    header_font = _font(int(12 * scale), bold=True)
    title_font = _font(int(14 * scale), bold=True)
    # Measure column widths from the first rows.
    probe = Image.new("RGB", (10, 10), "white")
    probe_draw = ImageDraw.Draw(probe)
    col_w = [int(72 * scale)] * cols
    for r_i, row in enumerate(rows[:40]):
        f = header_font if r_i < header_rows else font
        for c_i, cell in enumerate(row):
            value = (cell or "")[:48]
            w, _ = _measure(probe_draw, value or " ", f)
            col_w[c_i] = min(int(220 * scale), max(col_w[c_i], w + cell_pad_x * 2))

    row_h = int(22 * scale)
    margin = int(28 * scale)
    title_h = int(36 * scale)
    width = margin * 2 + sum(col_w) + 1
    height = margin + title_h + row_h * max(len(rows), 1) + margin
    width = min(width, int(2800 * scale / 1.5))
    height = min(height, int(3600 * scale / 1.5))

    image = Image.new("RGB", (width, height), (249, 249, 247))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, title_h + margin // 2), fill=(236, 236, 233))
    draw.text((margin, margin // 2), title[:120], fill=(20, 20, 18), font=title_font)

    y = margin + title_h
    x0 = margin
    for r_i, row in enumerate(rows):
        x = x0
        is_header = r_i < header_rows
        bg = (232, 238, 247) if is_header else ((255, 255, 255) if r_i % 2 == 0 else (244, 246, 248))
        for c_i, cell in enumerate(row):
            w = col_w[c_i]
            draw.rectangle((x, y, x + w, y + row_h), fill=bg, outline=(210, 212, 216))
            value = (cell or "").replace("\n", " ")[:48]
            draw.text((x + cell_pad_x, y + cell_pad_y), value, fill=(18, 18, 16),
                      font=header_font if is_header else font)
            x += w
        y += row_h
        if y + row_h > height - margin:
            break

    if len(table.get("rows") or []) > max_rows:
        note = _font(int(11 * scale))
        draw.text((margin, height - margin + 4), f"Showing first {max_rows} rows — full sheet on the right",
                  fill=(100, 100, 96), font=note)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _render_document(*, text: str, tables: list[dict[str, Any]], label: str | None, scale: float) -> bytes:
    from PIL import Image, ImageDraw

    page_w, page_h = int(874 * scale), int(1240 * scale)  # A4-ish at ~96dpi * scale
    margin = int(56 * scale)
    image = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # Soft page edge like a sheet on a desk.
    draw.rectangle((0, 0, page_w - 1, page_h - 1), outline=(220, 220, 216))

    title_font = _font(int(15 * scale), bold=True)
    body_font = _font(int(13 * scale))
    heading_font = _font(int(16 * scale), bold=True)
    small = _font(int(11 * scale))
    y = margin
    max_w = page_w - margin * 2

    if label:
        draw.text((margin, y), label[:100], fill=(110, 110, 105), font=small)
        y += int(22 * scale)

    # Interleave text lines and tables using [[TABLE:n]] markers.
    lines = (text or "").splitlines()
    if not lines and not tables:
        draw.text((margin, y), "(Empty page)", fill=(140, 140, 135), font=body_font)
    i = 0
    while i < len(lines) and y < page_h - margin:
        line = lines[i]
        m = TABLE_MARKER_RE.fullmatch(line.strip())
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(tables):
                y = _draw_inline_table(draw, tables[idx], x=margin, y=y, max_w=max_w, scale=scale)
                y += int(14 * scale)
            i += 1
            continue
        stripped = line.lstrip("#").strip() if line.startswith("#") else line
        is_heading = line.startswith("#")
        font = heading_font if is_heading else body_font
        fill = (15, 15, 14) if is_heading else (28, 28, 26)
        for wrapped in _wrap(stripped, font, draw, max_w):
            if y > page_h - margin:
                break
            draw.text((margin, y), wrapped, fill=fill, font=font)
            _, h = _measure(draw, wrapped or " ", font)
            y += h + int((8 if is_heading else 5) * scale)
        if is_heading:
            y += int(4 * scale)
        i += 1

    # Tables not referenced by markers (some pages only store tables).
    if tables and not any(TABLE_MARKER_RE.fullmatch((ln or "").strip()) for ln in lines):
        for table in tables:
            if y > page_h - margin:
                break
            y = _draw_inline_table(draw, table, x=margin, y=y, max_w=max_w, scale=scale)
            y += int(14 * scale)

    if y >= page_h - margin and (i < len(lines) or tables):
        draw.text((margin, page_h - margin + 2), "…continued in extract →", fill=(120, 120, 115), font=small)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_inline_table(draw: Any, table: dict[str, Any], *, x: int, y: int, max_w: int, scale: float) -> int:
    rows = [list(map(str, r))[:10] for r in (table.get("rows") or [])[:25]]
    if not rows:
        return y
    header_rows = int(table.get("header_rows") or 0)
    cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < cols:
            r.append("")
    font = _font(int(11 * scale))
    header_font = _font(int(11 * scale), bold=True)
    col_w = max(40, max_w // cols)
    row_h = int(20 * scale)
    for r_i, row in enumerate(rows):
        cx = x
        for cell in row:
            bg = (236, 240, 246) if r_i < header_rows else (255, 255, 255)
            draw.rectangle((cx, y, cx + col_w, y + row_h), fill=bg, outline=(210, 212, 216))
            value = (cell or "").replace("\n", " ")[:36]
            draw.text((cx + int(4 * scale), y + int(3 * scale)), value, fill=(20, 20, 18),
                      font=header_font if r_i < header_rows else font)
            cx += col_w
        y += row_h
    return y
