"""Alert rules, evaluated every 30 seconds by the scheduler's monitor tick:

  control_lane_stuck   a control-lane job (permission change, deletion) has waited more than 5 minutes
  dead_letters         jobs failed for good (retry them from the Monitor page)
  budget_80            today's model spend passed 80% of DAILY_BUDGET_USD
  error_spike          more than 20% of model calls failed in the last 5 minutes (and at least 10 calls)
  worker_missing       no worker heartbeat in the last 2 minutes while jobs are waiting
  reconcile_repairs    the reconcile job found and repaired differences between Postgres and Qdrant

An alert opens once, updates its count while it keeps firing, resolves itself when the condition clears, and is
sent to ALERT_WEBHOOK_URL (Teams, Slack or a generic JSON endpoint) when it opens.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from rag.config.settings import get_settings
from rag.db import schema
from rag.db.engine import transaction
from rag.providers.http import make_client

log = logging.getLogger(__name__)
A = schema.alerts.c


async def raise_alert(rule: str, severity: str, message: str, details: dict[str, Any] | None = None,
                      key: str = "") -> bool:
    """Open (or refresh) an alert. Returns True when it is new, so the caller can notify once."""
    now = datetime.now(UTC)
    stmt = insert(schema.alerts).values(rule=rule, key=key, severity=severity, message=message, details=details or {},
                                        first_seen_at=now, last_seen_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[A.rule, A.key], index_where=A.resolved_at.is_(None),
        set_={"last_seen_at": now, "count": A.count + 1, "message": message, "details": stmt.excluded.details,
              "severity": severity},
    ).returning(A.id, A.notified_at)
    async with transaction() as conn:
        row = (await conn.execute(stmt)).first()
    if row is not None and row.notified_at is None:
        await notify(rule, severity, message, details or {})
        async with transaction() as conn:
            await conn.execute(update(schema.alerts).where(A.id == row.id).values(notified_at=now))
        return True
    return False


async def resolve_alert(rule: str, key: str = "") -> None:
    async with transaction() as conn:
        await conn.execute(update(schema.alerts).where(A.rule == rule, A.key == key, A.resolved_at.is_(None))
                           .values(resolved_at=datetime.now(UTC)))


async def notify(rule: str, severity: str, message: str, details: dict[str, Any]) -> None:
    settings = get_settings()
    url = settings.alert_webhook_url
    if not url:
        return
    title = f"[{severity.upper()}] {settings.app_name}: {message}"
    if settings.alert_webhook_kind == "slack":
        body: dict[str, Any] = {"text": title}
    elif settings.alert_webhook_kind == "teams":
        body = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                    "content": {"type": "AdaptiveCard", "version": "1.4", "body": [
                                                        {"type": "TextBlock", "text": title, "wrap": True,
                                                         "weight": "Bolder"},
                                                        {"type": "TextBlock", "text": f"Rule: {rule}", "wrap": True}]}}]}
    else:
        body = {"rule": rule, "severity": severity, "message": message, "details": details,
                "at": datetime.now(UTC).isoformat()}
    try:
        async with make_client(timeout=10) as client:
            response = await client.post(url, json=body)
            if response.status_code >= 400:
                log.warning("alert webhook returned %s", response.status_code)
    except Exception:
        log.warning("alert webhook failed", exc_info=True)


async def evaluate() -> dict[str, bool]:
    """Check every rule once. Returns {rule: firing}."""
    settings = get_settings()
    firing: dict[str, bool] = {}
    async with transaction() as conn:
        ctrl = (await conn.execute(text(
            "SELECT EXTRACT(EPOCH FROM now() - min(run_after)) FROM jobs "
            "WHERE lane = 'control' AND status = 'queued' AND run_after <= now()"))).scalar()
        dead = (await conn.execute(text("SELECT count(*) FROM jobs WHERE status = 'dead'"))).scalar_one()
        spend = (await conn.execute(text(
            "SELECT coalesce(sum(cost_usd), 0) FROM usage_daily WHERE day = (now() AT TIME ZONE 'UTC')::date"))
        ).scalar_one()
        calls, errors = (await conn.execute(text(
            "SELECT coalesce(sum(calls), 0), coalesce(sum(errors), 0) FROM metric_minutes "
            "WHERE minute >= now() - interval '5 minutes' AND kind IN "
            "('chat','vision','embedder','reranker','groundedness','ocr','parser')"))).first()
        waiting = (await conn.execute(text(
            "SELECT count(*) FROM jobs WHERE status = 'queued' AND run_after <= now() - interval '1 minute'"))
        ).scalar_one()
        workers = (await conn.execute(text(
            "SELECT count(*) FROM process_stats WHERE kind = 'worker' AND heartbeat_at > now() - interval '2 minutes'"))
        ).scalar_one()

    checks = [
        ("control_lane_stuck", ctrl is not None and float(ctrl) > 300, "critical",
         f"Access-control jobs have waited {int(float(ctrl or 0) // 60)} minutes", {"oldest_s": float(ctrl or 0)}),
        ("dead_letters", dead > 0, "warning", f"{dead} jobs failed permanently", {"dead": dead}),
        ("budget_80", settings.daily_budget_usd > 0 and float(spend) >= 0.8 * settings.daily_budget_usd, "warning",
         f"Model spend today is ${float(spend):.2f} of ${settings.daily_budget_usd:.2f}", {"spend": float(spend)}),
        ("error_spike", calls >= 10 and errors / max(calls, 1) > 0.2, "critical",
         f"{errors} of {calls} model calls failed in the last 5 minutes", {"calls": calls, "errors": errors}),
        ("worker_missing", waiting > 0 and workers == 0, "critical",
         f"{waiting} jobs are waiting and no worker is running", {"waiting": waiting}),
    ]
    for rule, condition, severity, message, details in checks:
        firing[rule] = bool(condition)
        if condition:
            await raise_alert(rule, severity, message, details)
        else:
            await resolve_alert(rule)
    return firing


async def open_alerts(limit: int = 50) -> list[dict[str, Any]]:
    async with transaction() as conn:
        rows = (await conn.execute(select(schema.alerts).where(A.resolved_at.is_(None))
                                   .order_by(A.last_seen_at.desc()).limit(limit))).all()
    return [{"id": r.id, "rule": r.rule, "severity": r.severity, "message": r.message, "count": r.count,
             "first_seen_at": r.first_seen_at.isoformat(), "last_seen_at": r.last_seen_at.isoformat(),
             "details": r.details} for r in rows]
