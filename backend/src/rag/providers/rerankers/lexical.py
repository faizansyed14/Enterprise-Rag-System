"""`lexical/overlap`: offline reranker scoring query-term coverage (tests, demos, machines without the model server)."""

from __future__ import annotations

from collections.abc import Sequence

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.registry import registry
from rag.text.bm25 import BM25Analyzer, overlap_score


class LexicalReranker:
    languages: set[str] | None = None

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.analyzer = BM25Analyzer()

    async def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        q = self.analyzer.tokens(query, query=True)
        return [overlap_score(q, self.analyzer.tokens(d)) for d in documents]

    async def aclose(self) -> None:
        return None


registry.register(Capability.RERANKER, "lexical", local=True,
                  description="Query-term coverage (offline; no model)")(LexicalReranker)
