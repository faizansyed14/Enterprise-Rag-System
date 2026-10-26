"""Postgres job queue with lanes, priorities, leases, retries and dead letters.

Lanes are served strictly in order (control, then interactive, then bulk) and workers can be pinned to lanes, so a
million-file backfill never delays a permission change or a user's upload.

Leasing uses SELECT ... FOR UPDATE SKIP LOCKED on a partial index of queued rows: workers never block each other and
nothing depends on session state, so it works through PgBouncer in transaction mode. A lease is a deadline, renewed
by heartbeats; a worker that dies simply stops renewing and the janitor puts its jobs back.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from rag.db import schema
from rag.db.engine import transaction
from rag.jobs.retry_policy import Decision

log = logging.getLogger(__name__)

LANES: dict[str, int] = {"control": 0, "interactive": 1, "bulk": 2}
J = schema.jobs.c


@dataclass
class NewJob:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    lane: str = "bulk"
    priority: int = 0
    dedupe_key: str | None = None
    run_after: datetime | None = None
    max_attempts: int | None = None
    source_id: UUID | str | None = None
    version_id: UUID | str | None = None
    trace_id: str | None = None


@dataclass
class Job:
    id: int
    kind: str
    lane: str
    priority: int
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    progress: dict[str, Any]
    source_id: UUID | None
    version_id: UUID | None
    trace_id: str | None
    leased_by: str | None
    lease_expires_at: datetime | None
    rate_limited: int = 0

    @classmethod
    def from_row(cls, row: Any) -> Job:
        m = row._mapping
        return cls(
            id=m["id"], kind=m["kind"], lane=m["lane"], priority=m["priority"], payload=dict(m["payload"] or {}),
            attempts=m["attempts"], max_attempts=m["max_attempts"], dedupe_key=m["dedupe_key"],
            progress=dict(m["progress"] or {}), source_id=m["source_id"], version_id=m["version_id"],
            trace_id=m["trace_id"], leased_by=m["leased_by"], lease_expires_at=m["lease_expires_at"],
            rate_limited=m["rate_limited"],
        )


def _values(job: NewJob, default_max_attempts: int) -> dict[str, Any]:
    if job.lane not in LANES:
        raise ValueError(f"unknown lane {job.lane!r}")
    return {
        "kind": job.kind,
        "lane": job.lane,
        "lane_rank": LANES[job.lane],
        "priority": job.priority,
        "payload": job.payload,
        "dedupe_key": job.dedupe_key,
        "run_after": job.run_after or datetime.now(UTC),
        "max_attempts": job.max_attempts or default_max_attempts,
        "source_id": job.source_id,
        "version_id": job.version_id,
        "trace_id": job.trace_id,
    }


def _upsert(values: list[dict[str, Any]]) -> Any:
    stmt = insert(schema.jobs).values(values)
    ex = stmt.excluded
    # A duplicate of a job that is still waiting is merged into it: the existing job is promoted to the faster lane
    # and higher priority of the two, so re-uploading a file that is stuck in the bulk backlog gets it done now.
    return stmt.on_conflict_do_update(
        index_elements=[J.dedupe_key],
        index_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        set_={
            "lane_rank": text("LEAST(jobs.lane_rank, excluded.lane_rank)"),
            "lane": text("CASE WHEN excluded.lane_rank < jobs.lane_rank THEN excluded.lane ELSE jobs.lane END"),
            "priority": text("GREATEST(jobs.priority, excluded.priority)"),
            "run_after": text("LEAST(jobs.run_after, excluded.run_after)"),
        },
        where=(J.status == "queued") & ((ex.lane_rank < J.lane_rank) | (ex.priority > J.priority)),
    ).returning(J.id)


async def enqueue(conn: AsyncConnection, job: NewJob, *, default_max_attempts: int = 6) -> int | None:
    """Insert a job (or merge it into an identical waiting one). Returns the job id, or None if merged unchanged."""
    result = await conn.execute(_upsert([_values(job, default_max_attempts)]))
    row = result.first()
    return row[0] if row else None


async def enqueue_many(conn: AsyncConnection, jobs: Sequence[NewJob], *, default_max_attempts: int = 6,
                       batch: int = 1000) -> int:
    """Bulk insert for source syncs that discover thousands of items at once. Returns rows inserted or promoted."""
    total = 0
    # Duplicate dedupe keys inside one statement would make ON CONFLICT fail ("cannot affect row a second time").
    seen: set[str] = set()
    unique: list[NewJob] = []
    for job in jobs:
        if job.dedupe_key:
            if job.dedupe_key in seen:
                continue
            seen.add(job.dedupe_key)
        unique.append(job)
    for i in range(0, len(unique), batch):
        rows = [_values(j, default_max_attempts) for j in unique[i:i + batch]]
        result = await conn.execute(_upsert(rows))
        total += len(result.all())
    return total


_LEASE_SQL = text(
    """
    WITH next AS (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now() AND lane_rank = ANY(:ranks)
          AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY(CAST(:kinds AS text[])))
          AND (CAST(:exclude AS text[]) IS NULL OR NOT (kind = ANY(CAST(:exclude AS text[]))))
        ORDER BY lane_rank, priority DESC, run_after, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs SET status = 'running', leased_by = :worker, attempts = jobs.attempts + 1,
        lease_expires_at = now() + make_interval(secs => :lease), heartbeat_at = now(),
        started_at = COALESCE(jobs.started_at, now())
    FROM next WHERE jobs.id = next.id
    RETURNING jobs.*
    """
)


async def lease(worker_id: str, *, lanes: Sequence[str], kinds: Sequence[str] | None = None,
                exclude_kinds: Sequence[str] | None = None, limit: int = 1, lease_seconds: int = 300) -> list[Job]:
    if limit <= 0:
        return []
    ranks = sorted(LANES[lane] for lane in lanes)
    async with transaction() as conn:
        result = await conn.execute(
            _LEASE_SQL,
            {
                "ranks": ranks, "kinds": list(kinds) if kinds else None,
                "exclude": list(exclude_kinds) if exclude_kinds else None,
                "limit": limit, "worker": worker_id, "lease": float(lease_seconds),
            },
        )
        jobs = [Job.from_row(r) for r in result.all()]
    jobs.sort(key=lambda j: (LANES[j.lane], -j.priority, j.id))
    return jobs


_NEXT_DUE_SQL = text(
    """
    SELECT EXTRACT(EPOCH FROM min(run_after) - now()) FROM jobs
    WHERE (status = 'queued' OR (status = 'running' AND lease_expires_at > now())) AND lane_rank = ANY(:ranks)
      AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY(CAST(:kinds AS text[])))
      AND (CAST(:exclude AS text[]) IS NULL OR NOT (kind = ANY(CAST(:exclude AS text[]))))
    """
)


async def seconds_until_next(*, lanes: Sequence[str], kinds: Sequence[str] | None = None,
                             exclude_kinds: Sequence[str] | None = None) -> float | None:
    """Seconds until the earliest queued (or another worker's running) job on these lanes is due; None when there is
    nothing left. One-shot runs use it to wait for deferred jobs (NotReady, backoff) instead of stopping early."""
    async with transaction() as conn:
        value = (await conn.execute(_NEXT_DUE_SQL, {
            "ranks": sorted(LANES[lane] for lane in lanes), "kinds": list(kinds) if kinds else None,
            "exclude": list(exclude_kinds) if exclude_kinds else None})).scalar_one()
    return None if value is None else float(value)


async def heartbeat(job_id: int, worker_id: str, lease_seconds: int) -> bool:
    """Extend the lease. False means the lease was lost (expired and taken over): the worker must stop."""
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.jobs)
            .where(J.id == job_id, J.leased_by == worker_id, J.status == "running")
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
                    heartbeat_at=datetime.now(UTC))
            .returning(J.id)
        )
        return result.first() is not None


async def save_progress(job_id: int, worker_id: str, progress: dict[str, Any]) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.jobs)
            .where(J.id == job_id, J.leased_by == worker_id, J.status == "running")
            .values(progress=progress, heartbeat_at=datetime.now(UTC))
            .returning(J.id)
        )
        return result.first() is not None


async def complete(job_id: int, worker_id: str) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.jobs)
            .where(J.id == job_id, J.leased_by == worker_id, J.status == "running")
            .values(status="done", finished_at=datetime.now(UTC), lease_expires_at=None, last_error=None,
                    error_class=None)
            .returning(J.id)
        )
        return result.first() is not None


async def fail(job: Job, worker_id: str, error: str, decision: Decision) -> str:
    """Record a failure and decide what happens next. Returns the job's new status."""
    now = datetime.now(UTC)
    values: dict[str, Any] = {"last_error": error[:4000], "error_class": decision.error_class,
                              "lease_expires_at": None, "leased_by": None}
    if decision.error_class == "rate_limited" and job.rate_limited < 200:
        values |= {"status": "queued", "attempts": J.attempts - 1, "rate_limited": J.rate_limited + 1,
                   "run_after": now + timedelta(seconds=decision.delay_s)}
    elif decision.error_class == "permanent":
        values |= {"status": "failed", "finished_at": now}
    elif job.attempts < job.max_attempts:
        values |= {"status": "queued", "run_after": now + timedelta(seconds=decision.delay_s)}
    else:
        values |= {"status": "dead", "finished_at": now}
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.jobs).where(J.id == job.id, J.leased_by == worker_id, J.status == "running")
            .values(**values).returning(J.status)
        )
        row = result.first()
    return row[0] if row else "lost"


