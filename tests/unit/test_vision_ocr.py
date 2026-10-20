from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from PIL import Image

from rag.plugins.capabilities import OCRResult
from rag.providers.ocr.vision import VisionOCR, shrink, strip_fence


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _Vision:
    def __init__(self, content: str | Exception) -> None:
        self.content, self.calls = content, []

    async def complete(self, messages, max_tokens, temperature):
        self.calls.append((messages, max_tokens, temperature))
        if isinstance(self.content, Exception):
            raise self.content
        return SimpleNamespace(content=self.content)


class _Local:
    def __init__(self, text: str = "local words") -> None:
        self.text = text

    async def recognize(self, png: bytes) -> OCRResult:
        return OCRResult(text=self.text, lines=[], confidence=0.7)


class _Hub:
    def __init__(self, vision, local_text: str = "local words", privacy: bool = True) -> None:
        self._vision = vision
        self._local = _Local(local_text)
        self.settings = SimpleNamespace(privacy_mask_for_models=privacy, privacy_kind_list=("iban", "account", "card"))

    def spec(self, role):
        return SimpleNamespace(label="vision/default", is_off=False)

    def vision(self):
        return self._vision

    def get(self, role, line=None):
        return self._local


def test_the_page_comes_back_as_the_markdown_the_model_wrote_with_its_fence_removed() -> None:
    markdown = "```markdown\n# Costs\n\n| Item | Qty |\n|---|---|\n| Cement | 12 |\n```"
    vision = _Vision(markdown)
    result = asyncio.run(VisionOCR(_Hub(vision)).recognize(_png(100, 100)))
    assert result.text.startswith("# Costs") and "| Cement | 12 |" in result.text and "```" not in result.text
    assert vision.calls[0][1] >= 4000 and vision.calls[0][2] == 0.0  # room for a whole page, no creativity


def test_a_failing_or_empty_model_leaves_the_page_to_the_local_ocr() -> None:
    for answer in (RuntimeError("503"), "   "):
        result = asyncio.run(VisionOCR(_Hub(_Vision(answer))).recognize(_png(50, 50)))
        assert result.text == "local words"


def test_large_pages_are_scaled_down_and_small_ones_are_left_alone() -> None:
    small = _png(800, 600)
    assert shrink(small) == small
    big = shrink(_png(4000, 3000), max_pixels=1_000_000)
    width, height = Image.open(io.BytesIO(big)).size
    assert width * height <= 1_000_000 and abs(width / height - 4 / 3) < 0.02


def test_strip_fence_leaves_plain_text_alone() -> None:
    assert strip_fence("plain text") == "plain text" and strip_fence("```\nabc\n```") == "abc"


def test_with_no_vision_model_chosen_the_page_is_still_read_by_the_local_ocr() -> None:
    assert asyncio.run(VisionOCR(_Hub(None)).recognize(_png(40, 40))).text == "local words"


def test_a_page_holding_an_account_number_is_never_sent_to_the_vision_model() -> None:
    vision = _Vision("# should not be used")
    page = "Pay to the bank. Account Number: 0123456789"
    result = asyncio.run(VisionOCR(_Hub(vision, local_text=page)).recognize(_png(60, 60)))
    assert result.text == page and vision.calls == []   # read locally; the model was not called


def test_with_the_protection_switched_off_the_model_reads_every_page() -> None:
    vision = _Vision("# model text")
    page = "Account Number: 0123456789"
    result = asyncio.run(VisionOCR(_Hub(vision, local_text=page, privacy=False)).recognize(_png(60, 60)))
    assert result.text == "# model text" and len(vision.calls) == 1
