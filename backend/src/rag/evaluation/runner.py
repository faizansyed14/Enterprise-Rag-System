"""Evaluation runs: retrieval quality (hit@k, MRR, page hits) and answer quality (required facts present, wrong facts
absent, verified citations, groundedness, correct "not found"), with latency. Started from the Evaluation page,
`rag eval run`, or CI. Results are stored per question so two runs can be compared side by side.
"""

from __future__ import annotations

import logging
import statistics
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from rag.answering.agent import Answerer, AnswerOutcome
from rag.db import schema
from rag.db.engine import transaction
from rag.evaluation.golden import load_set
from rag.jobs.worker import JobContext, handler
from rag.metadata.identifiers import identifier_key
from rag.retrieval.query_router import QueryRouter
from rag.retrieval.retriever import Retriever
from rag.retrieval.types import SearchFilters
from rag.security.principals import principals_for

log = logging.getLogger(__name__)
K_VALUES = (1, 3, 5, 10)


def _matches(expected: list[str], doc: dict[str, Any]) -> bool:
    number = doc.get("doc_number") or ""
    name = (doc.get("file_name") or "").lower()
    title = (doc.get("title") or "").lower()
    for want in expected:
        if number and identifier_key(want) == identifier_key(number):
            return True
        lowered = want.lower()
        if lowered in name or (len(lowered) > 6 and lowered in title):
            return True
    return False


def score_retrieval(expect: dict[str, Any], docs: list[dict[str, Any]], passages: list[dict[str, Any]]
                    ) -> dict[str, Any]:
    wanted = expect.get("documents") or []
    if not wanted:
        return {}
    project = expect.get("project")

    def ok(doc: dict[str, Any]) -> bool:
        if not _matches(wanted, doc):
            return False
        return not (project and doc.get("project") and doc["project"] != project)

    rank = next((i + 1 for i, d in enumerate(docs) if ok(d)), None)
    metrics: dict[str, Any] = {f"hit@{k}": bool(rank and rank <= k) for k in K_VALUES}
    metrics["mrr"] = round(1.0 / rank, 4) if rank else 0.0
    metrics["rank"] = rank
    pages = expect.get("pages")
    if pages:
        metrics["page_hit@5"] = any(ok(p) and p.get("page_start") in pages for p in passages[:5])
    return metrics


def score_answer(expect: dict[str, Any], outcome: AnswerOutcome) -> dict[str, Any]:
    answer = outcome.answer.lower()
    metrics: dict[str, Any] = {"status": outcome.status, "groundedness": outcome.groundedness}
    needs = expect.get("answer_contains") or []
    if needs:
        found = [n for n in needs if n.lower() in answer]
        metrics["facts"] = round(len(found) / len(needs), 3)
    forbidden = expect.get("answer_not_contains") or []
    if forbidden:
        metrics["no_wrong_facts"] = not any(f.lower() in answer for f in forbidden)
    if "not_found" in expect:
        metrics["not_found_correct"] = (outcome.status == "not_found") == bool(expect["not_found"])
    if outcome.citations:
        metrics["citations_verified"] = round(sum(1 for c in outcome.citations if c.get("verified"))
                                              / len(outcome.citations), 3)
    return metrics


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"questions": len(rows)}
    keys = sorted({k for r in rows for k, v in r["metrics"].items() if isinstance(v, (bool, int, float))
                   and k not in {"rank"}})
    for key in keys:
        values = [float(r["metrics"][key]) for r in rows if isinstance(r["metrics"].get(key), (bool, int, float))
                  and r["metrics"].get(key) is not None]
        if values:
            out[key] = round(sum(values) / len(values), 4)
    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    if latencies:
        out["latency_p50_ms"] = round(statistics.median(latencies), 1)
        out["latency_p95_ms"] = round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
    return out


async def evaluate(services: Any, items: list[dict[str, Any]], principals: list[str], *, answer: bool = True
                   ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    retriever = Retriever(services)
    router = QueryRouter(known_projects=await services.known_projects())
    rows: list[dict[str, Any]] = []
    for item in items:
        expect = item.get("expect") or {}
        who = item.get("principals") or principals
        filters = SearchFilters.from_dict(item.get("filters"))
        start = time.perf_counter()
        plan = router.rules(item["question"], filters)
        result = await retriever.search(plan, who)
        docs = [d.__dict__ for d in result.documents]
        passages = [p.as_dict() for p in result.passages]
        metrics = score_retrieval(expect, docs, passages)
        answer_text = None
        if answer:
            outcome = AnswerOutcome()
            async for _ in Answerer(services).answer(item["question"], principals=who, filters=filters,
                                                     outcome=outcome):
                pass
            metrics.update(score_answer(expect, outcome))
            answer_text = outcome.answer
        latency = (time.perf_counter() - start) * 1000
        rows.append({"item_id": item["id"], "question": item["question"], "expected": expect,
                     "retrieved": [{"doc_number": d["doc_number"], "title": d["title"],
                                    "file_name": d["file_name"], "score": d["score"]} for d in docs[:10]],
                     "answer": answer_text, "metrics": metrics, "latency_ms": round(latency, 1)})
    return rows, aggregate(rows)


@handler("eval_run")
async def eval_run(ctx: JobContext) -> None:
    run_id = ctx.payload["run_id"]
    async with transaction() as conn:
        run = (await conn.execute(select(schema.eval_runs).where(schema.eval_runs.c.id == run_id))).first()
        if run is None:
            return
        user = (await conn.execute(select(schema.users).where(
            schema.users.c.id == ctx.payload.get("principal_user")))).first()
        principals = await principals_for(conn, user) if user else ["everyone"]
        await conn.execute(update(schema.eval_runs).where(schema.eval_runs.c.id == run_id)
                           .values(status="running", started_at=datetime.now(UTC)))
    try:
        items = load_set(run.golden_set)
        rows, metrics = await evaluate(ctx.services, items, principals, answer=bool(run.config.get("answer", True)))
    except Exception as exc:
        async with transaction() as conn:
            await conn.execute(update(schema.eval_runs).where(schema.eval_runs.c.id == run_id).values(
                status="failed", error=f"{type(exc).__name__}: {exc}"[:1000], finished_at=datetime.now(UTC)))
        raise
    async with transaction() as conn:
        await conn.execute(schema.eval_items.delete().where(schema.eval_items.c.run_id == run_id))
        if rows:
            await conn.execute(schema.eval_items.insert(), [{"run_id": run_id, **r} for r in rows])
        await conn.execute(update(schema.eval_runs).where(schema.eval_runs.c.id == run_id).values(
            status="done", metrics=metrics, finished_at=datetime.now(UTC)))
    log.info("evaluation %s finished: %s", run_id, metrics)
