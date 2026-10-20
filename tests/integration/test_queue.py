"""The job queue against a real Postgres: lanes, dedupe/promotion, leases, retries, dead letters, janitor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text, update

from rag.db import schema
from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.jobs.retry_policy import PermanentError, RateLimited, decide
from rag.jobs.worker import Worker, handler


async def _enqueue(*jobs: NewJob) -> list[int | None]:
    async with transaction() as conn:
        return [await queue.enqueue(conn, j) for j in jobs]


async def _status(job_id: int) -> dict:
    async with transaction() as conn:
        row = (await conn.execute(select(schema.jobs).where(schema.jobs.c.id == job_id))).first()
    return dict(row._mapping)


async def test_control_lane_is_served_before_a_bulk_backlog(db) -> None:
    await _enqueue(*[NewJob("noop", {"i": i}, lane="bulk") for i in range(5)])
    [ctrl] = await _enqueue(NewJob("noop", {"acl": True}, lane="control"))
    [inter] = await _enqueue(NewJob("noop", {"upload": True}, lane="interactive", priority=5))
    leased = await queue.lease("w1", lanes=["control", "interactive", "bulk"], limit=2)
    assert [j.id for j in leased] == [ctrl, inter]


async def test_workers_pinned_to_a_lane_never_take_other_lanes(db) -> None:
    await _enqueue(NewJob("noop", lane="bulk"))
    assert await queue.lease("w1", lanes=["control"], limit=5) == []


async def test_duplicate_waiting_job_is_promoted_not_duplicated(db) -> None:
    [first] = await _enqueue(NewJob("fetch", {"x": 1}, lane="bulk", dedupe_key="fetch:item1"))
    [again] = await _enqueue(NewJob("fetch", {"x": 1}, lane="interactive", priority=10, dedupe_key="fetch:item1"))
    assert again == first
    row = await _status(first)
    assert row["lane"] == "interactive" and row["lane_rank"] == 1 and row["priority"] == 10
    async with transaction() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
    assert count == 1
    # The same key may be enqueued again once the first job has finished.
    await queue.lease("w", lanes=["interactive"])
    await queue.complete(first, "w")
    [third] = await _enqueue(NewJob("fetch", {"x": 1}, dedupe_key="fetch:item1"))
    assert third is not None and third != first


async def test_concurrent_workers_never_lease_the_same_job(db) -> None:
    await _enqueue(*[NewJob("noop", {"i": i}) for i in range(40)])
    results = await asyncio.gather(*[queue.lease(f"w{i}", lanes=["bulk"], limit=7) for i in range(8)])
    ids = [j.id for batch in results for j in batch]
    assert len(ids) == len(set(ids)) == 40


async def test_rate_limit_retry_does_not_use_an_attempt(db) -> None:
    [jid] = await _enqueue(NewJob("noop"))
    [job] = await queue.lease("w", lanes=["bulk"])
    assert job.attempts == 1
    status = await queue.fail(job, "w", "429", decide(RateLimited("slow down", retry_after=5), job.attempts))
    row = await _status(jid)
    assert status == "queued" and row["attempts"] == 0 and row["rate_limited"] == 1
    assert row["run_after"] > datetime.now(UTC) + timedelta(seconds=4)


async def test_http_429_reads_retry_after() -> None:
    response = httpx.Response(429, headers={"retry-after": "17"}, request=httpx.Request("GET", "https://x"))
    exc = httpx.HTTPStatusError("429", request=response.request, response=response)
    decision = decide(exc, 1)
    assert decision.error_class == "rate_limited" and 17 <= decision.delay_s <= 17 * 1.25


async def test_permanent_errors_are_not_retried(db) -> None:
    [jid] = await _enqueue(NewJob("noop"))
    [job] = await queue.lease("w", lanes=["bulk"])
    status = await queue.fail(job, "w", "encrypted pdf", decide(PermanentError("encrypted"), job.attempts))
    assert status == "failed" and (await _status(jid))["error_class"] == "permanent"


async def test_transient_errors_back_off_then_become_dead_letters(db) -> None:
    [jid] = await _enqueue(NewJob("noop", max_attempts=2))
    for expected in ("queued", "dead"):
        async with transaction() as conn:
            await conn.execute(update(schema.jobs).where(schema.jobs.c.id == jid).values(run_after=datetime.now(UTC)))
        [job] = await queue.lease("w", lanes=["bulk"])
        status = await queue.fail(job, "w", "timeout", decide(TimeoutError("t"), job.attempts))
        assert status == expected
    assert await queue.retry([jid]) == 1
    row = await _status(jid)
    assert row["status"] == "queued" and row["attempts"] == 0


async def test_janitor_requeues_jobs_from_dead_workers(db) -> None:
    [jid] = await _enqueue(NewJob("noop"))
    await queue.lease("w-dead", lanes=["bulk"], lease_seconds=1)
    async with transaction() as conn:
        await conn.execute(update(schema.jobs).where(schema.jobs.c.id == jid)
                           .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=5)))
    assert await queue.requeue_expired() == {"requeued": 1, "dead": 0}
    # The dead worker can no longer complete it; a live worker can.
    assert not await queue.complete(jid, "w-dead")
    [job] = await queue.lease("w-live", lanes=["bulk"])
    assert job.id == jid and await queue.complete(jid, "w-live")


calls: list[int] = []


@handler("test_counting")
async def _counting(ctx) -> None:  # noqa: ANN001
    calls.append(ctx.job.payload["n"])
    if ctx.job.payload.get("fail_once") and ctx.job.attempts == 1:
        raise TimeoutError("first try times out")
    await ctx.checkpoint(done=True)


async def test_worker_runs_handlers_and_retries(db) -> None:
    calls.clear()
    jobs = [NewJob("test_counting", {"n": i}) for i in range(6)]
    jobs.append(NewJob("test_counting", {"n": 99, "fail_once": True}))
    ids = await _enqueue(*jobs)

    class _Services:
        settings = None

    worker = Worker(_Services(), lanes=["bulk"], concurrency=3, idle_poll_s=0.05)
    await worker.run(until_idle=True)
    # The failing job is queued again with backoff; pull its run_after forward and run once more.
    async with transaction() as conn:
        await conn.execute(update(schema.jobs).where(schema.jobs.c.status == "queued")
                           .values(run_after=datetime.now(UTC)))
    await Worker(_Services(), lanes=["bulk"], concurrency=3, idle_poll_s=0.05).run(until_idle=True)
    statuses = [(await _status(i))["status"] for i in ids]
    assert statuses == ["done"] * 7
    assert sorted(calls) == [0, 1, 2, 3, 4, 5, 99, 99]
    assert (await _status(ids[0]))["progress"] == {"done": True}


async def test_cleanup_removes_only_old_finished_jobs(db) -> None:
    ids = await _enqueue(NewJob("noop"), NewJob("noop"))
    async with transaction() as conn:
        await conn.execute(update(schema.jobs).where(schema.jobs.c.id == ids[0])
                           .values(status="done", finished_at=datetime.now(UTC) - timedelta(days=10)))
    assert await queue.cleanup_finished(keep_days=3) == 1
    assert (await _status(ids[1]))["status"] == "queued"


@pytest.mark.parametrize("lane", ["nope"])
async def test_unknown_lane_is_rejected(db, lane: str) -> None:
    with pytest.raises(ValueError, match="unknown lane"):
        await _enqueue(NewJob("noop", lane=lane))
