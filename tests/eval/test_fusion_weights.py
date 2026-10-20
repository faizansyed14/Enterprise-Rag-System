"""Do the fusion weights hold up when many documents look alike?

A library of near-duplicate specification clauses (same template, different project, element, level and values) is
ingested with the real local models. Three question styles are asked: the project is named; no project but a unique
reference code; no project and a unique person. Each weight set is scored on the same index. The shipped defaults must
not be worse than the dense-heavy weights this test was written to catch (see docs/retrieval.md).

Slow (a few minutes); needs RAG_TEST_MODELS_DIR (all-MiniLM-L6-v2 and ms-marco-MiniLM-L-6-v2)."""

from __future__ import annotations

import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from rag.config.settings import get_settings, set_settings
from rag.db.engine import dispose_engine, transaction
from rag.db.schema import metadata
from rag.jobs import queue
from rag.jobs.handlers import load_handlers
from rag.jobs.queue import NewJob
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.services import Services

from ..conftest import make_test_settings
from ..integration.conftest import add_source, reset_qdrant, run_jobs

MODELS = Path(os.environ.get("RAG_TEST_MODELS_DIR", "/nonexistent"))
ELEMENTS = ["columns", "transfer beams", "shear walls", "ground slab", "retaining walls", "pile caps", "roof slab",
            "stair cores", "lift pits", "podium deck"]
FIRST = ["Karim", "Nadia", "Omar", "Layla", "Yusuf", "Mariam", "Tariq", "Huda", "Samir", "Rania", "Faris", "Dina"]
LAST = ["Haddad", "Farouk", "Saleh", "Nasser", "Mansour", "Aziz", "Khoury", "Bakr", "Hamdan", "Jaber", "Rahal"]
PLACES = ["Harbour", "Marina", "Palm", "Creek", "Desert", "Bay", "Sunset", "Pearl", "Island", "Garden", "Canal", "Sky",
          "Coral", "Royal", "Metro", "Crown", "Oasis", "Summit", "Lagoon", "Vista"]
KINDS = ["View", "Tower", "Residences", "Plaza", "Heights"]

WEIGHT_SETS = {
    "dense-heavy (0.6/0.4)": {"question": {"dense": 0.6, "sparse": 0.4, "exact": 0.25}},
    "balanced (0.3/0.7)": {"question": {"dense": 0.3, "sparse": 0.7, "exact": 0.25}},
    "sparse-leaning (0.15/0.85)": {"question": {"dense": 0.15, "sparse": 0.85, "exact": 0.25}},
    "sparse-heavy (0.1/0.9)": {"question": {"dense": 0.1, "sparse": 0.9, "exact": 0.25}},
}


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _corpus(root: Path, projects: int = 20, per_project: int = 30) -> tuple[list[dict], list[str]]:
    rnd = random.Random(11)
    names = [f"{p} {k}" for p in PLACES for k in KINDS][:projects]
    people: set[str] = set()
    docs: list[dict] = []
    for name in names:
        folder = root / "Projects" / name
        folder.mkdir(parents=True, exist_ok=True)
        combos = [(e, lvl) for e in ELEMENTS for lvl in range(1, 13)]
        rnd.shuffle(combos)
        for element, level in combos[:per_project]:
            while True:
                person = f"{rnd.choice(FIRST)} {rnd.choice('ABCDEFGHJKLMNPRSTW')}. {rnd.choice(LAST)}"
                if person not in people:
                    people.add(person)
                    break
            code = f"S-{rnd.randrange(1000, 9999)}-{len(docs):03d}"
            grade = rnd.choice(["C30/37", "C35/45", "C40/50", "C45/55"])
            cover, cubes = rnd.choice([25, 30, 40, 50, 75]), rnd.choice([2, 3, 4])
            text = (f"Project {name}. Specification clause {code}: {element} on Level {level}. Required concrete grade "
                    f"{grade}. Nominal cover {cover} mm. Test frequency {cubes} cubes per 50 cubic metres. "
                    f"Approved by {person}. The contractor shall submit method statements before casting.")
            (folder / f"clause_{len(docs):03d}.txt").write_text(text, encoding="utf-8")
            docs.append({"project": name, "element": element, "level": level, "code": code, "person": person,
                         "grade": grade, "cover": cover})
    return docs, names


