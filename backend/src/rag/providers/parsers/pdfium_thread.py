"""PDFium is not thread-safe: every call into pypdfium2 in this process runs on one dedicated thread.

Parsing throughput scales with worker processes (each has its own PDFium thread), not with threads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdfium")


async def run_pdfium[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))


def run_pdfium_sync[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """For synchronous callers (CLI tools); blocks until the PDFium thread is free."""
    return _EXECUTOR.submit(fn, *args, **kwargs).result()
