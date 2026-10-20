"""The scheduler only decides *when* work is due and enqueues it; workers do the work.

Every periodic job is enqueued with a dedupe key that includes its time slot, so running two schedulers (for
availability) never doubles the work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob

log = logging.getLogger(__name__)

# (kind, every seconds, lane, payload)
PERIODIC: list[tuple[str, int, str, dict[str, Any]]] = [
    ("janitor", 30, "control", {}),
    ("monitor_tick", 30, "control", {}),
    ("resume_stalled", 120, "bulk", {}),
    ("maintenance", 3600, "bulk", {}),
    ("gc_blobs", 6 * 3600, "bulk", {}),
    ("reconcile", 24 * 3600, "bulk", {}),
]


def _slot(every: int, now: datetime) -> int:
    return int(now.timestamp()) // every


async def tick(settings: Any, now: datetime | None = None) -> int:
    """Enqueue whatever is due right now. Returns how many jobs were enqueued (or merged)."""
    now = now or datetime.now(UTC)
    jobs = [NewJob(kind, payload, lane=lane, dedupe_key=f"periodic:{kind}:{_slot(every, now)}", max_attempts=3)
            for kind, every, lane, payload in PERIODIC]
    async with transaction() as conn:
        due_sources = (await conn.execute(text(
            "SELECT id FROM sources WHERE enabled AND schedule_minutes IS NOT NULL AND schedule_minutes > 0 "
            "AND (last_sync_started_at IS NULL "
            "OR last_sync_started_at < now() - make_interval(mins => schedule_minutes))"))).all()
        jobs += [NewJob("sync_source", {"source_id": str(r.id)}, lane="bulk", dedupe_key=f"sync:{r.id}",
                        source_id=r.id) for r in due_sources]
        if settings.auth_mode in {"entra", "both"} and settings.entra_client_id:
            stale_users = (await conn.execute(text(
                "SELECT id FROM users WHERE auth_provider = 'entra' AND is_active "
                "AND last_login_at > now() - interval '14 days' "
                "AND (principals_synced_at IS NULL OR principals_synced_at < now() - make_interval(mins => :m)) "
                "LIMIT 500"), {"m": settings.principal_sync_minutes})).all()
            jobs += [NewJob("sync_principals", {"user_id": str(r.id)}, lane="control",
                            dedupe_key=f"principals:{r.id}") for r in stale_users]
            jobs.append(NewJob("sync_site_groups", {}, lane="control",
                               dedupe_key=f"periodic:site_groups:{_slot(settings.principal_sync_minutes * 60, now)}"))
        return await queue.enqueue_many(conn, jobs)


async def run(settings: Any, *, interval_s: float = 5.0, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    log.info("scheduler started (every %ss)", interval_s)
    while not stop.is_set():
        try:
            await tick(settings)
        except Exception:
            log.warning("scheduler tick failed", exc_info=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
