"""Sources (administrators): SharePoint sites, OneDrive accounts, SMB shares, folders. Secrets never go in the
database: connector configs hold names of environment variables, not passwords."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from rag.api.deps import AdminDep, ServicesDep, client_ip
from rag.connectors.base import known_kinds, make_connector
from rag.db import schema
from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.security.audit import audit

router = APIRouter(prefix="/sources", tags=["sources"])
SRC = schema.sources.c
SECRET_KEYS = {"password", "secret", "client_secret", "token", "api_key"}


class SourceBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    default_acl: list[str] = Field(default_factory=lambda: ["everyone"])
    schedule_minutes: int | None = Field(default=60, ge=5, le=60 * 24 * 7)
    enabled: bool = True

    @field_validator("config")
    @classmethod
    def _no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        leaked = [k for k in value if k.lower() in SECRET_KEYS]
        if leaked:
            raise ValueError(f"store secrets in environment variables, not in the source config ({', '.join(leaked)})")
        return value


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    config: dict[str, Any] | None = None
    default_acl: list[str] | None = None
    schedule_minutes: int | None = None
    enabled: bool | None = None


def _row(r: Any, counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    return {"id": str(r.id), "name": r.name, "kind": r.kind, "config": r.config, "enabled": r.enabled,
            "schedule_minutes": r.schedule_minutes, "default_acl": r.default_acl,
            "last_sync_started_at": r.last_sync_started_at.isoformat() if r.last_sync_started_at else None,
            "last_sync_finished_at": r.last_sync_finished_at.isoformat() if r.last_sync_finished_at else None,
            "last_sync_status": r.last_sync_status, "last_sync_error": r.last_sync_error, "stats": r.stats,
            "items": counts.get(str(r.id), {})}


@router.get("")
async def list_sources(_: AdminDep) -> dict:
    async with transaction() as conn:
        rows = (await conn.execute(select(schema.sources).order_by(SRC.created_at))).all()
        counts_rows = (await conn.execute(
            select(schema.source_items.c.source_id, schema.source_items.c.status, func.count())
            .group_by(schema.source_items.c.source_id, schema.source_items.c.status))).all()
    counts: dict[str, dict[str, int]] = {}
    for source_id, item_status, n in counts_rows:
        counts.setdefault(str(source_id), {})[item_status] = n
    return {"sources": [_row(r, counts) for r in rows], "kinds": known_kinds()}


@router.post("")
async def create_source(body: SourceBody, request: Request, user: AdminDep, services: ServicesDep) -> dict:
    make_connector(body.kind, body.config, services.settings)  # validates kind and required config
    async with transaction() as conn:
        exists = (await conn.execute(select(SRC.id).where(SRC.name == body.name))).first()
        if exists:
            raise HTTPException(status.HTTP_409_CONFLICT, "A source with this name exists")
        source_id = (await conn.execute(schema.sources.insert().values(
            name=body.name, kind=body.kind, config=body.config, default_acl=body.default_acl,
            schedule_minutes=body.schedule_minutes, enabled=body.enabled, created_by=user.id)
            .returning(SRC.id))).scalar_one()
    await audit("source_create", user_id=user.id, target=str(source_id), ip=client_ip(request),
                details={"kind": body.kind, "name": body.name})
    return {"id": str(source_id)}


@router.patch("/{source_id}")
async def patch_source(source_id: UUID, body: SourcePatch, request: Request, user: AdminDep) -> dict:
    values = body.model_dump(exclude_none=True)
    if "config" in values:
        SourceBody._no_secrets(values["config"])
    async with transaction() as conn:
        result = await conn.execute(update(schema.sources).where(SRC.id == source_id).values(**values)
                                    .returning(SRC.id))
        if result.first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
        if "default_acl" in values:
            # New default access applies to items without their own permissions: refresh them on the control lane.
            items = (await conn.execute(select(schema.source_items.c.id).where(
                schema.source_items.c.source_id == source_id, schema.source_items.c.status == "active"))).all()
            await queue.enqueue_many(conn, [NewJob("refresh_acl", {"item_id": str(i.id)}, lane="control",
                                                   dedupe_key=f"refresh_acl:{i.id}") for i in items])
    await audit("source_update", user_id=user.id, target=str(source_id), ip=client_ip(request),
                details={k: v for k, v in values.items() if k != "config"})
    return {"ok": True}


@router.delete("/{source_id}")
async def delete_source(source_id: UUID, request: Request, user: AdminDep) -> dict:
    """Disable the source and remove its documents from search (deletions run on the control lane)."""
    async with transaction() as conn:
        result = await conn.execute(update(schema.sources).where(SRC.id == source_id).values(enabled=False)
                                    .returning(SRC.id))
        if result.first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
        items = (await conn.execute(update(schema.source_items)
                                    .where(schema.source_items.c.source_id == source_id,
                                           schema.source_items.c.status != "deleted")
                                    .values(status="deleted").returning(schema.source_items.c.id))).all()
        await queue.enqueue_many(conn, [NewJob("delete_item", {"item_id": str(i.id)}, lane="control",
                                               dedupe_key=f"delete_item:{i.id}") for i in items])
    await audit("source_delete", user_id=user.id, target=str(source_id), ip=client_ip(request))
    return {"ok": True, "items_removed": len(items)}


@router.post("/{source_id}/test")
async def test_source(source_id: UUID, _: AdminDep, services: ServicesDep) -> dict:
    async with transaction() as conn:
        src = (await conn.execute(select(schema.sources).where(SRC.id == source_id))).first()
    if src is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    connector = make_connector(src.kind, dict(src.config), services.settings)
    try:
        return await connector.test()
    except Exception as exc:  # noqa: BLE001 - shown to the admin as the test result
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        await connector.aclose()


@router.post("/{source_id}/sync")
async def sync_source(source_id: UUID, request: Request, user: AdminDep, full: bool = False) -> dict:
    async with transaction() as conn:
        job_id = await queue.enqueue(conn, NewJob("sync_source", {"source_id": str(source_id), "full": full},
                                                  lane="interactive", dedupe_key=f"sync:{source_id}",
                                                  source_id=source_id))
    await audit("source_sync", user_id=user.id, target=str(source_id), ip=client_ip(request), details={"full": full})
    return {"job_id": job_id}
