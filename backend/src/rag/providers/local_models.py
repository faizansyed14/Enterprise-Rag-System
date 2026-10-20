"""Client for the local model server (backend/model_server): embeddings, cross-encoder reranking and pair
classification (HHEM groundedness) on ONNX Runtime inside this deployment. Nothing leaves the network.
"""

from __future__ import annotations

from typing import Any

from rag.config.settings import Settings
from rag.providers.http import check_response, make_client, with_retries

# What the backend needs to know about catalogue models before the server answers (index naming, gating).
KNOWN_MODELS: dict[str, dict[str, Any]] = {
    "multilingual-e5-small": {"kind": "embedder", "dims": 384, "max_tokens": 512, "multilingual": True},
    "multilingual-e5-small-int8": {"kind": "embedder", "dims": 384, "max_tokens": 512, "multilingual": True},
    "multilingual-e5-base": {"kind": "embedder", "dims": 768, "max_tokens": 512, "multilingual": True},
    "multilingual-e5-large": {"kind": "embedder", "dims": 1024, "max_tokens": 512, "multilingual": True},
    "bge-m3": {"kind": "embedder", "dims": 1024, "max_tokens": 1024, "multilingual": True},
    "all-MiniLM-L6-v2": {"kind": "embedder", "dims": 384, "max_tokens": 256, "multilingual": False},
    "bge-small-en-v1.5": {"kind": "embedder", "dims": 384, "max_tokens": 512, "multilingual": False},
    "ms-marco-MiniLM-L-6-v2": {"kind": "reranker", "languages": ["en"]},
    "bge-reranker-v2-m3": {"kind": "reranker", "languages": None},
    "bge-reranker-v2-m3-int8": {"kind": "reranker", "languages": None},
    "mmarco-mMiniLMv2-L12": {"kind": "reranker", "languages": None},
    "mmarco-mMiniLMv2-L12-int8": {"kind": "reranker", "languages": None},
    "hhem-2.1-open": {"kind": "pair_classifier", "languages": ["en"]},
}


class LocalModelClient:
    def __init__(self, settings: Settings) -> None:
        headers = {}
        token = settings.secret("local_models_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = make_client(settings.local_models_url.rstrip("/"), timeout=settings.local_models_timeout_s,
                                   headers=headers)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            response = await self._client.post(path, json=body)
            return check_response(response, "local-models").json()

        return await with_retries(call, provider="local-models", attempts=3, max_wait_s=5)

    async def get(self, path: str) -> dict[str, Any]:
        response = await self._client.get(path)
        return check_response(response, "local-models").json()

    async def aclose(self) -> None:
        await self._client.aclose()
