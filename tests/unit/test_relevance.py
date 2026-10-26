from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from rag.retrieval import relevance
from rag.retrieval.retriever import Retriever, _minmax
from rag.retrieval.types import Candidate, QueryPlan, SearchFilters
from rag.text.bm25 import BM25Analyzer

ANALYZER = BM25Analyzer()


def _cand(cid: str, text: str, *, dense: float | None = None, sparse: float | None = None, version: str = "v1",
          page: int = 1, header: str = "", final: float = 0.0, identifiers: list[str] | None = None) -> Candidate:
    legs: dict[str, float] = {}
    if dense is not None:
        legs["dense"] = dense
    if sparse is not None:
        legs["sparse"] = sparse
    cand = Candidate(id=cid, payload={"text": text, "header": header, "page_start": page, "version_id": version,
                                      "document_id": "d1", "identifiers": identifiers or []}, legs=legs)
    cand.final = cand.fused = final
    return cand


def test_coverage_is_the_share_of_informative_question_words_the_best_chunk_has() -> None:
    chunks = [_cand(str(i), f"general text about topic number {i} and other things", dense=0.7) for i in range(6)]
    chunks.append(_cand("hit", "the rooftop swimming pool is heated to a water temperature of twenty eight degrees", dense=0.8))
    sig = relevance.compute(ANALYZER, "what water temperature is the rooftop swimming pool kept at", [], chunks)
    assert sig.coverage > 0.7
    miss = relevance.compute(ANALYZER, "annual leave policy for contractor staff", [],
                             [_cand(str(i), f"general text about topic number {i}", dense=0.7) for i in range(6)])
    assert miss.coverage < 0.3


def test_the_gate_needs_every_enabled_signal_to_be_weak_and_never_turns_away_an_identifier() -> None:
    weak = relevance.Signals(dense=0.70, sparse=1.0, coverage=0.1)
    assert relevance.is_off_topic(weak, min_dense=0.78, min_sparse=3.0, min_coverage=0.3)
    # a paraphrase: the words are missing but the meaning is close -> not turned away
    assert not relevance.is_off_topic(relevance.Signals(dense=0.85, sparse=1.0, coverage=0.1),
                                      min_dense=0.78, min_sparse=3.0, min_coverage=0.3)
    # a code: low cosine, strong keyword score -> not turned away
    assert not relevance.is_off_topic(relevance.Signals(dense=0.70, sparse=9.0, coverage=0.1),
                                      min_dense=0.78, min_sparse=3.0, min_coverage=0.3)
    # an identifier found in the results is conclusive
    assert not relevance.is_off_topic(relevance.Signals(dense=0.1, sparse=0.0, coverage=0.0, identifier_found=True),
                                      min_dense=0.78, min_sparse=3.0, min_coverage=0.3)
    # everything switched off (the default): nothing is ever turned away
    assert not relevance.is_off_topic(weak, min_dense=0, min_sparse=0, min_coverage=0)


def test_the_cross_encoder_probability_is_the_signal_that_separates_best() -> None:
    off = dict(min_dense=0, min_sparse=0, min_coverage=0)
    assert relevance.is_off_topic(relevance.Signals(rerank=0.004), min_rerank=0.02, **off)
    assert not relevance.is_off_topic(relevance.Signals(rerank=0.6), min_rerank=0.02, **off)
    # a question that was not reranked (short, or names an identifier) has no such signal and is never turned away on it
    assert not relevance.is_off_topic(relevance.Signals(rerank=None), min_rerank=0.02, **off)
    # it still needs the others to agree when they are switched on: a strong keyword match keeps the question
    assert not relevance.is_off_topic(relevance.Signals(rerank=0.004, sparse=9.0), min_rerank=0.02,
                                      min_dense=0, min_sparse=3.0, min_coverage=0)
    assert not relevance.is_off_topic(relevance.Signals(rerank=0.004, identifier_found=True), min_rerank=0.02, **off)


