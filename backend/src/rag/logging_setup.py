"""Logging: plain text for development, one JSON object per line for production log shipping. Personal data in
log messages is masked when MASK_PII_IN_LOGS=true."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from rag.config.settings import Settings


class _PiiFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from rag.security.guardrails import mask_pii

        if isinstance(record.msg, str):
            record.msg = mask_pii(record.msg)
        if record.args:
            record.args = tuple(mask_pii(a) if isinstance(a, str) else a for a in record.args) \
                if isinstance(record.args, tuple) else record.args
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"at": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": record.levelname,
                "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            data["error"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


_configured = False


def configure_logging(settings: Settings) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    if settings.mask_pii_in_logs:
        handler.addFilter(_PiiFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "pdfminer", "PIL", "asyncio", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
