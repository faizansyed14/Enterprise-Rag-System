"""Prometheus exposition (PROMETHEUS_METRICS=true, scraped from /metrics). Built from the shared database rollups,
so one scrape of any API replica covers the whole deployment."""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from rag.db.engine import transaction
from rag.jobs import queue

_CACHE: dict[str, Any] = {"at": 0.0, "body": b""}


class _Snapshot:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def collect(self):
        d = self.data
        # Rolling windows are gauges: a 24-hour sum goes down as old minutes leave the window.
        calls = GaugeMetricFamily("rag_model_calls_24h", "Model calls in the last 24 hours",
                                  labels=["kind", "role", "provider", "model", "status"])
        for r in d["models"]:
            labels = [r["kind"], r["role"], r["provider"], r["model"]]
            calls.add_metric([*labels, "ok"], r["calls"] - r["errors"])
            calls.add_metric([*labels, "error"], r["errors"])
        yield calls
        latency = GaugeMetricFamily("rag_model_latency_avg_ms", "Average model call latency, last 5 minutes",
                                    labels=["kind", "role", "model"])
        for r in d["recent"]:
            if r["calls"]:
                latency.add_metric([r["kind"], r["role"], r["model"]], r["total_ms"] / r["calls"])
        yield latency
        questions = GaugeMetricFamily("rag_questions_5m", "Questions answered in the last 5 minutes", labels=["status"])
        for status, n in d["questions"].items():
            questions.add_metric([status], n)
        yield questions
        p95 = GaugeMetricFamily("rag_answer_p95_ms_5m", "95th percentile answer time over the last 5 minutes")
        p95.add_metric([], d["answer_p95"])
        yield p95
        cost = GaugeMetricFamily("rag_cost_usd_today", "Model spend today in USD")
        cost.add_metric([], d["cost_today"])
        yield cost
        budget = GaugeMetricFamily("rag_daily_budget_usd", "Daily model budget in USD (DAILY_BUDGET_USD)")
        budget.add_metric([], d["budget"])
        yield budget
        depth = GaugeMetricFamily("rag_queue_jobs", "Jobs by lane and status", labels=["lane", "status"])
        oldest = GaugeMetricFamily("rag_queue_oldest_ready_seconds", "Age of the oldest runnable job", labels=["lane"])
        for lane, stats in d["queue"]["lanes"].items():
            for status in ("queued", "running", "dead", "failed"):
                depth.add_metric([lane, status], stats.get(status, 0))
            oldest.add_metric([lane], stats.get("oldest_ready_s", 0.0))
        yield depth
        yield oldest
        procs = GaugeMetricFamily("rag_processes_online", "Processes with a heartbeat in the last 15s", labels=["kind"])
        for kind, n in d["processes"].items():
            procs.add_metric([kind], n)
        yield procs
        docs = GaugeMetricFamily("rag_live_documents", "Live document versions")
        docs.add_metric([], d["live_versions"])
        yield docs
        alerts = GaugeMetricFamily("rag_open_alerts", "Open alerts")
        alerts.add_metric([], d["alerts"])
        yield alerts


async def render(services: Any) -> tuple[bytes, str]:
    if time.monotonic() - _CACHE["at"] < 10:
        return _CACHE["body"], CONTENT_TYPE_LATEST
    async with transaction() as conn:
        models = (await conn.execute(text(
            "SELECT kind, role, provider, model, sum(calls) AS calls, sum(errors) AS errors FROM metric_minutes "
            "WHERE minute >= now() - interval '24 hours' GROUP BY 1, 2, 3, 4"))).mappings().all()
        recent = (await conn.execute(text(
            "SELECT kind, role, model, sum(calls) AS calls, sum(total_ms) AS total_ms FROM metric_minutes "
            "WHERE minute >= now() - interval '5 minutes' GROUP BY 1, 2, 3"))).mappings().all()
        cost_today = (await conn.execute(text(
            "SELECT coalesce(sum(cost_usd), 0) FROM usage_daily WHERE day = (now() AT TIME ZONE 'UTC')::date"))
        ).scalar_one()
        stats = await queue.queue_stats(conn)
        procs = (await conn.execute(text(
            "SELECT kind, count(*) FROM process_stats WHERE heartbeat_at > now() - interval '15 seconds' "
            "GROUP BY kind"))).all()
        live = (await conn.execute(text("SELECT count(*) FROM versions WHERE state = 'live'"))).scalar_one()
        chat = (await conn.execute(text(
            "SELECT coalesce(attrs->>'status', status) AS status, count(*) FROM traces "
            "WHERE kind = 'chat' AND started_at >= now() - interval '5 minutes' GROUP BY 1"))).all()
        answer_p95 = (await conn.execute(text(
            "SELECT coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), 0) FROM traces "
            "WHERE kind = 'chat' AND started_at >= now() - interval '5 minutes'"))).scalar_one()
        alerts = (await conn.execute(text("SELECT count(*) FROM alerts WHERE resolved_at IS NULL"))).scalar_one()
    registry = CollectorRegistry()
    registry.register(_Snapshot({
        "models": [{k: (int(v) if k in {"calls", "errors"} else v) for k, v in dict(r).items()} for r in models],
        "recent": [{**dict(r), "calls": int(r["calls"]), "total_ms": float(r["total_ms"])} for r in recent],
        "cost_today": float(cost_today), "budget": float(services.settings.daily_budget_usd), "queue": stats,
        "processes": dict(procs), "live_versions": live, "alerts": alerts,
        "questions": {str(k): int(n) for k, n in chat}, "answer_p95": float(answer_p95 or 0)}))
    body = generate_latest(registry)
    _CACHE.update(at=time.monotonic(), body=body)
    return body, CONTENT_TYPE_LATEST
