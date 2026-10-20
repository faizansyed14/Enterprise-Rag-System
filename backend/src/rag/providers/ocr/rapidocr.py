"""`local/rapidocr`: RapidOCR (PP-OCR models on ONNX Runtime, bundled in the wheel; no downloads, no network).

Recognised boxes are grouped into reading-order lines, and their positions are kept (normalised to the page) so
the citation viewer can highlight a quote on a scanned page.
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import ConfigurationError
from rag.plugins.capabilities import OCRResult, TextLine
from rag.plugins.registry import registry
from rag.text.ocr_spacing import restore_spaces

_ID_O_FIX = re.compile(r"\b([A-Z]{1,5}-)([0-9O]{2,})\b")


def fix_ocr_identifiers(text: str) -> str:
    """'SI-O14' -> 'SI-014': the letter O inside the number part of a code is almost always a zero."""
    return _ID_O_FIX.sub(lambda m: m.group(1) + m.group(2).replace("O", "0")
                         if any(ch.isdigit() for ch in m.group(2)) else m.group(0), text)


def group_lines(items: list[tuple[list[list[float]], str, float]], width: float, height: float, *,
                spacing: bool = True) -> list[TextLine]:
    """Merge word/phrase boxes that share a baseline into lines, top to bottom, left to right."""
    boxes = []
    for points, text, score in items:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        boxes.append((min(xs), min(ys), max(xs), max(ys), text, float(score)))
    boxes.sort(key=lambda b: ((b[1] + b[3]) / 2, b[0]))
    lines: list[list[tuple[float, float, float, float, str, float]]] = []
    for box in boxes:
        if lines:
            last = lines[-1]
            top = min(b[1] for b in last)
            bottom = max(b[3] for b in last)
            overlap = min(bottom, box[3]) - max(top, box[1])
            if overlap > 0.5 * min(bottom - top, box[3] - box[1]):
                last.append(box)
                continue
        lines.append([box])
    out: list[TextLine] = []
    for line in lines:
        line.sort(key=lambda b: b[0])
        text = " ".join(b[4] for b in line)
        x0, y0 = min(b[0] for b in line), min(b[1] for b in line)
        x1, y1 = max(b[2] for b in line), max(b[3] for b in line)
        conf = sum(b[5] for b in line) / len(line)
        text = restore_spaces(text) if spacing else text
        out.append(TextLine(text=fix_ocr_identifiers(text),
                            box=(x0 / width, y0 / height, x1 / width, y1 / height), confidence=round(conf, 4)))
    return out


class RapidOCREngine:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._engine: Any = None
        self.spacing = spec.options.get("spacing", "on") != "off"
        self._lock = threading.Lock()
        self._sem = asyncio.Semaphore(1)

    def _get(self) -> Any:
        with self._lock:
            if self._engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError as exc:  # pragma: no cover - depends on the optional extra
                    raise ConfigurationError("OCR=local/rapidocr needs the 'ocr' extra: uv sync --extra ocr") from exc
                self._engine = RapidOCR()
            return self._engine

    def _run(self, image_png: bytes) -> OCRResult:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(image_png)).convert("RGB")
        width, height = image.size
        result, _ = self._get()(np.asarray(image))
        lines = group_lines([r[:3] for r in result or []], float(width), float(height), spacing=self.spacing)
        confidence = sum(line.confidence for line in lines) / len(lines) if lines else 0.0
        return OCRResult(text="\n".join(line.text for line in lines), lines=lines, confidence=round(confidence, 4))

    async def recognize(self, image_png: bytes) -> OCRResult:
        async with self._sem:
            return await asyncio.to_thread(self._run, image_png)

    async def aclose(self) -> None:
        return None


registry.register(Capability.OCR, "local", local=True,
                  description="RapidOCR on ONNX Runtime (bundled models; English and Chinese scripts)")(RapidOCREngine)
