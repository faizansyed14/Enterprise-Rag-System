"""Managed PDF parsers for the PARSER role: LlamaParse (`llamaparse/default`), Azure AI Document Intelligence layout
(`azure_di/prebuilt-layout`) and Mistral OCR (`mistral/mistral-ocr-latest`). Each returns per-page text and tables in
the same shape as the local page router, so everything downstream is unchanged."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import PermanentError, TransientError
from rag.plugins.capabilities import ParsedDocument, ParsedPage, ParsedTable
from rag.plugins.registry import registry
from rag.providers.http import check_response, make_client, with_retries
from rag.providers.ocr.cloud import azure_analyze
from rag.text.language import detect_language
from rag.text.normalize import clean_text


class LlamaParseParser:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._client = make_client(settings.llama_cloud_base_url.rstrip("/"), timeout=120,
                                   headers={"Authorization": f"Bearer {settings.secret('llama_cloud_api_key')}"})
        self.max_wait_s = float(spec.options.get("max_wait_s", 1800))

    def attach(self, **_: Any) -> None:
        return None

    async def parse_pdf(self, data: bytes, filename: str) -> ParsedDocument:
        async def upload() -> str:
            response = await self._client.post("/api/v1/parsing/upload",
                                               files={"file": (filename or "document.pdf", data, "application/pdf")})
            return str(check_response(response, "llamaparse").json()["id"])

        job_id = await with_retries(upload, provider="llamaparse")
        waited, delay = 0.0, 2.0
        while waited < self.max_wait_s:
            await asyncio.sleep(delay)
            waited += delay
            status = check_response(await self._client.get(f"/api/v1/parsing/job/{job_id}"), "llamaparse").json()
            state = str(status.get("status", "")).upper()
            if state == "SUCCESS":
                break
            if state in {"ERROR", "CANCELED", "CANCELLED"}:
                raise PermanentError(f"LlamaParse could not parse the file: {status.get('error_message', state)}")
            delay = min(delay * 1.5, 15.0)
        else:
            raise TransientError("LlamaParse job did not finish in time")
        result = check_response(await self._client.get(f"/api/v1/parsing/job/{job_id}/result/json"),
                                "llamaparse").json()
        pages = []
        for i, page in enumerate(result.get("pages") or [], start=1):
            text = clean_text(page.get("md") or page.get("text") or "")
            pages.append(ParsedPage(number=int(page.get("page") or i), text=text, method="cloud",
                                    language=detect_language(text)))
        return ParsedDocument(pages=pages, format="pdf")

    async def aclose(self) -> None:
        await self._client.aclose()


class AzureLayoutParser:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._client = make_client(settings.azure_di_endpoint.rstrip("/"), timeout=300,
                                   headers={"Ocp-Apim-Subscription-Key": settings.secret("azure_di_key") or ""})

    def attach(self, **_: Any) -> None:
        return None

    async def parse_pdf(self, data: bytes, filename: str) -> ParsedDocument:
        result = await azure_analyze(self._client, self.spec.model or "prebuilt-layout", data, "application/pdf",
                                     max_wait_s=1800)
        tables_by_page: dict[int, list[ParsedTable]] = {}
        for table in result.get("tables") or []:
            rows: list[list[str]] = [[""] * int(table.get("columnCount", 0)) for _ in range(int(table.get("rowCount", 0)))]
            header_rows = 0
            for cell in table.get("cells") or []:
                r, c = int(cell.get("rowIndex", 0)), int(cell.get("columnIndex", 0))
                if r < len(rows) and c < len(rows[r]):
                    rows[r][c] = clean_text(cell.get("content", ""))
                if cell.get("kind") == "columnHeader":
                    header_rows = max(header_rows, r + 1)
            region = (table.get("boundingRegions") or [{"pageNumber": 1}])[0]
            tables_by_page.setdefault(int(region.get("pageNumber", 1)), []).append(
                ParsedTable(rows=rows, header_rows=header_rows or 1))
        pages = []
        for page in result.get("pages") or []:
            number = int(page.get("pageNumber", len(pages) + 1))
            text = clean_text("\n".join(line.get("content", "") for line in page.get("lines") or []))
            pages.append(ParsedPage(number=number, text=text, method="cloud", tables=tables_by_page.get(number, []),
                                    width=page.get("width"), height=page.get("height"),
                                    language=detect_language(text)))
        return ParsedDocument(pages=pages, format="pdf")

    async def aclose(self) -> None:
        await self._client.aclose()


class MistralPdfParser:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self._client = make_client(settings.mistral_base_url.rstrip("/"), timeout=600,
                                   headers={"Authorization": f"Bearer {settings.secret('mistral_api_key')}"})

    def attach(self, **_: Any) -> None:
        return None

    async def parse_pdf(self, data: bytes, filename: str) -> ParsedDocument:
        body = {"model": self.spec.model or "mistral-ocr-latest",
                "document": {"type": "document_url",
                             "document_url": "data:application/pdf;base64," + base64.b64encode(data).decode()}}

        async def call() -> dict[str, Any]:
            return check_response(await self._client.post("/v1/ocr", json=body), "mistral").json()

        result = await with_retries(call, provider="mistral")
        pages = []
        for page in result.get("pages") or []:
            text = clean_text(page.get("markdown", ""))
            pages.append(ParsedPage(number=int(page.get("index", len(pages))) + 1, text=text, method="cloud",
                                    language=detect_language(text)))
        return ParsedDocument(pages=pages, format="pdf")

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register(Capability.PARSER, "llamaparse", requires=("llama_cloud_api_key",),
                  description="LlamaParse (LlamaIndex cloud)")(LlamaParseParser)
registry.register(Capability.PARSER, "azure_di", requires=("azure_di_endpoint", "azure_di_key"),
                  description="Azure AI Document Intelligence layout (tables, reading order, Arabic)")(AzureLayoutParser)
registry.register(Capability.PARSER, "mistral", requires=("mistral_api_key",),
                  description="Mistral OCR for whole PDFs")(MistralPdfParser)