async def release(job: Job, worker_id: str, reason: str = "worker shutting down") -> None:
    """Give a job back without counting the attempt (graceful shutdown)."""
    async with transaction() as conn:
        await conn.execute(
            update(schema.jobs)
            .where(J.id == job.id, J.leased_by == worker_id, J.status == "running")
            .values(status="queued", attempts=J.attempts - 1, leased_by=None, lease_expires_at=None,
                    run_after=datetime.now(UTC), last_error=reason)
        )


async def requeue_expired() -> dict[str, int]:
    """Janitor: jobs whose worker stopped heart-beating go back to the queue (or to dead letters if out of tries)."""
    async with transaction() as conn:
        result = await conn.execute(
            text(
                """
                UPDATE jobs SET
                    status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                    finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
                    leased_by = NULL, lease_expires_at = NULL, run_after = now(),
                    last_error = 'lease expired: worker stopped responding', error_class = 'transient'
                WHERE status = 'running' AND lease_expires_at < now()
                RETURNING status
                """
            )
        )
        statuses = [r[0] for r in result.all()]
    return {"requeued": statuses.count("queued"), "dead": statuses.count("dead")}


async def retry(job_ids: Sequence[int]) -> int:
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.jobs)
            .where(J.id.in_(list(job_ids)), J.status.in_(["dead", "failed", "cancelled"]))
            .values(status="queued", attempts=0, run_after=datetime.now(UTC), finished_at=None, last_error=None,
                    error_class=None, rate_limited=0)
            .returning(J.id)
        )
        return len(result.all())


