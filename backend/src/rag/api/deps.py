"""Request dependencies: services, the signed-in user (with CSRF and origin checks), admin-only, principals."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from rag.db.engine import transaction
from rag.security.auth import SESSION_COOKIE, CurrentUser, csrf_ok, load_session
from rag.security.principals import principals_for
from rag.services import Services

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


def client_ip(request: Request) -> str:
    # Behind the reverse proxy the real address is in X-Forwarded-For; uvicorn's --proxy-headers sets client.host.
    return request.client.host if request.client else ""


def check_origin(request: Request, services: Services) -> None:
    origin = request.headers.get("origin")
    if request.method in UNSAFE and origin:
        allowed = set(services.settings.allowed_origin_list)
        own = f"{request.url.scheme}://{request.url.netloc}"
        if origin.rstrip("/") not in allowed and origin.rstrip("/") != own:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request refused")


async def current_user(request: Request, services: ServicesDep) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE, "")
    async with transaction() as conn:
        user = await load_session(conn, token, services.settings)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required")
    check_origin(request, services)
    if request.method in UNSAFE and not csrf_ok(user, request.headers.get("x-csrf-token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")
    request.state.user = user
    return user


UserDep = Annotated[CurrentUser, Depends(current_user)]


async def admin_user(user: UserDep) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrators only")
    return user


AdminDep = Annotated[CurrentUser, Depends(admin_user)]


async def user_principals(user: UserDep) -> list[str]:
    async with transaction() as conn:
        return await principals_for(conn, user)


PrincipalsDep = Annotated[list[str], Depends(user_principals)]


def page_params(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
