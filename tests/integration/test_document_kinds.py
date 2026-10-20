"""Retrieval accuracy per document kind: digital prose, a scan, native tables, a table pasted into a PDF as a picture,
and a photographed form. Each file is ingested through the real pipeline (pdfium, OCR, chunker, index) and twenty
questions with known answers are asked; the chunk that holds the answer must be among the first five passages.
Offline models, so this checks the parsing and chunking routes rather than embedding quality."""

from __future__ import annotations

import re
from collections import defaultdict

import pytest

from rag.db.engine import transaction
from rag.evaluation.bench_corpus import build
from rag.jobs import queue
from rag.jobs.handlers import load_handlers
from rag.jobs.queue import NewJob
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.services import Services

from .conftest import add_source, reset_qdrant, run_jobs


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[|*#`]", " ", text.lower().replace("×", "x"))).strip()


@pytest.fixture(scope="module")
async def kinds(migrated_db, settings, tmp_path_factory):  # noqa: ANN001, ANN201
    from sqlalchemy import text

    from rag.db.schema import metadata

    load_handlers()
    names = ", ".join(f'"{t.name}"' for t in metadata.sorted_tables)
    async with transaction() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    folder = tmp_path_factory.mktemp("kinds")
    truth = build(folder)
    services = Services.create(settings)
    await reset_qdrant(services)
    source = await add_source("Kinds", folder, ["everyone"])
    async with transaction() as conn:
        await queue.enqueue(conn, NewJob("sync_source", {"source_id": source}, lane="interactive"))
    await run_jobs(services)
    yield services, truth
    await services.aclose()


async def test_every_document_kind_is_retrievable(kinds) -> None:  # noqa: ANN001
    services, truth = kinds
    router = QueryRouter(known_projects=[])
    retriever = Retriever(services)
    hits: dict[str, list[bool]] = defaultdict(list)
    missed: list[str] = []
    for file_name, question, facts in truth:
        plan = router.rules(question, SearchFilters())
        result = await retriever.search(plan, ["everyone"], passages=5)
        found = any(file_name.split(".")[0].split("_", 1)[1].replace("_", " ") in _norm(p.file_name.replace("_", " "))
                    and all(_norm(f) in _norm(p.text) for f in facts) for p in result.passages)
        hits[file_name].append(found)
        if not found:
            missed.append(f"{file_name}: {question}")
    summary = {name: sum(v) / len(v) for name, v in hits.items()}
    print("\nhit@5 by document kind:", summary)
    assert not missed, "answer chunk not in the first five passages for: " + "; ".join(missed)
