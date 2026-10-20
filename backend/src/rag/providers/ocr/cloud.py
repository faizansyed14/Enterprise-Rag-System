"""Cloud OCR for single page images: Azure AI Document Intelligence (`azure_di/prebuilt-read`) and Mistral OCR
(`mistral/mistral-ocr-latest`). Both read Arabic. Page budgets (CLOUD_OCR_PAGES_PER_DAY) are enforced by the page
router before it calls these.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import TransientError
from rag.plugins.capabilities import OCRResult, TextLine
from rag.plugins.registry import registry
from rag.providers.http import check_response, make_client, with_retries

AZURE_DI_API_VERSION = "2024-11-30"


def _polygon_box(polygon: list[float], width: float, height: float) -> tuple[float, float, float, float] | None:
    if not polygon or not width or not height:
        return None
    xs, ys = polygon[0::2], polygon[1::2]
    return (min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height)


async def azure_analyze(client: Any, model: str, data: bytes, content_type: str, *, poll_s: float = 1.0,
                        max_wait_s: float = 300.0) -> dict[str, Any]:
    """Submit to Document Intelligence and poll the operation until it finishes."""
    path = f"/documentintelligence/documentModels/{model}:analyze?api-version={AZURE_DI_API_VERSION}"

    async def submit() -> str:
        response = await client.post(path, content=data, headers={"Content-Type": content_type})
        check_response(response, "azure_di")
        location = response.headers.get("operation-location")
        if not location:
            raise TransientError("azure_di did not return an operation location")
        return location

    location = await with_retries(submit, provider="azure_di")
    waited = 0.0
    while waited < max_wait_s:
        await asyncio.sleep(poll_s)
        waited += poll_s
        result = check_response(await client.get(location), "azure_di").json()
        status = result.get("status")
        if status == "succeeded":
            return result.get("analyzeResult") or {}
        if status == "failed":
            raise TransientError(f"azure_di analysis failed: {str(result.get('error'))[:200]}")
        poll_s = min(poll_s * 1.5, 5.0)
    raise TransientError("azure_di analysis timed out")


class AzureDIOCR:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._client = make_client(settings.azure_di_endpoint.rstrip("/"), timeout=120,
                                   headers={"Ocp-Apim-Subscription-Key": settings.secret("azure_di_key") or ""})

    async def recognize(self, image_png: bytes) -> OCRResult:
        result = await azure_analyze(self._client, self.spec.model or "prebuilt-read", image_png, "image/png")
        page = (result.get("pages") or [{}])[0]
        width, height = float(page.get("width") or 0), float(page.get("height") or 0)
        lines = [TextLine(text=ln.get("content", ""), box=_polygon_box(ln.get("polygon") or [], width, height))
                 for ln in page.get("lines") or []]
        words = page.get("words") or []
        confidence = sum(w.get("confidence", 0) for w in words) / len(words) if words else 0.0
        text = "\n".join(line.text for line in lines) or result.get("content", "")
        return OCRResult(text=text, lines=lines, confidence=round(confidence, 4))

    async def aclose(self) -> None:
        await self._client.aclose()


class MistralOCR:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._client = make_client(settings.mistral_base_url.rstrip("/"), timeout=120,
                                   headers={"Authorization": f"Bearer {settings.secret('mistral_api_key')}"})

    async def recognize(self, image_png: bytes) -> OCRResult:
        body = {"model": self.spec.model or "mistral-ocr-latest",
                "document": {"type": "image_url",
                             "image_url": "data:image/png;base64," + base64.b64encode(image_png).decode()}}

        async def call() -> dict[str, Any]:
            return check_response(await self._client.post("/v1/ocr", json=body), "mistral").json()

        data = await with_retries(call, provider="mistral")
        pages = data.get("pages") or []
        text = "\n\n".join(p.get("markdown", "") for p in pages)
        return OCRResult(text=text, lines=[TextLine(text=t) for t in text.split("\n") if t.strip()], confidence=0.9)

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register(Capability.OCR, "azure_di", requires=("azure_di_endpoint", "azure_di_key"),
                  description="Azure AI Document Intelligence Read (Arabic and handwriting)")(AzureDIOCR)
registry.register(Capability.OCR, "mistral", requires=("mistral_api_key",),
                  description="Mistral OCR")(MistralOCR)
