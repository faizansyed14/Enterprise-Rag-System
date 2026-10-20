"""Audit log: who did what, from where. Written for sign-ins, downloads, uploads, source/model/user/index changes and
job retries. Question text is not stored here (conversations hold it, under the user's own access)."""

from __future__ import annotations

import logging
from typing import Any

from rag.db import schema
from rag.db.engine import transaction

log = logging.getLogger("rag.audit")


async def audit(action: str, *, user_id: Any = None, target: str = "", ip: str | None = None,
                details: dict[str, Any] | None = None) -> None:
    try:
        async with transaction() as conn:
            await conn.execute(schema.audit_log.insert().values(
                user_id=user_id, action=action, target=target[:500], ip=(ip or "")[:64], details=details or {}))
    except Exception:
        log.exception("audit write failed for %s", action)
    log.info("audit %s user=%s target=%s", action, user_id, target[:120])
