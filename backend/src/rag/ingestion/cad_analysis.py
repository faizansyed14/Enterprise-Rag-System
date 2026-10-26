"""The full analysis of an AutoCAD sheet (.dwg, .dxf): its own flow, its own prompt, its own model (`CAD_VISION`).

A drawing is mostly small labels (room names, unit codes, areas), and a vision model reads them only if it is given enough
pixels. So how the picture is sent matters more than the wording of the prompt:

  1. the sheet is drawn at high resolution (`CAD_RENDER_PX`, about 360 dpi on A3) and the blank margin is cropped away
  2. it is cut into overlapping tiles (`CAD_TILES`, 4 columns x 3 rows by default; tall drawings are turned round)
  3. ONE request carries the system prompt, the user prompt, the full overview first, then every tile labelled R<row>C<col>
  4. the reply is a Markdown report (front matter, summary, tenant schedule, services, circulation, external works, detail
     references, gaps). It is added to the sheet's text, so every table and heading in it is searched like any other page.

Same safeguards as the other vision paths: the daily page budget, and a sheet whose text holds a bank or id number is never
sent. A sheet that cannot be analysed keeps the ordinary short description (`cad.describe_sheets`).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from rag.plugins.capabilities import ChatMessage, ParsedDocument

log = logging.getLogger(__name__)

MARKER = "[Sheet analysis]"
MAX_TILE_PX = 1568  # the longest side of an image sent to the model; larger images are scaled down by the provider anyway
OVERLAP = 0.10  # neighbouring tiles share this fraction of their width and height, so a label cut by an edge is whole in one
MAX_SENT_TEXT = 8000
MAX_OTHER_SHEET_TEXT = 2000  # characters of another sheet's text sent along (its title block, project, owner)
MIN_REPORT_CHARS = 200  # a reply shorter than this is not a report
MAX_RETRY_TOKENS = 32000  # an empty or cut-off reply is asked for again with the limit doubled, up to this
_TILE_LOCK = threading.Lock()  # a sheet drawn at 6,000 px takes ~150 MB to cut: one at a time per process
_NOT_STATED = ("n/s", "not stated", "not shown", "not present", "not visible", "not on ", "not given", "none", "null", "unknown", "n/a")

SYSTEM_PROMPT = (
    "You are an architectural drawing analyst. You read CAD drawings (site plans, floor plans, tenancy/leasing plans) and "
    "extract every piece of information accurately. Never guess: if a value is not visible on the drawing, write \"n/s\" "
    "(not stated). Transcribe labels exactly as written, and note any misspellings separately."
)

USER_PROMPT = """Attached is a CAD drawing. Image 1 is the full overview. Images 2 onward are high-resolution tiles of the same drawing, numbered by row and column (Tile R1C1 = top-left). Labels may be split across tile edges, so cross-check neighbouring tiles.



Review the drawing in detail and produce a Markdown file with these sections:



1. YAML front matter: title, document_type, discipline, sheet_count, location (state if inferred and why), project_name, drawing_number, revision, scale, north_arrow, title_block, anchor_tenant, number of tenant units, detail_sheets_referenced, tags. Use "Not stated" where missing.



2. Summary: one paragraph describing the overall layout (building shape, wings, parking, access, basement, service areas) and what the colours/highlights mean.



3. Tenant schedule: a table with columns Wing | Tenant no. | Occupant | Area | Unit code. Include every unit, with areas exactly as written.



4. Service and back-of-house rooms: every plant, utility, toilet, janitor, loading and corridor space, with room codes, grouped by location.



5. Vertical circulation and building services: stairs, lifts, travelators/escalators (with slopes), fire hose cabinets, ventilation shafts, columns.



6. External works and parking: roads and traffic direction, parking types and layout, accessible bays, ramps (with slopes), turning radii, drop-off areas, crossings, bollards, speed tables, plot limits.



7. Detail references: a table of every callout bubble (detail number + sheet number + description).



8. Gaps and observations: missing title block info, unfilled placeholder tags, conflicting or duplicate unit numbers, spelling errors, and anything that suggests what stage or purpose the drawing is for (construction vs. leasing/marketing).



