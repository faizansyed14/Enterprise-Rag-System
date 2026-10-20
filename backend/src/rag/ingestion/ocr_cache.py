"""OCR results keyed by the rendered page image's SHA-256 and the OCR engine, shared by every worker.

The same scanned page often appears in many files (cover sheets, re-saved PDFs, the same scan filed in two
projects); it is read once.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from rag.db import schema
from rag.db.engine import transaction
from rag.plugins.capabilities import OCRResult, TextLine


async def cache_get(image_hash: str, engine: str) -> OCRResult | None:
    async with transaction() as conn:
        row = (await conn.execute(
            select(schema.ocr_cache.c.text, schema.ocr_cache.c.lines, schema.ocr_cache.c.confidence)
            .where(schema.ocr_cache.c.image_hash == image_hash, schema.ocr_cache.c.engine == engine)
        )).first()
    if row is None:
        return None
    lines = [TextLine(text=item["text"], box=tuple(item["box"]) if item.get("box") else None,
                      confidence=item.get("confidence", 1.0)) for item in row.lines or []]
    return OCRResult(text=row.text, lines=lines, confidence=row.confidence)


async def cache_put(image_hash: str, engine: str, result: OCRResult) -> None:
    lines = [{"text": line.text, "box": list(line.box) if line.box else None, "confidence": line.confidence}
             for line in result.lines]
    async with transaction() as conn:
        await conn.execute(
            insert(schema.ocr_cache)
            .values(image_hash=image_hash, engine=engine, text=result.text, lines=lines, confidence=result.confidence)
            .on_conflict_do_nothing()
        )
