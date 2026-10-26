from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rag.answering.agent import Answerer
from rag.cache import Cache, digest


class FakeRedis:
    """Just enough of redis.asyncio for the cache: strings with expiry, counters, a hash and a pipeline."""

    def __init__(self) -> None:
        self.data: dict[bytes, bytes] = {}
        self.hashes: dict[str, dict[bytes, bytes]] = {}

    @staticmethod
    def _b(v) -> bytes:
        return v if isinstance(v, bytes) else str(v).encode()

    async def get(self, k): return self.data.get(self._b(k))
    async def set(self, k, v, ex=None): self.data[self._b(k)] = self._b(v)
    async def mget(self, keys): return [self.data.get(self._b(k)) for k in keys]
    async def incr(self, k):
        n = int(self.data.get(self._b(k), b"0")) + 1
        self.data[self._b(k)] = str(n).encode()
        return n
    async def hincrby(self, h, f, n=1):
        d = self.hashes.setdefault(h, {})
        d[f.encode()] = str(int(d.get(f.encode(), b"0")) + n).encode()
    async def hgetall(self, h): return dict(self.hashes.get(h, {}))
    async def dbsize(self): return len(self.data)
    async def info(self, section): return {"used_memory_human": "1M"}
    async def aclose(self): pass

    def pipeline(self, transaction=True):
        outer = self

        class Pipe:
            def __init__(self): self.ops = []
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def incr(self, k): self.ops.append(("incr", k))
            def expire(self, k, s): self.ops.append(("expire", k))
            def set(self, k, v, ex=None): self.ops.append(("set", k, v))
            async def execute(self):
                out = []
                for op in self.ops:
                    if op[0] == "incr": out.append(await outer.incr(op[1]))
                    elif op[0] == "set": out.append(await outer.set(op[1], op[2]))
                    else: out.append(True)
                return out

        return Pipe()


class DeadRedis:
    async def get(self, k): raise ConnectionError("down")
    async def set(self, *a, **k): raise ConnectionError("down")
    async def mget(self, k): raise ConnectionError("down")


def run(coro):
    return asyncio.run(coro)


def test_vectors_and_scores_round_trip_through_the_cache() -> None:
    async def go() -> None:
        cache = Cache(client=FakeRedis())
        await cache.set_floats("vec:a", [0.25, -1.5, 3.0], 60)
        assert await cache.get_floats("vec:a") == [0.25, -1.5, 3.0]
        assert await cache.get_floats("vec:missing") is None
        await cache.mset_floats({"rr:1": 0.5, "rr:2": 0.125}, 60)
        assert await cache.mget_floats(["rr:1", "rr:nope", "rr:2"]) == [0.5, None, 0.125]
    run(go())


def test_a_dead_redis_is_never_an_error_for_the_caller() -> None:
    async def go() -> None:
        cache = Cache(client=DeadRedis())
        assert await cache.get("x") is None
        await cache.set("x", b"1", 5)
        assert await cache.mget_floats(["a", "b"]) == [None, None]
        assert await cache.incr_window("k", 60) is None          # the caller then counts in Postgres instead
        assert (await cache.stats())["available"] is False
    run(go())


def test_request_counting_per_window() -> None:
    async def go() -> None:
        cache = Cache(client=FakeRedis())
        assert [await cache.incr_window("chat:u1", 60) for _ in range(3)] == [1, 2, 3]
        assert await cache.peek_window("chat:u1", 60) == 3
        assert await cache.peek_window("chat:other", 60) == 0
    run(go())


def _answerer(cache: Cache) -> Answerer:
    labels = {"LLM_ANSWER": "openrouter/a", "LLM_ANSWER_HARD": "openrouter/a", "LLM_ROUTER": "none", "LLM_ANSWER_FALLBACK": "none"}
    hub = SimpleNamespace(spec=lambda role: SimpleNamespace(label=labels[role]))
    settings = SimpleNamespace(cache_answers=True, llm_answer_max_tokens=6000)
    answerer = Answerer.__new__(Answerer)
    answerer.services, answerer.settings = SimpleNamespace(hub=hub), settings
    return answerer


def test_an_answer_is_cached_per_question_access_and_library_epoch() -> None:
    async def go() -> None:
        cache = Cache(client=FakeRedis())
        a = _answerer(cache)
        base = await a._answer_key(cache, "What is the VAT?", ["user:1", "everyone"], None, None, None)
        assert base and base == await a._answer_key(cache, "What is the VAT?", ["everyone", "user:1"], None, None, None)
        assert base != await a._answer_key(cache, "What is the VAT?", ["user:2", "everyone"], None, None, None)   # someone else's access
        assert base != await a._answer_key(cache, "What is the total?", ["user:1", "everyone"], None, None, None)
        assert await a._answer_key(cache, "And the total?", ["user:1"], None, None, [{"role": "user", "content": "x"}]) is None  # a follow-up
        await cache.bump_epoch()                                                                       # the library changed
        assert base != await a._answer_key(cache, "What is the VAT?", ["user:1", "everyone"], None, None, None)
        assert await _answerer(Cache(""))._answer_key(Cache(""), "q", ["u"], None, None, None) is None  # no Redis: no caching
    run(go())


def test_digest_is_stable_and_order_independent_for_dicts() -> None:
    assert digest({"a": 1, "b": 2}, "x") == digest({"b": 2, "a": 1}, "x") and digest("a") != digest("b")
