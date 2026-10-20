"""Buffers spans and traces in memory and writes them to Postgres in batches.

- spans/traces rows (kept MONITOR_RETENTION_DAYS, daily partitions)
- metric_minutes: per-minute aggregates by kind/role/provider/model (kept METRIC_RETENTION_DAYS)
- usage_daily: tokens and cost per user and model (budgets, per-user usage)
- process_stats: this process's live counters, refreshed every 2 seconds (the Monitor page reads them)

Writes are additive upserts, so any number of API and worker processes can record at once. If the database is
unreachable the buffer is bounded and the oldest records are dropped (and counted) rather than growing without limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, literal_column
from sqlalchemy.dialects.postgresql import insert

from rag.db import schema
from rag.monitoring.tracing import SpanRecord, TraceContext, new_id

log = logging.getLogger(__name__)

LATENCY_BUCKETS_MS = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
MODEL_KINDS = frozenset({"chat", "vision", "embedder", "reranker", "groundedness", "parser", "ocr"})


def bucket_index(ms: float) -> int:
    for i, edge in enumerate(LATENCY_BUCKETS_MS):
        if ms <= edge:
            return i
    return len(LATENCY_BUCKETS_MS)


def percentile_from_buckets(buckets: list[int], q: float) -> float | None:
    total = sum(buckets)
    if total == 0:
        return None
    target = q * total
    running = 0
    for i, count in enumerate(buckets):
        running += count
        if running >= target:
            return float(LATENCY_BUCKETS_MS[i]) if i < len(LATENCY_BUCKETS_MS) else float(LATENCY_BUCKETS_MS[-1] * 2)
    return float(LATENCY_BUCKETS_MS[-1] * 2)


@dataclass
class _Agg:
    calls: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(LATENCY_BUCKETS_MS) + 1))

    def add(self, s: SpanRecord) -> None:
        self.calls += 1
        self.errors += 1 if s.status != "ok" else 0
        self.total_ms += s.duration_ms
        self.max_ms = max(self.max_ms, s.duration_ms)
        self.tokens_in += s.tokens_in
        self.tokens_out += s.tokens_out
        self.cost += s.cost_usd or 0.0
        self.buckets[bucket_index(s.duration_ms)] += 1


@dataclass
class LiveStats:
    """Counters for this process. Read by the heartbeat writer and the in-process /metrics fallback."""

    in_flight: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    current: dict[str, Any] = field(default_factory=dict)

    def incr(self, key: str, amount: int = 1) -> None:
        self.totals[key] = self.totals.get(key, 0) + amount

    @contextlib.contextmanager
    def active(self, key: str):
        self.in_flight[key] = self.in_flight.get(key, 0) + 1
        try:
            yield
        finally:
            self.in_flight[key] -= 1


class Recorder:
    def __init__(self, *, max_buffer: int = 50_000) -> None:
        self._spans: deque[SpanRecord] = deque(maxlen=max_buffer)
        self._traces: deque[tuple[TraceContext, float]] = deque(maxlen=max_buffer)
        self.dropped = 0
        self.live = LiveStats()
        self.process_id = f"{socket.gethostname()}:{os.getpid()}:{new_id()[:6]}"
        self.process_kind = "process"
        self.version = ""
        self.started_at = datetime.now(UTC)
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None  # created in start() on the running loop
        self.enabled = True

    # ---- recording (never raises, never awaits) ----------------------------------------------------------------------
    def record_span(self, record: SpanRecord) -> None:
        if not self.enabled:
            return
        if len(self._spans) == self._spans.maxlen:
            self.dropped += 1
        self._spans.append(record)
        if record.kind in MODEL_KINDS:
            self.live.incr(f"calls:{record.kind}")
            if record.status != "ok":
                self.live.incr(f"errors:{record.kind}")
                self.live.current["last_error"] = {
                    "at": record.started_at.isoformat(), "role": record.role, "model": record.model,
                    "error": record.error,
                }

    def record_trace(self, ctx: TraceContext, duration_ms: float) -> None:
        if not self.enabled:
            return
        self._traces.append((ctx, duration_ms))

    # ---- lifecycle ---------------------------------------------------------------------------------------------------
    def start(self, kind: str, version: str = "") -> None:
        self.process_kind = kind
        self.version = version
        if self._task is None or self._task.done():
            self._stopping = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="monitor-recorder")

    async def stop(self) -> None:
        if self._task is not None and self._stopping is not None:
            self._stopping.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=10)
        self._task = None
        with contextlib.suppress(Exception):
            await self.flush()
        with contextlib.suppress(Exception):
            await self._remove_heartbeat()

    async def _run(self) -> None:
        assert self._stopping is not None
        last_beat = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            try:
                await self.flush()
                if loop.time() - last_beat >= 2.0:
                    await self.heartbeat()
                    last_beat = loop.time()
            except Exception:
                log.warning("monitor flush failed; will retry", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=1.0)

    # ---- writing -----------------------------------------------------------------------------------------------------
    async def flush(self) -> int:
        if not self._spans and not self._traces:
            return 0
        spans = [self._spans.popleft() for _ in range(len(self._spans))]
        traces = [self._traces.popleft() for _ in range(len(self._traces))]
        try:
            await self._write(spans, traces)
        except Exception:
            # Put them back (bounded) so a short database outage loses nothing.
            for s in reversed(spans):
                self._spans.appendleft(s)
            for t in reversed(traces):
                self._traces.appendleft(t)
            raise
        return len(spans) + len(traces)

    async def _write(self, spans: list[SpanRecord], traces: list[tuple[TraceContext, float]]) -> None:
        from rag.db.engine import transaction

        aggregates: dict[tuple[datetime, str, str, str, str], _Agg] = {}
        usage: dict[tuple[Any, str, str, str], _Agg] = {}
        for s in spans:
            minute = s.started_at.replace(second=0, microsecond=0)
            key = (minute, s.kind, s.role or "", s.provider or "", s.model or "")
            aggregates.setdefault(key, _Agg()).add(s)
            if s.kind in {"chat", "vision"} and s.model:
                ukey = (s.started_at.date(), s.user_id or "system", s.role or "", f"{s.provider}/{s.model}")
                usage.setdefault(ukey, _Agg()).add(s)

        async with transaction() as conn:
            if traces:
                await conn.execute(
                    insert(schema.traces).on_conflict_do_nothing(),
                    [
                        {
                            "id": t.id, "started_at": t.started_at, "kind": t.kind, "name": t.name,
                            "user_id": t.user_id, "status": t.status, "duration_ms": ms, "attrs": t.attrs,
                        }
                        for t, ms in traces
                    ],
                )
            if spans:
                await conn.execute(
                    insert(schema.spans).on_conflict_do_nothing(),
                    [
                        {
                            "id": s.id, "trace_id": s.trace_id, "parent_id": s.parent_id, "started_at": s.started_at,
                            "name": s.name, "kind": s.kind, "role": s.role, "provider": s.provider,
                            "model": s.model, "duration_ms": s.duration_ms, "status": s.status,
                            "error": s.error, "tokens_in": s.tokens_in, "tokens_out": s.tokens_out,
                            "cost_usd": s.cost_usd, "items": s.items, "attrs": s.attrs,
                        }
                        for s in spans
                    ],
                )
            for (minute, kind, role, provider, model), agg in aggregates.items():
                stmt = insert(schema.metric_minutes).values(
                    minute=minute, kind=kind, role=role, provider=provider, model=model, calls=agg.calls,
                    errors=agg.errors, total_ms=agg.total_ms, max_ms=agg.max_ms, buckets=agg.buckets,
                    tokens_in=agg.tokens_in, tokens_out=agg.tokens_out, cost_usd=agg.cost,
                )
                mm = schema.metric_minutes.c
                ex = stmt.excluded
                stmt = stmt.on_conflict_do_update(
                    index_elements=[mm.minute, mm.kind, mm.role, mm.provider, mm.model],
                    set_={
                        "calls": mm.calls + ex.calls,
                        "errors": mm.errors + ex.errors,
                        "total_ms": mm.total_ms + ex.total_ms,
                        "max_ms": func.greatest(mm.max_ms, ex.max_ms),
                        "tokens_in": mm.tokens_in + ex.tokens_in,
                        "tokens_out": mm.tokens_out + ex.tokens_out,
                        "cost_usd": mm.cost_usd + ex.cost_usd,
                        "buckets": _HISTOGRAM_SUM,
                    },
                )
                await conn.execute(stmt)
            for (day, user_key, role, model), agg in usage.items():
                stmt = insert(schema.usage_daily).values(
                    day=day, user_key=str(user_key), role=role, model=model, calls=agg.calls,
                    tokens_in=agg.tokens_in, tokens_out=agg.tokens_out, cost_usd=agg.cost,
                )
                ud = schema.usage_daily.c
                stmt = stmt.on_conflict_do_update(
                    index_elements=[ud.day, ud.user_key, ud.role, ud.model],
                    set_={
                        "calls": ud.calls + stmt.excluded.calls,
                        "tokens_in": ud.tokens_in + stmt.excluded.tokens_in,
                        "tokens_out": ud.tokens_out + stmt.excluded.tokens_out,
                        "cost_usd": ud.cost_usd + stmt.excluded.cost_usd,
                    },
                )
                await conn.execute(stmt)

    async def heartbeat(self) -> None:
        from rag.db.engine import transaction

        stats = {
            "in_flight": dict(self.live.in_flight),
            "totals": dict(self.live.totals),
            "current": self.live.current,
            "buffered": len(self._spans),
            "dropped": self.dropped,
            "rss_mb": _rss_mb(),
        }
        now = datetime.now(UTC)
        stmt = insert(schema.process_stats).values(
            process_id=self.process_id, kind=self.process_kind, host=socket.gethostname(), pid=os.getpid(),
            version=self.version, started_at=self.started_at, heartbeat_at=now, stats=stats,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[schema.process_stats.c.process_id],
            set_={"heartbeat_at": now, "stats": stats},
        )
        async with transaction() as conn:
            await conn.execute(stmt)

    async def _remove_heartbeat(self) -> None:
        from rag.db.engine import transaction

        async with transaction() as conn:
            await conn.execute(
                schema.process_stats.delete().where(schema.process_stats.c.process_id == self.process_id)
            )


# Element-wise sum of two int[] latency histograms (missing elements count as 0), used in the upsert above.
_HISTOGRAM_SUM = literal_column(
    "(SELECT coalesce(array_agg(coalesce(a, 0) + coalesce(b, 0) ORDER BY i), '{}'::int[]) "
    "FROM unnest(metric_minutes.buckets, excluded.buckets) WITH ORDINALITY AS u(a, b, i))"
)


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/statm") as fh:  # noqa: PTH123 - Linux-only fast path
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except (OSError, ValueError, IndexError):
        return None


_recorder = Recorder()


def get_recorder() -> Recorder:
    return _recorder
