"""Wrappers that record every model call (role, provider, model, duration, status, error, tokens, cost, trace id).

The registry wraps each plugin it creates, so providers never deal with monitoring themselves and no call can be
missed. Attributes the wrappers do not intercept (dims, slug, languages, supports_tools ...) pass straight through.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.monitoring.tracing import span
from rag.plugins.capabilities import ChatMessage, ChatResult, StreamChunk, ToolSpec
from rag.privacy.shield import MASKED_VALUES


class _Metered:
    def __init__(self, inner: Any, spec: RoleSpec) -> None:
        self._inner = inner
        self._spec = spec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def inner(self) -> Any:
        return self._inner

    def _span(self, name: str, **attrs: Any):
        return span(name, self._spec.capability.value, **attrs)

    def _label(self, record: Any) -> None:
        record.role = self._spec.role
        record.provider = self._spec.provider
        record.model = self._spec.model

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


class MeteredChat(_Metered):
    async def complete(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] | None = None,
                       **kwargs: Any) -> ChatResult:
        async with self._span("chat.complete", tools=len(tools or [])) as record:
            self._label(record)
            record.attrs["masked_values"] = MASKED_VALUES.get()  # None: no privacy shield on this call
            result: ChatResult = await self._inner.complete(messages, tools=tools, **kwargs)
            record.tokens_in = result.usage.input_tokens
            record.tokens_out = result.usage.output_tokens
            record.cost_usd = result.usage.cost_usd
            record.attrs["tool_calls"] = len(result.tool_calls)
            record.attrs["finish_reason"] = result.finish_reason
            return result

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        async with self._span("chat.stream") as record:
            self._label(record)
            record.attrs["masked_values"] = MASKED_VALUES.get()
            start = time.perf_counter()
            first = True
            async for chunk in self._inner.stream(messages, **kwargs):
                if first and chunk.delta:
                    record.attrs["ttft_ms"] = round((time.perf_counter() - start) * 1000, 1)
                    first = False
                if chunk.result is not None:
                    record.tokens_in = chunk.result.usage.input_tokens
                    record.tokens_out = chunk.result.usage.output_tokens
                    record.cost_usd = chunk.result.usage.cost_usd
                    record.attrs["finish_reason"] = chunk.result.finish_reason
                yield chunk


class MeteredEmbedder(_Metered):
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        async with self._span("embed.documents") as record:
            self._label(record)
            record.items = len(texts)
            return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        async with self._span("embed.query") as record:
            self._label(record)
            return await self._inner.embed_query(text)


class MeteredReranker(_Metered):
    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        async with self._span("rerank") as record:
            self._label(record)
            record.items = len(documents)
            return await self._inner.rerank(query, documents)


class MeteredGroundedness(_Metered):
    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        async with self._span("groundedness") as record:
            self._label(record)
            record.items = len(pairs)
            return await self._inner.score(pairs)


class MeteredOCR(_Metered):
    async def recognize(self, image_png: bytes) -> Any:
        async with self._span("ocr") as record:
            self._label(record)
            result = await self._inner.recognize(image_png)
            record.attrs["chars"] = len(result.text)
            record.attrs["confidence"] = round(result.confidence, 3)
            return result


class MeteredParser(_Metered):
    async def parse_pdf(self, data: bytes, filename: str) -> Any:
        async with self._span("parse.pdf", bytes=len(data)) as record:
            self._label(record)
            doc = await self._inner.parse_pdf(data, filename)
            record.items = len(doc.pages)
            return doc


_WRAPPERS: dict[Capability, type[_Metered]] = {
    Capability.CHAT: MeteredChat,
    Capability.VISION: MeteredChat,
    Capability.EMBEDDER: MeteredEmbedder,
    Capability.RERANKER: MeteredReranker,
    Capability.GROUNDEDNESS: MeteredGroundedness,
    Capability.OCR: MeteredOCR,
    Capability.PARSER: MeteredParser,
}


def wrap(plugin: Any, spec: RoleSpec) -> Any:
    wrapper = _WRAPPERS.get(spec.capability)
    return wrapper(plugin, spec) if wrapper else plugin


def unwrap(plugin: Any) -> Any:
    return plugin.inner if isinstance(plugin, _Metered) else plugin
