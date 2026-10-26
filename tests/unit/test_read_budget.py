"""Reading up to 40 documents in full within one budget, and vision OCR that waits out rate limits."""
import asyncio
from types import SimpleNamespace

from rag.answering.strategy import fair_shares
from rag.errors import RateLimited
from rag.plugins.capabilities import OCRResult
from rag.providers.ocr import vision as vision_module
from rag.providers.ocr.vision import VisionOCR


def test_short_documents_are_read_whole_and_long_ones_share_the_rest():
    sizes = {f"licence{i}": 2_000 for i in range(38)} | {"contract": 300_000, "lease": 90_000}
    shares = fair_shares(sizes, 400_000)
    assert all(shares[f"licence{i}"] == 2_000 for i in range(38))  # every short one complete
    assert shares["lease"] + shares["contract"] <= 400_000 - 38 * 2_000
    assert shares["lease"] == 90_000  # fits its fair share of what is left, so it is whole as well
    assert shares["contract"] == 400_000 - 38 * 2_000 - 90_000  # the long one gets what remains
    assert sum(shares.values()) <= 400_000


def test_the_budget_is_never_exceeded_and_equal_documents_get_equal_shares():
    shares = fair_shares({f"d{i}": 50_000 for i in range(40)}, 400_000)
    assert set(shares.values()) == {10_000}
    assert sum(shares.values()) == 400_000
    assert fair_shares({}, 100) == {}
    assert fair_shares({"a": 10}, 0) == {"a": 0}


def _hub(**over):
    settings = SimpleNamespace(vision_concurrency=2, vision_retries=2, privacy_mask_for_models=False)
    for key, value in over.items():
        setattr(settings, key, value)
    return settings


class _Vision:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimited("openrouter returned HTTP 429: Provider returned error")
        return SimpleNamespace(content="Licence No. 123")


class _Hub:
    def __init__(self, vision, settings):
        self._vision, self.settings = vision, settings
        self.local_reads = 0

    def spec(self, role):
        return SimpleNamespace(role=role)

    def vision(self):
        return self._vision

    def get(self, role, choice=None):
        hub = self

        class Local:
            async def recognize(self, png):
                hub.local_reads += 1
                return OCRResult(text="local text", lines=[], confidence=0.5)

        return Local()


def _png() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(out, format="PNG")
    return out.getvalue()


def _recognize(vision, monkeypatch, **settings):
    waits = []

    async def no_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(vision_module.asyncio, "sleep", no_sleep)
    hub = _Hub(vision, _hub(**settings))
    result = asyncio.run(VisionOCR(hub).recognize(_png()))
    return result, hub, waits


def test_a_rate_limited_page_is_tried_again_and_keeps_the_vision_reading(monkeypatch):
    vision = _Vision(failures=2)
    result, hub, waits = _recognize(vision, monkeypatch)
    assert result.text == "Licence No. 123" and vision.calls == 3
    assert hub.local_reads == 0  # the page was not downgraded to the local OCR
    assert len(waits) == 2 and waits[0] < waits[1]  # waits grow


def test_a_page_that_stays_rate_limited_falls_back_to_the_local_ocr_after_the_retries(monkeypatch):
    vision = _Vision(failures=99)
    result, hub, waits = _recognize(vision, monkeypatch, vision_retries=2)
    assert vision.calls == 3 and result.text == "local text" and hub.local_reads == 1
    assert len(waits) == 2


def test_no_retries_means_one_try(monkeypatch):
    vision = _Vision(failures=1)
    result, hub, waits = _recognize(vision, monkeypatch, vision_retries=0)
    assert vision.calls == 1 and result.text == "local text" and not waits


# ---- the second vision model -------------------------------------------------------------------------------------------
class _Model:
    def __init__(self, label, outcome):
        self.spec = SimpleNamespace(label=label)
        self.outcome, self.calls = outcome, 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(content=self.outcome)


def test_a_429_from_the_first_vision_model_goes_to_the_second():
    first = _Model("openrouter/google/gemini-2.5-flash-lite", RateLimited("429"))
    second = _Model("openrouter/anthropic/claude-haiku-5.5", "page text")
    answer = asyncio.run(vision_module.FallbackVision(first, second).complete(["m"], max_tokens=10))
    assert answer.content == "page text" and (first.calls, second.calls) == (1, 1)


def test_the_second_model_is_not_called_when_the_first_works():
    first, second = _Model("a", "fine"), _Model("b", "unused")
    assert asyncio.run(vision_module.FallbackVision(first, second).complete(["m"])).content == "fine"
    assert second.calls == 0


def test_when_both_fail_the_error_reaches_the_caller_and_a_missing_key_is_not_hidden():
    from rag.errors import ConfigurationError

    both = vision_module.FallbackVision(_Model("a", RateLimited("429")), _Model("b", RateLimited("also 429")))
    try:
        asyncio.run(both.complete(["m"]))
        raise AssertionError("expected RateLimited")
    except RateLimited as exc:
        assert "also 429" in str(exc)
    broken = vision_module.FallbackVision(_Model("a", ConfigurationError("no key")), _Model("b", "unused"))
    try:
        asyncio.run(broken.complete(["m"]))
        raise AssertionError("expected ConfigurationError")
    except ConfigurationError:
        pass


def test_other_attributes_come_from_the_first_model():
    first = _Model("openrouter/google/gemini-2.5-flash-lite", "x")
    wrapped = vision_module.FallbackVision(first, _Model("b", "y"))
    assert wrapped.spec.label == "openrouter/google/gemini-2.5-flash-lite"
