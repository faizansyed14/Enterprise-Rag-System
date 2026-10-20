"""Absolute relevance: does the library hold anything that answers this question? No language model.

Search always returns "the best chunks", even for a question the library knows nothing about, and every score used for
ranking is relative (normalised by the best hit), so ranking cannot tell the two cases apart. Independent signals can, and
a question is turned away only when ALL of the enabled ones are weak:

  dense      the best raw cosine similarity between the question and a chunk (meaning)
  sparse     the best raw keyword (BM25) score, per question term (words)
  coverage   the share of the question's informative words that the best chunk, or its heading, actually contains
  rerank     the cross-encoder's best score, a probability between 0 and 1. A cross-encoder is trained to say
             whether a passage answers a question, so unlike the three above its score means the same thing for every
             question. Only present when the question was reranked (a long enough question without an identifier).

A single signal is not enough. A code such as "SUB-084" has a low cosine and a high keyword score; a paraphrase has the
opposite. A question that names an identifier found in the results is never turned away.

Thresholds are set from measurements on the library's own questions (`rag eval bench` prints the separation and
suggests values for a chosen loss rate); at 0 a signal is off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from rag.text.bm25 import BM25Analyzer


@dataclass
class Signals:
    dense: float | None = None
    sparse: float | None = None  # best raw BM25 score divided by the number of question terms
    coverage: float = 0.0
    rerank: float | None = None  # best cross-encoder score (0..1, the model server applies the sigmoid); None if not reranked
    identifier_found: bool = False
    terms: int = 0
    extra: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"dense": None if self.dense is None else round(self.dense, 4),
                "sparse": None if self.sparse is None else round(self.sparse, 3), "coverage": round(self.coverage, 3),
                "rerank": None if self.rerank is None else round(self.rerank, 5),
                "identifier_found": self.identifier_found, "terms": self.terms}


def _idf_weights(terms: list[str], term_sets: list[set[str]]) -> dict[str, float]:
    """How much each question word is worth, judged from the candidates: a word every candidate contains says nothing
    about which one is right; a word only one contains says a lot."""
    n = len(term_sets)
    out: dict[str, float] = {}
    for term in terms:
        df = sum(1 for s in term_sets if term in s)
        out[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5)) if n >= 5 else 1.0
    return out


def coverage_of(terms: list[str], weights: dict[str, float], body: set[str], head: set[str]) -> float:
    total = sum(weights.get(t, 1.0) for t in terms) or 1.0
    got = sum(weights.get(t, 1.0) * (1.0 if t in body else 0.5 if t in head else 0.0) for t in terms)
    return min(1.0, got / total)


def compute(analyzer: BM25Analyzer, query: str, identifiers: list[str], ranked: list[Any], *, top: int = 12) -> Signals:
    """Signals for the best `top` candidates (already ordered best first; each has `.legs` and `.payload`)."""
    sig = Signals()
    if not ranked:
        return sig
    terms = [t for t in dict.fromkeys(analyzer.tokens(query, query=True)) if not t.startswith("#")]
    sig.terms = len(terms)
    head_n = ranked[:top]
    dense = [c.legs.get("dense", c.legs.get("exact")) for c in ranked[:40] if c.legs.get("dense") is not None or c.legs.get("exact") is not None]
    sig.dense = max(dense) if dense else None
    sparse = [c.legs["sparse"] for c in ranked[:40] if "sparse" in c.legs]
    if sparse:
        sig.sparse = max(sparse) / max(1, len(terms))
    scored = [c.rerank for c in ranked if getattr(c, "rerank", None) is not None]
    if scored:
        sig.rerank = max(scored)
    if identifiers:
        haystack = " ".join(" ".join(c.payload.get("identifiers") or []) + " " + str(c.payload.get("text", ""))[:3000]
                            for c in head_n).casefold()
        keys = [i.casefold() for i in identifiers]
        sig.identifier_found = any(k in haystack for k in keys)
    if terms:
        bodies = [set(analyzer.tokens(str(c.payload.get("text", "")))) for c in head_n]
        heads = [set(analyzer.tokens(str(c.payload.get("header", "")) + " " + str(c.payload.get("file_name", ""))))
                 for c in head_n]
        weights = _idf_weights(terms, bodies)
        sig.coverage = max(coverage_of(terms, weights, b, h) for b, h in zip(bodies, heads, strict=True))
    return sig


def is_off_topic(sig: Signals, *, min_dense: float, min_sparse: float, min_coverage: float, min_rerank: float = 0.0) -> bool:
    """True when every signal that is switched on (threshold above 0) is weak. Switched off, nothing is ever turned away.
    A question that was not reranked has no rerank signal and the others decide."""
    if sig.identifier_found:
        return False
    votes: list[bool] = []
    if min_rerank > 0 and sig.rerank is not None:
        votes.append(sig.rerank < min_rerank)
    if min_dense > 0 and sig.dense is not None:
        votes.append(sig.dense < min_dense)
    if min_sparse > 0:
        votes.append((sig.sparse or 0.0) < min_sparse)
    if min_coverage > 0:
        votes.append(sig.coverage < min_coverage)
    return bool(votes) and all(votes)
