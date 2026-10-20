"""Cohere embeddings (`cohere/embed-v4.0`, `cohere/embed-multilingual-v3.0`). OpenAI-style embedders live in
providers/llm/openai_compat.py next to the chat adapter that shares their authentication."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.registry import registry
from rag.providers.http import check_response, make_client, with_retries

_COHERE_DIMS = {"embed-v4.0": 1536, "embed-multilingual-v3.0": 1024, "embed-english-v3.0": 1024,
                "embed-multilingual-light-v3.0": 384}


class CohereEmbedder:
    multilingual = True

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        self.dims = spec.dims or _COHERE_DIMS.get(spec.model, 1024)
        self.max_tokens = 512
        self._client = make_client(settings.cohere_base_url.rstrip("/"), timeout=60,
                                   headers={"Authorization": f"Bearer {settings.secret('cohere_api_key')}"})

    @property
    def slug(self) -> str:
        return f"cohere-{self.spec.model}-{self.dims}".lower()

    async def _embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 96):
            body: dict[str, Any] = {"model": self.spec.model, "texts": list(texts[i: i + 96]),
                                    "input_type": input_type, "embedding_types": ["float"]}
            if self.spec.dims:
                body["output_dimension"] = self.spec.dims

            async def call(body: dict[str, Any] = body) -> dict[str, Any]:
                return check_response(await self._client.post("/v2/embed", json=body), "cohere").json()

            data = await with_retries(call, provider="cohere")
            out.extend(data["embeddings"]["float"])
        return out

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "search_document")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "search_query"))[0]

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register(Capability.EMBEDDER, "cohere", requires=("cohere_api_key",),
                  description="Cohere Embed (multilingual)")(CohereEmbedder)
