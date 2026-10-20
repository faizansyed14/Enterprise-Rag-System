"""Show, for questions whose right page is not the first result, what ranked first and where the right page was.

  python /eval/failures.py GOLDEN.jsonl [kind,kind] [limit] [name=value,...]

Retrieval only (no reranker, no language model). For each miss: the question, the pages that are right, the rank of the first
chunk on a right page, and the top chunk next to the best chunk of a right page, so it is visible whether the fault is ranking
(the right chunk is there but second), chunking (the answer is split or lacks its heading) or parsing (the text is wrong).
"""

from __future__ import annotations

import asyncio
import os
import sys

from rag.config.settings import get_settings
from rag.evaluation import retrieval_bench as bench
from rag.jobs.handlers import load_handlers
from rag.retrieval.retriever import Retriever
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.types import SearchFilters
from rag.services import Services


def show(text: str, width: int = 420) -> str:
    return " ".join(text.split())[:width]


async def main(golden: str, kinds: set[str], limit: int, overrides: str) -> None:
    from blend_sweep import apply, principals_of  # same directory

    load_handlers()
    settings = get_settings()
    apply(settings, overrides)
    services = Services.create(settings)
    principals = await principals_of(os.environ.get("BENCH_AS", "admin@example.com"))
    retriever = Retriever(services)
    router = QueryRouter(known_projects=await services.known_projects())
    shown = 0
    try:
        for item in bench.load_questions(golden):
            if item.get("not_found") or (kinds and item.get("kind") not in kinds) or not item.get("pages"):
                continue
            plan = router.rules(item["q"], SearchFilters.from_dict(item.get("filters")))
            result = await retriever.search(plan, principals, rerank=False, debug=True)
            ranked = result.debug["ranked"]
            wanted, pages = bench._files(item), [int(p) for p in item["pages"]]
            hits = [bench._file_ok(c["file_name"], wanted) and bench._page_ok(c["page"], c["page_end"], pages) for c in ranked]
            rank = next((i + 1 for i, ok in enumerate(hits) if ok), None)
            if rank == 1:
                continue
            shown += 1
            print(f"\n=== [{item.get('kind')}] {item['q']}")
            print(f"    right: {wanted} pages {pages}   first right chunk at rank: {rank}")
            top = ranked[0]
            print(f"    TOP   ({top['file_name'][:40]} p{top['page']} fused {top['fused']}): {show(top['text'])}")
            if rank:
                good = ranked[rank - 1]
                print(f"    RIGHT ({good['file_name'][:40]} p{good['page']} fused {good['fused']}): {show(good['text'])}")
            if shown >= limit:
                break
    finally:
        await services.aclose()


if __name__ == "__main__":
    kinds = {k for k in (sys.argv[2] if len(sys.argv) > 2 else "").split(",") if k}
    asyncio.run(main(sys.argv[1], kinds, int(sys.argv[3]) if len(sys.argv) > 3 else 25, sys.argv[4] if len(sys.argv) > 4 else ""))
