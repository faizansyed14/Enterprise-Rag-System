"""Runs the layout engine in a separate process.

The engine is pure Python and takes seconds to minutes on a long document; in the worker's own process it would hold the
GIL and stall every other job. A small pool of spawned processes keeps the engine's start-up cost (its dictionaries are
built once per process) out of the per-document time. A run that exceeds its timeout is abandoned by killing the pool, so
one pathological PDF cannot occupy a worker for hours; the caller then falls back to the typography-only structure.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor

from rag.providers.parsers.layout.adapter import LayoutOutline, analyse

log = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_size = 0


def _get_pool(processes: int) -> ProcessPoolExecutor:
    global _pool, _pool_size
    with _lock:
        if _pool is None or _pool_size != processes:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            _pool = ProcessPoolExecutor(max_workers=processes, mp_context=multiprocessing.get_context("spawn"))
            _pool_size = processes
        return _pool


def _kill_pool() -> None:
    """Stop the pool and its processes now (a timed-out run cannot be cancelled any other way)."""
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is None:
        return
    for process in list(getattr(pool, "_processes", {}).values()):
        try:
            process.terminate()
        except Exception:  # noqa: BLE001, S110 - the process may already be gone
            pass
    pool.shutdown(wait=False, cancel_futures=True)


async def outline_of(data: bytes, *, timeout_s: float, processes: int = 1) -> LayoutOutline:
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_get_pool(max(1, processes)), analyse, data)
    try:
        return await asyncio.wait_for(future, timeout_s)
    except TimeoutError:
        log.warning("layout analysis exceeded %.0f s; abandoning it", timeout_s)
        _kill_pool()
        raise
    except Exception:
        # A crashed child poisons the executor ("BrokenProcessPool"): start a fresh one next time.
        _kill_pool()
        raise
