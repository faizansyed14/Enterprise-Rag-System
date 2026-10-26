"""Compare reranker blend strategies in ONE process, so each (question, chunk) pair is scored by the cross-encoder once and every
later configuration reads the score from the cache. Retrieval only: no language model.

  python /eval/blend_sweep.py <golden.jsonl> <reranker> [name=value,name=value ...] ...

`reranker` is the model spec (local/ms-marco-MiniLM-L-6-v2, ...) or `none`. Each remaining argument is one configuration:
comma-separated setting overrides, optionally prefixed `label|`. The first configuration pays for the model; the rest are
nearly free. One line per configuration and question kind: pg@1 / pg@3 / mrrP.
"""

from __future__ import annotations

import asyncio
import os
import pickle
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path

from rag.config.settings import get_settings
from rag.evaluation import retrieval_bench as bench
from rag.jobs.handlers import load_handlers
from rag.services import Services

KINDS = ("ALL", "exact", "keyword", "noisy", "paraphrase", "table")


async def principals_of(email: str) -> list[str]:
    """What the CLI's `--as` does: the access list of that user (documents are filed under principals, not 'everyone')."""
    from sqlalchemy import func, select

    from rag.db import schema
    from rag.db.engine import transaction
    from rag.security.principals import principals_for

    async with transaction() as conn:
        user = (await conn.execute(select(schema.users).where(func.lower(schema.users.c.email) == email.lower()))).first()
        return await principals_for(conn, user)


def apply(settings, overrides: str) -> dict:
    old = {}
    for pair in filter(None, overrides.split(",")):
        key, _, value = pair.partition("=")
        current = getattr(settings, key)
        old[key] = current
        setattr(settings, key, value.lower() in {"1", "true", "yes"} if isinstance(current, bool) else type(current)(value))
    return old


async def main(golden: str, reranker: str, configs: list[str]) -> None:
    load_handlers()
    settings = get_settings()
    if reranker != "none":
        settings.reranker = reranker
        settings.reranker_languages = "*" if "mmarco" in reranker or "bge" in reranker else "en"
    services = Services.create(settings)
    cache_file = Path(os.environ.get("RERANK_CACHE_DIR", "/eval/.cache")) / f"rerank_{re.sub(r'[^A-Za-z0-9]+', '_', reranker)}.pkl"
    cache = services.__dict__.setdefault("_rerank_cache", OrderedDict())
    if cache_file.exists():  # scores of (reranker, question, chunk text) never change, so earlier runs are reused
        cache.update(pickle.loads(cache_file.read_bytes()))
        print(f"{len(cache)} rerank scores loaded from {cache_file.name}", flush=True)
    settings.rerank_cache_size = 1_000_000
    principals = await principals_of(os.environ.get("BENCH_AS") or os.environ["BOOTSTRAP_ADMIN_EMAIL"])
    items = bench.load_questions(golden)
    print(f"{len(items)} questions, reranker {reranker}", flush=True)
    header = "config".ljust(34) + "".join(f"{k:>22}" for k in KINDS)
    print(header + "\n" + "(each cell: pg@1 / pg@3 / mrrP)".rjust(len(header)), flush=True)
    try:
        for entry in configs:
            label, _, overrides = entry.partition("|")
            if not overrides and "=" in label:
                overrides = label
            old = apply(settings, overrides)
            started = time.perf_counter()
            rows, summary = await bench.run(services, items, principals, rerank=reranker != "none")
            took = time.perf_counter() - started
            cells = []
            for kind in KINDS:
                block = summary["overall"] if kind == "ALL" else summary["by_kind"].get(kind, {})
                cells.append(f"{block.get('page_hit@1', 0) * 100:5.1f}/{block.get('page_hit@3', 0) * 100:5.1f}/{block.get('mrr_page', 0):.3f}")
            print(label[:33].ljust(34) + "".join(f"{c:>22}" for c in cells) + f"   {took:5.0f}s", flush=True)
            for key, value in old.items():
                setattr(settings, key, value)
            if reranker != "none":
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(pickle.dumps(dict(cache)))
    finally:
        await services.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
