from __future__ import annotations

from rag.config.settings import Settings
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import Candidate


def _retriever(**settings: object) -> Retriever:
    retriever = Retriever.__new__(Retriever)
    retriever.settings = Settings(_env_file=None, **settings)  # type: ignore[call-arg]
    return retriever


def _top(*phrases: float) -> list[Candidate]:
    return [Candidate(id=str(i), payload={}, phrase=p) for i, p in enumerate(phrases)]


def test_a_question_nobody_quotes_gets_the_full_blend() -> None:
    r = _retriever(rerank_blend=0.7, rerank_blend_floor=0.1)
    assert r._blend(_top(0.0, 0.0, 0.0)) == 0.7


def test_a_question_a_chunk_quotes_is_left_to_the_fused_order() -> None:
    r = _retriever(rerank_blend=0.7, rerank_blend_floor=0.1)
    assert abs(r._blend(_top(0.2, 1.0, 0.0)) - 0.1) < 1e-9


def test_the_blend_falls_steadily_as_more_of_the_phrase_is_quoted() -> None:
    r = _retriever(rerank_blend=0.7, rerank_blend_floor=0.1)
    values = [r._blend(_top(share)) for share in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert values == sorted(values, reverse=True) and values[0] > values[-1]


def test_adaptive_off_means_a_fixed_blend() -> None:
    r = _retriever(rerank_blend=0.4, rerank_adaptive=False)
    assert r._blend(_top(1.0)) == r._blend(_top(0.0)) == 0.4


def test_the_floor_never_exceeds_the_ceiling() -> None:
    r = _retriever(rerank_blend=0.2, rerank_blend_floor=0.9)
    assert r._blend(_top(1.0)) <= 0.2
