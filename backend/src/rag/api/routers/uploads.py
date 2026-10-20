"""Uploads: validated, stored by content hash, registered as items of the user's upload source and ingested on the
interactive lane (ahead of any bulk backlog)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from rag.api.deps import PrincipalsDep, ServicesDep, UserDep, client_ip
from rag.connectors.base import acl_hash
from rag.db import schema
from rag.db.engine import transaction
from rag.errors import PermanentError
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.providers.storage.objects import blob_key
from rag.security.audit import audit
from rag.security.principals import EVERYONE, user_principal
from rag.security.rate_limit import RateLimitExceeded, hit
from rag.security.uploads import UploadRejected, clamav_scan, safe_filename, validate_upload

router = APIRouter(tags=["uploads"])
MAX_FILES = 50


async def upload_source_id() -> Any:
    async with transaction() as conn:
        row = (await conn.execute(select(schema.sources.c.id).where(schema.sources.c.kind == "upload"))).first()
        if row:
            return row.id
        return (await conn.execute(insert(schema.sources).values(name="Uploads", kind="upload", config={})
                                   .on_conflict_do_nothing().returning(schema.sources.c.id))).scalar_one()


@router.post("/uploads")
async def upload(request: Request, user: UserDep, principals: PrincipalsDep, services: ServicesDep,
                 files: list[UploadFile] = File(...), share_with: str = Form(default=""),
                 project: str = Form(default="")) -> dict:
    s = services.settings
    if len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Upload at most {MAX_FILES} files at a time")
    try:
        await hit(f"upload:{user.id}", s.rate_limit_uploads_per_hour, 3600)
    except RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many uploads this hour",
                            headers={"Retry-After": str(exc.retry_after)}) from exc
    # Sharing: the uploader always; plus groups the uploader belongs to (admins may share with anyone/everyone).
    acl = {user_principal(user)}
    for principal in [p.strip() for p in share_with.split(",") if p.strip()]:
        if principal in principals or user.is_admin:
            acl.add(principal)
        elif principal == EVERYONE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only administrators can share uploads with everyone")
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"You are not a member of {principal}")
    source_id = await upload_source_id()
    max_bytes = s.upload_max_mb * 1024 * 1024
    results: list[dict[str, Any]] = []
    for file in files:
        name = safe_filename(file.filename or "upload")
        data = await file.read(max_bytes + 1)
        try:
            fmt = validate_upload(data, name, max_bytes=max_bytes)
            if s.clamav_url:
                await asyncio.to_thread(clamav_scan, data, s.clamav_url)
        except UploadRejected as exc:
            results.append({"file": name, "ok": False, "error": str(exc)})
            continue
        except PermanentError as exc:
            results.append({"file": name, "ok": False, "error": f"Could not scan the file: {exc}"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        key = blob_key(sha)
        if not await services.objects.exists(key):
            await services.objects.put(key, data)
        folder = f"Uploads/{project.strip()[:80]}" if project.strip() else f"Uploads/{user.email}"
        fields = {"project": project.strip()} if project.strip() else {}
        async with transaction() as conn:
            await conn.execute(insert(schema.blobs).values(sha256=sha, size=len(data), format=fmt,
                                                           mime=file.content_type or "application/octet-stream",
                                                           storage_key=key).on_conflict_do_nothing())
            # One upload per user and file name: uploading the same name again replaces the earlier file (its chunks,
            # vectors, pages and stored file are removed once the new one takes over).
            item_id = (await conn.execute(
                insert(schema.source_items).values(
                    source_id=source_id, external_id=f"{user.id}/{name}", path=f"{folder}/{name}", name=name,
                    etag=sha, size=len(data), mime=file.content_type, acl=sorted(acl), acl_hash=acl_hash(sorted(acl)),
                    fields=fields)
                .on_conflict_do_update(index_elements=[schema.source_items.c.source_id,
                                                       schema.source_items.c.external_id],
                                       set_={"status": "active", "etag": sha, "size": len(data), "mime": file.content_type,
                                             "path": f"{folder}/{name}", "acl": sorted(acl),
                                             "acl_hash": acl_hash(sorted(acl)), "fields": fields, "error": None})
                .returning(schema.source_items.c.id))).scalar_one()
            # Uploads made before this rule were keyed by user, hash and name: retire them the same way.
            older = (await conn.execute(text(
                "UPDATE source_items SET status = 'deleted' WHERE source_id = :s AND name = :n AND id <> :i "
                "AND external_id LIKE :p AND status <> 'deleted' RETURNING version_id"),
                {"s": source_id, "n": name, "i": item_id, "p": f"{user.id}/%/%"})).all()
            for row in older:
                if row.version_id:
                    await queue.enqueue(conn, NewJob("release_version", {"version_id": str(row.version_id)}, lane="control",
                                                     dedupe_key=f"release_version:{row.version_id}",
                                                     source_id=source_id))
            await queue.enqueue(conn, NewJob("fetch", {"item_id": str(item_id)}, lane="interactive", priority=10,
                                             dedupe_key=f"fetch:{item_id}:{sha}", source_id=source_id))
        results.append({"file": name, "ok": True, "item_id": str(item_id), "sha256": sha, "format": fmt})
        await audit("upload", user_id=user.id, target=name, ip=client_ip(request), details={"sha256": sha})
    return {"files": results}


class StatusBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)


@router.post("/uploads/status")
async def upload_status_batch(body: StatusBody, user: UserDep) -> dict:
    """The progress of many uploads in one request (the upload dialog follows hundreds of files at once). No privacy scan here:
    that reads every page, so it is asked for one file at a time."""
    try:
        ids = [str(uuid.UUID(i)) for i in body.ids]
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid upload id") from exc
    async with transaction() as conn:
        rows = (await conn.execute(text(
            """
            SELECT i.id::text AS id, i.status, i.error, i.skip_reason, v.state, v.document_id::text AS document_id,
                   (b.extracted_at IS NOT NULL) AS extracted
            FROM source_items i LEFT JOIN versions v ON v.id = i.version_id LEFT JOIN blobs b ON b.sha256 = i.content_sha256
            WHERE i.id = ANY(CAST(:ids AS uuid[])) AND i.external_id LIKE :own
            """), {"ids": ids, "own": f"{user.id}/%"})).mappings().all()
    items = []
    for r in rows:
        stage = r["state"] or ("failed" if r["status"] in {"error", "skipped"} else "processing")
        items.append({"item_id": r["id"], "stage": stage, "step": "ready" if stage == "live" else ("indexing" if r["extracted"] else "reading"),
                      "error": r["skip_reason"] or r["error"], "document_id": r["document_id"]})
    return {"items": items}


@router.get("/uploads/{item_id}")
async def upload_status(item_id: str, user: UserDep, services: ServicesDep) -> dict:
    from rag.privacy.report import scan_pages

    async with transaction() as conn:
        row = (await conn.execute(
            select(schema.source_items.c.id, schema.source_items.c.name, schema.source_items.c.status,
                   schema.source_items.c.error, schema.source_items.c.skip_reason, schema.versions.c.state,
                   schema.versions.c.document_id, schema.source_items.c.content_sha256)
            .outerjoin(schema.versions, schema.versions.c.id == schema.source_items.c.version_id)
            .where(schema.source_items.c.id == item_id,
                   schema.source_items.c.external_id.like(f"{user.id}/%")))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found")
    stage = row.state or ("failed" if row.status in {"error", "skipped"} else "processing")
    s = services.settings
    privacy: dict | None = None
    step = "reading"
    if row.content_sha256:
        async with transaction() as conn:
            extracted = (await conn.execute(text("SELECT extracted_at IS NOT NULL FROM blobs WHERE sha256 = :s"),
                                            {"s": row.content_sha256})).scalar()
            pages = (await conn.execute(text("SELECT page_no, text, tables FROM pages WHERE blob_sha256 = :s"),
                                        {"s": row.content_sha256})).all() if extracted else []
        if extracted:
            step = "indexing"
            report = scan_pages([(p[0], p[1] or "", p[2] or []) for p in pages], s.privacy_kind_list)
            privacy = {**report, "shield": s.privacy_mask_for_models,
                       "held_back": bool(report["found"] and s.privacy_auto_local_only), "redacted": s.privacy_redact_stored}
    return {"item_id": str(row.id), "name": row.name, "stage": stage, "step": "ready" if stage == "live" else step,
            "error": row.skip_reason or row.error, "document_id": str(row.document_id) if row.document_id else None,
            "privacy": privacy}