Be exhaustive. Read every label on every tile before writing."""

TEXT_LAYER_NOTE = (
    "Text read directly from the CAD file for this sheet (exact characters, without positions). Use it only to check the spelling "
    "of labels you can see on the images; do not report anything that is not visible on the drawing."
)

# front matter key -> the title-block field the rest of the system uses
_FRONT_MATTER = {"title": "title", "drawing_number": "doc_number", "revision": "revision", "scale": "scale",
                 "project_name": "project"}


@dataclass
class Options:
    """What the ingestion stage needs to analyse a sheet; built from the settings by the model hub."""

    model: Any = None  # the CAD_VISION model, or None: the ordinary short description is used
    tiles: str = "4x3"
    render_px: int = 6000
    max_sheets: int = 3
    send_text: bool = True
    max_tokens: int = 16000
    colour: bool = True  # the drawing is shown in its own colours (see cad.render)
    timeout_s: float = 300.0


@dataclass
class Tile:
    row: int
    col: int
    png: bytes

    @property
    def label(self) -> str:
        return f"R{self.row}C{self.col}"


def parse_grid(spec: str) -> tuple[int, int]:
    """'4x3' -> (4 columns, 3 rows); anything unreadable gives 4x3."""
    match = re.fullmatch(r"\s*(\d{1,2})\s*[x×]\s*(\d{1,2})\s*", spec or "")
    cols, rows = (int(match.group(1)), int(match.group(2))) if match else (4, 3)
    return max(1, min(cols, 8)), max(1, min(rows, 8))


def _png(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _content_box(image: Any) -> tuple[int, int, int, int] | None:
    """The rectangle that holds everything that is not white, or None for a blank image."""
    return image.convert("L").point(lambda v: 0 if v > 245 else 255).getbbox()


def make_tiles(png: bytes, spec: str = "4x3", *, overlap: float = OVERLAP, max_px: int = MAX_TILE_PX,
               ) -> tuple[bytes, list[Tile], tuple[int, int]]:
    """(overview PNG, tiles, (columns, rows) used) for one drawn sheet.

    The blank margin is cropped first. Tiles that are completely white are left out (they would cost a picture and say
    nothing); the labels of the others keep their place in the grid."""
    from PIL import Image

    cols, rows = parse_grid(spec)
    with _TILE_LOCK:
        Image.MAX_IMAGE_PIXELS = None  # the sheet is drawn by us, at a size we chose
        image = Image.open(io.BytesIO(png)).convert("RGB")  # colour stays: it often says what a unit is (never grey-scale it)
        box = _content_box(image)
        if box is None:
            raise ValueError("the sheet is blank")
        pad = max(8, round(max(image.size) * 0.01))
        image = image.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(image.width, box[2] + pad),
                            min(image.height, box[3] + pad)))
        width, height = image.size
        if height > width * 1.15 and cols > rows:  # a tall drawing: more rows than columns
            cols, rows = rows, cols
        overview = image.copy()
        overview.thumbnail((max_px, max_px), Image.LANCZOS)
        overview_png = _png(overview)
        step = 1 - overlap
        tile_w = width / (1 + (cols - 1) * step)
        tile_h = height / (1 + (rows - 1) * step)
        tiles: list[Tile] = []
        for r in range(rows):
            for c in range(cols):
                left, top = round(c * tile_w * step), round(r * tile_h * step)
                right, bottom = min(width, round(left + tile_w)), min(height, round(top + tile_h))
                piece = image.crop((left, top, right, bottom))
                if _content_box(piece) is None:
                    continue
                if max(piece.size) > max_px:
                    piece.thumbnail((max_px, max_px), Image.LANCZOS)
                tiles.append(Tile(r + 1, c + 1, _png(piece)))
    return overview_png, tiles, (cols, rows)


def other_sheets_note(parsed: ParsedDocument, index: int) -> str:
    """What the file's OTHER sheets state: their title-block fields and the start of the text written on them. A drawing's real
    title block (number, revision, date, scale, project, owner) is often on its paper-space layout, while the plan itself sits
    in the model space, which has none."""
    blocks = []
    for number, page in enumerate(parsed.pages):
        if number == index:
            continue
        lines = [page.label]
        if page.title_block:
            lines.append("; ".join(f"{k}: {v}" for k, v in page.title_block.items()))
        written = page.text.split("Text on the drawing:", 1)[-1].strip()[:MAX_OTHER_SHEET_TEXT]
        if written:
            lines.append("Text on this sheet: " + " | ".join(written.split("\n")))
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_messages(page_text: str, overview: bytes, tiles: list[Tile], grid: tuple[int, int], *, send_text: bool,
                   other_sheets: str = "") -> list[ChatMessage]:
    """The system prompt, then one user message: the prompt, the overview, every labelled tile (and the text layer)."""
    cols, rows = grid

    def image(data: bytes) -> dict[str, str]:
        return {"type": "image", "media_type": "image/png", "data": base64.b64encode(data).decode()}

    parts: list[dict[str, str]] = [
        {"type": "text", "text": USER_PROMPT},
        {"type": "text", "text": "Image 1: the full overview of the drawing."},
        image(overview),
    ]
    for number, tile in enumerate(tiles, start=2):
        position = f"row {tile.row} of {rows}, column {tile.col} of {cols}"
        parts.append({"type": "text", "text": f"Image {number}: Tile {tile.label} ({position})."})
        parts.append(image(tile.png))
    if send_text and page_text.strip():
        parts.append({"type": "text", "text": f"{TEXT_LAYER_NOTE}\n<drawing_text>\n{page_text[:MAX_SENT_TEXT]}\n</drawing_text>"})
    if other_sheets:
        parts.append({"type": "text", "text": "What the other sheets of the same CAD file state (these sheets are not "
                                              "shown; use them for the title block, drawing number, revision, scale, project and owner of this "
                                              f"drawing):\n<other_sheets>\n{other_sheets}\n</other_sheets>"})
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", parts)]


def _strip_fence(markdown: str) -> str:
    """The report without a code fence around it. A report cut off at the output limit has the opening fence only."""
    text = markdown.strip()
    match = re.match(r"```(?:markdown|md|yaml)?[ \t]*\n", text)
    if not match:
        return text
    text = text[match.end():]
    return re.sub(r"\n```\s*$", "", text).strip()


def _plain(value: Any) -> str:
    """A front-matter value as the short fact it states: models add remarks in brackets ("A11-02 (from the supplied title-block
    data; not printed on the plan)"), which must not become the document's number or title."""
    text = " ".join(str(value).split())
    return re.split(r"\s+[(\[]", text, maxsplit=1)[0].strip(" \"'")[:120]


