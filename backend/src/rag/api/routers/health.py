"""Health (liveness, readiness, detailed checks) and the optional Prometheus endpoint."""

from __future__ import annotations

import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from rag.api.deps import AdminDep, ServicesDep
from rag.config.startup_checks import run_checks
from rag.db.engine import transaction

router = APIRouter(tags=["health"])
metrics_router = APIRouter(tags=["metrics"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(services: ServicesDep) -> JSONResponse:
    checks: dict[str, Any] = {}
    ok = True
    t = time.perf_counter()
    try:
        async with transaction() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "ms": round((time.perf_counter() - t) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        ok = False
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    t = time.perf_counter()
    try:
        await services.vectors.client.get_collections()
        checks["qdrant"] = {"ok": True, "ms": round((time.perf_counter() - t) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["qdrant"] = {"ok": False, "error": type(exc).__name__}
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)


@router.get("/health/details")
async def details(_: AdminDep, services: ServicesDep) -> dict:
    results = await run_checks(services)
    return {"checks": [r.as_dict() for r in results]}


@metrics_router.get("/metrics")
async def metrics(services: ServicesDep, authorization: str = Header(default="")) -> PlainTextResponse:
    if not services.settings.prometheus_metrics:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prometheus metrics are disabled (PROMETHEUS_METRICS=false)")
    token = os.environ.get("METRICS_TOKEN", "")
    if token and not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    from rag.monitoring.prometheus import render

    body, content_type = await render(services)
    return PlainTextResponse(body, media_type=content_type)
