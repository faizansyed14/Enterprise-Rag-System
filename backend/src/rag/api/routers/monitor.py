"""Monitor page (administrators): live counters, model calls by role, latency, cost against budget, queues, workers,
alerts, recent failures and traces. Everything comes from the database rollups, so it covers every API and worker
process, and it updates over server-sent events every 2 seconds."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from rag.api.deps import AdminDep, ServicesDep, client_ip
from rag.api.sse import sse_response
from rag.cache import get_cache
from rag.db.engine import transaction
from rag.jobs import queue
from rag.monitoring.alerts import open_alerts
from rag.monitoring.recorder import percentile_from_buckets
from rag.security.audit import audit

router = APIRouter(tags=["monitor"])


async def _model_rows(conn: Any, minutes: int) -> list[dict[str, Any]]:
    rows = (await conn.execute(text(
        """
        SELECT kind, role, provider, model, sum(calls) AS calls, sum(errors) AS errors, sum(total_ms) AS total_ms,
               max(max_ms) AS max_ms, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
               sum(cost_usd) AS cost, array_agg(buckets) AS bucket_sets
        FROM metric_minutes WHERE minute >= now() - make_interval(mins => :m)
          AND kind IN ('chat','vision','embedder','reranker','groundedness','ocr','parser')
        GROUP BY kind, role, provider, model ORDER BY sum(calls) DESC
        """), {"m": minutes})).mappings().all()
    out = []
    for r in rows:
        merged: list[int] = []
        for buckets in r["bucket_sets"] or []:
            for i, v in enumerate(buckets or []):
                if i >= len(merged):
                    merged.append(0)
                merged[i] += v
        calls = int(r["calls"] or 0)
        out.append({"kind": r["kind"], "role": r["role"], "provider": r["provider"], "model": r["model"],
                    "calls": calls, "errors": int(r["errors"] or 0),
                    "avg_ms": round(float(r["total_ms"] or 0) / calls, 1) if calls else None,
                    "p50_ms": percentile_from_buckets(merged, 0.5), "p95_ms": percentile_from_buckets(merged, 0.95),
                    "max_ms": round(float(r["max_ms"] or 0), 1), "tokens_in": int(r["tokens_in"] or 0),
                    "tokens_out": int(r["tokens_out"] or 0), "cost_usd": round(float(r["cost"] or 0), 6)})
    return out


async def _live(conn: Any) -> dict[str, Any]:
    procs = (await conn.execute(text(
        "SELECT process_id, kind, host, pid, version, started_at, heartbeat_at, stats, "
        "EXTRACT(EPOCH FROM now() - heartbeat_at) AS age FROM process_stats ORDER BY kind, host, pid"))
    ).mappings().all()
    return {
        "processes": [{"id": p["process_id"], "kind": p["kind"], "host": p["host"], "pid": p["pid"],
                       "started_at": p["started_at"].isoformat(), "age_s": round(float(p["age"]), 1),
                       "online": float(p["age"]) < 15, "stats": p["stats"]} for p in procs],
        "queue": await queue.queue_stats(conn),
    }


@router.get("/monitor/summary")
async def summary(_: AdminDep, services: ServicesDep, minutes: int = 60) -> dict:
    minutes = max(5, min(minutes, 60 * 24 * 7))
    s = services.settings
    async with transaction() as conn:
        chat = (await conn.execute(text(
            """
            SELECT count(*) AS requests, count(*) FILTER (WHERE status <> 'ok') AS errors,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
            FROM traces WHERE kind = 'chat' AND started_at >= now() - make_interval(mins => :m)
            """), {"m": minutes})).mappings().first()
        cost_today = (await conn.execute(text(
            "SELECT coalesce(sum(cost_usd), 0) FROM usage_daily WHERE day = (now() AT TIME ZONE 'UTC')::date")
        )).scalar_one()
        content = (await conn.execute(text(
            """
            SELECT (SELECT count(*) FROM documents WHERE status = 'active') AS documents,
                   (SELECT count(*) FROM versions WHERE state = 'live') AS live_versions,
                   (SELECT count(*) FROM versions WHERE state IN ('pending','chunked','embedded')) AS in_progress,
                   (SELECT count(*) FROM versions WHERE state = 'failed') AS failed_versions,
                   (SELECT count(*) FROM source_items WHERE status IN ('error','skipped')) AS failed_items,
                   (SELECT coalesce(sum(chunk_count), 0) FROM versions WHERE state = 'live') AS chunks,
                   (SELECT coalesce(sum(page_count), 0) FROM versions WHERE state = 'live') AS pages
            """))).mappings().first()
        failures = (await conn.execute(text(
            "SELECT id, kind, lane, status, attempts, last_error, finished_at FROM jobs "
            "WHERE status IN ('dead', 'failed') ORDER BY finished_at DESC NULLS LAST LIMIT 20"))).mappings().all()
        model_errors = (await conn.execute(text(
            "SELECT started_at, role, provider, model, error, trace_id FROM spans WHERE status = 'error' "
            "AND started_at >= now() - make_interval(mins => :m) ORDER BY started_at DESC LIMIT 20"),
            {"m": minutes})).mappings().all()
        models = await _model_rows(conn, minutes)
        live = await _live(conn)
    return {
        "window_minutes": minutes,
        "chat": {"requests": chat["requests"], "errors": chat["errors"],
                 "p50_ms": round(chat["p50"] or 0, 1), "p95_ms": round(chat["p95"] or 0, 1)},
        "cost": {"today_usd": round(float(cost_today), 4), "budget_usd": s.daily_budget_usd},
        "content": dict(content), "models": models, "roles": services.hub.describe(), **live,
        "alerts": await open_alerts(), "cache": await get_cache(s).stats(),
        "failures": [{**dict(f), "finished_at": f["finished_at"].isoformat() if f["finished_at"] else None,
                      "last_error": (f["last_error"] or "")[:400]} for f in failures],
        "model_errors": [{**dict(e), "started_at": e["started_at"].isoformat()} for e in model_errors],
    }


@router.get("/monitor/timeseries")
async def timeseries(_: AdminDep, minutes: int = 60) -> dict:
    minutes = max(5, min(minutes, 60 * 24))
    async with transaction() as conn:
        chat = (await conn.execute(text(
            """
            SELECT date_trunc('minute', started_at) AS minute, count(*) AS requests,
                   count(*) FILTER (WHERE status <> 'ok') AS errors,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
            FROM traces WHERE kind = 'chat' AND started_at >= now() - make_interval(mins => :m)
            GROUP BY 1 ORDER BY 1"""), {"m": minutes})).mappings().all()
        calls = (await conn.execute(text(
            """
            SELECT minute, kind, sum(calls) AS calls, sum(errors) AS errors, sum(cost_usd) AS cost
            FROM metric_minutes WHERE minute >= now() - make_interval(mins => :m) GROUP BY minute, kind
            ORDER BY minute"""), {"m": minutes})).mappings().all()
    return {
        "chat": [{"minute": r["minute"].isoformat(), "requests": r["requests"], "errors": r["errors"],
                  "p95_ms": round(r["p95"] or 0, 1)} for r in chat],
        "calls": [{"minute": r["minute"].isoformat(), "kind": r["kind"], "calls": int(r["calls"]),
                   "errors": int(r["errors"]), "cost_usd": float(r["cost"] or 0)} for r in calls],
    }


@router.get("/monitor/stream")
async def stream(request: Request, _: AdminDep):
    async def events() -> AsyncIterator[dict[str, Any]]:
        for _tick in range(1800):  # one hour per connection; the browser reconnects
            if await request.is_disconnected():
                return
            async with transaction() as conn:
                live = await _live(conn)
                live["models"] = await _model_rows(conn, 5)
            live["alerts"] = len(await open_alerts())
            yield {"event": "live", "data": live}
            await asyncio.sleep(2)

    return sse_response(events())


@router.get("/monitor/traces")
async def traces(_: AdminDep, kind: str = "chat", limit: int = 50, status_filter: str = "") -> dict:
    sql = (
        "SELECT t.id, t.started_at, t.kind, t.name, t.status, t.duration_ms, t.attrs, u.email FROM traces t "  # noqa: S608 - fixed fragments, bound values
           "LEFT JOIN users u ON u.id = t.user_id WHERE t.kind = :k AND t.started_at >= now() - interval '7 days' "
           + ("AND t.status = :s " if status_filter else "") + "ORDER BY t.started_at DESC LIMIT :l")
    async with transaction() as conn:
        rows = (await conn.execute(text(sql), {"k": kind, "s": status_filter, "l": min(limit, 200)})).mappings().all()
    return {"traces": [{**dict(r), "started_at": r["started_at"].isoformat()} for r in rows]}


@router.get("/monitor/traces/{trace_id}")
async def trace_detail(trace_id: str, _: AdminDep) -> dict:
    async with transaction() as conn:
        t = (await conn.execute(text("SELECT * FROM traces WHERE id = :i ORDER BY started_at DESC LIMIT 1"),
                                {"i": trace_id})).mappings().first()
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found (kept MONITOR_RETENTION_DAYS)")
        spans = (await conn.execute(text("SELECT * FROM spans WHERE trace_id = :i ORDER BY started_at"),
                                    {"i": trace_id})).mappings().all()
    return {"trace": {**dict(t), "started_at": t["started_at"].isoformat()},
            "spans": [{**dict(s), "started_at": s["started_at"].isoformat()} for s in spans]}


# ---- jobs --------------------------------------------------------------------------------------------------------------


@router.get("/jobs")
async def list_jobs(_: AdminDep, status_filter: str = "", kind: str = "", lane: str = "", limit: int = 50,
                    offset: int = 0) -> dict:
    where = ["true"]
    params: dict[str, Any] = {"l": min(limit, 200), "o": max(0, offset)}
    if status_filter:
        where.append("status = :s")
        params["s"] = status_filter
    if kind:
        where.append("kind = :k")
        params["k"] = kind
    if lane:
        where.append("lane = :lane")
        params["lane"] = lane
    sql = (f"SELECT id, kind, lane, priority, status, attempts, max_attempts, run_after, created_at, started_at, "  # noqa: S608
           f"finished_at, last_error, error_class, payload, rate_limited FROM jobs WHERE {' AND '.join(where)} "
           f"ORDER BY id DESC LIMIT :l OFFSET :o")
    async with transaction() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
        stats = await queue.queue_stats(conn)
    def iso(v: Any) -> Any:
        return v.isoformat() if hasattr(v, "isoformat") else v
    return {"jobs": [{k: iso(v) for k, v in dict(r).items()} for r in rows], "stats": stats}


class JobIds(BaseModel):
    ids: list[int]


@router.post("/jobs/retry")
async def retry_jobs(body: JobIds, request: Request, user: AdminDep) -> dict:
    count = await queue.retry(body.ids)
    await audit("jobs_retry", user_id=user.id, ip=client_ip(request), details={"ids": body.ids[:100]})
    return {"retried": count}


@router.post("/jobs/cancel")
async def cancel_jobs(body: JobIds, request: Request, user: AdminDep) -> dict:
    count = await queue.cancel(body.ids)
    await audit("jobs_cancel", user_id=user.id, ip=client_ip(request), details={"ids": body.ids[:100]})
    return {"cancelled": count}