def _questions(docs: list[dict]) -> list[tuple[str, str, str]]:
    rnd = random.Random(5)
    picks = rnd.sample(docs, 105)
    out: list[tuple[str, str, str]] = []
    for d in picks[:35]:
        out.append(("project named", f"What concrete grade is required for {d['element']} on Level {d['level']} in "
                                     f"{d['project']}?", d["code"]))
    for d in picks[35:70]:
        out.append(("reference code", f"What cover does clause {d['code']} require?", d["code"]))
    for d in picks[70:]:
        out.append(("unique person", f"Which {d['element']} on Level {d['level']} did {d['person']} approve?", d["code"]))
    return out


async def _score(retriever: Retriever, router: QueryRouter, questions: list[tuple[str, str, str]]) -> dict[str, dict[str, float]]:
    ranks: dict[str, list[int | None]] = {}
    for style, question, code in questions:
        plan = router.rules(question, SearchFilters())
        result = await retriever.search(plan, ["everyone"], passages=20)
        order = [p.doc_number or "" for p in result.passages]
        texts = [p.text for p in result.passages]
        rank = next((i for i, t in enumerate(texts, 1) if code in t or code in order[i - 1]), None)
        ranks.setdefault(style, []).append(rank)
    table: dict[str, dict[str, float]] = {}
    for style, rs in ranks.items():
        n = len(rs)
        table[style] = {f"hit@{k}": sum(1 for r in rs if r and r <= k) / n for k in (1, 5, 20)}
    return table


@pytest.mark.models
@pytest.mark.skipif(not (MODELS / "all-MiniLM-L6-v2" / "model.onnx").is_file(), reason="set RAG_TEST_MODELS_DIR")
async def test_defaults_hold_up_against_near_duplicates(migrated_db, tmp_path) -> None:  # noqa: ANN001
    port = _port()
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
                                  local_models_url=f"http://127.0.0.1:{port}", qdrant_collection_prefix="tune_chunks",
                                  qdrant_alias="tune_chunks_live")
        set_settings(real)
        await dispose_engine()
        load_handlers()
        from sqlalchemy import text

        names = ", ".join(f'"{t.name}"' for t in metadata.sorted_tables)
        async with transaction() as conn:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        services = Services.create(real)
        try:
            await reset_qdrant(services)
            root = tmp_path / "library"
            docs, projects = _corpus(root)
            source = await add_source("Library", root, ["everyone"])
            async with transaction() as conn:
                await queue.enqueue(conn, NewJob("sync_source", {"source_id": source}, lane="interactive"))
            await run_jobs(services, rounds=6)
            router = QueryRouter(known_projects=projects)
            questions = _questions(docs)
            results: dict[str, dict[str, dict[str, float]]] = {}
            for label, weights in WEIGHT_SETS.items():
                import json

                retriever = Retriever(services)
                retriever.settings = real.model_copy(update={"fusion_weights": json.dumps(weights)})
                results[label] = await _score(retriever, router, questions)
            retriever = Retriever(services)
            results["SHIPPED DEFAULTS"] = await _score(retriever, router, questions)
        finally:
            await services.aclose()
        print(f"\n{len(docs)} near-duplicate clauses, {len(questions)} questions")  # noqa: T201
        for label, table in results.items():
            cells = "  ".join(f"{s}: {t['hit@1']:.0%}/{t['hit@5']:.0%}/{t['hit@20']:.0%}" for s, t in table.items())
            print(f"{label:28} {cells}")  # noqa: T201
        shipped = results["SHIPPED DEFAULTS"]
        best = {style: max(r[style]["hit@20"] for r in results.values()) for style in shipped}
        for style in shipped:
            assert shipped[style]["hit@20"] >= best[style] - 0.03, f"{style}: shipped weights fall behind the best set"
            assert shipped[style]["hit@20"] >= 0.95, style
    finally:
        server.terminate()
        server.wait(timeout=10)
        set_settings(previous)
        await dispose_engine()
