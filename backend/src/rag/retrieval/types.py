"""Data passed between the retrieval stages and to the answering layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SearchFilters:
    """Filters chosen in the UI or read from the question. Empty lists mean no restriction."""

    project_ids: list[str] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    exclude_local_only: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SearchFilters:
        data = data or {}
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in known and v not in (None, "", [])}
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [], False)}


@dataclass
class QueryPlan:
    original: str
    query: str
    language: str
    identifiers: list[str]
    query_type: str  # identifier | keyword | question | non_english
    filters: SearchFilters
    english: str | None = None
    hard: bool = False
    needs_research: bool = False
    boosts: dict[str, list[str]] = field(default_factory=dict)  # soft preferences: doc_types
    reasons: list[str] = field(default_factory=list)
    router: str = "rules"  # rules | llm


@dataclass
class Candidate:
    """One chunk with its scores from each stage."""

    id: str
    payload: dict[str, Any]
    legs: dict[str, float] = field(default_factory=dict)  # raw scores per leg
    fused: float = 0.0
    rerank: float | None = None
    final: float = 0.0
    demoted: bool = False
    phrase: float = 0.0  # share of the question's word pairs this chunk contains in order

    @property
    def document_id(self) -> str:
        return self.payload.get("document_id", "")

    @property
    def version_id(self) -> str:
        return self.payload.get("version_id", "")


@dataclass
class Passage:
    n: int
    chunk_id: str
    version_id: str
    document_id: str
    doc_number: str
    title: str
    page_start: int
    page_end: int
    section_path: str
    kind: str
    text: str
    core_text: str
    score: float
    language: str
    file_name: str
    project: str
    doc_type: str
    local_only: bool = False

    def label(self) -> str:
        number = self.doc_number or self.file_name or self.title
        pages = f"p.{self.page_start}" if self.page_start == self.page_end else f"pp.{self.page_start}-{self.page_end}"
        return f"{number} · {self.title} · {pages}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DocumentHit:
    document_id: str
    version_id: str
    score: float
    doc_number: str
    title: str
    file_name: str
    project: str
    doc_type: str
    pages: list[int]


@dataclass
class RetrievalResult:
    plan: QueryPlan
    passages: list[Passage]
    documents: list[DocumentHit]
    timings_ms: dict[str, float] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)
    restricted_local_only: int = 0
    signals: dict[str, Any] = field(default_factory=dict)  # absolute relevance signals of this search (see relevance.py)
    off_topic: bool = False  # nothing in the library answers the question; `passages` is empty
