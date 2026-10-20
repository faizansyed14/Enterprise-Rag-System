"""Keep access data fresh (control lane jobs):

  sync_principals   a user's transitive Entra group memberships (Microsoft Graph), at sign-in and periodically
  sync_site_groups  SharePoint site group members for every SharePoint source (SharePoint REST)

Members of SharePoint site groups come back as login names; they are stored as principals the user also carries:
  i:0#.f|membership|alex@contoso.com      -> upn:alex@contoso.com
  c:0t.c|tenant|<group id>                -> group:<group id>   (security groups)
  c:0o.c|federateddirectoryclaimprovider|<group id> -> group:<group id> (Microsoft 365 groups)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from rag.connectors.graph_client import GraphClient, TokenProvider, sharepoint_rest
from rag.db import schema
from rag.db.engine import transaction
from rag.jobs.worker import JobContext, handler
from rag.security.principals import replace_principals

log = logging.getLogger(__name__)


def principal_from_login(login: str) -> str | None:
    login = (login or "").strip()
    if not login:
        return None
    if login.lower().startswith("i:0#.f|membership|"):
        return "upn:" + login.split("|")[-1].lower()
    if login.startswith(("c:0t.c|tenant|", "c:0o.c|federateddirectoryclaimprovider|")):
        group = login.split("|")[-1].removesuffix("_o")
        return f"group:{group}"
    if "spo-grid-all-users" in login or login.startswith("c:0(.s|true"):
        return "everyone"
    return None


@handler("sync_principals")
async def sync_principals(ctx: JobContext) -> None:
    settings = ctx.settings
    user_id = ctx.payload["user_id"]
    async with transaction() as conn:
        user = (await conn.execute(select(schema.users).where(schema.users.c.id == user_id))).first()
    if user is None or user.auth_provider != "entra" or not user.external_id:
        return
    graph = GraphClient(settings)
    groups: list[str] = []
    try:
        async for page in graph.pages(f"/users/{user.external_id}/transitiveMemberOf/microsoft.graph.group",
                                      {"$select": "id", "$top": "999"}):
            groups.extend(g["id"] for g in page.get("value", []) if g.get("id"))
    finally:
        await graph.aclose()
    principals = [f"group:{g}" for g in groups]
    is_admin = bool(settings.entra_admin_group_id) and settings.entra_admin_group_id in groups
    async with transaction() as conn:
        await replace_principals(conn, user_id, principals, "entra")
        values: dict[str, Any] = {"principals_synced_at": datetime.now(UTC)}
        if settings.entra_admin_group_id:
            values["is_admin"] = is_admin
        await conn.execute(update(schema.users).where(schema.users.c.id == user_id).values(**values))
    log.info("synced %s Entra groups for user %s", len(groups), user_id)


@handler("sync_site_groups")
async def sync_site_groups(ctx: JobContext) -> None:
    settings = ctx.settings
    async with transaction() as conn:
        sources = (await conn.execute(select(schema.sources).where(schema.sources.c.kind == "sharepoint",
                                                                   schema.sources.c.enabled.is_(True)))).all()
    if not sources:
        return
    tokens = TokenProvider(settings)
    graph = GraphClient(settings, tokens)
    try:
        for src in sources:
            site_url = (src.config or {}).get("site_url")
            if not site_url:
                continue
            site = await graph.get_json(f"/sites/{site_url.split('://', 1)[-1].replace('/sites/', ':/sites/', 1)}")
            data = await sharepoint_rest(settings, tokens, site_url,
                                         "/_api/web/sitegroups?$expand=Users&$select=Id,Title,Users/LoginName")
            rows = []
            for group in data.get("value", []):
                members = sorted({p for u in group.get("Users", []) if (p := principal_from_login(u.get("LoginName")))})
                rows.append({"site_id": site["id"], "group_id": str(group["Id"]), "title": group.get("Title", ""),
                             "members": members, "synced_at": datetime.now(UTC)})
            if rows:
                stmt = insert(schema.site_groups).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[schema.site_groups.c.site_id, schema.site_groups.c.group_id],
                    set_={"title": stmt.excluded.title, "members": stmt.excluded.members,
                          "synced_at": stmt.excluded.synced_at})
                async with transaction() as conn:
                    await conn.execute(stmt)
            log.info("synced %s site groups for %s", len(rows), site_url)
    finally:
        await graph.aclose()
