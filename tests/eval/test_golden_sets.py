"""Evaluation gates on the demo golden set.

1. Offline models (hashing embedder, lexical reranker, extractive answers): the pipeline's structure alone --
   identifiers, BM25, fusion, access control, families -- must already find the right documents.
2. Real local models (all-MiniLM-L6-v2 embedder + ms-marco-MiniLM-L-6-v2 reranker on the ONNX model server),
   when RAG_TEST_MODELS_DIR points at them.

Metrics are printed so the numbers appear in the test log.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from rag.config.settings import get_settings, set_settings
from rag.db.engine import dispose_engine, transaction
from rag.db.schema import metadata
from rag.evaluation.golden import load_set
from rag.evaluation.runner import evaluate
from rag.jobs import queue
from rag.jobs.handlers import load_handlers
from rag.jobs.queue import NewJob
from rag.services import Services

from ..conftest import make_test_settings
from ..integration.conftest import add_source, reset_qdrant, run_jobs

MODELS = Path(os.environ.get("RAG_TEST_MODELS_DIR", "./models"))


async def _ingest(services: Services, corpus_root: Path, target: Path) -> None:
    names = ", ".join(f'"{t.name}"' for t in metadata.sorted_tables)
    async with transaction() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    shutil.rmtree(Path(services.settings.object_store_url.removeprefix("file://")), ignore_errors=True)
    shutil.copytree(corpus_root, target)
    await reset_qdrant(services)
    for name, folder, acl in (("Library", target / "public", ["everyone"]),
                              ("Commercial", target / "restricted", ["lgroup:commercial"])):
        source = await add_source(name, folder, acl)
        async with transaction() as conn:
            await queue.enqueue(conn, NewJob("sync_source", {"source_id": source}, lane="interactive"))
    await run_jobs(services)


def _report(label: str, rows: list[dict], metrics: dict) -> None:
    print(f"\n=== {label} ===")  # noqa: T201
    for r in rows:
        m = r["metrics"]
        print(f"  {r['item_id']:18} rank={m.get('rank')!s:4} facts={m.get('facts', '-')!s:5} "  # noqa: T201
              f"status={m.get('status')}")
    print("  " + ", ".join(f"{k}={v}" for k, v in metrics.items()))  # noqa: T201


async def test_offline_pipeline_meets_the_retrieval_gate(migrated_db, settings, corpus_root, tmp_path) -> None:  # noqa: ANN001
    load_handlers()
    services = Services.create(settings)
    try:
        await _ingest(services, corpus_root, tmp_path / "corpus")
        rows, metrics = await evaluate(services, load_set("demo"), ["everyone", "user:eval"], answer=True)
    finally:
        await services.aclose()
    _report("offline models", rows, metrics)
    assert metrics["hit@5"] >= 0.9
    assert metrics["mrr"] >= 0.75
    assert metrics["not_found_correct"] == 1.0
    assert metrics["no_wrong_facts"] == 1.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.models
@pytest.mark.skipif(not (MODELS / "all-MiniLM-L6-v2" / "model.onnx").is_file(),
                    reason="set RAG_TEST_MODELS_DIR to a folder with all-MiniLM-L6-v2 and ms-marco-MiniLM-L-6-v2")
async def test_real_local_models_meet_the_quality_gate(migrated_db, corpus_root, tmp_path) -> None:  # noqa: ANN001
    port = _free_port()
    env = {**os.environ, "MODELS_DIR": str(MODELS), "LOCAL_MODELS": "all-MiniLM-L6-v2,ms-marco-MiniLM-L-6-v2"}
    server = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "rag_model_server.cli", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    previous = get_settings()
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        real = make_test_settings(tmp_path, embedder="local/all-MiniLM-L6-v2", reranker="local/ms-marco-MiniLM-L-6-v2",
                                  local_models_url=f"http://127.0.0.1:{port}", qdrant_collection_prefix="eval_chunks",
                                  qdrant_alias="eval_chunks_live")
        set_settings(real)
        await dispose_engine()
        load_handlers()
        services = Services.create(real)
        try:
            await _ingest(services, corpus_root, tmp_path / "corpus")
            assert (await services.chunker()).counter.exact, "chunks should be sized with the model's tokenizer"
            rows, metrics = await evaluate(services, load_set("demo"), ["everyone", "user:eval"], answer=True)
        finally:
            await services.aclose()
        _report("all-MiniLM-L6-v2 + ms-marco-MiniLM-L-6-v2", rows, metrics)
        assert metrics["hit@5"] >= 0.9
        assert metrics["mrr"] >= 0.8
        assert metrics["no_wrong_facts"] == 1.0
    finally:
        server.terminate()
        server.wait(timeout=10)
        set_settings(previous)
        await dispose_engine()
