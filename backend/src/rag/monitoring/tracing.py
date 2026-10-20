"""Traces and spans for one request or job, carried in context variables.

    async with trace("chat", "answer", user_id=user.id) as t:
        async with span("retrieval", "retrieval") as s:
            s.attrs["candidates"] = 40

Spans and traces are handed to the recorder, which writes them in batches; recording never blocks or fails the
caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(8)


@dataclass
class TraceContext:
    id: str
    kind: str
    name: str
    user_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"


@dataclass
class SpanRecord:
    id: str
    trace_id: str | None
    parent_id: str | None
    started_at: datetime
    name: str
    kind: str
    duration_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    items: int = 1
    attrs: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None


_trace: ContextVar[TraceContext | None] = ContextVar("rag_trace", default=None)
_span: ContextVar[str | None] = ContextVar("rag_span", default=None)


def _safe_reset(var: ContextVar[Any], token: Token[Any]) -> None:
    # An async generator closed from another task (client disconnect, garbage collection) runs its cleanup in a
    # different context; the value then simply goes away with that context.
    with contextlib.suppress(ValueError):
        var.reset(token)


def current_trace() -> TraceContext | None:
    return _trace.get()


def current_trace_id() -> str | None:
    t = _trace.get()
    return t.id if t else None


def current_span_id() -> str | None:
    return _span.get()


@asynccontextmanager
async def trace(kind: str, name: str, *, user_id: str | None = None, trace_id: str | None = None,
                **attrs: Any) -> AsyncIterator[TraceContext]:
    from rag.monitoring.recorder import get_recorder

    ctx = TraceContext(id=trace_id or new_id("t"), kind=kind, name=name, user_id=user_id, attrs=dict(attrs))
    token = _trace.set(ctx)
    span_token = _span.set(None)
    start = time.perf_counter()
    try:
        yield ctx
    except (GeneratorExit, asyncio.CancelledError):
        ctx.status = "cancelled"
        raise
    except BaseException:
        ctx.status = "error"
        raise
    finally:
        _safe_reset(_span, span_token)
        _safe_reset(_trace, token)
        get_recorder().record_trace(ctx, (time.perf_counter() - start) * 1000)


@asynccontextmanager
async def span(name: str, kind: str, **attrs: Any) -> AsyncIterator[SpanRecord]:
    from rag.monitoring.recorder import get_recorder

    t = _trace.get()
    record = SpanRecord(
        id=new_id("s"),
        trace_id=t.id if t else None,
        parent_id=_span.get(),
        started_at=datetime.now(UTC),
        name=name,
        kind=kind,
        attrs=dict(attrs),
        user_id=t.user_id if t else None,
    )
    token = _span.set(record.id)
    start = time.perf_counter()
    try:
        yield record
    except (GeneratorExit, asyncio.CancelledError):
        record.status = "cancelled"
        raise
    except BaseException as exc:
        record.status = "error"
        record.error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        _safe_reset(_span, token)
        record.duration_ms = (time.perf_counter() - start) * 1000
        get_recorder().record_span(record)
