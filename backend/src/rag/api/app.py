"""The FastAPI application: routers under /api/v1, security headers, error mapping, lifecycle."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from rag import __version__
from rag.api.routers import admin, auth, chat, documents, health, models, monitor, sources, uploads
from rag.config.settings import Settings, get_settings
from rag.db import schema
from rag.db.engine import dispose_engine, transaction
from rag.errors import ConfigurationError, PermanentError, RagError, RateLimited
from rag.logging_setup import configure_logging
from rag.monitoring.recorder import get_recorder
from rag.monitoring.tracing import SpanRecord, new_id
from rag.services import Services

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


async def bootstrap_admin(settings: Settings) -> None:
    """First start: create the admin from BOOTSTRAP_ADMIN_EMAIL/PASSWORD when no users exist yet."""
    email = settings.bootstrap_admin_email
    password = settings.secret("bootstrap_admin_password")
    if not email or not password:
        return
    from rag.security.auth import create_user

    async with transaction() as conn:
        # Several API processes start together; the lock makes exactly one of them create the account.
        await conn.execute(text("SELECT pg_advisory_xact_lock(7231001)"))
        count = (await conn.execute(select(func.count()).select_from(schema.users))).scalar_one()
        if count == 0:
            await create_user(conn, email=email, password=password, display_name="Administrator", is_admin=True)
            log.warning("created the first administrator %s; change the password after signing in", email)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = services or Services.create(settings)
        recorder = get_recorder()
        recorder.start("api", __version__)
        try:
            await bootstrap_admin(settings)
        except Exception:
            log.exception("could not create the bootstrap administrator")
        yield
        await recorder.stop()
        await app.state.services.aclose()
        await dispose_engine()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan,
                  docs_url="/api/docs" if settings.app_env != "prod" else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if settings.app_env != "prod" else None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origin_list, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "X-CSRF-Token"], max_age=600)

    @app.middleware("http")
    async def headers_and_timing(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Content-Security-Policy", API_CSP)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                response.headers.setdefault("Cache-Control", "no-store")
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        if path not in {"/api/v1/health", "/metrics"} and "text/event-stream" not in response.headers.get(
                "content-type", ""):
            get_recorder().record_span(SpanRecord(
                id=new_id("s"), trace_id=None, parent_id=None, started_at=datetime.now(UTC),
                name=f"{request.method} {path}", kind="api", role=path,
                provider=request.method, model=f"{response.status_code // 100}xx",
                duration_ms=(time.perf_counter() - start) * 1000,
                status="ok" if response.status_code < 500 else "error"))
        return response

    @app.exception_handler(RateLimited)
    async def rate_limited(_: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=429,
                            headers={"Retry-After": str(int(exc.retry_after))})

    @app.exception_handler(ConfigurationError)
    async def misconfigured(_: Request, exc: ConfigurationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "configuration"}, status_code=503)

    @app.exception_handler(PermanentError)
    async def bad_request(_: Request, exc: PermanentError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RagError)
    async def rag_error(_: Request, exc: RagError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "message": e.get("msg", "")}
                  for e in exc.errors()]
        return JSONResponse({"detail": "Invalid request", "errors": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        ref = new_id("e")
        log.exception("unhandled error %s on %s %s", ref, request.method, request.url.path)
        return JSONResponse({"detail": f"Internal error (reference {ref})"}, status_code=500)

    prefix = "/api/v1"
    for module in (auth, chat, documents, uploads, sources, models, monitor, admin, health):
        app.include_router(module.router, prefix=prefix)
    app.include_router(health.metrics_router)
    return app
