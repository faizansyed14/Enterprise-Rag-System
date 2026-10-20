"""Sign-in and sessions.

Sessions are opaque random tokens in an httpOnly, SameSite=Lax cookie (Secure in production); only their SHA-256 is
stored, so a database leak does not leak sessions. They expire after SESSION_TTL_HOURS, or SESSION_IDLE_MINUTES of
inactivity. Every state-changing request must echo the session's CSRF token in X-CSRF-Token (double submit), and a
request whose Origin is not an allowed origin is refused.

Local accounts use Argon2id password hashes with lockout after repeated failures. Microsoft Entra ID sign-in uses
the OpenID Connect authorization-code flow with PKCE, state and nonce; ID tokens are verified against the tenant's
published signing keys (issuer, audience, expiry, nonce).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from rag.config.settings import Settings
from rag.db import schema
from rag.errors import PermanentError
from rag.providers.http import check_response, make_client

SESSION_COOKIE = "rag_session"
CSRF_COOKIE = "rag_csrf"
OIDC_COOKIE = "rag_oidc"
LOCKOUT_AFTER = 8
LOCKOUT_MINUTES = 15

_hasher = PasswordHasher()  # Argon2id with library defaults (time_cost=3, memory_cost=64 MiB, parallelism=4)
_DUMMY_HASH = _hasher.hash("timing-equaliser")


@dataclass
class CurrentUser:
    id: UUID
    email: str
    display_name: str
    is_admin: bool
    auth_provider: str
    external_id: str | None
    session_hash: str = ""
    csrf_token: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": str(self.id), "email": self.email, "display_name": self.display_name,
                "is_admin": self.is_admin, "auth_provider": self.auth_provider}


class AuthError(PermanentError):
    pass


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise AuthError("Passwords must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    if not stored:
        # Spend the same time as a real check so response timing does not reveal which e-mails exist.
        with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHashError):
            _hasher.verify(_DUMMY_HASH, password)
        return False
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- local accounts ---------------------------------------------------------------------------------------------------


async def authenticate_local(conn: AsyncConnection, email: str, password: str) -> Any | None:
    """The user row on success; None on bad credentials or lockout (same answer either way)."""
    row = (await conn.execute(select(schema.users).where(func.lower(schema.users.c.email) == email.strip().lower()))
           ).first()
    now = datetime.now(UTC)
    if row is None or not row.is_active or row.auth_provider != "local":
        verify_password(None, password)
        return None
    if row.locked_until and row.locked_until > now:
        verify_password(None, password)
        return None
    if not verify_password(row.password_hash, password):
        failed = row.failed_logins + 1
        values: dict[str, Any] = {"failed_logins": failed}
        if failed >= LOCKOUT_AFTER:
            values = {"failed_logins": 0, "locked_until": now + timedelta(minutes=LOCKOUT_MINUTES)}
        await conn.execute(update(schema.users).where(schema.users.c.id == row.id).values(**values))
        return None
    if _hasher.check_needs_rehash(row.password_hash):
        await conn.execute(update(schema.users).where(schema.users.c.id == row.id)
                           .values(password_hash=_hasher.hash(password)))
    await conn.execute(update(schema.users).where(schema.users.c.id == row.id)
                       .values(failed_logins=0, locked_until=None, last_login_at=now))
    return row


async def create_user(conn: AsyncConnection, *, email: str, password: str | None, display_name: str = "",
                      is_admin: bool = False, auth_provider: str = "local", external_id: str | None = None) -> UUID:
    values: dict[str, Any] = {"email": email.strip(), "display_name": display_name or email.split("@", maxsplit=1)[0],
                              "is_admin": is_admin, "auth_provider": auth_provider, "external_id": external_id}
    if password is not None:
        values["password_hash"] = hash_password(password)
    result = await conn.execute(insert(schema.users).values(**values).returning(schema.users.c.id))
    return result.scalar_one()


# ---- sessions ---------------------------------------------------------------------------------------------------------


async def create_session(conn: AsyncConnection, user_id: UUID, settings: Settings, *, ip: str | None,
                         user_agent: str | None) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    await conn.execute(insert(schema.sessions).values(
        token_hash=token_hash(token), user_id=user_id, csrf_token=csrf,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
        ip=(ip or "")[:64], user_agent=(user_agent or "")[:300]))
    return token, csrf


async def load_session(conn: AsyncConnection, token: str, settings: Settings) -> CurrentUser | None:
    if not token or len(token) > 200:
        return None
    digest = token_hash(token)
    row = (await conn.execute(
        select(schema.sessions.c.csrf_token, schema.sessions.c.expires_at, schema.sessions.c.last_seen_at,
               schema.users.c.id, schema.users.c.email, schema.users.c.display_name, schema.users.c.is_admin,
               schema.users.c.is_active, schema.users.c.auth_provider, schema.users.c.external_id)
        .join(schema.users, schema.users.c.id == schema.sessions.c.user_id)
        .where(schema.sessions.c.token_hash == digest))).first()
    now = datetime.now(UTC)
    if row is None or not row.is_active or row.expires_at <= now:
        return None
    if row.last_seen_at + timedelta(minutes=settings.session_idle_minutes) <= now:
        await conn.execute(schema.sessions.delete().where(schema.sessions.c.token_hash == digest))
        return None
    if (now - row.last_seen_at).total_seconds() > 60:
        await conn.execute(update(schema.sessions).where(schema.sessions.c.token_hash == digest)
                           .values(last_seen_at=now))
    return CurrentUser(id=row.id, email=row.email, display_name=row.display_name, is_admin=row.is_admin,
                       auth_provider=row.auth_provider, external_id=row.external_id, session_hash=digest,
                       csrf_token=row.csrf_token)


async def end_session(conn: AsyncConnection, session_hash: str) -> None:
    await conn.execute(schema.sessions.delete().where(schema.sessions.c.token_hash == session_hash))


def csrf_ok(user: CurrentUser, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(user.csrf_token, header_value or "")


# ---- signed short-lived cookie for the OIDC round trip -----------------------------------------------------------------


def sign(data: dict[str, Any], secret: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(value: str, secret: str, *, max_age_s: int = 600) -> dict[str, Any] | None:
    try:
        body, mac = value.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    if time.time() - float(data.get("t", 0)) > max_age_s:
        return None
    return data


# ---- Microsoft Entra ID (OpenID Connect) -------------------------------------------------------------------------------


class EntraOIDC:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tenant = settings.entra_tenant_id
        self.client_id = settings.entra_client_id
        self.base = f"{settings.entra_authority.rstrip('/')}/{self.tenant}"
        self.issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        self._jwks: tuple[float, dict[str, Any]] | None = None

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url.rstrip('/')}/api/v1/auth/oidc/callback"

    def start(self) -> tuple[str, str]:
        """(authorize URL, signed cookie value carrying state, nonce and the PKCE verifier)."""
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "response_mode": "query", "scope": "openid profile email offline_access", "state": state,
            "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        cookie = sign({"s": state, "n": nonce, "v": verifier, "t": time.time()},
                      self.settings.secret_key.get_secret_value())
        return f"{self.base}/oauth2/v2.0/authorize?{query}", cookie

    async def _keys(self) -> dict[str, Any]:
        if self._jwks and time.time() - self._jwks[0] < 3600:
            return self._jwks[1]
        async with make_client(timeout=15) as client:
            config = check_response(await client.get(f"{self.base}/v2.0/.well-known/openid-configuration"),
                                    "entra").json()
            jwks = check_response(await client.get(config["jwks_uri"]), "entra").json()
        keys = {k["kid"]: k for k in jwks.get("keys", []) if k.get("kid")}
        self._jwks = (time.time(), keys)
        return keys

    async def finish(self, *, code: str, state: str, cookie: str) -> dict[str, Any]:
        data = unsign(cookie or "", self.settings.secret_key.get_secret_value())
        if data is None or not hmac.compare_digest(str(data.get("s", "")), state or ""):
            raise AuthError("Sign-in expired or was tampered with; please try again")
        body = {"client_id": self.client_id, "grant_type": "authorization_code", "code": code,
                "redirect_uri": self.redirect_uri, "code_verifier": data["v"],
                "client_secret": self.settings.secret("entra_client_secret") or ""}
        async with make_client(timeout=20) as client:
            tokens = check_response(await client.post(f"{self.base}/oauth2/v2.0/token", data=body), "entra").json()
        id_token = tokens.get("id_token") or ""
        header = jwt.get_unverified_header(id_token)
        keys = await self._keys()
        jwk = keys.get(header.get("kid", ""))
        if jwk is None:
            self._jwks = None
            jwk = (await self._keys()).get(header.get("kid", ""))
        if jwk is None:
            raise AuthError("Unknown token signing key")
        claims = jwt.decode(id_token, key=RSAAlgorithm.from_jwk(json.dumps(jwk)), algorithms=["RS256"],
                            audience=self.client_id, issuer=self.issuer, options={"require": ["exp", "iat", "aud"]})
        if not hmac.compare_digest(str(claims.get("nonce", "")), str(data.get("n", ""))):
            raise AuthError("Sign-in nonce mismatch")
        return claims


async def upsert_entra_user(conn: AsyncConnection, claims: dict[str, Any]) -> Any:
    oid = claims.get("oid") or claims.get("sub")
    email = claims.get("preferred_username") or claims.get("email") or claims.get("upn") or f"{oid}@entra"
    name = claims.get("name") or email
    stmt = insert(schema.users).values(email=email, display_name=name, auth_provider="entra", external_id=oid,
                                       last_login_at=datetime.now(UTC))
    stmt = stmt.on_conflict_do_update(
        index_elements=[schema.users.c.external_id],
        index_where=schema.users.c.external_id.isnot(None),
        set_={"email": email, "display_name": name, "last_login_at": datetime.now(UTC)},
    ).returning(schema.users)
    return (await conn.execute(stmt)).first()
