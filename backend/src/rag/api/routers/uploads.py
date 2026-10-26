"""Uploads: validated, stored by content hash, registered as items of the user's upload source and ingested on the
interactive lane (ahead of any bulk backlog)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
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
from rag.ingestion import archive, protection
from rag.ingestion.formats import SUPPORTED
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


@router.get("/uploads/options")
async def upload_options(_: UserDep, services: ServicesDep) -> dict:
    """What the upload dialog needs to preselect files: the types it unticks by default and the types that can be read."""
    s = services.settings
    return {"deselected_by_default": sorted(s.skip_extension_set), "supported": sorted(SUPPORTED),
            "max_file_mb": s.upload_max_mb, "max_zip_mb": s.upload_zip_max_mb}


async def _target(request: Request, user: Any, principals: list[str], services: Any, share_with: str, project: str) -> _Target:
    s = services.settings
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
    folder = f"Uploads/{project.strip()[:80]}" if project.strip() else f"Uploads/{user.email}"
    return _Target(source_id=await upload_source_id(), user=user, acl=sorted(acl), folder=folder,
                   fields={"project": project.strip()} if project.strip() else {}, settings=s, services=services,
                   ip=client_ip(request))


@router.post("/uploads")
async def upload(request: Request, user: UserDep, principals: PrincipalsDep, services: ServicesDep,
                 files: list[UploadFile] = File(...), share_with: str = Form(default=""),
                 project: str = Form(default="")) -> dict:
    """Files chosen one by one. What to upload was decided in the dialog (types such as JPG are unticked there by default and
    can be ticked); nothing is refused here for its type except what no reader exists for."""
    if len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Upload at most {MAX_FILES} files at a time")
    target = await _target(request, user, principals, services, share_with, project)
    max_bytes = target.settings.upload_max_mb * 1024 * 1024
    results: list[dict[str, Any]] = []
    for file in files:
        name = safe_filename(file.filename or "upload")
        data = await file.read(max_bytes + 1)
        results.append(await _register(target, name, name, data, file.content_type))
    return {"files": results}


# ---- one file -----------------------------------------------------------------------------------------------------------
@dataclass
class _Target:
    """Where the files of one upload request go: the uploader, the sharing, the folder and project."""

    source_id: Any
    user: Any
    acl: list[str]
    folder: str
    fields: dict[str, str]
    settings: Any
    services: Any
    ip: str | None


async def _register(t: _Target, name: str, rel: str, data: bytes, mime: str | None, archive: str | None = None) -> dict:
    """Validate, store and queue one file. `rel` is its path in the upload ("a.pdf", or "Site.zip/Drawings/a.pdf" for a file
    from a ZIP folder): the same path uploaded again replaces the earlier file."""
    s = t.settings
    out: dict[str, Any] = {"file": rel, **({"archive": archive} if archive else {})}
    try:
        fmt = validate_upload(data, name, max_bytes=s.upload_max_mb * 1024 * 1024)
        if s.clamav_url:
            await asyncio.to_thread(clamav_scan, data, s.clamav_url)
    except UploadRejected as exc:
        return {**out, "ok": False, "error": str(exc)}
    except PermanentError as exc:
        return {**out, "ok": False, "error": f"Could not scan the file: {exc}"}
    locked = protection.detect(data, name)
    if locked:  # nothing is queued for a file that cannot be read: the dialog shows why
        return {**out, "ok": False, "protected": True, "error": locked}
    services = t.services
    sha = hashlib.sha256(data).hexdigest()
    key = blob_key(sha)
    if not await services.objects.exists(key):
        await services.objects.put(key, data)
    path = f"{t.folder}/{rel}"
    acl_digest = acl_hash(t.acl)
    async with transaction() as conn:
        await conn.execute(insert(schema.blobs).values(sha256=sha, size=len(data), format=fmt,
                                                       mime=mime or "application/octet-stream",
                                                       storage_key=key).on_conflict_do_nothing())
        # One upload per user and path: uploading the same file (or the same ZIP folder) again replaces the earlier one
        # (its chunks, vectors, pages and stored file are removed once the new one takes over).
        item_id = (await conn.execute(
            insert(schema.source_items).values(
                source_id=t.source_id, external_id=f"{t.user.id}/{rel}", path=path, name=name,
                etag=sha, size=len(data), mime=mime, acl=t.acl, acl_hash=acl_digest, fields=t.fields)
            .on_conflict_do_update(index_elements=[schema.source_items.c.source_id, schema.source_items.c.external_id],
                                   set_={"status": "active", "etag": sha, "size": len(data), "mime": mime, "path": path,
                                         "acl": t.acl, "acl_hash": acl_digest, "fields": t.fields, "error": None})
            .returning(schema.source_items.c.id))).scalar_one()
        if archive is None:
            # Uploads made before this rule were keyed by user, hash and name: retire them the same way.
            older = (await conn.execute(text(
                "UPDATE source_items SET status = 'deleted' WHERE source_id = :s AND name = :n AND id <> :i "
                "AND external_id ~ :p AND status <> 'deleted' RETURNING version_id"),
                {"s": t.source_id, "n": name, "i": item_id, "p": f"^{t.user.id}/[0-9a-f]{{64}}/"})).all()
            for row in older:
                if row.version_id:
                    await queue.enqueue(conn, NewJob("release_version", {"version_id": str(row.version_id)}, lane="control",
                                                     dedupe_key=f"release_version:{row.version_id}",
                                                     source_id=t.source_id))
        await queue.enqueue(conn, NewJob("fetch", {"item_id": str(item_id)}, lane="interactive", priority=10,
                                         dedupe_key=f"fetch:{item_id}:{sha}", source_id=t.source_id))
    await audit("upload", user_id=t.user.id, target=rel, ip=t.ip, details={"sha256": sha})
    return {**out, "ok": True, "item_id": str(item_id), "sha256": sha, "format": fmt}


# ---- a ZIP folder: open it first, upload what is ticked ---------------------------------------------------------------------
@router.post("/uploads/extract")
async def extract_zip(user: UserDep, services: ServicesDep, file: UploadFile = File(...)) -> dict:
    """Open an uploaded ZIP (and every ZIP inside it) into a staging folder and list its files; nothing is read or indexed
    yet. The dialog shows the list, and /uploads/commit uploads only the files the person kept ticked."""
    s = services.settings
    name = safe_filename(file.filename or "archive.zip")
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > s.upload_zip_max_mb * 1024 * 1024:
        raise HTTPException(413,
                            f"The ZIP is larger than {s.upload_zip_max_mb} MB (UPLOAD_ZIP_MAX_MB).")
    await asyncio.to_thread(archive.sweep, s)
    try:
        staging_id, entries = await asyncio.to_thread(archive.extract, s, file.file, name, str(user.id))
    except PermanentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    default_off = s.skip_extension_set
    files = [{**e, "selected": e["supported"] and not e["problem"] and not e["protected"] and e["ext"] not in default_off}
             for e in entries]
    return {"staging_id": staging_id, "name": name, "files": files, "expires_in_hours": archive.STAGING_TTL_S // 3600}


class CommitBody(BaseModel):
    staging_id: str = Field(min_length=32, max_length=32)
    ids: list[str] = Field(min_length=1, max_length=archive.MAX_FILES)
    share_with: str = ""
    project: str = ""


@router.post("/uploads/commit")
async def commit_zip(body: CommitBody, request: Request, user: UserDep, principals: PrincipalsDep,
                     services: ServicesDep) -> dict:
    """Upload the files of an opened ZIP that were ticked. The unpacked files stay (so more can be ticked and uploaded) until
    the dialog is closed (DELETE /uploads/staged/...) or they expire."""
    s = services.settings
    target = await _target(request, user, principals, services, body.share_with, body.project)
    try:
        root, manifest = archive.load(s, body.staging_id, str(user.id))
    except PermanentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    results: list[dict[str, Any]] = []
    for fid in dict.fromkeys(body.ids):
        entry = manifest["files"].get(fid)
        if entry is None or entry.get("problem"):
            results.append({"id": fid, "file": (entry or {}).get("path", fid), "ok": False,
                            "protected": bool((entry or {}).get("protected")),
                            "error": (entry or {}).get("problem") or "Not part of this ZIP"})
            continue
        data = await asyncio.to_thread((root / "files" / fid).read_bytes)
        res = await _register(target, entry["name"], f"{manifest['name']}/{entry['path']}", data, None,
                              archive=manifest["name"])
        results.append({**res, "id": fid})
    queued = sum(1 for r in results if r["ok"])
    await audit("upload_zip", user_id=user.id, target=manifest["name"], ip=client_ip(request),
                details={"selected": len(results), "queued": queued})
    return {"files": results, "queued": queued, "failed": len(results) - queued}


@router.delete("/uploads/staged/{staging_id}")
async def discard_zip(staging_id: str, user: UserDep, services: ServicesDep) -> dict:
    """The dialog was closed without uploading: remove the unpacked files."""
    s = services.settings
    try:
        archive.load(s, staging_id, str(user.id))
    except PermanentError:
        return {"ok": True}  # already gone, or not theirs: nothing to do and nothing to tell
    await asyncio.to_thread(archive.remove, s, staging_id)
    return {"ok": True}


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
        items.append(
            {
                "item_id": r["id"],
                "stage": stage,
                "step": "ready" if stage == "live" else ("indexing" if r["extracted"] else "reading"),
                "error": r["skip_reason"] or r["error"],
                "document_id": r["document_id"],
            }
        )
    return {"items": items}


@router.get("/uploads/{item_id}")
async def upload_status(item_id: str, user: UserDep, services: ServicesDep) -> dict:
    from rag.privacy.report import scan_pages

    async with transaction() as conn:
        row = (
            await conn.execute(
                select(
                    schema.source_items.c.id,
                    schema.source_items.c.name,
                    schema.source_items.c.status,
                    schema.source_items.c.error,
                    schema.source_items.c.skip_reason,
                    schema.versions.c.state,
                    schema.versions.c.document_id,
                    schema.source_items.c.content_sha256,
                )
                .outerjoin(schema.versions, schema.versions.c.id == schema.source_items.c.version_id)
                .where(schema.source_items.c.id == item_id, schema.source_items.c.external_id.like(f"{user.id}/%"))
            )
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found")
    stage = row.state or ("failed" if row.status in {"error", "skipped"} else "processing")
    s = services.settings
    privacy: dict | None = None
    step = "reading"
    if row.content_sha256:
        async with transaction() as conn:
            extracted = (
                await conn.execute(
                    text("SELECT extracted_at IS NOT NULL FROM blobs WHERE sha256 = :s"), {"s": row.content_sha256}
                )
            ).scalar()
            pages = (
                (
                    await conn.execute(
                        text("SELECT page_no, text, tables FROM pages WHERE blob_sha256 = :s"), {"s": row.content_sha256}
                    )
                ).all()
                if extracted
                else []
            )
        if extracted:
            step = "indexing"
            report = scan_pages([(p[0], p[1] or "", p[2] or []) for p in pages], s.privacy_kind_list)
            privacy = {
                **report,
                "shield": s.privacy_mask_for_models,
                "held_back": bool(report["found"] and s.privacy_auto_local_only),
                "redacted": s.privacy_redact_stored,
            }
    return {
        "item_id": str(row.id),
        "name": row.name,
        "stage": stage,
        "step": "ready" if stage == "live" else step,
        "error": row.skip_reason or row.error,
        "document_id": str(row.document_id) if row.document_id else None,
        "privacy": privacy,
    }
