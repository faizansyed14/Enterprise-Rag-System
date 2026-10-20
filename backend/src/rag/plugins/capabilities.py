"""What each kind of plugin must do. Providers implement these protocols; the rest of the code only sees them.

Every plugin method is async. CPU-bound local work (OCR, parsing) runs in a worker thread inside the plugin so the
event loop stays responsive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from rag.config.roles import RoleSpec

# ---- chat ------------------------------------------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}

    def anthropic_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    cached_input_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd


@dataclass
class ChatMessage:
    """Provider-neutral message. `content` is text, or a list of parts for images:
    [{"type": "text", "text": "..."}, {"type": "image", "media_type": "image/png", "data": "<base64>"}]."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = "stop"


@dataclass
class StreamChunk:
    """One streamed piece: text deltas while streaming, then exactly one final chunk with `result` set."""

    delta: str = ""
    result: ChatResult | None = None


@runtime_checkable
class ChatModel(Protocol):
    spec: RoleSpec
    supports_tools: bool
    supports_vision: bool

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> ChatResult: ...

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[StreamChunk]: ...

    async def aclose(self) -> None: ...


# ---- embeddings ------------------------------------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    spec: RoleSpec
    dims: int
    max_tokens: int
    multilingual: bool

    @property
    def slug(self) -> str:
        """Short stable name used in index collection names, e.g. 'local-multilingual-e5-small-384'."""
        ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


# ---- reranking -------------------------------------------------------------------------------------------------------


@runtime_checkable
class Reranker(Protocol):
    spec: RoleSpec
    languages: set[str] | None  # None means every language

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """Scores in [0, 1], aligned with `documents`."""
        ...

    async def aclose(self) -> None: ...


# ---- groundedness ----------------------------------------------------------------------------------------------------


@runtime_checkable
class GroundednessJudge(Protocol):
    spec: RoleSpec

    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        """Probability in [0, 1] that each hypothesis (second) is supported by its premise (first)."""
        ...

    async def aclose(self) -> None: ...


# ---- OCR and parsing -------------------------------------------------------------------------------------------------


@dataclass
class TextLine:
    text: str
    # Normalised box on the page: x0, y0, x1, y1 in [0, 1], origin top-left.
    box: tuple[float, float, float, float] | None = None
    confidence: float = 1.0


@dataclass
class OCRResult:
    text: str
    lines: list[TextLine] = field(default_factory=list)
    confidence: float = 0.0
    language: str | None = None


@runtime_checkable
class OCREngine(Protocol):
    spec: RoleSpec

    async def recognize(self, image_png: bytes) -> OCRResult: ...

    async def aclose(self) -> None: ...


@dataclass
class ParsedTable:
    rows: list[list[str]]
    header_rows: int = 1
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class ParsedPage:
    number: int  # 1-based
    text: str
    method: str  # text_layer | ocr | text_layer+ocr | vision | native | cloud
    tables: list[ParsedTable] = field(default_factory=list)
    lines: list[TextLine] = field(default_factory=list)
    width: float | None = None
    height: float | None = None
    language: str | None = None
    label: str | None = None  # "Sheet: Rates", "Slide 4", "Section 2" for formats without fixed pages
    is_drawing: bool = False
    title_block: dict[str, str] = field(default_factory=dict)
    quality: dict[str, float] = field(default_factory=dict)
    image_hash: str | None = None


@dataclass
class ParsedDocument:
    pages: list[ParsedPage]
    format: str  # pdf | docx | xlsx | pptx | email | html | text | image
    title: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    attachments: list[tuple[str, bytes]] = field(default_factory=list)  # e-mail attachments become child items
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class DocumentParser(Protocol):
    """Whole-document PDF parsers (local pdfium page router, LlamaParse, Azure Document Intelligence, Mistral)."""

    spec: RoleSpec

    async def parse_pdf(self, data: bytes, filename: str) -> ParsedDocument: ...

    async def aclose(self) -> None: ...
