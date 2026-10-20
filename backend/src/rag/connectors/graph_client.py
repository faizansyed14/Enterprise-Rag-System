"""Microsoft Graph and SharePoint REST access with app-only credentials (client secret or certificate).

Throttling (429/503 with Retry-After) surfaces as RateLimited(provider="graph"), so the job queue waits without
using attempts and every worker backs off together.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import jwt

from rag.config.settings import Settings
from rag.errors import ConfigurationError
from rag.providers.http import check_response, make_client, with_retries


class TokenProvider:
    """Client-credentials tokens per resource, cached until shortly before expiry."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tenant = settings.entra_tenant_id
        self.client_id = settings.entra_client_id
        if not self.tenant or not self.client_id:
            raise ConfigurationError("ENTRA_TENANT_ID and ENTRA_CLIENT_ID are required for Microsoft 365 sources")
        self._cache: dict[str, tuple[str, float]] = {}
        self._http = make_client(settings.entra_authority.rstrip("/"), timeout=30)

    def _assertion(self) -> str | None:
        import os

        cert_path = os.environ.get("ENTRA_CERT_PATH")
        thumbprint = os.environ.get("ENTRA_CERT_THUMBPRINT")
        if not cert_path or not thumbprint:
            return None
        key = Path(cert_path).read_text(encoding="utf-8")
        now = int(time.time())
        claims = {"aud": f"{self.settings.entra_authority}/{self.tenant}/oauth2/v2.0/token", "iss": self.client_id,
                  "sub": self.client_id, "jti": str(uuid.uuid4()), "nbf": now, "exp": now + 600}
        import base64

        x5t = base64.urlsafe_b64encode(bytes.fromhex(thumbprint)).decode().rstrip("=")
        return jwt.encode(claims, key, algorithm="RS256", headers={"x5t": x5t})

    async def token(self, scope: str = "https://graph.microsoft.com/.default") -> str:
        cached = self._cache.get(scope)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        data: dict[str, str] = {"client_id": self.client_id, "scope": scope, "grant_type": "client_credentials"}
        assertion = self._assertion()
        if assertion:
            data["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            data["client_assertion"] = assertion
        else:
            secret = self.settings.secret("entra_client_secret")
            if not secret:
                raise ConfigurationError("set ENTRA_CLIENT_SECRET or ENTRA_CERT_PATH/ENTRA_CERT_THUMBPRINT")
            data["client_secret"] = secret
        response = await self._http.post(f"/{self.tenant}/oauth2/v2.0/token", data=data)
        payload = check_response(response, "entra").json()
        self._cache[scope] = (payload["access_token"], time.time() + float(payload.get("expires_in", 3600)))
        return payload["access_token"]

    async def aclose(self) -> None:
        await self._http.aclose()


class GraphClient:
    def __init__(self, settings: Settings, tokens: TokenProvider | None = None) -> None:
        self.settings = settings
        self.tokens = tokens or TokenProvider(settings)
        self.base = settings.graph_base_url.rstrip("/")
        self._http = make_client(timeout=120)

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.tokens.token()}"}

    def _url(self, path_or_url: str) -> str:
        return path_or_url if path_or_url.startswith("https://") else f"{self.base}{path_or_url}"

    async def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            response = await self._http.get(self._url(path_or_url), params=params, headers=await self._headers())
            return check_response(response, "graph").json()

        return await with_retries(call, provider="graph", attempts=4, max_wait_s=30)

    async def pages(self, path_or_url: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        """Follow @odata.nextLink; yields each page (the last one carries @odata.deltaLink for delta queries)."""
        url: str | None = path_or_url
        first = True
        seen: set[str] = set()
        while url and url not in seen:
            seen.add(url)
            page = await self.get_json(url, params if first else None)
            first = False
            yield page
            url = page.get("@odata.nextLink")

    async def download(self, path: str) -> bytes:
        async def call() -> bytes:
            response = await self._http.get(self._url(path), headers=await self._headers(), follow_redirects=True)
            return check_response(response, "graph").content

        return await with_retries(call, provider="graph", attempts=4, max_wait_s=30)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.tokens.aclose()


async def sharepoint_rest(settings: Settings, tokens: TokenProvider, site_url: str, path: str) -> dict[str, Any]:
    """SharePoint REST (site group membership is not in Graph). Needs certificate credentials for app-only access."""
    host = httpx.URL(site_url).host
    token = await tokens.token(f"https://{host}/.default")
    async with make_client(timeout=60) as client:
        response = await client.get(f"{site_url.rstrip('/')}{path}",
                                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        return check_response(response, "sharepoint").json()