async def cancel(job_ids: Sequence[int]) -> int:
    async with transaction() as conn:
        result = await conn.execute(
            update(schema.jobs)
            .where(J.id.in_(list(job_ids)), J.status == "queued")
            .values(status="cancelled", finished_at=datetime.now(UTC))
            .returning(J.id)
        )
        return len(result.all())


async def cleanup_finished(keep_days: int, batch: int = 10_000) -> int:
    """Delete finished jobs older than keep_days, in batches so the table stays small without long locks."""
    removed = 0
    while True:
        async with transaction() as conn:
            result = await conn.execute(
                text(
                    "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE status IN ('done', 'cancelled') "
                    "AND finished_at < now() - make_interval(days => :days) LIMIT :batch)"
                ),
                {"days": keep_days, "batch": batch},
            )
            removed += result.rowcount or 0
            if (result.rowcount or 0) < batch:
                return removed


async def queue_stats(conn: AsyncConnection) -> dict[str, Any]:
    rows = await conn.execute(
        text(
            """
            SELECT lane, status, count(*) AS n,
                   EXTRACT(EPOCH FROM now() - min(run_after) FILTER (WHERE status = 'queued' AND run_after <= now()))
                       AS oldest_ready_s
            FROM jobs WHERE status IN ('queued', 'running', 'dead', 'failed')
            GROUP BY lane, status
            """
        )
    )
    lanes: dict[str, dict[str, Any]] = {lane: {"queued": 0, "running": 0, "dead": 0, "failed": 0,
                                               "oldest_ready_s": 0.0} for lane in LANES}
    for lane, status, n, oldest in rows.all():
        entry = lanes.setdefault(lane, {"queued": 0, "running": 0, "dead": 0, "failed": 0, "oldest_ready_s": 0.0})
        entry[status] = n
        if oldest is not None:
            entry["oldest_ready_s"] = max(entry["oldest_ready_s"], float(oldest))
    kinds = await conn.execute(
        text("SELECT kind, status, count(*) FROM jobs WHERE status IN ('queued', 'running', 'dead') "
             "GROUP BY kind, status ORDER BY kind")
    )
    by_kind: dict[str, dict[str, int]] = {}
    for kind, status, n in kinds.all():
        by_kind.setdefault(kind, {})[status] = n
    return {"lanes": lanes, "kinds": by_kind}


async def get_job(conn: AsyncConnection, job_id: int) -> Job | None:
    row = (await conn.execute(select(schema.jobs).where(J.id == job_id))).first()
    return Job.from_row(row) if row else None