def test_the_best_rerank_score_among_the_candidates_is_recorded() -> None:
    low, high = _cand("a", "x"), _cand("b", "y")
    low.rerank, high.rerank = 0.01, 0.7
    unscored = _cand("c", "z")
    assert relevance.compute(ANALYZER, "some question words here", [], [low, high, unscored]).rerank == 0.7
    assert relevance.compute(ANALYZER, "some question words here", [], [unscored]).rerank is None


def test_identifier_found_in_the_best_chunks_is_recorded() -> None:
    chunks = [_cand("a", "SUB-084 curtain wall shop drawings", dense=0.6, identifiers=["sub-084"])]
    sig = relevance.compute(ANALYZER, "SUB-084 status", ["sub-084"], chunks)
    assert sig.identifier_found


def _retriever(**settings: Any) -> Retriever:
    base = {"page_boost": 0.2, "rerank_blend": 0.5, "rerank_top_n": 40, "rerank_min_words": 5, "rerank_skip_identifiers": True,
            "rerank_cache_size": 100, "family_demotion": 0.6, "rerank_adaptive": False, "rerank_blend_floor": 0.1,
            "rerank_adapt_power": 1.0}
    base.update(settings)
    services = SimpleNamespace(settings=SimpleNamespace(**base), hub=SimpleNamespace(reranker=lambda: None))
    return Retriever(services)


def test_a_page_with_two_matching_chunks_beats_a_page_with_one_slightly_better_chunk() -> None:
    ranked = [_cand("lucky", "x", page=7, final=0.80), _cand("a", "x", page=3, final=0.78), _cand("b", "x", page=3, final=0.74)]
    out = _retriever()._page_aggregate(ranked)
    assert [c.id for c in out][:2] == ["a", "b"]
    assert [c.id for c in _retriever(page_boost=0.0)._page_aggregate(
        [_cand("lucky", "x", page=7, final=0.80), _cand("a", "x", page=3, final=0.78)])] == ["lucky", "a"]


def test_minmax_scales_between_worst_and_best() -> None:
    assert _minmax({"a": 2.0, "b": 4.0, "c": 3.0}) == {"a": 0.0, "b": 1.0, "c": 0.5}
    assert _minmax({"a": 1.0, "b": 1.0}) == {"a": 0.0, "b": 0.0}
    assert _minmax({}) == {}


class _FakeReranker:
    languages = None
    spec = SimpleNamespace(label="local/fake")

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.calls += 1
        return [0.9 if "answer" in t else 0.1 for t in texts]


def _plan(question: str, identifiers: list[str] | None = None, kind: str = "question") -> QueryPlan:
    return QueryPlan(original=question, query=question, language="en", identifiers=identifiers or [], query_type=kind,
                     filters=SearchFilters())


def test_reranker_blends_with_retrieval_and_remembers_scores() -> None:
    from rag.cache import Cache, set_cache

    set_cache(Cache(""))  # no shared Redis here: the per-process memory is what is tested
    fake = _FakeReranker()
    retriever = _retriever()
    retriever.services.hub = SimpleNamespace(reranker=lambda: fake)
    ranked = [_cand("a", "no match here", final=1.0), _cand("b", "the answer is here", final=0.6), _cand("c", "other", final=0.2)]
    for c in ranked:
        c.fused = c.final
    plan = _plan("where is the answer to this question stored")
    out = asyncio.run(retriever._rerank(plan, ranked))
    assert out[0].id == "b"  # 0.5 * 1.0 (reranker best) + 0.5 * 0.5 beats 0.5 * 0 + 0.5 * 1.0
    asyncio.run(retriever._rerank(plan, [_cand("a", "no match here", final=1.0), _cand("b", "the answer is here", final=0.6),
                                         _cand("c", "other", final=0.2)]))
    assert fake.calls == 1  # the second identical question is answered from the cache


def test_reranker_is_skipped_for_short_questions_and_identifier_lookups() -> None:
    retriever = _retriever()
    assert not retriever._rerank_wanted(_plan("SUB-084 status"))
    assert not retriever._rerank_wanted(_plan("what is the status of SUB-084 on the project", ["sub-084"]))
    assert retriever._rerank_wanted(_plan("what is the status of the curtain wall shop drawings"))
