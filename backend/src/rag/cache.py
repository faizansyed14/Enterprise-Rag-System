"""A shared cache, counters and request counting on Redis. Optional: without REDIS_URL (or when Redis is down) every caller
falls back to what it did before (a per-process cache, a Postgres table), so Redis can never take the application down.

What lives here, and why it is worth sharing between processes:

  vectors   the embedding of a question: the same question comes back, on any API process, and the model need not run again
  rerank    the cross-encoder score of a (question, passage) pair, which never changes: reranking is the slowest search step
  answers   a finished, verified answer, keyed by the question, who is asking (their access list), the filters, the models and
            the prompt, and by the library's "epoch": any change to the library or to access bumps the epoch, so a stale
            answer can never be served
  limits    request counters for rate limiting, so each chat or upload request no longer writes a row to Postgres

Redis is sized as a cache (maxmemory with least-recently-used eviction, nothing persisted): losing it costs speed, not data.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from typing import Any

from rag.config.settings import Settings, get_settings

log = logging.getLogger(__name__)

PREFIX = "rag:"
BREAKER_S = 15.0  # after a failure Redis is not tried again for this long, so a dead Redis costs one timeout, not one per request


def digest(*parts: Any) -> str:
    """A short stable key fragment for any JSON-able values."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Cache:
    def __init__(self, url: str = "", *, client: Any = None) -> None:
        self.url = url
        self._client = client
        self._down_until = 0.0
        self._logged_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.url or self._client is not None)

    def _redis(self) -> Any | None:
        if not self.enabled or time.monotonic() < self._down_until:
            return None
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self.url, socket_timeout=1.5, socket_connect_timeout=1.5,
                                             health_check_interval=30, decode_responses=False)
        return self._client

    def _failed(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + BREAKER_S
        if time.monotonic() - self._logged_at > 60:
            self._logged_at = time.monotonic()
            log.warning("redis unavailable (%s); using the in-process fallbacks for %ss", type(exc).__name__, int(BREAKER_S))

    async def _run(self, op: str, *args: Any, **kwargs: Any) -> Any | None:
        client = self._redis()
        if client is None:
            return None
        try:
            return await getattr(client, op)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - any Redis failure means "no cache", never an error for the caller
            self._failed(exc)
            return None

    # ---- values ---------------------------------------------------------------------------------------------------------
    async def get(self, key: str) -> bytes | None:
        return await self._run("get", PREFIX + key)

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        await self._run("set", PREFIX + key, value, ex=max(1, int(ttl)))

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        await self.set(key, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"), ttl)

    async def get_floats(self, key: str) -> list[float] | None:
        raw = await self.get(key)
        if raw is None or len(raw) % 4:
            return None
        import array

        values = array.array("f")
        values.frombytes(raw)
        return values.tolist()

    async def set_floats(self, key: str, values: list[float], ttl: int) -> None:
        import array

        await self.set(key, array.array("f", values).tobytes(), ttl)

    async def mget_floats(self, keys: list[str]) -> list[float | None]:
        """Scores for many keys in one round trip (None where missing). One float32 per key."""
        if not keys:
            return []
        rows = await self._run("mget", [PREFIX + k for k in keys])
        if rows is None:
            return [None] * len(keys)
        import struct

        return [struct.unpack("f", r)[0] if r is not None and len(r) == 4 else None for r in rows]

    async def mset_floats(self, items: dict[str, float], ttl: int) -> None:
        if not items:
            return
        client = self._redis()
        if client is None:
            return
        import struct

        try:
            async with client.pipeline(transaction=False) as pipe:
                for key, value in items.items():
                    pipe.set(PREFIX + key, struct.pack("f", value), ex=max(1, int(ttl)))
                await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)

    # ---- the library's epoch ---------------------------------------------------------------------------------------------
    async def epoch(self) -> int:
        raw = await self._run("get", PREFIX + "epoch")
        return int(raw) if raw else 0

    async def bump_epoch(self) -> None:
        """Something changed in the library or in who may see what: every cached answer is now out of date."""
        await self._run("incr", PREFIX + "epoch")

    # ---- counters ---------------------------------------------------------------------------------------------------------
    async def incr_window(self, key: str, window_s: int) -> int | None:
        """Count one request in a fixed window; None when Redis is not available (the caller then uses its fallback)."""
        client = self._redis()
        if client is None:
            return None
        window = int(time.time()) // window_s
        name = f"{PREFIX}rl:{key}:{window}"
        try:
            async with client.pipeline(transaction=True) as pipe:
                pipe.incr(name)
                pipe.expire(name, window_s + 5)
                count, _ = await pipe.execute()
            return int(count)
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)
            return None

    async def peek_window(self, key: str, window_s: int) -> int | None:
        window = int(time.time()) // window_s
        raw = await self._run("get", f"{PREFIX}rl:{key}:{window}")
        if raw is None:
            return 0 if self._redis() is not None else None
        return int(raw)

    async def stat(self, name: str, n: int = 1) -> None:
        await self._run("hincrby", PREFIX + "stats", name, n)

    async def stats(self) -> dict[str, Any]:
        """Hit and miss counters and memory use, for the Monitor page and `rag check`."""
        if not self.enabled:
            return {"enabled": False}
        raw = await self._run("hgetall", PREFIX + "stats")
        if raw is None:
            return {"enabled": True, "available": False}
        counters = {k.decode(): int(v) for k, v in raw.items()}
        info = await self._run("info", "memory") or {}
        keys = await self._run("dbsize")
        out: dict[str, Any] = {"enabled": True, "available": True, "keys": int(keys or 0),
                               "memory": info.get("used_memory_human", ""), "epoch": await self.epoch()}
        for family in ("answer", "embed", "rerank"):
            hit, miss = counters.get(f"{family}_hit", 0), counters.get(f"{family}_miss", 0)
            out[family] = {"hits": hit, "misses": miss, "hit_rate": round(hit / (hit + miss), 3) if hit + miss else None}
        return out

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # closing at shutdown: a dropped connection is already closed
                await self._client.aclose()


_cache: Cache | None = None


def get_cache(settings: Settings | None = None) -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache((settings or get_settings()).redis_url)
    return _cache


def set_cache(cache: Cache | None) -> None:
    """For tests."""
    global _cache
    _cache = cache
