"""`cohere/<rerank-model>`: Cohere Rerank (multilingual, so Arabic questions are reranked too)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.registry import registry
from rag.providers.http import check_response, make_client, with_retries


class CohereReranker:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.languages = None if "multilingual" in spec.model or "v3.5" in spec.model or "v4" in spec.model \
            else settings.reranker_language_set
        self._client = make_client(settings.cohere_base_url.rstrip("/"), timeout=30,
                                   headers={"Authorization": f"Bearer {settings.secret('cohere_api_key')}"})

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        body = {"model": self.spec.model, "query": query, "documents": list(documents), "top_n": len(documents)}

        async def call() -> dict[str, Any]:
            return check_response(await self._client.post("/v2/rerank", json=body), "cohere").json()

        data = await with_retries(call, provider="cohere")
        scores = [0.0] * len(documents)
        for row in data.get("results", []):
            scores[int(row["index"])] = float(row["relevance_score"])
        return scores

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register(Capability.RERANKER, "cohere", requires=("cohere_api_key",),
                  description="Cohere Rerank (multilingual models rerank Arabic too)")(CohereReranker)
