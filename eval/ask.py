"""Retrieval only: print the top results for each question. python /eval/ask.py "question" ["question" ...]"""
from __future__ import annotations

import asyncio
import os
import sys

from rag.config.settings import get_settings
from rag.jobs.handlers import load_handlers
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.services import Services


async def main(questions: list[str]) -> None:
    from blend_sweep import principals_of

    load_handlers()
    services = Services.create(get_settings())
    principals = await principals_of(os.environ.get("BENCH_AS", "admin@example.com"))
    retriever = Retriever(services)
    router = QueryRouter(known_projects=await services.known_projects())
    for q in questions:
        result = await retriever.search(router.rules(q, SearchFilters.from_dict(None)), principals, rerank=True, debug=True)
        print(f"\n=== {q}")
        for i, c in enumerate(result.debug["ranked"][:4], 1):
            print(f"  #{i} {c['file_name'][:44]} p{c['page']}: {' '.join(c['text'].split())[:150]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
