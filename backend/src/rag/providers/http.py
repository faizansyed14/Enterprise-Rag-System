"""HTTP plumbing shared by every cloud provider: one client per provider, error mapping, bounded retries.

Status codes become the shared error types (rag.errors), so ingestion jobs and the chat path react the same way:
429 -> RateLimited (with Retry-After), 408/409/425/5xx and network errors -> TransientError, other 4xx ->
PermanentError. Error messages never include request bodies or keys.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

import httpx

from rag.errors import PermanentError, RateLimited, TransientError
from rag.jobs.retry_policy import retry_after_seconds

log = logging.getLogger(__name__)

USER_AGENT = "rag-enterprise/2.0"


def make_client(base_url: str = "", *, timeout: float = 60.0, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        follow_redirects=False,
    )


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        err = data.get("error") or data.get("message") or data.get("detail") or data
        if isinstance(err, dict):
            err = err.get("message") or err.get("code") or err
        return str(err)[:300]
    return str(data)[:300]


def check_response(response: httpx.Response, provider: str) -> httpx.Response:
    status = response.status_code
    if status < 400:
        return response
    detail = _detail(response)
    message = f"{provider} returned HTTP {status}: {detail}"
    if status == 429:
        raise RateLimited(message, retry_after=retry_after_seconds(response), provider=provider)
    if status in (408, 409, 425) or status >= 500:
        raise TransientError(message)
    raise PermanentError(message)


async def with_retries[T](
    call: Callable[[], Awaitable[T]], *, provider: str, attempts: int = 3, max_wait_s: float = 20.0
) -> T:
    """Retry rate limits and transient failures a few times inside one request. Longer waits are left to the job
    queue (ingestion) or to the fallback model (chat)."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except RateLimited as exc:
            if attempt == attempts or exc.retry_after > max_wait_s:
                raise
            wait = exc.retry_after
        except (TransientError, httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == attempts:
                if isinstance(exc, httpx.HTTPError):
                    raise TransientError(f"{provider}: {type(exc).__name__}: {exc}") from exc
                raise
            wait = min(max_wait_s, 0.5 * 2 ** (attempt - 1))
        log.info("%s call failed (attempt %s/%s); retrying in %.1fs", provider, attempt, attempts, wait)
        await asyncio.sleep(wait * random.uniform(0.9, 1.2))  # noqa: S311 - jitter
    raise AssertionError("unreachable")
