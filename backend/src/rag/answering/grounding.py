"""Groundedness of a whole answer: how much of what it says is supported by the passages it cites.

Each cited sentence is scored by the GROUNDEDNESS judge against its cited passages; long sentences that make claims
without any citation count against the answer. The judge falls back to the built-in lexical check when it cannot read
the answer's language (HHEM is English-only) or fails.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from rag.answering.citations import MARKER, split_sentences
from rag.providers.groundedness.judges import lexical_support
from rag.retrieval.types import Passage
from rag.text.language import detect_language

log = logging.getLogger(__name__)

_FILLER = re.compile(r"(?i)^(here is|here are|in summary|to summari[sz]e|according to the (documents|passages))")


@dataclass
class GroundingReport:
    score: float
    sentences: list[dict[str, Any]] = field(default_factory=list)
    uncited_claims: int = 0
    judge: str = "builtin/lexical"


async def groundedness(judge: Any, answer: str, passages: list[Passage]) -> GroundingReport:
    by_n = {p.n: p for p in passages}
    pairs: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []
    uncited = 0
    for sentence in split_sentences(answer):
        markers = [int(m) for m in MARKER.findall(sentence)]
        claim = MARKER.sub("", sentence).strip()
        if not claim:
            continue
        if not markers:
            if len(claim.split()) >= 8 and not _FILLER.match(claim) and re.search(r"\d|[A-Z]{2,}", claim):
                uncited += 1
                rows.append({"sentence": claim, "cited": [], "score": 0.0})
            continue
        premise = "\n".join(by_n[n].text for n in markers if n in by_n)[:4000]
        if not premise:
            rows.append({"sentence": claim, "cited": markers, "score": 0.0})
            continue
        pairs.append((premise, claim))
        rows.append({"sentence": claim, "cited": markers, "score": None})

    label = "builtin/lexical"
    scores: list[float] = []
    if pairs:
        use_judge = judge is not None
        spec = getattr(judge, "spec", None)
        if use_judge and spec is not None and "hhem" in spec.model and detect_language(answer) not in {"en", "und"}:
            use_judge = False  # HHEM reads English only
        if use_judge:
            try:
                scores = list(await judge.score(pairs))
                label = spec.label if spec is not None else "judge"
            except Exception:
                log.warning("groundedness judge failed; using the lexical check", exc_info=True)
                scores = []
        if not scores:
            scores = [lexical_support(p, h) for p, h in pairs]
    it = iter(scores)
    for row in rows:
        if row["score"] is None:
            row["score"] = round(float(next(it)), 3)
    judged = [r["score"] for r in rows]
    if not judged:
        return GroundingReport(score=0.0, sentences=rows, uncited_claims=uncited, judge=label)
    mean = sum(judged) / len(judged)
    return GroundingReport(score=round(mean, 3), sentences=rows, uncited_claims=uncited, judge=label)
