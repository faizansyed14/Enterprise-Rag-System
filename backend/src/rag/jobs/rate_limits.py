"""Shared provider cooldowns, per-process request pacing and daily page budgets.

When any worker gets a 429 from a provider, it writes a cooldown row; every other worker checks it before calling
that provider and re-queues its job (without using an attempt) instead of hammering the provider.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from rag.db import schema
from rag.db.engine import transaction
from rag.jobs.retry_policy import RateLimited


async def set_cooldown(provider: str, seconds: float, reason: str = "") -> None:
    until = datetime.now(UTC) + timedelta(seconds=seconds)
    stmt = insert(schema.provider_cooldowns).values(provider=provider, until=until, reason=reason[:500])
    stmt = stmt.on_conflict_do_update(
        index_elements=[schema.provider_cooldowns.c.provider],
        set_={"until": func.greatest(schema.provider_cooldowns.c.until, stmt.excluded.until),
              "reason": stmt.excluded.reason},
    )
    async with transaction() as conn:
        await conn.execute(stmt)


async def check_cooldown(provider: str) -> None:
    """Raise RateLimited if the provider is cooling down (the job goes back to the queue untouched)."""
    async with transaction() as conn:
        row = (await conn.execute(
            select(schema.provider_cooldowns.c.until).where(schema.provider_cooldowns.c.provider == provider)
        )).first()
    if row and row[0] > datetime.now(UTC):
        remaining = (row[0] - datetime.now(UTC)).total_seconds()
        raise RateLimited(f"{provider} is cooling down after a rate limit", retry_after=remaining, provider=None)


async def take_budget(bucket: str, amount: int, limit: int) -> bool:
    """Atomically use `amount` from today's budget. False (and nothing used) if it would exceed `limit`."""
    if limit <= 0:
        return False
    today = datetime.now(UTC).date()
    async with transaction() as conn:
        await conn.execute(
            insert(schema.daily_budgets).values(day=today, bucket=bucket, used=0).on_conflict_do_nothing()
        )
        result = await conn.execute(
            text(
                "UPDATE daily_budgets SET used = used + :amount "
                "WHERE day = :day AND bucket = :bucket AND used + :amount <= :limit RETURNING used"
            ),
            {"amount": amount, "day": today, "bucket": bucket, "limit": limit},
        )
        return result.first() is not None


class Pacer:
    """Token bucket per provider inside one process (requests per minute)."""

    def __init__(self, rpm: dict[str, int] | None = None) -> None:
        self._rpm = rpm or {}
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, provider: str) -> None:
        rpm = self._rpm.get(provider)
        if not rpm:
            return
        rate = rpm / 60.0
        while True:
            async with self._lock:
                tokens, last = self._state.get(provider, (float(rpm), time.monotonic()))
                now = time.monotonic()
                tokens = min(float(rpm), tokens + (now - last) * rate)
                if tokens >= 1:
                    self._state[provider] = (tokens - 1, now)
                    return
                self._state[provider] = (tokens, now)
                wait = (1 - tokens) / rate
            await asyncio.sleep(wait)
