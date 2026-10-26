"""Verified citations: every [n] in an answer is checked against passage n before the user sees it.

For each cited sentence the best-matching span of the passage is found (RapidFuzz partial alignment on sentence
windows); that span becomes the quote shown and highlighted in the viewer. A citation is `verified` when the span
supports the sentence well enough AND every number in the sentence appears in the passage -- numbers are where
wrong answers hide (C35 vs C40, 25 vs 50 mm).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz

from rag.answering.arithmetic import derived
from rag.providers.groundedness.judges import numbers_in
from rag.retrieval.types import Passage

MARKER = re.compile(r"\[(\d{1,3})\]")
# Split after sentence punctuation, but keep citation markers that follow the full stop ("... writing. [1]") with
# the sentence they belong to.
_SENTENCES = re.compile(r"(?<=[.!?؟])\s+(?=[^\s\[])|(?<=\])\s+(?=[^\s\[])|\n+")
# Passage sentences: a label stays with what it introduces ("Response: Provide ..."), so ":" does not split.
_PASSAGE_SENTENCES = re.compile(r"(?<=[.!?؟;])\s+|\n+")
_WORDS = re.compile(r"\w+")
# the "#" counter at the start of a table row the answer writes: a number the answer made up, not one the source states
_ROW_NUMBER = re.compile(r"^\|\s*\d{1,4}\s*\|\s*")
VERIFY_THRESHOLD = 60.0


@dataclass
class Citation:
    n: int
    passage: Passage
    quote: str
    score: float
    verified: bool
    sentences: list[str] = field(default_factory=list)
    missing_numbers: list[str] = field(default_factory=list)

    def as_event(self) -> dict[str, object]:
        p = self.passage
        return {
            "n": self.n, "document_id": p.document_id, "version_id": p.version_id, "chunk_id": p.chunk_id,
            "doc_number": p.doc_number, "title": p.title,
            "page": p.page_start, "page_end": p.page_end, "section_path": p.section_path, "file_name": p.file_name,
            "project": p.project, "quote": self.quote, "score": round(self.score, 1), "verified": self.verified,
            "missing_numbers": self.missing_numbers,
        }


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCES.split(text) if s and s.strip()]


def cited_sentences(answer: str) -> dict[int, list[str]]:
    """{n: [sentences citing passage n]} with the markers removed from each sentence."""
    out: dict[int, list[str]] = {}
    for sentence in split_sentences(answer):
        numbers = [int(m) for m in MARKER.findall(sentence)]
        if not numbers:
            continue
        clean = _ROW_NUMBER.sub("| ", MARKER.sub("", sentence).strip())
        for n in dict.fromkeys(numbers):
            out.setdefault(n, []).append(clean)
    return out


def _words(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in (m.lower() for m in _WORDS.findall(text))}


def best_span(claim: str, passage_text: str) -> tuple[str, float]:
    """The passage window (1-3 consecutive sentences) that best supports the claim, and its support score.

    Fuzzy ratios reward a short window that merely appears inside the claim ("Response:" scores 100 against
    "Response: provide 600 mm panels"), so windows are ranked by the ratio scaled by how much of the claim they
    cover; the score returned is the plain ratio of the chosen window, which decides verification."""
    sentences = [s.strip() for s in _PASSAGE_SENTENCES.split(passage_text) if s and s.strip()]
    if not sentences:
        return "", 0.0
    claim_words = _words(claim)
    best = ("", 0.0, 0.0)  # window, rank, ratio
    for size in (1, 2, 3):
        for i in range(0, max(1, len(sentences) - size + 1)):
            window = " ".join(sentences[i: i + size])
            ratio = max(fuzz.token_set_ratio(claim, window), fuzz.partial_ratio(claim, window) * 0.9)
            covered = len(claim_words & _words(window)) / max(1, len(claim_words))
            rank = ratio * (0.4 + 0.6 * min(1.0, covered * 1.25))
            # Prefer shorter windows unless a longer one is clearly better.
            if rank > best[1] + (2.0 if size > 1 else 0.0):
                best = (window, rank, float(ratio))
    return best[0], best[2]


def verify(answer: str, passages: list[Passage]) -> list[Citation]:
    by_n = {p.n: p for p in passages}
    citations: list[Citation] = []
    for n, sentences in cited_sentences(answer).items():
        passage = by_n.get(n)
        if passage is None:
            continue  # a marker for a passage that does not exist is dropped (and counted as unverified)
        source_numbers = numbers_in(passage.text + " " + passage.title + " " + passage.doc_number)
        quotes: list[tuple[str, float]] = [best_span(s, passage.text) for s in sentences]
        quote, score = max(quotes, key=lambda q: q[1])
        missing = sorted({num for s in sentences for num in numbers_in(s)} - source_numbers - {str(n)})
        # a total or difference worked out from the passage's own numbers is supported, though the passage never writes it
        missing = [m for m in missing if not any(m in numbers_in(s) and derived(m, sorted(numbers_in(s))) for s in sentences)]
        verified = score >= VERIFY_THRESHOLD and not missing
        citations.append(Citation(n=n, passage=passage, quote=quote[:600], score=score, verified=verified,
                                  sentences=sentences, missing_numbers=missing))
    citations.sort(key=lambda c: c.n)
    return citations


def unknown_markers(answer: str, passages: list[Passage]) -> list[int]:
    known = {p.n for p in passages}
    return sorted({int(m) for m in MARKER.findall(answer)} - known)


def strip_unknown_markers(answer: str, passages: list[Passage]) -> str:
    known = {p.n for p in passages}
    return MARKER.sub(lambda m: m.group(0) if int(m.group(1)) in known else "", answer)


def citation_dicts(citations: list[Citation]) -> list[dict[str, object]]:
    return [c.as_event() for c in citations]


def as_plain(c: Citation) -> dict[str, object]:
    data = asdict(c)
    data["passage"] = c.passage.as_dict()
    return data