def front_matter(markdown: str) -> dict[str, str]:
    """The title-block fields stated in the report's YAML front matter ("Not stated" and "n/s" are left out)."""
    match = re.match(r"\s*---\s*\n(.*?)\n---", markdown, re.S)
    if not match:
        return {}
    try:
        import yaml

        data = yaml.safe_load(match.group(1))
    except Exception:  # noqa: BLE001 - a report with broken front matter still has its text
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, str] = {}
    for key, field in _FRONT_MATTER.items():
        value = data.get(key)
        if isinstance(value, (str, int, float)) and _plain(value) and not _plain(value).lower().startswith(_NOT_STATED):
            found[field] = _plain(value)
    return found


async def _ask(options: Options, messages: list[ChatMessage], label: str) -> str:
    """The model's report. A reply that is empty or too short to be one is nearly always a model that spent its whole output
    allowance before writing (reasoning counts against it), so the request is repeated once with the limit doubled. A long
    report that was cut off at the limit is kept (it is still most of the report) and said so in the log."""
    limit = options.max_tokens
    for attempt in range(2):
        result = await options.model.complete(messages, max_tokens=limit, temperature=0.0, timeout=options.timeout_s)
        report = _strip_fence(result.content or "")
        finish = getattr(result, "finish_reason", "") or ""
        if len(report) >= MIN_REPORT_CHARS:
            if finish == "length":
                log.warning("report for %s was cut off at the limit of %d tokens", label, limit)
            return report
        used = getattr(getattr(result, "usage", None), "output_tokens", 0)
        log.warning("no report for %s (finish=%s, %d characters, %s output tokens, limit %d)", label, finish or "?", len(report),
                    used, limit)
        if attempt == 0 and limit < MAX_RETRY_TOKENS:
            limit = min(limit * 2, MAX_RETRY_TOKENS)
            continue
        break
    raise ValueError("the model returned no report")


async def analyze_sheets(parsed: ParsedDocument, hires: list[bytes | None], options: Options, *, take_budget: Any = None,
                         protect_kinds: Any = None) -> set[int]:
    """Analyse the sheets that were drawn at high resolution; returns the indexes (into parsed.pages) that now carry the
    report. A sheet that is skipped or fails keeps its text and is left for the ordinary short description."""
    from rag.privacy.shield import has_sensitive

    done: set[int] = set()
    if options.model is None:
        return done
    for index, (page, big) in enumerate(zip(parsed.pages, hires, strict=True)):
        if big is None:
            continue
        if protect_kinds and has_sensitive(page.text, protect_kinds):
            parsed.warnings.append(f"{page.label}: holds a bank or id number, so the drawing was not sent for analysis")
            continue
        if take_budget is not None and not await take_budget("vision_pages", 1):
            parsed.warnings.append(f"{page.label}: vision budget for today is used up; the sheet was not analysed")
            continue
        try:
            overview, tiles, grid = await asyncio.to_thread(make_tiles, big, options.tiles)
            messages = build_messages(page.text, overview, tiles, grid, send_text=options.send_text,
                                      other_sheets=other_sheets_note(parsed, index))
            report = await _ask(options, messages, page.label)
        except Exception as exc:  # noqa: BLE001 - the sheet keeps its text and gets the short description instead
            log.warning("analysis of %s failed: %s", page.label, exc)
            parsed.warnings.append(f"{page.label}: sheet analysis failed ({type(exc).__name__})")
            continue
        for field, value in front_matter(report).items():
            page.title_block.setdefault(field, value)
        page.text = f"{page.text}\n\n{MARKER}\n{report}"
        page.method = "cad+vision"
        done.add(index)
        log.info("analysed %s: %d tiles, %d characters", page.label, len(tiles), len(report))
    return done
