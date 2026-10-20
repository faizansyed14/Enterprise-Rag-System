"""Job worker: leases jobs for its lanes, runs their handlers concurrently, heart-beats leases, applies retry policy.

    rag worker --lanes control,interactive,bulk --concurrency 4
    rag worker --lanes bulk --kinds extract --concurrency 2      # a dedicated OCR/parsing box

Shutdown is graceful: on SIGTERM the worker stops leasing, lets running jobs finish for a grace period, then gives
any still-running jobs back to the queue without counting the attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rag.jobs import queue
from rag.jobs.queue import Job, NewJob
from rag.jobs.retry_policy import LeaseLost, RateLimited, decide
from rag.monitoring.recorder import get_recorder
from rag.monitoring.tracing import trace

log = logging.getLogger(__name__)

Handler = Callable[["JobContext"], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return decorator


def registered_kinds() -> list[str]:
    return sorted(_HANDLERS)


@dataclass
class JobContext:
    job: Job
    worker_id: str
    services: Any
    lease_seconds: int
    progress: dict[str, Any] = field(default_factory=dict)

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload

    @property
    def settings(self) -> Any:
        return self.services.settings

    async def checkpoint(self, **progress: Any) -> None:
        """Save progress so a retry resumes where this attempt stopped (e.g. the next embedding batch)."""
        self.progress.update(progress)
        if not await queue.save_progress(self.job.id, self.worker_id, self.progress):
            raise LeaseLost(f"job {self.job.id} lease lost")

    async def enqueue(self, job: NewJob) -> int | None:
        from rag.db.engine import transaction

        job.trace_id = job.trace_id or self.job.trace_id
        async with transaction() as conn:
            return await queue.enqueue(conn, job, default_max_attempts=self.settings.job_max_attempts)


class Worker:
    def __init__(
        self,
        services: Any,
        *,
        lanes: Sequence[str] = ("control", "interactive", "bulk"),
        kinds: Sequence[str] | None = None,
        exclude_kinds: Sequence[str] | None = None,
        concurrency: int = 4,
        lease_seconds: int = 300,
        idle_poll_s: float = 0.5,
        grace_s: float = 60.0,
        worker_id: str | None = None,
        idle_horizon_s: float = 120.0,
    ) -> None:
        unknown = [lane for lane in lanes if lane not in queue.LANES]
        if unknown:
            raise ValueError(f"unknown lanes: {unknown}")
        self.services = services
        self.lanes = list(lanes)
        self.kinds = list(kinds) if kinds else None
        self.exclude_kinds = list(exclude_kinds) if exclude_kinds else None
        self.concurrency = max(1, concurrency)
        self.lease_seconds = lease_seconds
        self.idle_poll_s = idle_poll_s
        self.grace_s = grace_s
        self.idle_horizon_s = idle_horizon_s
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._running: dict[int, tuple[asyncio.Task[None], Job]] = {}
        self._stop = asyncio.Event()
        self.processed = 0

    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, max_jobs: int | None = None, until_idle: bool = False) -> int:
        """Process jobs until stopped. `max_jobs` / `until_idle` make it finite (tests, one-shot CLI runs)."""
        recorder = get_recorder()
        recorder.live.current["worker"] = {"lanes": self.lanes, "kinds": self.kinds, "concurrency": self.concurrency}
        idle_rounds = 0
        while not self._stop.is_set():
            free = self.concurrency - len(self._running)
            if max_jobs is not None:
                free = min(free, max_jobs - self.processed - len(self._running))
            leased: list[Job] = []
            if free > 0:
                try:
                    leased = await queue.lease(self.worker_id, lanes=self.lanes, kinds=self.kinds,
                                               exclude_kinds=self.exclude_kinds, limit=free,
                                               lease_seconds=self.lease_seconds)
                except Exception:
                    log.warning("lease failed; retrying", exc_info=True)
                    await self._sleep(2.0)
                    continue
            for job in leased:
                task = asyncio.create_task(self._handle(job), name=f"job-{job.id}-{job.kind}")
                self._running[job.id] = (task, job)
                task.add_done_callback(lambda _t, jid=job.id: self._running.pop(jid, None))
            recorder.live.current["jobs_running"] = [
                {"id": j.id, "kind": j.kind, "lane": j.lane} for _, j in self._running.values()
            ]
            if max_jobs is not None and self.processed + len(self._running) >= max_jobs and not self._running:
                break
            if not leased:
                if until_idle and not self._running:
                    # Idle means nothing left to do soon, not just nothing ready this instant: jobs waiting on another
                    # job (NotReady) or in a short backoff are waited for; far-future ones (schedules) are not.
                    due = await queue.seconds_until_next(lanes=self.lanes, kinds=self.kinds,
                                                         exclude_kinds=self.exclude_kinds)
                    if due is None or due > self.idle_horizon_s:
                        idle_rounds += 1
                        if idle_rounds >= 2:
                            break
                    else:
                        idle_rounds = 0
                        await self._sleep(min(max(due, self.idle_poll_s), 5.0))
                        continue
                await self._sleep(self.idle_poll_s if not self._running else 0.1)
            else:
                idle_rounds = 0
                await asyncio.sleep(0)
        await self._drain()
        return self.processed

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _drain(self) -> None:
        if not self._running:
            return
        tasks = [t for t, _ in self._running.values()]
        _, pending = await asyncio.wait(tasks, timeout=self.grace_s)
        for task in pending:
            task.cancel()
        for task, job in list(self._running.values()):
            if task in pending:
                with contextlib.suppress(Exception):
                    await queue.release(job, self.worker_id)
        with contextlib.suppress(Exception):
            await asyncio.gather(*pending, return_exceptions=True)

    async def _heartbeat(self, job: Job, owner: asyncio.Task[None]) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await queue.heartbeat(job.id, self.worker_id, self.lease_seconds)
            except Exception:
                log.warning("heartbeat failed for job %s", job.id, exc_info=True)
                continue
            if not alive:
                log.warning("lost lease on job %s; cancelling it here", job.id)
                owner.cancel()
                return

    async def _handle(self, job: Job) -> None:
        recorder = get_recorder()
        fn = _HANDLERS.get(job.kind)
        current = asyncio.current_task()
        assert current is not None
        beat = asyncio.create_task(self._heartbeat(job, current))
        ctx = JobContext(job=job, worker_id=self.worker_id, services=self.services,
                         lease_seconds=self.lease_seconds, progress=dict(job.progress))
        try:
            with recorder.live.active(f"job:{job.kind}"):
                async with trace("job", job.kind, trace_id=job.trace_id, job_id=job.id, lane=job.lane,
                                 attempt=job.attempts):
                    if fn is None:
                        raise RuntimeError(f"no handler for job kind {job.kind!r}")
                    await fn(ctx)
            if await queue.complete(job.id, self.worker_id):
                recorder.live.incr(f"jobs_done:{job.kind}")
            self.processed += 1
        except asyncio.CancelledError:
            # Either shutdown (released in _drain) or lost lease (someone else owns it now): write nothing.
            raise
        except LeaseLost:
            log.warning("job %s lost its lease; result discarded", job.id)
        except Exception as exc:  # noqa: BLE001 - every failure goes through the retry policy
            decision = decide(exc, job.attempts)
            detail = f"{type(exc).__name__}: {exc}"
            if decision.error_class == "transient" and job.attempts >= job.max_attempts:
                detail += "\n" + "".join(traceback.format_exception(exc, limit=8))[-3000:]
            status = await queue.fail(job, self.worker_id, detail, decision)
            if isinstance(exc, RateLimited) and exc.provider:
                from rag.jobs.rate_limits import set_cooldown

                with contextlib.suppress(Exception):
                    await set_cooldown(exc.provider, exc.retry_after, str(exc))
            recorder.live.incr(f"jobs_{status}:{job.kind}")
            self.processed += 1
            level = logging.ERROR if status == "dead" else logging.WARNING
            log.log(level, "job %s (%s) failed [%s -> %s]: %s", job.id, job.kind, decision.error_class, status,
                    detail.splitlines()[0][:300])
            await self._on_final_failure(job, status, detail)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat

    async def _on_final_failure(self, job: Job, status: str, detail: str) -> None:
        """Mark the item/version as failed so the UI shows why, when a job will not be retried."""
        if status not in {"failed", "dead"}:
            return
        hook = getattr(self.services, "on_job_failed", None)
        if hook is not None:
            with contextlib.suppress(Exception):
                await hook(job, status, detail)
