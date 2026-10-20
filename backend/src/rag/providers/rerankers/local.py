"""`local/<model>` cross-encoder reranker on the model server (ms-marco-MiniLM-L-6-v2 by default, English-only)."""

from __future__ import annotations

from collections.abc import Sequence

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.registry import registry
from rag.providers.local_models import KNOWN_MODELS, LocalModelClient


class LocalReranker:
    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        info = KNOWN_MODELS.get(spec.model, {})
        langs = info.get("languages", settings.reranker_language_set and sorted(settings.reranker_language_set))
        configured = settings.reranker_language_set
        # RERANKER_LANGUAGES narrows what the model claims; it never widens an English-only model.
        if langs is None:
            self.languages = configured
        else:
            self.languages = set(langs) & configured if configured is not None else set(langs)
        self._client = LocalModelClient(settings)

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        data = await self._client.post("/rerank", {"model": self.spec.model, "query": query,
                                                   "documents": list(documents)})
        return [float(s) for s in data["scores"]]

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register(Capability.RERANKER, "local", requires=("local_models_url",), local=True,
                  description="Local cross-encoder on the ONNX model server")(LocalReranker)
