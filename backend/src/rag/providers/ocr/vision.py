"""`vision/default`: read scanned pages, tables and pictures with the VISION model instead of the local OCR.

A vision model (Gemini Flash, Haiku, GPT-4o mini ...) reads a page the way a person does: reading order, headings, handwriting,
stamps, and above all tables, which come back as Markdown tables that the chunker turns into labelled rows. Local OCR returns
loose lines and loses table structure. The price is a model call per page, so it is a choice (`OCR=vision/default`, or Change on
the Scanned pages row of the Models page), and the daily page budget (`CLOUD_OCR_PAGES_PER_DAY`) applies.

It plugs in where the local OCR does: the PDF reader calls `recognize(png)` for scanned pages, for pictures inside text
pages and for uploaded images, so nothing else changes. If the model fails or answers nothing, the local OCR reads the page,
so a model outage never loses a page.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
from typing import Any

from rag.config.roles import Capability
from rag.errors import ConfigurationError, RateLimited
from rag.plugins.capabilities import ChatMessage, OCRResult
from rag.plugins.registry import registry
from rag.privacy.shield import bank_kinds, has_sensitive

log = logging.getLogger(__name__)

MAX_PIXELS = 4_000_000  # larger pages are scaled down: more pixels cost more tokens and rarely read better
MAX_OUTPUT_TOKENS = 6000

PROMPT = (
    "Transcribe this page into Markdown, in reading order. Rules:\n"
    "- Copy the text exactly as written. Do not translate, summarise, correct or add anything.\n"
    "- Headings become lines starting with # (use ## and ### for lower levels).\n"
    "- Write every table as a Markdown pipe table: a header row, a separator row, then one line per row. Keep every cell; "
    "repeat the value of a merged cell or leave it empty.\n"
    "- For a picture, chart, diagram or stamp write one line: "
    "[Figure: what it shows, with the labels and numbers you can read].\n"
    "- Keep handwritten text; mark a word you cannot read as [illegible].\n"
    "- Leave out page numbers, running headers and footers.\n"
    "Output only the Markdown, with no commentary."
)


def shrink(png: bytes, max_pixels: int = MAX_PIXELS) -> bytes:
    """The same picture scaled down so that it has at most `max_pixels` pixels (unchanged when it already fits)."""
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    width, height = image.size
    if width * height <= max_pixels:
        return png
    scale = (max_pixels / (width * height)) ** 0.5
    image = image.convert("RGB").resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def strip_fence(text: str) -> str:
    """Models often wrap the answer in a ```markdown fence; the page text is what is inside it."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    return body.strip()


def _name(model: Any) -> str:
    spec = getattr(model, "spec", None)
    return getattr(spec, "label", None) or type(model).__name__


class FallbackVision:
    """The vision model, with a second one behind it: when the first fails (most often HTTP 429, the provider's rate limit)
    the same request goes to the second, so the page keeps a vision reading instead of dropping to the local OCR.

    The hub returns this from `vision()` when VISION_FALLBACK is set, so page OCR and drawing descriptions both get it. Both
    models are metered and privacy-shielded like any other model call; every other attribute is the first model's."""

    def __init__(self, primary: Any, backup: Any) -> None:
        self.primary = primary
        self.backup = backup

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)

    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        try:
            return await self.primary.complete(messages, **kwargs)
        except ConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure of the first model is a reason to try the second
            log.warning("vision model %s failed (%s); trying %s", _name(self.primary), type(exc).__name__, _name(self.backup))
            return await self.backup.complete(messages, **kwargs)


class VisionOCR:
    """OCR through the VISION role. Built by the model hub, which gives it access to the vision model and the local OCR."""

    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.spec = hub.spec("OCR")
        # One of these per worker process: the provider answers HTTP 429 when too many pages arrive at once.
        self._slots = asyncio.Semaphore(max(1, hub.settings.vision_concurrency))

    async def recognize(self, image_png: bytes) -> OCRResult:
        vision = self.hub.vision()
        if vision is None:
            log.warning(
                "OCR is set to the vision model but 'Drawing and image reading' is off; reading the page with the local OCR"
            )
            return await self._fallback(image_png)
        settings = self.hub.settings
        if settings.privacy_mask_for_models:
            # an image cannot be masked: read the page locally first and keep it away from the model if it holds such a value
            local = await self._fallback(image_png)
            if has_sensitive(local.text, bank_kinds(settings.privacy_kind_list)):
                log.info("a page holds a bank or id number; it was read locally and not sent to the vision model")
                return local
        try:
            message = ChatMessage(
                "user",
                [
                    {"type": "text", "text": PROMPT},
                    {"type": "image", "media_type": "image/png", "data": base64.b64encode(shrink(image_png)).decode()},
                ],
            )
            result = await self._complete(vision, message)
            text = strip_fence(result.content or "")
            if text:
                return OCRResult(text=text, lines=[], confidence=0.9)
            log.warning("the vision model returned no text for a page; reading it with the local OCR")
        except Exception as exc:  # noqa: BLE001 - never lose a page because the model failed
            log.warning("vision OCR failed (%s); reading the page with the local OCR", type(exc).__name__)
        return await self._fallback(image_png)

    async def _complete(self, vision: Any, message: ChatMessage) -> Any:
        """One page to the vision model: a few at a time, and after a 429 (rate limited) wait and try again, with growing
        waits, before giving up (the page is then read with the local OCR)."""
        tries = max(0, self.hub.settings.vision_retries) + 1
        for attempt in range(tries):
            try:
                async with self._slots:
                    return await vision.complete([message], max_tokens=MAX_OUTPUT_TOKENS, temperature=0.0)
            except RateLimited:
                if attempt == tries - 1:
                    raise
                wait = min(20.0, 3.0 * 2**attempt) * (1 + 0.3 * random.random())  # 3 s, 6 s, 12 s ... plus a little jitter
                log.info("the vision model is rate limited; trying this page again in %.0f s", wait)
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _fallback(self, image_png: bytes) -> OCRResult:
        local = self.hub.get("OCR", "local/rapidocr")
        if local is None:
            return OCRResult(text="", lines=[], confidence=0.0)
        return await local.recognize(image_png)

    async def aclose(self) -> None:
        return None


def _factory(spec: Any, settings: Any) -> Any:  # pragma: no cover - the hub builds VisionOCR itself (it needs the hub)
    raise ConfigurationError("OCR=vision/default is built by the model hub")


registry.register(
    Capability.OCR,
    "vision",
    description="The vision model reads the page: tables, handwriting and pictures too (a model call per page)",
)(_factory)
