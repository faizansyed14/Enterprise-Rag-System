"""`local/<model>` embedder: the ONNX model server (multilingual-e5-small by default)."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import ConfigurationError
from rag.plugins.registry import registry
from rag.providers.local_models import KNOWN_MODELS, LocalModelClient


class LocalEmbedder:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        info = KNOWN_MODELS.get(spec.model, {})
        dims = spec.dims or info.get("dims") or int(spec.options.get("dims", 0))
        if not dims:
            raise ConfigurationError(f"{spec.label}: unknown model; add @<dims> to the EMBEDDER line")
        self.dims = int(dims)
        self.max_tokens = int(info.get("max_tokens", 512))
        self.multilingual = bool(info.get("multilingual", False))
        self.batch = max(1, settings.embedder_batch)
        self._client = LocalModelClient(settings)
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._slots = asyncio.Semaphore(max(1, settings.embedder_concurrency))

    @property
    def slug(self) -> str:
        return f"local-{self.spec.model}-{self.dims}".lower()

    async def _embed(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            async with self._slots:
                data = await self._client.post("/embed", {"model": self.spec.model, "inputs": list(texts[i: i + self.batch]),
                                                          "kind": kind})
            vectors = data["embeddings"]
            if vectors and len(vectors[0]) != self.dims:
                raise ConfigurationError(f"model server returned {len(vectors[0])} dims for {self.spec.model}, "
                                         f"expected {self.dims}")
            out.extend(vectors)
        return out

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "passage")

    async def embed_query(self, text: str) -> list[float]:
        """Question vectors are cached: the same question (and every re-run of an evaluation) costs one model call."""
        cached = self._query_cache.get(text)
        if cached is not None:
            self._query_cache.move_to_end(text)
            return cached
        vector = (await self._embed([text], "query"))[0]
        self._query_cache[text] = vector
        while len(self._query_cache) > 2048:
            self._query_cache.popitem(last=False)
        return vector

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register(Capability.EMBEDDER, "local", requires=("local_models_url",), local=True,
                  description="Local ONNX model server (e5, bge-m3, MiniLM)")(LocalEmbedder)
