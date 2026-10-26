"""Sign-in (local accounts and Microsoft Entra ID), sign-out and the current user."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from rag.api.deps import ServicesDep, UserDep, check_origin, client_ip
from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.security.audit import audit
from rag.security.auth import (
    CSRF_COOKIE,
    OIDC_COOKIE,
    SESSION_COOKIE,
    AuthError,
    EntraOIDC,
    authenticate_local,
    create_session,
    end_session,
    upsert_entra_user,
)
from rag.security.rate_limit import RateLimitExceeded, check, hit

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)


def _set_session_cookies(response: Response, services: ServicesDep, token: str, csrf: str) -> None:
    s = services.settings
    max_age = s.session_ttl_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=s.secure_cookies,
                        samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=s.secure_cookies,
                        samesite="lax", path="/")


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, services: ServicesDep) -> dict:
    s = services.settings
    if s.auth_mode == "entra":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use Microsoft sign-in")
    check_origin(request, services)
    ip = client_ip(request)
    # Failed attempts per address are limited (successful sign-ins are not counted, so many people behind one
    # office address are fine); each account also locks after repeated failures (authenticate_local).
    try:
        await check(f"login:{ip}", s.login_attempts_per_15min, 900)
    except RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed sign-in attempts from this address",
                            headers={"Retry-After": str(exc.retry_after)}) from exc
    # Commit the failure counter before answering, so lockout works (raising inside the block would roll it back).
    async with transaction() as conn:
        user = await authenticate_local(conn, body.email, body.password)
    if user is None:
        with contextlib.suppress(RateLimitExceeded):
            await hit(f"login:{ip}", s.login_attempts_per_15min, 900)
        await audit("login_failed", target=body.email.lower()[:120], ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong e-mail or password")
    async with transaction() as conn:
        token, csrf = await create_session(conn, user.id, s, ip=ip, user_agent=request.headers.get("user-agent"))
    _set_session_cookies(response, services, token, csrf)
    await audit("login", user_id=user.id, ip=ip)
    return {"user": {"id": str(user.id), "email": user.email, "display_name": user.display_name,
                     "is_admin": user.is_admin}, "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response, user: UserDep) -> dict:
    async with transaction() as conn:
        await end_session(conn, user.session_hash)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    await audit("logout", user_id=user.id, ip=client_ip(request))
    return {"ok": True}


@router.get("/me")
async def me(user: UserDep, services: ServicesDep) -> dict:
    s = services.settings
    return {"user": user.as_dict(), "csrf_token": user.csrf_token,
            "app": {"name": s.app_name, "show_model_badge": s.show_model_badge,
                    "allow_model_switch": s.allow_ui_model_switch, "auth_mode": s.auth_mode,
                    "model_choices": s.model_choice_lines, "answer_model": services.hub.spec("LLM_ANSWER").label,
                    "known_projects": await services.known_projects()}}


@router.get("/config")
async def public_config(services: ServicesDep) -> dict:
    """What the sign-in page needs before anyone is signed in."""
    s = services.settings
    return {"app_name": s.app_name, "auth_mode": s.auth_mode,
            "entra_enabled": s.auth_mode in {"entra", "both"} and bool(s.entra_client_id)}


@router.get("/oidc/login")
async def oidc_login(services: ServicesDep) -> RedirectResponse:
    s = services.settings
    if s.auth_mode not in {"entra", "both"} or not s.entra_client_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Microsoft sign-in is not configured")
    url, cookie = EntraOIDC(s).start()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(OIDC_COOKIE, cookie, max_age=600, httponly=True, secure=s.secure_cookies, samesite="lax",
                        path="/api/v1/auth/oidc")
    return response


@router.get("/oidc/callback")
async def oidc_callback(request: Request, services: ServicesDep, code: str = "", state: str = "",
                        error: str = "") -> RedirectResponse:
    s = services.settings
    target = s.public_url.rstrip("/")
    if error or not code:
        return RedirectResponse(f"{target}/login?error=microsoft", status_code=302)
    try:
        claims = await EntraOIDC(s).finish(code=code, state=state, cookie=request.cookies.get(OIDC_COOKIE, ""))
    except (AuthError, Exception) as exc:
        await audit("login_failed", target="entra", ip=client_ip(request), details={"error": type(exc).__name__})
        return RedirectResponse(f"{target}/login?error=microsoft", status_code=302)
    async with transaction() as conn:
        user = await upsert_entra_user(conn, claims)
        if not user.is_active:
            return RedirectResponse(f"{target}/login?error=disabled", status_code=302)
        token, csrf = await create_session(conn, user.id, s, ip=client_ip(request),
                                           user_agent=request.headers.get("user-agent"))
        await queue.enqueue(conn, NewJob("sync_principals", {"user_id": str(user.id)}, lane="control",
                                         dedupe_key=f"principals:{user.id}"))
    response = RedirectResponse(f"{target}/", status_code=302)
    _set_session_cookies(response, services, token, csrf)
    response.delete_cookie(OIDC_COOKIE, path="/api/v1/auth/oidc")
    await audit("login", user_id=user.id, ip=client_ip(request), details={"provider": "entra"})
    return response
