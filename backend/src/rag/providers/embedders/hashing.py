"""`hashing/hash-<dims>`: deterministic feature-hashing embeddings (word unigrams, bigrams and character trigrams).

No model, no network: it captures lexical overlap only, which is enough to exercise the whole pipeline in tests and
on machines without the model server. Not for production retrieval quality.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Sequence

import mmh3
import numpy as np

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.plugins.registry import registry
from rag.text.bm25 import BM25Analyzer

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class HashingEmbedder:
    multilingual = True

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        digits = re.findall(r"\d+", spec.model)
        self.dims = spec.dims or (int(digits[-1]) if digits else 384)
        self.max_tokens = 512
        self.analyzer = BM25Analyzer()

    @property
    def slug(self) -> str:
        return f"hashing-{self.dims}"

    def _vector(self, text: str) -> list[float]:
        vec = np.zeros(self.dims, dtype=np.float32)
        tokens = [t for t in self.analyzer.tokens(text) if not t.startswith("#")]
        features: list[tuple[str, float]] = [(t, 1.0) for t in tokens]
        features += [(f"{a}_{b}", 0.6) for a, b in itertools.pairwise(tokens)]
        for word in tokens:
            padded = f"<{word}>"
            features += [(padded[i: i + 3], 0.25) for i in range(max(0, len(padded) - 2))]
        for feature, weight in features:
            h = mmh3.hash(feature, signed=False)
            vec[h % self.dims] += weight if (h >> 31) & 1 else -weight
        norm = float(np.linalg.norm(vec))
        if norm == 0 or math.isnan(norm):
            vec[0] = 1.0
            norm = 1.0
        return (vec / norm).tolist()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    async def aclose(self) -> None:
        return None


registry.register(Capability.EMBEDDER, "hashing", local=True,
                  description="Feature-hashing vectors (offline tests and demos; lexical only)")(HashingEmbedder)
