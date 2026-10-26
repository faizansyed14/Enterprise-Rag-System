"""`local/pdfium`: the per-page PDF router.

Every page gets the cheapest method that reads it correctly:
  good text layer                -> pdfium text (fast, reading order) ; tables with pdfplumber when the page has rules
  scanned or broken text layer   -> render + OCR (OCR role; cached by page-image hash)
  mostly-image page with a little text (a scanned insert with a stamped header) -> text layer + OCR, merged
  drawing sheet (A3 and larger)  -> title-block region read separately; vision model describes sheets whose text
                                    layer says little (VISION role, within VISION_PAGES_PER_DAY)
There is no page cap: a 2,000-page scanned contract is read in full, one page at a time, without holding page images
in memory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import PermanentError, RateLimited
from rag.metadata.title_block import parse_title_block
from rag.plugins.capabilities import ChatMessage, OCRResult, ParsedDocument, ParsedPage, ParsedTable, TextLine
from rag.plugins.registry import registry
from rag.privacy.shield import has_sensitive
from rag.providers.parsers import layout_runner, structure, table_flow, table_quality
from rag.providers.parsers.pdfium_thread import run_pdfium
from rag.text import ligatures
from rag.text.language import detect_language
from rag.text.normalize import clean_text

log = logging.getLogger(__name__)

DRAWING_MIN_SIDE_PT = 1190.0  # A3 long side is 1191 pt
DRAWING_MIN_PATHS = 3000      # a CAD plot printed small on a Letter or A4 page still holds thousands of vector strokes
MAX_RENDER_PIXELS = 12_000_000
OcrCacheGet = Callable[[str, str], Awaitable[OCRResult | None]]
OcrCachePut = Callable[[str, str, OCRResult], Awaitable[None]]
BudgetTake = Callable[[str, int], Awaitable[bool]]


@dataclass
class PagePlan:
    number: int
    width: float
    height: float
    text: str
    chars: int
    garbage_ratio: float
    image_coverage: float
    path_objects: int
    is_drawing: bool
    tables: list[ParsedTable] = field(default_factory=list)
    title_block: dict[str, str] = field(default_factory=dict)
    image_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)  # (x0, y0, x1, y1) as page fractions
    label: str | None = None  # printed page number found in the running header/footer
    typo_text: str | None = None  # the text with headings marked from type sizes (fallback for the layout engine)
    rotation: int = 0

    @property
    def method(self) -> str:
        return "text_layer"


def _text_rotation(textpage: Any) -> int:
    """The turn of the page's text, from the angle pdfium reports for a sample of its characters (a landscape drawing on a
    portrait page has its text at 90 or 270 degrees)."""
    try:
        import pypdfium2.raw as pdfium_c

        total = textpage.count_chars()
        votes: dict[int, int] = {}
        for index in range(0, total, max(1, total // 200)):
            angle = pdfium_c.FPDFText_GetCharAngle(textpage.raw, index)
            if angle >= 0:
                key = round(math.degrees(angle) / 90) % 4 * 90
                votes[key] = votes.get(key, 0) + 1
        return max(votes, key=votes.get) if votes else 0
    except Exception:  # noqa: BLE001 - an older pdfium without this call: assume upright
        return 0


_LABEL_WORDS = re.compile(
    r"(?i)\b(date|revision|rev|scale|drawn|checked|approved|dwg\.?|drawing|title|cont\.?dwg\.?ref|ref|no\.?|"
    r"project|client|contractor|location|by|sheet|number)\b|[^A-Za-z0-9]+"
)


def _is_label_text(value: str) -> bool:
    """True when a title-block 'value' is only other labels ('DATE REVISION', 'CONT.DWG.REF NO'): the label's real value sits
    elsewhere on the sheet, and keeping this would hide the value the vision model reads."""
    return len(_LABEL_WORDS.sub("", value)) < 2


def _garbage_ratio(text: str) -> float:
    if not text:
        return 1.0
    bad = sum(1 for ch in text if ch == "�" or "" <= ch <= "" or (ord(ch) < 32 and ch not in "\n\t\r"))
    return bad / len(text)


@dataclass
class ObjectStats:
    image_coverage: float = 0.0
    paths: int = 0
    h_rules: int = 0  # long thin horizontal strokes (table rules, underlines)
    v_rules: int = 0
    boxes: int = 0  # rectangles of cell size (a table drawn as outlined cells)

    @property
    def may_hold_table(self) -> bool:
        """A table with ruled lines needs a grid: at least two horizontal and two vertical rules, or several cell boxes.
        Everything else (figures, equations, plain text) cannot be found by the ruling-line detector, so the expensive
        pdfplumber pass is skipped for it."""
        return (self.h_rules >= 2 and self.v_rules >= 2) or self.boxes >= 4


def _object_stats(page: Any, width: float, height: float, *, cap: int = 4000) -> ObjectStats:
    import pypdfium2.raw as pdfium_c

    stats = ObjectStats()
    image_area = 0.0
    page_area = max(width * height, 1.0)
    for i, obj in enumerate(page.get_objects(max_depth=2)):
        if i >= cap:
            break
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            left, bottom, right, top = obj.get_bounds()
            image_area += max(0.0, right - left) * max(0.0, top - bottom)
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            stats.paths += 1
            left, bottom, right, top = obj.get_bounds()
            w, h = right - left, top - bottom
            if h < 2.5 and w > 0.04 * width:
                stats.h_rules += 1
            elif w < 2.5 and h > 0.015 * height:
                stats.v_rules += 1
            elif 6 <= w < 0.9 * width and 6 <= h < 0.6 * height:
                stats.boxes += 1
    stats.image_coverage = min(1.0, image_area / page_area)
    return stats


Box = tuple[float, float, float, float]


def _image_boxes(page: Any, width: float, height: float, min_fraction: float, *, cap: int = 4000) -> list[Box]:
    """Boxes (page fractions, y from the top) of embedded pictures big enough to hold readable content: a table or a
    form pasted into a text PDF as an image has no text layer, so its words exist only in the pixels."""
    import pypdfium2.raw as pdfium_c

    boxes: list[Box] = []
    page_area = max(width * height, 1.0)
    for i, obj in enumerate(page.get_objects(max_depth=2)):
        if i >= cap:
            break
        if obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
            continue
        left, bottom, right, top = obj.get_bounds()
        if max(0.0, right - left) * max(0.0, top - bottom) / page_area < min_fraction:
            continue
        boxes.append((max(0.0, left / width), max(0.0, 1 - top / height), min(1.0, right / width), min(1.0, 1 - bottom / height)))
    return boxes


def _title_block_text(textpage: Any, width: float, height: float) -> dict[str, str]:
    regions = [
        (width * 0.55, 0.0, width, height * 0.35),  # bottom-right (most common)
        (width * 0.75, 0.0, width, height),  # right-hand strip
        (0.0, 0.0, width, height * 0.2),  # bottom strip
        (width * 0.55, height * 0.65, width, height),  # top-right (rotated sheets)
    ]
    best: dict[str, str] = {}
    for left, bottom, right, top in regions:
        try:
            text = textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top)
        except Exception:  # noqa: BLE001, S112 - some malformed pages reject bounded extraction; that region is skipped
            continue
        fields = parse_title_block(clean_text(text))
        if len(fields) > len(best):
            best = fields
    return best


# pdfplumber glues words whose gap is under 3 points ("speechrecognition", "Kim and Choi (2018)" -> "KimandChoi(2018)") in the
# tightly set type of many journals; a gap measured against the font size (15% of it) keeps the spaces and no real word apart.
_WORD_GAP = {"x_tolerance": 3, "x_tolerance_ratio": 0.15}


def _tables_for_page(plumber_pdf: Any, index: int) -> tuple[list[ParsedTable], str | None]:
    """Tables and the page text outside them (None when the page has no tables)."""
    page = plumber_pdf.pages[index]
    found = page.find_tables(table_settings={"vertical_strategy": "lines", "horizontal_strategy": "lines"})
    tables: list[ParsedTable] = []
    tops: list[float] = []
    outside = page
    for table in found:
        rows = [[clean_text(c or "") for c in row] for row in table.extract(**_WORD_GAP) or []]
        rows = [r for r in rows if any(cell for cell in r)]
        rows = table_quality.tidy(rows)
        if not table_quality.is_usable(rows):
            continue  # shredded by merged cells: the page text reads better than rows labelled with the wrong columns
        x0, top, x1, bottom = table.bbox
        tables.append(
            ParsedTable(
                rows=rows, header_rows=1, bbox=(x0 / page.width, top / page.height, x1 / page.width, bottom / page.height)
            )
        )
        tops.append(top)
        try:
            outside = outside.outside_bbox(table.bbox)
        except ValueError:
            continue
    if not tables:
        return [], None
    lines = outside.extract_text_lines(strip=True, return_chars=False, **_WORD_GAP) or []
    return tables, clean_text(table_flow.interleave(lines, tops))


def analyse_pdf(data: bytes, settings: Settings) -> tuple[list[PagePlan], dict[str, str]]:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        message = str(exc)
        if "password" in message.lower():
            raise PermanentError("PDF is password-protected") from exc
        raise PermanentError(f"PDF cannot be opened: {message}") from exc
    plumber_pdf = None
    plans: list[PagePlan] = []
    styled: list[list[structure.StyledLine]] = []
    try:
        properties = {k.lower(): str(v) for k, v in (pdf.get_metadata_dict(skip_empty=True) or {}).items()}
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                width, height = page.get_size()
                textpage = page.get_textpage()
                raw = textpage.get_text_bounded()
                text = clean_text(raw, keep_glyph_codes=True)
                try:
                    styled.append(structure.page_styled_lines(textpage, raw))
                except Exception:  # noqa: BLE001 - typography is a bonus; a font-table oddity must not lose the page
                    log.debug("font information unavailable on page %s", index + 1, exc_info=True)
                    styled.append([])
                ostats = _object_stats(page, width, height)
                coverage, paths = ostats.image_coverage, ostats.paths
                is_drawing = (
                    max(width, height) >= DRAWING_MIN_SIDE_PT and (paths > 400 or len(text) < 4000)
                ) or paths >= DRAWING_MIN_PATHS
                plan = PagePlan(
                    number=index + 1,
                    width=width,
                    height=height,
                    text=text,
                    chars=len(text.strip()),
                    garbage_ratio=round(_garbage_ratio(raw), 4),
                    image_coverage=round(coverage, 3),
                    path_objects=paths,
                    is_drawing=is_drawing,
                )
                plan.rotation = _text_rotation(textpage) if is_drawing else 0
                if not is_drawing and plan.image_coverage < 0.6:
                    plan.image_boxes = _image_boxes(page, width, height, settings.ocr_image_min_fraction)
                if is_drawing:
                    plan.title_block = _title_block_text(textpage, width, height)
                elif plan.chars >= settings.ocr_min_text_chars and ostats.may_hold_table:
                    if plumber_pdf is None:
                        import pdfplumber

                        plumber_pdf = pdfplumber.open(io.BytesIO(data))
                    try:
                        tables, outside_text = _tables_for_page(plumber_pdf, index)
                    except Exception:
                        log.debug("table extraction failed on page %s", index + 1, exc_info=True)
                        tables, outside_text = [], None
                    if tables:
                        plan.tables = tables
                        plan.text = outside_text or ""
                textpage.close()
            finally:
                page.close()
            plans.append(plan)
    finally:
        if plumber_pdf is not None:
            plumber_pdf.close()
        pdf.close()
    table_flow.link_continued_tables([p.tables for p in plans])
    _repair_glyphs(plans, styled)
    _apply_structure(plans, styled, properties, settings)
    return plans, properties


def _repair_glyphs(plans: list[PagePlan], styled: list[list[structure.StyledLine]]) -> None:
    """Work out the letters behind unnamed font glyphs (ligatures) once for the whole document, then restore them in the
    page text and in the typography lines (which must keep matching it)."""
    texts = [p.text for p in plans]
    if not any(ligatures.has_placeholders(t) for t in texts):
        return
    mapping = ligatures.infer_mapping(texts)
    for plan in plans:
        plan.text = ligatures.apply_mapping(plan.text, mapping)
    for lines in styled:
        for line in lines:
            line.text = ligatures.apply_mapping(line.text, mapping)
    log.debug("glyph codes resolved: %s", {hex(ord(k) - ligatures.PLACEHOLDER_BASE): v for k, v in mapping.items()})


def _is_deep(settings: Settings, pages: int) -> bool:
    return settings.pdf_layout == "deep" and settings.pdf_layout_min_pages <= pages <= settings.pdf_layout_max_pages


def _apply_structure(
    plans: list[PagePlan], styled: list[list[structure.StyledLine]], properties: dict[str, str], settings: Settings
) -> None:
    """Remove running headers/footers and number the pages; mark headings from the type sizes read from the PDF.

    In deep mode (long documents) the headings come from the layout engine afterwards, so here the text is left without
    heading markers and the typographic version is kept in `typo_text` as the fallback."""
    if settings.pdf_layout == "off":
        return
    try:
        found = structure.analyse(styled, drawing_pages={i for i, p in enumerate(plans) if p.is_drawing})
    except Exception:  # noqa: BLE001
        log.warning("document structure could not be analysed", exc_info=True)
        return
    if not found.body_size:
        return
    deep = _is_deep(settings, len(plans))
    for index, plan in enumerate(plans):
        if plan.is_drawing:
            continue
        plan.typo_text = structure.apply(plan.text, found)
        plan.text = structure.apply(plan.text, found, headings=not deep)
        plan.label = found.labels.get(index)
    if found.title:
        properties["title_guess"] = found.title
    properties["body_font_size"] = str(found.body_size)
    if found.furniture:
        properties["furniture"] = " | ".join(sorted(found.furniture))[:600]


def render_page_png(data: bytes, page_number: int, *, dpi: int = 200, max_pixels: int = MAX_RENDER_PIXELS) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[page_number - 1]
        width, height = page.get_size()
        scale = dpi / 72.0
        if width * height * scale * scale > max_pixels:
            scale = math.sqrt(max_pixels / (width * height))
        image = page.render(scale=scale).to_pil()
        page.close()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False)
        return buffer.getvalue()
    finally:
        pdf.close()


VISION_PROMPT = (
    "This image is one sheet of a construction drawing set. Return JSON only with these keys: "
    '"title", "drawing_number", "scale", "date", "summary" (2-4 sentences describing what '
    "the sheet shows: plan/section/detail, areas, levels, key notes and dimensions you can read). Use null for "
    "anything you cannot read. Do not guess numbers."
)


class PdfiumParser:
    """The local page router. The ingestion stage attaches the OCR and vision models, the OCR cache and the budget
    checker before calling parse_pdf (they are separate roles with their own providers)."""

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        self.ocr: Any = None
        self.ocr_label = "none"
        self.ocr_is_cloud = False
        self.vision: Any = None
        self.protect_kinds: tuple[str, ...] | None = None
        self.cache_get: OcrCacheGet | None = None
        self.cache_put: OcrCachePut | None = None
        self.take_budget: BudgetTake | None = None
        self.cad: Any = None  # rag.ingestion.cad_analysis.Options: how AutoCAD sheets are analysed (None: ordinary description)

    def attach(
        self,
        *,
        ocr: Any = None,
        ocr_label: str = "none",
        ocr_is_cloud: bool = False,
        vision: Any = None,
        cache_get: OcrCacheGet | None = None,
        cache_put: OcrCachePut | None = None,
        take_budget: BudgetTake | None = None,
        protect_kinds: tuple[str, ...] | None = None,
        cad: Any = None,
    ) -> None:
        self.ocr, self.ocr_label, self.ocr_is_cloud = ocr, ocr_label, ocr_is_cloud
        self.protect_kinds = protect_kinds
        self.vision = vision
        self.cad = cad
        self.cache_get, self.cache_put, self.take_budget = cache_get, cache_put, take_budget

    async def parse_pdf(self, data: bytes, filename: str) -> ParsedDocument:
        plans, properties = await run_pdfium(analyse_pdf, data, self.settings)
        warnings: list[str] = []
        await self._deepen_structure(data, plans, properties, warnings)
        pages: list[ParsedPage] = [await self._finish_page(data, plan, warnings) for plan in plans]
        return ParsedDocument(pages=pages, format="pdf", title=properties.get("title"), properties=properties, warnings=warnings)

    async def _deepen_structure(
        self, data: bytes, plans: list[PagePlan], properties: dict[str, str], warnings: list[str]
    ) -> None:
        """Mark the document's headings from the layout engine's outline (long documents, `PDF_LAYOUT=deep`).

        The engine only says where the sections are; the text stays pdfium's. Any failure, a timeout, or an outline that
        does not fit the text falls back to the headings found from type sizes."""
        s = self.settings
        if not _is_deep(s, len(plans)):
            return

        def fallback() -> None:
            for plan in plans:
                if plan.typo_text is not None:
                    plan.text = plan.typo_text

        readable = sum(1 for p in plans if not p.is_drawing and p.chars >= s.ocr_min_text_chars)
        if readable < max(3, len(plans) // 2):
            fallback()  # mostly scans or drawings: there is no typography to analyse
            return
        timeout = max(s.pdf_layout_timeout_s, 2.0 * len(plans))
        try:
            outline = await layout_runner.outline_of(data, timeout_s=timeout, processes=s.pdf_layout_processes)
        except Exception as exc:  # noqa: BLE001 - structure is an enrichment; the document must still be read
            warnings.append(f"layout analysis skipped ({type(exc).__name__})")
            log.warning("layout analysis failed", exc_info=True)
            fallback()
            return
        entries = [(e.level, e.title, e.start) for e in outline.entries]
        texts, found = structure.mark_outline_headings([p.text for p in plans], entries)
        if entries and found >= max(2, int(0.4 * len(entries))):
            for plan, text in zip(plans, texts, strict=True):
                if not plan.is_drawing:
                    plan.text = text
            properties["outline_source"] = outline.source
            properties["outline_entries"] = str(len(entries))
        else:
            fallback()
        if outline.title:
            properties["title_guess"] = outline.title

    async def _finish_page(self, data: bytes, plan: PagePlan, warnings: list[str]) -> ParsedPage:
        text_ok = plan.chars >= self.settings.ocr_min_text_chars and plan.garbage_ratio < 0.2
        mostly_image = plan.image_coverage >= 0.6 and plan.chars < 400
        method = "text_layer"
        text = plan.text if text_ok or plan.garbage_ratio < 0.2 else ""
        lines: list[TextLine] = []
        image_hash = None
        quality: dict[str, float] = {
            "chars": float(plan.chars),
            "garbage_ratio": plan.garbage_ratio,
            "image_coverage": plan.image_coverage,
        }

        if (not text_ok or mostly_image) and not plan.is_drawing:
            ocr = await self._ocr_page(data, plan.number, warnings)
            if ocr is not None:
                image_hash, result = ocr
                quality["ocr_confidence"] = result.confidence
                if text and text_ok:
                    method = "text_layer+ocr"
                    text = _merge_text(text, result.text)
                else:
                    method = "ocr"
                    text = result.text
                lines = result.lines
            elif not text:
                method = "empty"
        elif plan.is_drawing and plan.chars < self.settings.ocr_min_text_chars:
            # Scanned drawing: OCR the whole sheet so its notes and title block are searchable.
            ocr = await self._ocr_page(data, plan.number, warnings)
            if ocr is not None:
                image_hash, result = ocr
                method, text, lines = "ocr", result.text, result.lines
                quality["ocr_confidence"] = result.confidence
                plan.title_block = plan.title_block or parse_title_block(result.text)

        if (
            plan.is_drawing
            and self.vision is not None
            and not (self.protect_kinds is not None and has_sensitive(text, self.protect_kinds))
        ):
            described = await self._describe_drawing(data, plan, warnings)
            if described:
                summary = described.pop("summary", "")
                plan.title_block = {k: v for k, v in plan.title_block.items() if not _is_label_text(v)}
                for key, value in described.items():
                    if value and key not in plan.title_block:
                        plan.title_block[key] = str(value)
                if summary:
                    text = (text + "\n\n[Sheet description] " + summary).strip()
                    method = method + "+vision"

        if plan.image_boxes and method == "text_layer":
            extra = await self._ocr_regions(data, plan, warnings)
            if extra:
                text = _merge_text(text, extra) if text else extra
                method = "text_layer+image_ocr"

        language = detect_language(text + " " + " ".join(c for t in plan.tables for r in t.rows for c in r))
        return ParsedPage(
            number=plan.number,
            text=text,
            method=method,
            tables=plan.tables,
            lines=lines,
            width=plan.width,
            height=plan.height,
            language=language,
            is_drawing=plan.is_drawing,
            title_block=plan.title_block,
            quality=quality,
            image_hash=image_hash,
            label=plan.label,
        )

    async def _ocr_regions(self, data: bytes, plan: PagePlan, warnings: list[str]) -> str:
        """OCR the pictures embedded in a page that has a text layer; returns their text, one picture after another."""
        if self.ocr is None:
            return ""
        from PIL import Image

        png = await run_pdfium(
            render_page_png, data, plan.number, dpi=self.settings.ocr_render_dpi, max_pixels=self.settings.ocr_max_pixels
        )
        page_image = Image.open(io.BytesIO(png))
        width, height = page_image.size
        found: list[str] = []
        for x0, y0, x1, y1 in plan.image_boxes:
            crop = page_image.crop((int(x0 * width), int(y0 * height), math.ceil(x1 * width), math.ceil(y1 * height)))
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG")
            crop_png = buffer.getvalue()
            key = hashlib.sha256(crop_png).hexdigest()
            result = await self.cache_get(key, self.ocr_label) if self.cache_get is not None else None
            if result is None:
                if self.ocr_is_cloud and self.take_budget is not None and not await self.take_budget("cloud_ocr_pages", 1):
                    warnings.append(f"page {plan.number}: cloud OCR budget for today is used up")
                    continue
                result = await self.ocr.recognize(crop_png)
                if self.cache_put is not None:
                    await self.cache_put(key, self.ocr_label, result)
            if result.text.strip():
                found.append(result.text.strip())
        return "\n".join(found)

    async def _ocr_page(self, data: bytes, number: int, warnings: list[str]) -> tuple[str, OCRResult] | None:
        if self.ocr is None:
            warnings.append(f"page {number}: no text layer and OCR is off")
            return None
        png = await run_pdfium(
            render_page_png, data, number, dpi=self.settings.ocr_render_dpi, max_pixels=self.settings.ocr_max_pixels
        )
        image_hash = hashlib.sha256(png).hexdigest()
        if self.cache_get is not None:
            cached = await self.cache_get(image_hash, self.ocr_label)
            if cached is not None:
                return image_hash, cached
        if self.ocr_is_cloud and self.take_budget is not None and not await self.take_budget("cloud_ocr_pages", 1):
            warnings.append(f"page {number}: cloud OCR budget for today is used up")
            return None
        result = await self.ocr.recognize(png)
        if self.cache_put is not None:
            await self.cache_put(image_hash, self.ocr_label, result)
        return image_hash, result

    async def _describe_drawing(self, data: bytes, plan: PagePlan, warnings: list[str]) -> dict[str, Any] | None:
        if self.take_budget is not None and not await self.take_budget("vision_pages", 1):
            warnings.append(f"page {plan.number}: vision budget for today is used up")
            return None
        # large enough to read small notes (about 2,400 px on the long side), turned upright when the sheet is
        # landscape on a portrait page
        long_side_in = max(plan.width, plan.height) / 72.0
        png = await run_pdfium(
            render_page_png, data, plan.number, dpi=int(min(250, max(110, 2400 / long_side_in))), max_pixels=6_000_000
        )
        if plan.rotation:
            from PIL import Image

            upright = Image.open(io.BytesIO(png)).rotate(-plan.rotation, expand=True)
            buffer = io.BytesIO()
            upright.save(buffer, format="PNG")
            png = buffer.getvalue()
        message = ChatMessage(
            "user",
            [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image", "media_type": "image/png", "data": base64.b64encode(png).decode()},
            ],
        )
        try:
            for attempt in range(4):
                try:
                    result = await self.vision.complete([message], max_tokens=800, temperature=0.0)
                    break
                except (
                    RateLimited
                ) as exc:  # the provider asked us to slow down: wait as long as it says (up to 20 s), then try again
                    if attempt == 3:
                        raise
                    await asyncio.sleep(min(20.0, max(2.0, exc.retry_after)))
            raw = result.content
            parsed = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        except Exception as exc:  # noqa: BLE001 - a vision failure must not fail the document
            warnings.append(f"page {plan.number}: vision description failed ({type(exc).__name__})")
            return None
        if not isinstance(parsed, dict):
            return None
        mapping = {"drawing_number": "doc_number"}
        return {mapping.get(k, k): v for k, v in parsed.items() if v not in (None, "", "null")}

    async def aclose(self) -> None:
        return None


def _merge_text(layer: str, ocr: str) -> str:
    """Keep the text layer and add OCR lines it does not already contain."""
    known = {line.strip().lower() for line in layer.split("\n") if line.strip()}
    extra = [line for line in ocr.split("\n") if line.strip() and line.strip().lower() not in known]
    return layer + ("\n" + "\n".join(extra) if extra else "")


registry.register(
    Capability.PARSER, "local", local=True, description="pdfium text + pdfplumber tables + per-page OCR/vision routing"
)(PdfiumParser)
