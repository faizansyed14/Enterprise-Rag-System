"""How a failed job is retried.

  rate limited  -> wait for Retry-After (or the provider cooldown) and try again WITHOUT using up an attempt
  transient     -> exponential backoff with jitter; after max_attempts the job becomes a dead letter
  permanent     -> no retry; the job is marked failed and its item is skipped with the reason shown in the UI
"""

from __future__ import annotations

import random
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx

from rag.errors import LeaseLost, NotReady, PermanentError, RagError, RateLimited, TransientError

ErrorClass = Literal["rate_limited", "transient", "permanent"]

MAX_BACKOFF_S = 3600.0
BASE_BACKOFF_S = 10.0

JobError = RagError
__all__ = ["Decision", "JobError", "LeaseLost", "NotReady", "PermanentError", "RateLimited", "TransientError",
           "backoff_seconds", "classify", "decide", "retry_after_seconds"]


@dataclass(frozen=True)
class Decision:
    error_class: ErrorClass
    delay_s: float


def retry_after_seconds(response: httpx.Response, default: float = 30.0) -> float:
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(1.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                return max(1.0, (when - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                pass
    reset = response.headers.get("x-ratelimit-reset-requests") or response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(1.0, float(reset.rstrip("s")))
        except ValueError:
            pass
    return default


def classify(exc: BaseException) -> ErrorClass:
    if isinstance(exc, RateLimited):
        return "rate_limited"
    if isinstance(exc, PermanentError):
        return "permanent"
    if isinstance(exc, TransientError):
        return "transient"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "rate_limited"
        if status in (408, 409, 425) or status >= 500:
            return "transient"
        return "permanent"
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)):
        return "transient"
    if isinstance(exc, (zipfile.BadZipFile, UnicodeDecodeError)):
        return "permanent"
    name = type(exc).__name__
    if name in {"PdfiumError", "PDFSyntaxError", "PackageNotFoundError", "InvalidFileException"}:
        return "permanent"
    # Unknown errors are retried with backoff and end up as dead letters if they keep failing, so a bug never
    # silently drops a document and never loops forever.
    return "transient"


def backoff_seconds(attempts: int, *, base: float = BASE_BACKOFF_S, cap: float = MAX_BACKOFF_S) -> float:
    raw = min(cap, base * (2 ** max(0, attempts - 1)))
    return raw * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not security


def decide(exc: BaseException, attempts: int) -> Decision:
    error_class = classify(exc)
    if error_class == "rate_limited":
        if isinstance(exc, RateLimited):
            delay = exc.retry_after
        elif isinstance(exc, httpx.HTTPStatusError):
            delay = retry_after_seconds(exc.response)
        else:
            delay = 30.0
        return Decision("rate_limited", delay * random.uniform(1.0, 1.25))  # noqa: S311 - spread retries
    if error_class == "transient":
        return Decision("transient", backoff_seconds(attempts))
    return Decision("permanent", 0.0)
