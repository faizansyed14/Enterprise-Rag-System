"""Housekeeping jobs enqueued by the scheduler:

  janitor       every 30 s   give jobs of dead workers back to the queue
  monitor_tick  every 30 s   evaluate alert rules
  maintenance   hourly       daily partitions ahead/behind, retention, expired sessions, old finished jobs
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from rag.db.engine import maintain_partitions, transaction
from rag.jobs import queue
from rag.jobs.worker import JobContext, handler
from rag.monitoring import alerts

log = logging.getLogger(__name__)


@handler("janitor")
async def janitor(ctx: JobContext) -> None:
    result = await queue.requeue_expired()
    if result["requeued"] or result["dead"]:
        log.warning("janitor: %s jobs re-queued, %s moved to dead letters", result["requeued"], result["dead"])


@handler("monitor_tick")
async def monitor_tick(ctx: JobContext) -> None:
    await alerts.evaluate()


@handler("maintenance")
async def maintenance(ctx: JobContext) -> None:
    s = ctx.settings
    async with transaction() as conn:
        parts = await maintain_partitions(conn, s.monitor_retention_days)
        await conn.execute(text("DELETE FROM metric_minutes WHERE minute < now() - make_interval(days => :d)"),
                           {"d": s.metric_retention_days})
        await conn.execute(text("DELETE FROM process_stats WHERE heartbeat_at < now() - interval '15 minutes'"))
        await conn.execute(text("DELETE FROM rate_limits WHERE window_start < now() - interval '1 day'"))
        await conn.execute(text("DELETE FROM sessions WHERE expires_at < now()"))
        await conn.execute(text("DELETE FROM alerts WHERE resolved_at < now() - interval '90 days'"))
        await conn.execute(text("DELETE FROM daily_budgets WHERE day < (now() - interval '90 days')::date"))
    removed = await queue.cleanup_finished(s.keep_done_jobs_days)
    log.info("maintenance: partitions %s, %s finished jobs removed", parts, removed)
