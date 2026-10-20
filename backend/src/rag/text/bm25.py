"""BM25 analyzer producing Qdrant sparse vectors (the collection's sparse vector uses the IDF modifier, so Qdrant
supplies inverse document frequency at query time and the analyzer only computes the saturated term frequency).

Tokens: NFKC + lower-case + folded Arabic letter variants and digits; English stop words removed and Snowball
(Porter2) stems; Arabic stop words removed and Snowball Arabic stems; numbers kept as written; identifier keys
(A-101, RFI-23, SPEC-033000) added as whole tokens so a document number matches exactly however it was typed.
Token ids are 32-bit MurmurHash3 values.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import mmh3
import snowballstemmer

from rag.metadata.identifiers import identifier_keys
from rag.text.normalize import is_arabic_char, normalize_for_search

ANALYZER_VERSION = "bm25v2"

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

STOP_EN = frozenset(
    """a about above after again against all am an and any are as at be because been before being below between both
    but by can could did do does doing down during each few for from further had has have having he her here hers
    herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on
    once only or other our ours ourselves out over own same she should so some such than that the their theirs them
    themselves then there these they this those through to too under until up very was we were what when where which
    while who whom why will with would you your yours yourself yourselves shall may must also per via etc""".split()
)
STOP_AR = frozenset(
    """في من على إلى الى عن مع هذا هذه ذلك تلك التي الذي الذين ان أن إن او أو ثم كما قد لقد كان كانت يكون تكون هو هي هم
    هن نحن انت أنت انا أنا ما ماذا لماذا كيف اين أين متى لا لم لن ليس غير بين كل بعض أي اي عند حتى إذا اذا لكن بل
    و ب ل ك ف""".split()
)
STOP_AR = frozenset(normalize_for_search(w) for w in STOP_AR)


@lru_cache(maxsize=1)
def _stemmers() -> tuple[object, object]:
    return snowballstemmer.stemmer("english"), snowballstemmer.stemmer("arabic")


@lru_cache(maxsize=200_000)
def _stem(word: str, arabic: bool) -> str:
    en, ar = _stemmers()
    return (ar if arabic else en).stemWord(word)  # type: ignore[attr-defined]


def token_id(token: str) -> int:
    return mmh3.hash(token, signed=False)


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]

    def is_empty(self) -> bool:
        return not self.indices


class BM25Analyzer:
    version = ANALYZER_VERSION

    def __init__(self, *, k1: float = 1.2, b: float = 0.75, avgdl: float = 180.0) -> None:
        self.k1 = k1
        self.b = b
        self.avgdl = avgdl

    def tokens(self, text: str, *, query: bool = False) -> list[str]:
        if not text:
            return []
        out: list[str] = []
        for word in _WORD.findall(normalize_for_search(text)):
            if word.isdigit():
                out.append(word)
                continue
            arabic = is_arabic_char(word[0])
            if arabic:
                if word in STOP_AR or len(word) < 2:
                    continue
                out.append(_stem(word, True))
            else:
                if word in STOP_EN or (len(word) == 1 and not word.isdigit()):
                    continue
                out.append(word if any(ch.isdigit() for ch in word) else _stem(word, False))
        out.extend("#" + key.lower() for key in identifier_keys(text, query=query))
        return out

    def document_vector(self, text: str) -> SparseVector:
        counts = Counter(self.tokens(text))
        if not counts:
            return SparseVector([], [])
        length = sum(counts.values())
        norm = self.k1 * (1 - self.b + self.b * length / self.avgdl)
        weights: dict[int, float] = {}
        for token, tf in counts.items():
            idx = token_id(token)
            weights[idx] = weights.get(idx, 0.0) + tf * (self.k1 + 1) / (tf + norm)
        return SparseVector(list(weights), [round(v, 5) for v in weights.values()])

    def query_vector(self, text: str) -> SparseVector:
        counts = Counter(self.tokens(text, query=True))
        if not counts:
            return SparseVector([], [])
        weights: dict[int, float] = {}
        for token in counts:
            idx = token_id(token)
            # Identifier tokens are rare by nature; a mild boost keeps an exact number ahead of topical matches.
            weights[idx] = 1.5 if token.startswith("#") else 1.0
        return SparseVector(list(weights), list(weights.values()))


def overlap_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    """Cheap lexical relevance in [0, 1] (fraction of distinct query tokens present, log-damped by repetition).
    Used by the offline `lexical` reranker and the extractive answerer."""
    if not query_tokens or not doc_tokens:
        return 0.0
    q = set(query_tokens)
    d = Counter(doc_tokens)
    hit = sum(1.0 + math.log1p(d[t] - 1) * 0.1 for t in q if d.get(t))
    return min(1.0, hit / len(q))
