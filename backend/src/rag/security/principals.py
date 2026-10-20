"""Who a user is for access control: the set of principals their documents are matched against.

  everyone                       every signed-in user
  user:<entra object id | local user id>
  upn:<sign-in name>             Entra users (how SharePoint site groups list their members)
  group:<entra group id>         transitive Entra membership (synced from Microsoft Graph)
  lgroup:<name>                  local groups assigned by an admin (local accounts and dev mode)
  sitegroup:<site id>:<group id> SharePoint site groups whose members include the user or one of their groups

Documents carry the principals that may read them (version_access in Postgres, `acl` in every Qdrant point). A search
filters on the overlap of the two sets, and every candidate is checked again in Postgres before anything is shown.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from rag.db import schema

EVERYONE = "everyone"


def user_principal(user: Any) -> str:
    external = getattr(user, "external_id", None) if not isinstance(user, dict) else user.get("external_id")
    ident = getattr(user, "id", None) if not isinstance(user, dict) else user.get("id")
    return f"user:{external or ident}"


async def principals_for(conn: AsyncConnection, user: Any) -> list[str]:
    user_id = user["id"] if isinstance(user, dict) else user.id
    base = {EVERYONE, user_principal(user)}
    provider = user["auth_provider"] if isinstance(user, dict) else getattr(user, "auth_provider", "local")
    email = user["email"] if isinstance(user, dict) else getattr(user, "email", "")
    if provider == "entra" and email:
        base.add(f"upn:{email.lower()}")  # SharePoint site group members are listed by sign-in name
    rows = await conn.execute(
        select(schema.user_principals.c.principal).where(schema.user_principals.c.user_id == user_id)
    )
    base.update(r[0] for r in rows.all())
    site_rows = await conn.execute(
        text("SELECT 'sitegroup:' || site_id || ':' || group_id FROM site_groups WHERE members && CAST(:p AS text[])"),
        {"p": sorted(base)},
    )
    base.update(r[0] for r in site_rows.all())
    return sorted(base)


async def replace_principals(conn: AsyncConnection, user_id: Any, principals: list[str], source: str) -> None:
    """Set the user's principals from one source (entra | local) without touching the others."""
    await conn.execute(
        schema.user_principals.delete().where(schema.user_principals.c.user_id == user_id,
                                              schema.user_principals.c.source == source)
    )
    unique = sorted(set(principals))
    if unique:
        from sqlalchemy.dialects.postgresql import insert

        await conn.execute(
            insert(schema.user_principals).on_conflict_do_nothing(),
            [{"user_id": user_id, "principal": p, "source": source} for p in unique],
        )
