"""Fixed-window rate limits stored in Postgres, so every API replica shares the same counters."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from rag.db.engine import transaction


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limit exceeded; retry in {retry_after}s")
        self.retry_after = retry_after


def _window(window_s: int) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now, datetime.fromtimestamp(int(now.timestamp()) // window_s * window_s, tz=UTC)


async def check(key: str, limit: int, window_s: int) -> None:
    """Raise RateLimitExceeded if `key` has already reached `limit` in this window, without counting a request.
    Used where only failures count (sign-in), so a busy office behind one address is not locked out."""
    if limit <= 0:
        return
    now, start = _window(window_s)
    async with transaction() as conn:
        count = (await conn.execute(text("SELECT count FROM rate_limits WHERE key = :k AND window_start = :w"),
                                    {"k": key[:200], "w": start})).scalar()
    if count is not None and count >= limit:
        raise RateLimitExceeded(int(window_s - (now - start).total_seconds()) + 1)


async def hit(key: str, limit: int, window_s: int) -> int:
    """Count one request for `key`; raise RateLimitExceeded beyond `limit` per `window_s`. Returns the count."""
    if limit <= 0:
        return 0
    now, start = _window(window_s)
    async with transaction() as conn:
        count = (await conn.execute(text(
            "INSERT INTO rate_limits (key, window_start, count) VALUES (:k, :w, 1) "
            "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1 RETURNING count"),
            {"k": key[:200], "w": start})).scalar_one()
    if count > limit:
        raise RateLimitExceeded(int(window_s - (now - start).total_seconds()) + 1)
    return count
