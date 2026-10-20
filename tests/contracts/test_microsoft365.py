"""SharePoint/OneDrive connector and Entra ID sign-in against mocked Microsoft endpoints."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from rag.config.settings import Settings
from rag.connectors.sharepoint import SharePointConnector, principals_from_permissions
from rag.security.auth import EntraOIDC, unsign
from rag.security.sync import principal_from_login

GRAPH = "https://graph.microsoft.com/v1.0"
TENANT = "11111111-2222-3333-4444-555555555555"


def _settings(**overrides: object) -> Settings:
    values = {"entra_tenant_id": TENANT, "entra_client_id": "client-1", "entra_client_secret": "secret-1",
              "public_url": "https://rag.example.com", "auth_mode": "both"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _token_route() -> None:
    respx.post(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600}))


def test_permissions_map_to_principals() -> None:
    perms = [
        {"grantedToV2": {"user": {"id": "u-1"}}},
        {"grantedToV2": {"group": {"id": "g-1"}}},
        {"grantedToV2": {"siteGroup": {"id": 5, "displayName": "Project Members"}}},
        {"link": {"scope": "organization"}, "grantedToIdentitiesV2": [{"user": {"id": "u-2"}}]},
    ]
    assert principals_from_permissions(perms, "site-9") == ["everyone", "group:g-1", "sitegroup:site-9:5",
                                                            "user:u-1", "user:u-2"]


def test_site_group_member_logins() -> None:
    assert principal_from_login("i:0#.f|membership|Alex@Contoso.com") == "upn:alex@contoso.com"
    assert principal_from_login("c:0t.c|tenant|0f0e-1") == "group:0f0e-1"
    assert principal_from_login("c:0o.c|federateddirectoryclaimprovider|abc_o") == "group:abc"
    assert principal_from_login("c:0-.f|rolemanager|spo-grid-all-users/xyz") == "everyone"


@respx.mock
async def test_sharepoint_delta_sync_download_and_permissions() -> None:
    _token_route()
    respx.get(f"{GRAPH}/sites/contoso.sharepoint.com:/sites/Projects").mock(
        return_value=httpx.Response(200, json={"id": "site-9"}))
    respx.get(f"{GRAPH}/sites/site-9/drives").mock(return_value=httpx.Response(200, json={"value": [
        {"id": "drive-1", "name": "Documents"}, {"id": "drive-2", "name": "Archive"}]}))
    respx.get(f"{GRAPH}/drives/drive-1/root/delta", params={"token": "page2"}).mock(return_value=httpx.Response(200, json={
        "value": [], "@odata.deltaLink": f"{GRAPH}/drives/drive-1/root/delta?token=next"}))
    respx.get(f"{GRAPH}/drives/drive-1/root/delta").mock(return_value=httpx.Response(200, json={
        "value": [
            {"id": "folder-1", "name": "Specs", "folder": {}},
            {"id": "item-1", "name": "03 30 00.pdf", "cTag": "c1", "eTag": "e1", "size": 1000,
             "lastModifiedDateTime": "2025-03-01T10:00:00Z", "file": {"mimeType": "application/pdf"},
             "parentReference": {"path": "/drives/drive-1/root:/Marina Tower/Specs"}},
            {"id": "item-2", "name": "old.pdf", "deleted": {"state": "deleted"}},
        ],
        "@odata.nextLink": f"{GRAPH}/drives/drive-1/root/delta?token=page2"}))
    respx.get(f"{GRAPH}/drives/drive-1/items/item-1/content").mock(
        return_value=httpx.Response(302, headers={"location": "https://download.example.com/blob"}))
    respx.get("https://download.example.com/blob").mock(return_value=httpx.Response(200, content=b"%PDF-1.7 data"))
    respx.get(f"{GRAPH}/drives/drive-1/items/item-1/permissions").mock(return_value=httpx.Response(200, json={
        "value": [{"grantedToV2": {"siteGroup": {"id": 3}}}, {"grantedToV2": {"user": {"id": "u-7"}}}]}))

    connector = SharePointConnector({"site_url": "https://contoso.sharepoint.com/sites/Projects",
                                     "drives": ["Documents"]}, _settings())
    pages = [page async for page in connector.changes({})]
    items = [i for p in pages for i in p.items]
    assert [(i.external_id, i.deleted) for i in items] == [("drive-1:item-1", False), ("drive-1:item-2", True)]
    first = items[0]
    assert first.path == "Documents/Marina Tower/Specs/03 30 00.pdf" and first.etag == "c1"
    assert pages[-1].done and pages[-1].cursor["delta_links"]["drive-1"].endswith("token=next")
    assert await connector.download(first) == b"%PDF-1.7 data"
    assert await connector.permissions(first) == ["sitegroup:site-9:3", "user:u-7"]
    await connector.aclose()


@respx.mock
async def test_graph_throttling_becomes_a_rate_limit() -> None:
    from rag.errors import RateLimited

    _token_route()
    respx.get(f"{GRAPH}/sites/site-9/drives").mock(return_value=httpx.Response(429, headers={"retry-after": "120"}))
    connector = SharePointConnector({"site_id": "site-9"}, _settings())
    with pytest.raises(RateLimited) as exc_info:
        await connector.drives()
    assert exc_info.value.retry_after == 120 and exc_info.value.provider == "graph"
    await connector.aclose()


@respx.mock
async def test_entra_sign_in_verifies_the_id_token() -> None:
    settings = _settings()
    oidc = EntraOIDC(settings)
    url, cookie = oidc.start()
    assert url.startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?")
    assert "code_challenge_method=S256" in url and "nonce=" in url
    state = unsign(cookie, settings.secret_key.get_secret_value())
    assert state is not None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": "client-1", "iat": now,
              "nbf": now, "exp": now + 600, "nonce": state["n"], "oid": "oid-42", "preferred_username": "alex@contoso.com",
              "name": "Alex"}
    id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})
    respx.get(f"https://login.microsoftonline.com/{TENANT}/v2.0/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json={"jwks_uri": "https://login.microsoftonline.com/keys"}))
    respx.get("https://login.microsoftonline.com/keys").mock(return_value=httpx.Response(200, json={"keys": [jwk]}))
    token_route = respx.post(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"id_token": id_token, "access_token": "at"}))

    result = await oidc.finish(code="auth-code", state=state["s"], cookie=cookie)
    assert result["oid"] == "oid-42"
    sent = token_route.calls[0].request.content.decode()
    assert "code_verifier=" in sent and "grant_type=authorization_code" in sent

    from rag.security.auth import AuthError

    with pytest.raises(AuthError):
        await oidc.finish(code="auth-code", state="forged-state", cookie=cookie)
    wrong_audience = jwt.encode({**claims, "aud": "someone-else"}, key, algorithm="RS256", headers={"kid": "k1"})
    token_route.mock(return_value=httpx.Response(200, json={"id_token": wrong_audience}))
    with pytest.raises(jwt.InvalidAudienceError):
        await oidc.finish(code="auth-code", state=state["s"], cookie=cookie)
