from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from qdrant_client.http.exceptions import ResponseHandlingException

from rag.providers.vectorstore.qdrant import VectorStore


class _Flaky:
    """A client call that fails with the given errors first, then answers."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> str:
        self.calls += 1
        if self.errors:
            raise ResponseHandlingException(self.errors.pop(0))
        return "ok"


def test_a_connection_the_server_already_closed_is_retried() -> None:
    call = _Flaky(httpx.ReadError("closed"))
    assert asyncio.run(VectorStore._call(call, collection_name="c")) == "ok"
    assert call.calls == 2


def test_a_timeout_is_not_retried() -> None:
    call = _Flaky(httpx.ReadTimeout("slow"))
    with pytest.raises(ResponseHandlingException):
        asyncio.run(VectorStore._call(call, collection_name="c"))
    assert call.calls == 1


def test_a_server_that_stays_down_fails_after_three_attempts() -> None:
    call = _Flaky(httpx.ConnectError("a"), httpx.ConnectError("b"), httpx.ConnectError("c"), httpx.ConnectError("d"))
    with pytest.raises(ResponseHandlingException):
        asyncio.run(VectorStore._call(call, collection_name="c"))
    assert call.calls == 3
