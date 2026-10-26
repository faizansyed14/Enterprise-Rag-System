"""Research tools for hard questions. Every tool works inside the user's access: documents are resolved and read
only if one of their live versions is visible to the user's principals.

Limits per question: AGENT_MAX_STEPS tool rounds and AGENT_MAX_PAGES pages read in full.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import re
import uuid
from typing import Any

from sqlalchemy import text

from rag.db.engine import transaction
from rag.metadata.identifiers import identifier_key
from rag.plugins.capabilities import ToolSpec
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import Passage, QueryPlan, SearchFilters
from rag.security.guardrails import sanitize_evidence

log = logging.getLogger(__name__)

TOOLS = [
    ToolSpec("search", "Search the document library. Returns numbered passages.", {
        "type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for (keywords, numbers, codes)."},
            "doc_type": {"type": "string", "description": "Optional: drawing, specification, rfi, submittal, "
                                                          "minutes, report, change_order, invoice, contract ..."},
        },
        "required": ["query"]}),
    ToolSpec("search_within", "Search inside one document (by document number or id).", {
        "type": "object", "properties": {"document": {"type": "string"}, "query": {"type": "string"}},
        "required": ["document", "query"]}),
    ToolSpec("read_pages", "Read whole pages of one document (by document number or id). Use for tables, "
                           "schedules and long clauses.", {
        "type": "object", "properties": {"document": {"type": "string"},
                                         "pages": {"type": "array", "items": {"type": "integer"}, "maxItems": 5}},
        "required": ["document", "pages"]}),
    ToolSpec("open_reference", "Find documents by number or code (drawing number, RFI, submittal, spec section).", {
        "type": "object", "properties": {"identifier": {"type": "string"}}, "required": ["identifier"]}),
]


class ToolBox:
    def __init__(self, retriever: Retriever, plan: QueryPlan, principals: list[str], passages: list[Passage],
                 *, max_pages: int = 30, max_passages: int = 24) -> None:
        self.retriever = retriever
        self.plan = plan
        self.principals = principals
        self.passages = passages
        self.max_pages = max_pages
        self.max_passages = max_passages
        self.pages_read = 0

    # ---- helpers ----------------------------------------------------------------------------------------------------
    def _add(self, new: list[Passage]) -> list[Passage]:
        known = {(p.chunk_id, p.page_start) for p in self.passages}
        added = []
        for p in new:
            if (p.chunk_id, p.page_start) in known or len(self.passages) >= self.max_passages:
                continue
            p = dataclasses.replace(p, n=len(self.passages) + 1)  # noqa: PLW2901
            self.passages.append(p)
            added.append(p)
        return added

    @staticmethod
    def _summary(passages: list[Passage], limit: int = 600) -> str:
        if not passages:
            return "No new passages found."
        return "\n\n".join(f"[{p.n}] {p.label()}\n{sanitize_evidence(p.text[:limit])}" for p in passages)

    async def _resolve(self, document: str) -> list[dict[str, Any]]:
        """Visible live versions of a document given its id or number (current first)."""
        doc_id = None
        with contextlib.suppress(ValueError):
            doc_id = str(uuid.UUID(document.strip()))
        key = identifier_key(document) if not doc_id else None
        sql = (
            "SELECT d.id::text AS document_id, v.id::text AS version_id, d.doc_number, v.title, "  # noqa: S608 - fixed fragments, bound values
            "v.blob_sha256, v.page_count FROM documents d "
            "JOIN versions v ON v.document_id = d.id AND v.state = 'live' "
            "WHERE EXISTS (SELECT 1 FROM version_access a WHERE a.version_id = v.id AND a.principal = ANY(:p)) AND "
            + ("d.id = CAST(:doc AS uuid)" if doc_id else ":key = ANY(v.identifiers)")
            + " ORDER BY v.created_at DESC LIMIT 20"
        )
        async with transaction() as conn:
            rows = (await conn.execute(text(sql), {"p": self.principals, "doc": doc_id, "key": key})).mappings().all()
        return [dict(r) for r in rows]

    # ---- tools ------------------------------------------------------------------------------------------------------
    async def run(self, name: str, args: dict[str, Any]) -> str:
        try:
            handler = getattr(self, f"tool_{name}")
        except AttributeError:
            return f"Unknown tool {name}."
        try:
            return await handler(**{k: v for k, v in args.items() if not k.startswith("_")})
        except TypeError as exc:
            return f"Invalid arguments for {name}: {exc}"

    async def tool_search(self, query: str, doc_type: str | None = None) -> str:
        filters = dataclasses.replace(self.plan.filters, doc_types=[doc_type] if doc_type else self.plan.filters.doc_types)
        sub = dataclasses.replace(self.plan, query=query, filters=filters, english=None,
                                  identifiers=[identifier_key(i) for i in re.findall(r"[A-Z]{1,5}-?\d[\w.-]*", query)])
        result = await self.retriever.search(sub, self.principals, passages=6)
        return self._summary(self._add(result.passages))

    async def tool_search_within(self, document: str, query: str) -> str:
        versions = await self._resolve(document)
        if not versions:
            return f"No document {document!r} is visible to you."
        filters = SearchFilters(document_ids=[versions[0]["document_id"]],
                                exclude_local_only=self.plan.filters.exclude_local_only)
        sub = dataclasses.replace(self.plan, query=query, filters=filters, english=None, identifiers=[])
        result = await self.retriever.search(sub, self.principals, passages=6)
        return self._summary(self._add(result.passages))

    async def tool_read_pages(self, document: str, pages: list[int]) -> str:
        versions = await self._resolve(document)
        if not versions:
            return f"No document {document!r} is visible to you."
        version = versions[0]
        wanted = sorted({int(p) for p in pages if int(p) > 0})[:5]
        if self.pages_read + len(wanted) > self.max_pages:
            return f"Page budget reached ({self.max_pages} pages per question)."
        async with transaction() as conn:
            rows = (await conn.execute(text(
                "SELECT page_no, text, tables FROM pages WHERE blob_sha256 = :sha AND page_no = ANY(:pages) "
                "ORDER BY page_no"), {"sha": version["blob_sha256"], "pages": wanted})).all()
        self.pages_read += len(rows)
        new = []
        for row in rows:
            body = row.text
            for table in row.tables or []:
                body += "\n" + "\n".join(" | ".join(r) for r in table.get("rows", [])[:80])
            new.append(Passage(
                n=0, chunk_id=f"page:{version['version_id']}:{row.page_no}", version_id=version["version_id"],
                document_id=version["document_id"], doc_number=version["doc_number"] or "", title=version["title"],
                page_start=row.page_no,
                page_end=row.page_no, section_path="", kind="page", text=body[:6000], core_text=body[:6000],
                score=0.0, language="und", file_name="", project="", doc_type=""))
        return self._summary(self._add(new), limit=6000)

    async def read_whole(self, top: Passage, budget_chars: int) -> bool:
        """Replace the passages with every chunk of the top passage's document, when they fit the budget (no model call)."""
        async with transaction() as conn:
            rows = (await conn.execute(text(
                "SELECT chunk_no, page_start, page_end, section_path, kind, text FROM chunks WHERE version_id = CAST(:v AS uuid) "
                "ORDER BY chunk_no"), {"v": top.version_id})).all()
        if not rows or sum(len(r.text) + 40 for r in rows) > budget_chars:
            return False
        self.passages[:] = [
            dataclasses.replace(
                top,
                n=i,
                chunk_id=f"whole:{top.version_id}:{r.chunk_no}",
                page_start=r.page_start,
                page_end=r.page_end,
                section_path=r.section_path,
                kind=r.kind,
                text=r.text,
                core_text=r.text,
            )
            for i, r in enumerate(rows, 1)
        ]
        self.max_passages = max(self.max_passages, len(rows))
        return True

    async def tool_open_reference(self, identifier: str) -> str:
        versions = await self._resolve(identifier)
        if not versions:
            return f"No visible document matches {identifier!r}."
        lines = [f"- {v['doc_number'] or v['title']} id={v['document_id']}: {v['title']}" for v in versions[:10]]
        return "\n".join(lines)
