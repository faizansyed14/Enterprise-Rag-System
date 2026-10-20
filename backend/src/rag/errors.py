"""Error types shared by providers, ingestion jobs and the API.

The retry policy keys off these: RateLimited waits without using an attempt, TransientError backs off and retries,
PermanentError is not retried. Providers translate HTTP status codes into them in one place (providers/http.py).
"""

from __future__ import annotations


class RagError(Exception):
    pass


class PermanentError(RagError):
    """Retrying cannot help: corrupt or encrypted file, unsupported format, validation failure, 4xx from a provider."""


class TransientError(RagError):
    """Retrying later may help: timeouts, connection resets, 5xx."""


class RateLimited(RagError):
    def __init__(self, message: str, retry_after: float = 30.0, provider: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))
        self.provider = provider


class NotReady(RateLimited):
    """A job's input is still being produced by another job (e.g. extraction of the same file). Wait, don't count."""

    def __init__(self, message: str, retry_after: float = 10.0) -> None:
        super().__init__(message, retry_after=retry_after, provider=None)


class LeaseLost(RagError):
    """Another worker took over this job (our lease expired); stop without writing results."""


class ConfigurationError(RagError):
    """A role or provider is misconfigured (missing key, unknown model)."""
