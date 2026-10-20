"""Server-sent events with heartbeats, so proxies keep long research steps alive and disconnects are noticed."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

HEARTBEAT_S = 15.0


def format_event(name: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


async def with_heartbeat(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)

    async def pump() -> None:
        try:
            async for event in events:
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield format_event(item["event"], item["data"])
        await task  # surface errors from the producer
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def sse_response(events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    return StreamingResponse(with_heartbeat(events), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})
