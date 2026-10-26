"""Documents the user can see: list and facets, details with references, page text, rendered page
images with quote highlights, and the original file. Every endpoint checks access per version."""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from qdrant_client import models
from rapidfuzz import fuzz
from sqlalchemy import text

from rag.api.deps import PrincipalsDep, ServicesDep, UserDep, client_ip
from rag.cache import get_cache
from rag.db.engine import transaction
from rag.errors import PermanentError
from rag.ingestion.cad import CAD_FORMATS
from rag.providers.parsers.office_preview import OFFICE_PREVIEW_FORMATS, render_office_page_png
from rag.providers.parsers.pdfium_thread import run_pdfium
from rag.providers.storage.objects import blob_key
from rag.providers.vectorstore.qdrant import match_value, version_filter
from rag.security.audit import audit

router = APIRouter(prefix="/documents", tags=["documents"])

VISIBLE = "EXISTS (SELECT 1 FROM version_access a WHERE a.version_id = v.id AND a.principal = ANY(CAST(:p AS text[])))"
IMAGE_FORMATS = frozenset({"pdf", "image"}) | OFFICE_PREVIEW_FORMATS | CAD_FORMATS
# list orderings (whitelisted: the value is put into the query text); the name tie-break keeps pages stable
SORTS = {
    "newest": "d.updated_at DESC, lower(coalesce(vv.file_name, d.title)), d.id",
    "oldest": "d.updated_at ASC, lower(coalesce(vv.file_name, d.title)), d.id",
    "name_asc": "lower(coalesce(vv.file_name, d.title)) ASC, d.id",
    "name_desc": "lower(coalesce(vv.file_name, d.title)) DESC, d.id",
}
NO_PROJECT = "__none__"  # the folder of files that have no project yet


@router.get("")
async def list_documents(principals: PrincipalsDep, q: str = "", project: list[str] = Query(default=[]),
                         doc_type: list[str] = Query(default=[]),
                         sort: str = "newest", limit: int = 50, offset: int = 0) -> dict:
    where = ["d.status = 'active'"]
    params: dict[str, Any] = {"p": principals, "limit": max(1, min(limit, 200)), "offset": max(0, offset)}
    if q:
        where.append("(d.title ILIKE :q OR d.doc_number ILIKE :q OR vv.file_name ILIKE :q)")
        params["q"] = f"%{q.strip()[:100]}%"
    if project:
        named = [p for p in project if p != NO_PROJECT]
        clauses = []
        if named:
            clauses.append("d.project_ids && CAST(:projects AS text[])")
            params["projects"] = named
        if NO_PROJECT in project:
            clauses.append("cardinality(d.project_ids) = 0")
        where.append("(" + " OR ".join(clauses) + ")")
    if doc_type:
        where.append("d.doc_type = ANY(CAST(:types AS text[]))")
        params["types"] = doc_type
    sql = f"""
        SELECT d.id, d.doc_number, d.title, d.project_ids, d.doc_type, d.updated_at,
               vv.version_id, vv.page_count, vv.language, vv.file_name, count(*) OVER () AS total
        FROM documents d
        JOIN LATERAL (
            SELECT v.id AS version_id, v.page_count, v.language,
                   (SELECT i.name FROM source_items i WHERE i.version_id = v.id AND i.status = 'active' LIMIT 1)
                       AS file_name
            FROM versions v WHERE v.document_id = d.id AND v.state = 'live' AND {VISIBLE}
            ORDER BY v.created_at DESC LIMIT 1) vv ON true
        WHERE {' AND '.join(where)}
        ORDER BY {SORTS.get(sort, SORTS['newest'])} LIMIT :limit OFFSET :offset"""  # noqa: S608 - fixed fragments, values bound
    async with transaction() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    total = rows[0]["total"] if rows else 0
    return {"total": total, "documents": [{
        "id": str(r["id"]), "doc_number": r["doc_number"], "title": r["title"], "project_ids": r["project_ids"],
        "doc_type": r["doc_type"], "updated_at": r["updated_at"].isoformat(),
        "version_id": str(r["version_id"]),
        "page_count": r["page_count"], "language": r["language"], "file_name": r["file_name"]} for r in rows]}


@router.get("/facets")
async def facets(principals: PrincipalsDep) -> dict:
    sql = f"""
        SELECT kind, value, count(*) FROM (
            SELECT 'project' AS kind, unnest(d.project_ids) AS value FROM documents d
            WHERE d.status = 'active' AND EXISTS (SELECT 1 FROM versions v WHERE v.document_id = d.id
                                                  AND v.state = 'live' AND {VISIBLE})
            UNION ALL
            SELECT 'unassigned', '' FROM documents d WHERE d.status = 'active' AND cardinality(d.project_ids) = 0
              AND EXISTS (SELECT 1 FROM versions v WHERE v.document_id = d.id AND v.state = 'live' AND {VISIBLE})
            UNION ALL
            SELECT 'doc_type', d.doc_type FROM documents d WHERE d.status = 'active' AND d.doc_type IS NOT NULL
              AND EXISTS (SELECT 1 FROM versions v WHERE v.document_id = d.id AND v.state = 'live' AND {VISIBLE})
        ) f GROUP BY kind, value ORDER BY kind, count(*) DESC"""  # noqa: S608 - fixed fragments, values bound
    async with transaction() as conn:
        rows = (await conn.execute(text(sql), {"p": principals})).all()
    out: dict[str, Any] = {"project": [], "doc_type": [], "unassigned": 0}
    for kind, value, count in rows:
        if kind == "unassigned":
            out["unassigned"] = count
        else:
            out[kind].append({"value": value, "count": count})
    return out


STEP_LABELS = {
    "fetch": "Fetching the file", "extract": "Reading the file (text, tables, OCR)", "enrich": "Finding title, type and project",
    "chunk": "Splitting into chunks", "embed": "Creating search embeddings", "publish": "Publishing to search",
}
STEP_ORDER = ["fetch", "extract", "enrich", "chunk", "embed", "publish"]
SHOWN = 300  # rows of the queue sent to the page; the counts above them are over every file


async def _unfinished_items(conn: Any, user: Any) -> list[dict[str, Any]]:
    """Files that are not searchable yet (or could not be read): the caller's own uploads, or everything for an administrator."""
    rows = (await conn.execute(text(
        """
        SELECT i.id::text AS id, i.name, i.status, i.error, i.skip_reason, i.content_sha256, i.version_id::text AS version_id,
               v.state AS version_state, v.chunk_count, v.page_count, s.name AS source
        FROM source_items i JOIN sources s ON s.id = i.source_id LEFT JOIN versions v ON v.id = i.version_id
        WHERE i.status <> 'deleted' AND (v.state IS DISTINCT FROM 'live' OR i.status IN ('error', 'skipped'))
          AND (CAST(:admin AS boolean) OR i.external_id LIKE :own)
        ORDER BY i.name LIMIT 20000
        """), {"admin": bool(user.is_admin), "own": f"{user.id}/%"})).mappings().all()
    return [dict(r) for r in rows]


async def _being_read_again(conn: Any, user: Any, known: set[str]) -> list[dict[str, Any]]:
    """Searchable files that have a step queued or running (a reprocess): they stay searchable, but their progress is shown."""
    active = (
        (
            await conn.execute(
                text(
                    "SELECT payload FROM jobs WHERE status IN ('queued', 'running') "
                    "AND kind IN ('extract', 'enrich', 'chunk', 'embed', 'publish') "
                    "AND ((payload->>'force') IS NOT NULL OR (payload->>'ocr') IS NOT NULL) LIMIT 500"
                )
            )
        )
        .scalars()
        .all()
    )
    shas = [p["sha256"] for p in active if p.get("sha256")]
    item_ids = [p["item_id"] for p in active if p.get("item_id")]
    if not shas and not item_ids:
        return []
    rows = (
        (
            await conn.execute(
                text(
                    """
        SELECT i.id::text AS id, i.name, i.status, i.error, i.skip_reason, i.content_sha256, i.version_id::text AS version_id,
               v.state AS version_state, v.chunk_count, v.page_count, s.name AS source
        FROM source_items i JOIN sources s ON s.id = i.source_id LEFT JOIN versions v ON v.id = i.version_id
        WHERE i.status = 'active' AND (i.content_sha256 = ANY(:shas) OR i.id::text = ANY(:items))
          AND (CAST(:admin AS boolean) OR i.external_id LIKE :own)
        LIMIT 100
        """
                ),
                {"shas": shas, "items": item_ids, "admin": bool(user.is_admin), "own": f"{user.id}/%"},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows if r["id"] not in known]


async def _jobs_for(conn: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    rows = (
        (
            await conn.execute(
                text(
                    """
        SELECT id, kind, status, lane, attempts, max_attempts, started_at, created_at, run_after, last_error, payload
        FROM jobs
        WHERE status <> 'done' AND kind IN ('fetch', 'extract', 'enrich', 'chunk', 'embed', 'publish')
          AND (payload->>'item_id' = ANY(:items) OR payload->>'sha256' = ANY(:shas) OR payload->>'version_id' = ANY(:vids))
        """
                ),
                {
                    "items": [i["id"] for i in items],
                    "shas": [i["content_sha256"] for i in items if i["content_sha256"]],
                    "vids": [i["version_id"] for i in items if i["version_id"]],
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/processing")
async def processing_queue(user: UserDep) -> dict:
    """Which files are still being processed, and at which step. Failed ones are listed with the reason."""
    from datetime import UTC, datetime

    async with transaction() as conn:
        items = await _unfinished_items(conn, user)
        items += await _being_read_again(conn, user, {i["id"] for i in items})
        jobs = await _jobs_for(conn, items)
        ahead = (
            await conn.execute(text("SELECT count(*) FROM jobs WHERE status = 'queued' AND run_after <= now()"))
        ).scalar_one()
    now = datetime.now(UTC)
    by_item: dict[str, list[dict[str, Any]]] = {}
    by_sha: dict[str, list[dict[str, Any]]] = {}
    by_version: dict[str, list[dict[str, Any]]] = {}
    for j in jobs:  # one pass: thousands of files must not mean thousands of scans over the jobs
        for index, key in ((by_item, "item_id"), (by_sha, "sha256"), (by_version, "version_id")):
            if j["payload"].get(key):
                index.setdefault(j["payload"][key], []).append(j)
    out, running, waiting, failed = [], 0, 0, 0
    for item in items:
        found = {
            j["id"]: j
            for j in (
                by_item.get(item["id"], [])
                + (by_sha.get(item["content_sha256"], []) if item["content_sha256"] else [])
                + (by_version.get(item["version_id"], []) if item["version_id"] else [])
            )
        }
        mine = list(found.values())
        bad = [j for j in mine if j["status"] in ("failed", "dead")]
        active = sorted(
            (j for j in mine if j["status"] in ("queued", "running")),
            key=lambda j: STEP_ORDER.index(j["kind"]) if j["kind"] in STEP_ORDER else 99,
        )
        if item["status"] in ("error", "skipped") and not active:
            state, detail = "failed", item["skip_reason"] or item["error"] or "Could not be read"
        elif bad and not active:
            state, detail = "failed", (bad[0]["last_error"] or "A step failed")[:300]
        elif active:
            job = next((j for j in active if j["status"] == "running"), active[0])
            state = "running" if job["status"] == "running" else "waiting"
            detail = STEP_LABELS.get(job["kind"], job["kind"])
            if state == "waiting":
                detail = "Waiting: " + detail.lower()
        else:
            state, detail = "waiting", "Waiting to be picked up"
        started = next((j["started_at"] for j in active if j["status"] == "running" and j["started_at"]), None)
        running += state == "running"
        waiting += state == "waiting"
        failed += state == "failed"
        out.append(
            {
                "id": item["id"],
                "name": item["name"],
                "source": item["source"],
                "state": state,
                "step": detail,
                "seconds": round((now - started).total_seconds()) if started else None,
                "chunks": item["chunk_count"] or None,
                "pages": item["page_count"] or None,
                "retry_jobs": [j["id"] for j in bad],
            }
        )
    order = {"running": 0, "waiting": 1, "failed": 2}
    out.sort(key=lambda r: (order[r["state"]], r["name"].lower()))
    return {"items": out[:SHOWN], "more": max(0, len(out) - SHOWN), "running": running, "waiting": waiting, "failed": failed,
            "jobs_ahead": int(ahead)}


@router.get("/reprocess-options")
async def reprocess_options(_: UserDep, services: ServicesDep) -> dict:
    """What the Reprocess dialog offers: local OCR, and LLM OCR when a vision model is set."""
    await services.hub.refresh_overrides()
    hub = services.hub
    ocr, vision = hub.spec("OCR"), hub.spec("VISION")
    return {
        "local": {"label": "RapidOCR on this server"},
        "vision": {"available": not vision.is_off, "model": None if vision.is_off else vision.label},
        "current": "vision" if ocr.provider == "vision" else "local",
    }


class ReprocessBody(BaseModel):
    mode: str = Field(pattern="^(local|vision)$")


@router.post("/{document_id}/reprocess")
async def reprocess_document(
    document_id: UUID, body: ReprocessBody, request: Request, user: UserDep, principals: PrincipalsDep, services: ServicesDep
) -> dict:
    """Read the stored file again with the chosen OCR (local, or the vision model), then redo its chunks and embeddings."""
    import uuid as _uuid

    from rag.jobs import queue
    from rag.jobs.queue import NewJob

    v = await _version(document_id, principals, None)
    await services.hub.refresh_overrides()
    if body.mode == "vision":
        if services.hub.spec("VISION").is_off:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "LLM OCR needs a vision model: choose one on the Models page first")
        async with transaction() as conn:
            local_only = bool(
                (await conn.execute(text("SELECT local_only FROM documents WHERE id = :d"), {"d": document_id})).scalar()
            )
        if local_only:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This document is local-only: use local OCR")
    async with transaction() as conn:
        item = (
            await conn.execute(
                text(
                    "SELECT i.id, i.name, i.source_id FROM source_items i WHERE i.version_id = :v AND i.status = 'active' LIMIT 1"
                ),
                {"v": v["id"]},
            )
        ).first()
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "The file behind this document is gone")
        await queue.enqueue(
            conn,
            NewJob(
                "extract",
                {"sha256": v["blob_sha256"], "name": item.name, "force": True, "ocr": body.mode},
                lane="interactive",
                priority=10,
                source_id=item.source_id,
                dedupe_key=f"extract:{v['blob_sha256']}:reprocess:{_uuid.uuid4().hex[:8]}",
            ),
        )
    await audit("document_reprocess", user_id=user.id, target=str(document_id), ip=client_ip(request), details={"ocr": body.mode})
    return {"ok": True, "mode": body.mode}


class RetryBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/processing/retry")
async def processing_retry(body: RetryBody, user: UserDep) -> dict:
    """Queue failed steps again. Only steps that belong to files the caller may see in the list are retried."""
    from rag.jobs import queue

    async with transaction() as conn:
        items = await _unfinished_items(conn, user)
        allowed = {j["id"] for j in await _jobs_for(conn, items) if j["status"] in ("failed", "dead")}
    wanted = [i for i in body.ids if i in allowed]
    return {"retried": await queue.retry(wanted) if wanted else 0}


async def _visible_versions(document_id: UUID, principals: list[str]) -> list[dict[str, Any]]:
    sql = f"""
        SELECT v.id, v.title, v.language, v.state,
               v.page_count, v.chunk_count, v.blob_sha256, v.metadata, v.live_at, b.format, b.storage_key,
               b.extract_method, b.extract_warnings,
               (SELECT i.name FROM source_items i WHERE i.version_id = v.id AND i.status = 'active' LIMIT 1) AS file_name,
               (SELECT i.path FROM source_items i WHERE i.version_id = v.id AND i.status = 'active' LIMIT 1) AS path
        FROM versions v JOIN blobs b ON b.sha256 = v.blob_sha256
        WHERE v.document_id = :d AND v.state = 'live' AND {VISIBLE}
        ORDER BY v.created_at DESC"""  # noqa: S608 - fixed fragments, values bound
    async with transaction() as conn:
        return [dict(r) for r in (await conn.execute(text(sql), {"d": document_id, "p": principals})).mappings().all()]


async def _version(document_id: UUID, principals: list[str], version_id: UUID | None) -> dict[str, Any]:
    versions = await _visible_versions(document_id, principals)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if version_id is None:
        return versions[0]
    for v in versions:
        if v["id"] == version_id:
            return v
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")


@router.get("/{document_id}")
async def get_document(document_id: UUID, principals: PrincipalsDep) -> dict:
    versions = await _visible_versions(document_id, principals)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    async with transaction() as conn:
        doc = (await conn.execute(text("SELECT * FROM documents WHERE id = :d"), {"d": document_id})).mappings().first()
        refs = (
            (
                await conn.execute(
                    text(
                        "SELECT r.target, r.kind, r.page_no, r.context, r.target_document_id FROM doc_references r "
                        "WHERE r.from_version_id = :v ORDER BY r.page_no LIMIT 100"
                    ),
                    {"v": versions[0]["id"]},
                )
            )
            .mappings()
            .all()
        )
        key = versions[0]["metadata"].get("doc_number", {}).get("value") if versions[0]["metadata"] else None
        cited_by = []
        if doc["doc_number"]:
            from rag.metadata.identifiers import identifier_key

            cited_by = (
                (
                    await conn.execute(
                        text(f"""
                SELECT DISTINCT d.id, d.doc_number, d.title FROM doc_references r
                JOIN versions v ON v.id = r.from_version_id AND v.state = 'live' AND {VISIBLE}
                JOIN documents d ON d.id = v.document_id
                WHERE r.target = :t AND d.id <> :d LIMIT 50"""),  # noqa: S608 - fixed fragments, values bound
                        {"t": identifier_key(doc["doc_number"]), "d": document_id, "p": principals},
                    )
                )
                .mappings()
                .all()
            )
    return {
        "id": str(doc["id"]),
        "doc_number": doc["doc_number"],
        "title": doc["title"],
        "project_ids": doc["project_ids"],
        "doc_type": doc["doc_type"],
        "spec_section": doc["spec_section"],
        "family_id": str(doc["family_id"]) if doc["family_id"] else None,
        "local_only": doc["local_only"],
        "number_key": key,
        "versions": [
            {
                "id": str(v["id"]),
                "title": v["title"],
                "language": v["language"],
                "page_count": v["page_count"],
                "chunk_count": v["chunk_count"],
                "format": v["format"],
                "file_name": v["file_name"],
                "path": v["path"],
                "extract_method": v["extract_method"],
                "warnings": v["extract_warnings"][:20],
                "metadata": v["metadata"],
                "live_at": v["live_at"].isoformat() if v["live_at"] else None,
            }
            for v in versions
        ],
        "references": [
            {**dict(r), "target_document_id": str(r["target_document_id"]) if r["target_document_id"] else None} for r in refs
        ],
        "cited_by": [{"id": str(r["id"]), "doc_number": r["doc_number"], "title": r["title"]} for r in cited_by],
    }


@router.get("/{document_id}/privacy")
async def document_privacy(
    document_id: UUID, principals: PrincipalsDep, services: ServicesDep, version: UUID | None = None
) -> dict:
    """What the privacy shield found in this document and what a cloud model would receive in its place."""
    from rag.privacy.report import model_view

    s = services.settings
    v = await _version(document_id, principals, version)
    async with transaction() as conn:
        rows = (
            await conn.execute(
                text("SELECT page_start, header, text FROM chunks WHERE version_id = :v ORDER BY chunk_no"), {"v": v["id"]}
            )
        ).all()
        local_only = bool(
            (await conn.execute(text("SELECT local_only FROM documents WHERE id = :d"), {"d": document_id})).scalar()
        )
    report = model_view([(r[0], r[1] or "", r[2] or "") for r in rows], s.privacy_kind_list)
    return {
        **report,
        "shield": s.privacy_mask_for_models,
        "kinds": list(s.privacy_kind_list),
        "local_only": local_only,
        "auto_local_only": s.privacy_auto_local_only,
        "redacted_in_storage": s.privacy_redact_stored,
    }


READERS = {
    "pdf": "PDF reader (pdfium text layer, pdfplumber tables); scanned or image pages go to OCR",
    "docx": "Word reader (python-docx), local",
    "doc": "LibreOffice converts it to Word, then the Word reader, local",
    "odt": "LibreOffice converts it to Word, then the Word reader, local",
    "rtf": "LibreOffice converts it to Word, then the Word reader, local",
    "xlsx": "Excel reader (openpyxl, streamed in pages of 2,000 rows), local",
    "xlsm": "Excel reader (openpyxl, streamed in pages of 2,000 rows), local",
    "xls": "LibreOffice converts it to Excel, then the Excel reader, local",
    "ods": "LibreOffice converts it to Excel, then the Excel reader, local",
    "pptx": "PowerPoint reader (python-pptx), local",
    "ppt": "LibreOffice converts it to PowerPoint, then the slide reader, local",
    "odp": "LibreOffice converts it to PowerPoint, then the slide reader, local",
    "text": "Plain-text / CSV reader, local",
    "html": "HTML reader, local",
    "email": "E-mail reader (headers, body, attachments), local",
    "msg": "Outlook message reader, local",
    "image": "Image: OCR (and a cloud vision model if one is set for scanned pages)",
    "xer": "Primavera XER reader, local",
}
METHOD_LABELS = {
    "native": "Read directly from the file",
    "text_layer": "PDF text layer",
    "text_layer+image_ocr": "PDF text layer and OCR of the images on the page",
    "text_layer+vision": "PDF text layer and a vision model's description of the images",
    "ocr": "OCR of a scanned page or image",
}
LOCAL_PROVIDERS = frozenset({"local", "builtin", "extractive", "none"})


@router.get("/{document_id}/processing")
async def document_processing(
    document_id: UUID, principals: PrincipalsDep, services: ServicesDep, version: UUID | None = None
) -> dict:
    """How this file was read: the reader, OCR or vision per page, and every model call made for it (with cost)."""
    v = await _version(document_id, principals, version)
    sha = v["blob_sha256"]
    async with transaction() as conn:
        blob = (
            (
                await conn.execute(
                    text(
                        "SELECT format, size, extracted_at, extract_method, extract_warnings, extract_error "
                        "FROM blobs WHERE sha256 = :s"
                    ),
                    {"s": sha},
                )
            )
            .mappings()
            .first()
        )
        pages = (
            (
                await conn.execute(
                    text(
                        """
            SELECT p.page_no, p.method, p.label, p.is_drawing, p.quality, length(p.text) AS chars,
                   jsonb_array_length(p.tables) AS tables,
                   (SELECT string_agg(o.engine, ', ' ORDER BY o.engine) FROM ocr_cache o
                    WHERE o.image_hash = p.image_hash) AS engines
            FROM pages p WHERE p.blob_sha256 = :s ORDER BY p.page_no
            """
                    ),
                    {"s": sha},
                )
            )
            .mappings()
            .all()
        )
        items = [
            str(r[0])
            for r in (await conn.execute(text("SELECT id FROM source_items WHERE version_id = :v"), {"v": v["id"]})).all()
        ]
        calls = (
            (
                await conn.execute(
                    text(
                        """
            SELECT s.role, s.provider, s.model, count(*) AS calls, count(*) FILTER (WHERE s.status <> 'ok') AS errors,
                   coalesce(sum(s.tokens_in), 0) AS tokens_in, coalesce(sum(s.tokens_out), 0) AS tokens_out,
                   coalesce(sum(s.cost_usd), 0) AS cost, coalesce(sum((s.attrs->>'masked_values')::int), 0) AS masked
            FROM spans s
            JOIN traces t ON t.id = s.trace_id
            JOIN jobs j ON j.id = (t.attrs->>'job_id')::bigint
            WHERE s.role IN ('VISION', 'OCR', 'LLM_ENRICH', 'EMBEDDER', 'PARSER')
              AND (j.payload->>'sha256' = :s OR j.payload->>'version_id' = :v OR j.payload->>'item_id' = ANY(:items))
            GROUP BY 1, 2, 3 ORDER BY 1, 3
            """
                    ),
                    {"s": sha, "v": str(v["id"]), "items": items},
                )
            )
            .mappings()
            .all()
        )
    by_method: dict[str, int] = {}
    for p in pages:
        by_method[p["method"]] = by_method.get(p["method"], 0) + 1
    model_calls = [
        {
            "role": c["role"],
            "provider": c["provider"],
            "model": c["model"],
            "calls": c["calls"],
            "errors": c["errors"],
            "tokens_in": int(c["tokens_in"]),
            "tokens_out": int(c["tokens_out"]),
            "cost_usd": round(float(c["cost"]), 6),
            "masked_values": int(c["masked"]),
            "cloud": (c["provider"] or "") not in LOCAL_PROVIDERS,
        }
        for c in calls
    ]
    # OCR set to "vision": every image is read by the local OCR first (to check it for bank or id numbers), and the vision model
    # reads those that pass. Say so, so the two rows are not mistaken for two separate jobs.
    vision_ran = any(c["role"] == "VISION" for c in model_calls)
    for c in model_calls:
        if c["role"] == "OCR" and not c["cloud"] and vision_ran:
            c["note"] = "Checks each image locally first; images without bank or id numbers are then read by the vision model."
    recorded = next((c for c in model_calls if c["role"] == "EMBEDDER"), None)
    embedder = (
        f"{recorded['provider']}/{recorded['model']}" if recorded else f"{services.hub.spec('EMBEDDER').label} (current setting)"
    )
    fmt = blob["format"] or v["format"] or ""
    return {
        "format": fmt,
        "size": blob["size"],
        "reader": READERS.get(fmt, "Built-in reader, local"),
        "read_at": blob["extracted_at"].isoformat() if blob["extracted_at"] else None,
        "error": blob["extract_error"],
        "warnings": list(blob["extract_warnings"] or [])[:30],
        "methods": [{"method": m, "label": METHOD_LABELS.get(m, m), "pages": n} for m, n in by_method.items()],
        "pages": [
            {
                "page": p["page_no"],
                "method": p["method"],
                "label": p["label"],
                "drawing": p["is_drawing"],
                "chars": p["chars"],
                "tables": p["tables"],
                "engines": p["engines"],
                "ocr_confidence": (p["quality"] or {}).get("ocr_confidence"),
            }
            for p in pages[:300]
        ],
        "page_count": len(pages),
        "chunks": v["chunk_count"],
        "embedder": embedder,
        "calls": model_calls,
        "cloud_used": any(c["cloud"] for c in model_calls),
        "cost_usd": round(sum(c["cost_usd"] for c in model_calls), 6),
    }


@router.get("/{document_id}/pages/{page_no}")
async def get_page(document_id: UUID, page_no: int, principals: PrincipalsDep, version: UUID | None = None) -> dict:
    v = await _version(document_id, principals, version)
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT page_no, text, method, language, label, width, height, is_drawing, title_block, tables, lines "
                        "FROM pages WHERE blob_sha256 = :sha AND page_no = :n"
                    ),
                    {"sha": v["blob_sha256"], "n": page_no},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return {
        **dict(row),
        "version_id": str(v["id"]),
        "page_count": v["page_count"],
        "format": v["format"],
        "has_image": v["format"] in IMAGE_FORMATS,
    }


def _render(data: bytes, page_no: int, scale: float, fmt: str) -> bytes:
    from rag.providers.parsers.pdf import render_page_png

    if fmt == "pdf":
        return render_page_png(data, page_no, dpi=int(72 * scale), max_pixels=8_000_000)
    from PIL import Image, ImageSequence

    image = Image.open(io.BytesIO(data))
    frames = list(ImageSequence.Iterator(image))
    if page_no > len(frames):
        raise ValueError("page out of range")
    frame = frames[page_no - 1].convert("RGB")
    frame.thumbnail((2400, 2400))
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")
    return buffer.getvalue()


@router.get("/{document_id}/pages/{page_no}/image")
async def page_image(
    document_id: UUID,
    page_no: int,
    principals: PrincipalsDep,
    services: ServicesDep,
    version: UUID | None = None,
    scale: float = 1.5,
) -> Response:
    v = await _version(document_id, principals, version)
    fmt = v["format"] or ""
    if fmt not in IMAGE_FORMATS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This format has no page images; use the text view")
    scale = max(0.5, min(scale, 3.0))
    cache = Path(services.settings.cache_dir) / "pages" / v["blob_sha256"][:2] / f"{v['blob_sha256']}-{page_no}-{scale:.2f}.png"
    if cache.is_file():
        png = cache.read_bytes()
    elif fmt in CAD_FORMATS:
        # drawn while the file was read (see cad.prime_previews); another zoom level is drawn from the stored file once
        from rag.ingestion import cad

        data = await services.objects.get(v["storage_key"])
        try:
            png = await asyncio.to_thread(cad.render_page, data, fmt, page_no, scale, colour=services.settings.cad_colour)
        except (ValueError, PermanentError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"This sheet cannot be shown: {exc}") from exc
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(png)
    elif fmt in OFFICE_PREVIEW_FORMATS:
        async with transaction() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT text, tables, label FROM pages WHERE blob_sha256 = :sha AND page_no = :n"),
                        {"sha": v["blob_sha256"], "n": page_no},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
        try:
            png = await run_pdfium(
                render_office_page_png,
                fmt=fmt,
                text=row["text"] or "",
                tables=row["tables"] or [],
                label=row["label"],
                scale=scale,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found") from exc
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(png)
    else:
        data = await services.objects.get(v["storage_key"])
        try:
            png = await run_pdfium(_render, data, page_no, scale, fmt)
        except (ValueError, IndexError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found") from exc
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(png)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


def _pdf_boxes(data: bytes, page_no: int, quote: str) -> list[list[float]]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[page_no - 1]
        width, height = page.get_size()
        textpage = page.get_textpage()
        words = re.sub(r"\s+", " ", quote).strip()
        boxes: list[list[float]] = []
        for size in (len(words), 120, 60, 30):
            probe = words[:size].strip()
            if len(probe) < 8:
                break
            searcher = textpage.search(probe, match_case=False)
            found = searcher.get_next()
            if found:
                index, count = found
                for i in range(textpage.count_rects(index, count)):
                    left, bottom, right, top = textpage.get_rect(i)
                    boxes.append([left / width, 1 - top / height, right / width, 1 - bottom / height])
                break
        return boxes
    finally:
        pdf.close()


@router.get("/{document_id}/pages/{page_no}/highlights")
async def highlights(
    document_id: UUID,
    page_no: int,
    principals: PrincipalsDep,
    services: ServicesDep,
    q: str = Query(min_length=3, max_length=1000),
    version: UUID | None = None,
) -> dict:
    """Boxes (x0, y0, x1, y1 as fractions of the page, origin top-left) around the quoted text on the page."""
    v = await _version(document_id, principals, version)
    async with transaction() as conn:
        page = (
            (
                await conn.execute(
                    text("SELECT method, lines FROM pages WHERE blob_sha256 = :sha AND page_no = :n"),
                    {"sha": v["blob_sha256"], "n": page_no},
                )
            )
            .mappings()
            .first()
        )
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    boxes: list[list[float]] = []
    if v["format"] == "pdf" and page["method"].startswith("text_layer"):
        data = await services.objects.get(v["storage_key"])
        boxes = await run_pdfium(_pdf_boxes, data, page_no, q)
    if not boxes and page["lines"]:
        for line in page["lines"]:
            if line.get("box") and fuzz.partial_ratio(line["text"], q) >= 80 and len(line["text"]) > 3:
                boxes.append(line["box"])
    return {"boxes": boxes[:200]}


# Formats a browser can show itself. Everything else is offered as a download, because a browser cannot display it.
_INLINE_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "txt": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
}


@router.get("/{document_id}/file")
async def original_file(
    document_id: UUID,
    request: Request,
    user: UserDep,
    principals: PrincipalsDep,
    services: ServicesDep,
    version: UUID | None = None,
) -> Response:
    """The original document exactly as it was uploaded, for the browser to open (a PDF opens at a page with `#page=N`).
    Access is checked like every other read; the file is served as itself and never as HTML."""
    v = await _version(document_id, principals, version)
    data = await services.objects.get(v["storage_key"])
    name = (v["file_name"] or f"document.{v['format'] or 'bin'}").replace('"', "")
    extension = (v["format"] or name.rsplit(".", 1)[-1]).lower()
    media_type = _INLINE_TYPES.get(extension)
    await audit(
        "open_original", user_id=user.id, target=str(document_id), ip=client_ip(request), details={"version_id": str(v["id"])}
    )
    from urllib.parse import quote

    disposition = "inline" if media_type else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    if extension == "pdf":
        # the browser's own PDF viewer is an embedded object; the API-wide `default-src 'none'` would blank it
        headers["Content-Security-Policy"] = (
            "default-src 'none'; object-src 'self'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
        )
    return Response(data, media_type=media_type or "application/octet-stream", headers=headers)


@router.get("/{document_id}/download")
async def download(
    document_id: UUID,
    request: Request,
    user: UserDep,
    principals: PrincipalsDep,
    services: ServicesDep,
    version: UUID | None = None,
) -> Response:
    v = await _version(document_id, principals, version)
    data = await services.objects.get(v["storage_key"])
    name = (v["file_name"] or f"document.{v['format'] or 'bin'}").replace('"', "")
    await audit("download", user_id=user.id, target=str(document_id), ip=client_ip(request), details={"version_id": str(v["id"])})
    from urllib.parse import quote

    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{document_id}")
async def delete_document(
    document_id: UUID, request: Request, user: UserDep, principals: PrincipalsDep, services: ServicesDep
) -> dict:
    """Remove a document the caller can see: source items, versions, chunks, vectors, pages, and stored files
    that nothing else still uses (same content hash elsewhere is kept)."""
    if not await _visible_versions(document_id, principals):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    result = await _remove_document(document_id, services)
    await get_cache(services.settings).bump_epoch()
    await audit("document_delete", user_id=user.id, target=str(document_id), ip=client_ip(request), details=result)
    return {"ok": True, **result}


class BulkDelete(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)


@router.post("/bulk-delete")
async def bulk_delete(
    body: BulkDelete, request: Request, user: UserDep, principals: PrincipalsDep, services: ServicesDep
) -> dict:
    """Remove several documents at once; those the caller cannot see are skipped and reported."""
    removed, skipped = 0, []
    for document_id in dict.fromkeys(body.ids):
        if not await _visible_versions(document_id, principals):
            skipped.append(str(document_id))
            continue
        result = await _remove_document(document_id, services)
        await audit("document_delete", user_id=user.id, target=str(document_id), ip=client_ip(request), details=result)
        removed += 1
    await get_cache(services.settings).bump_epoch()
    return {"ok": True, "removed": removed, "skipped": skipped}


class DocumentUpdate(BaseModel):
    project: str = Field(default="", max_length=80)


@router.patch("/{document_id}")
async def update_document(
    document_id: UUID, body: DocumentUpdate, request: Request, user: UserDep, principals: PrincipalsDep, services: ServicesDep
) -> dict:
    """Set or clear the project of a document the caller can see (for a file uploaded without one). The change reaches the
    search filters at once and is kept if the file is read again."""
    if not await _visible_versions(document_id, principals):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    name = " ".join(body.project.split())
    projects = [name] if name else []
    async with transaction() as conn:
        await conn.execute(
            text("UPDATE documents SET project_ids = CAST(:p AS text[]), updated_at = now() WHERE id = :d"),
            {"p": projects, "d": document_id},
        )
        await conn.execute(
            text(
                "UPDATE source_items SET fields = CASE WHEN :n = '' THEN coalesce(fields, '{}'::jsonb) - 'project' "
                "ELSE coalesce(fields, '{}'::jsonb) || jsonb_build_object('project', CAST(:n AS text)) END "
                "WHERE status <> 'deleted' AND version_id IN (SELECT id FROM versions WHERE document_id = :d)"
            ),
            {"n": name, "d": document_id},
        )
    for build in await services.builds(refresh=True):
        await services.vectors.set_payload(
            build.collection, {"project_ids": projects}, models.Filter(must=[match_value("document_id", str(document_id))])
        )
    await get_cache(services.settings).bump_epoch()
    await audit("document_update", user_id=user.id, target=str(document_id), ip=client_ip(request), details={"project": name})
    return {"ok": True, "project_ids": projects}


async def _remove_document(document_id: UUID, services: Any) -> dict[str, int]:
    """Delete one document completely: items, versions, chunks, vectors, pages, and files nothing else uses."""
    async with transaction() as conn:
        versions = (
            await conn.execute(text("SELECT id, blob_sha256 FROM versions WHERE document_id = :d"), {"d": document_id})
        ).all()
        if not versions:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
        version_ids = [str(v.id) for v in versions]
        shas = {v.blob_sha256 for v in versions}

        items = (
            await conn.execute(
                text(
                    "UPDATE source_items SET status = 'deleted', version_id = NULL "
                    "WHERE version_id = ANY(CAST(:v AS uuid[])) AND status <> 'deleted' RETURNING id, content_sha256, etag"
                ),
                {"v": version_ids},
            )
        ).all()
        item_ids = [str(i.id) for i in items]
        for row in items:
            if row.content_sha256:
                shas.add(row.content_sha256)
            if row.etag:
                shas.add(row.etag)

        if item_ids:
            children = (
                await conn.execute(
                    text(
                        "UPDATE source_items SET status = 'deleted', version_id = NULL "
                        "WHERE parent_item_id = ANY(CAST(:ids AS uuid[])) AND status <> 'deleted' "
                        "RETURNING content_sha256, etag"
                    ),
                    {"ids": item_ids},
                )
            ).all()
            for row in children:
                if row.content_sha256:
                    shas.add(row.content_sha256)
                if row.etag:
                    shas.add(row.etag)

        sha_list = sorted(shas)
        shared: set[str] = set()
        if sha_list:
            # Any other version row (even soft-deleted) still FKs the blob; keep those files.
            shared = {
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            """
                SELECT DISTINCT blob_sha256 FROM versions
                WHERE blob_sha256 = ANY(:h) AND document_id <> :d
                UNION
                SELECT DISTINCT content_sha256 FROM source_items
                WHERE content_sha256 = ANY(:h) AND status <> 'deleted'
                UNION
                SELECT DISTINCT etag FROM source_items
                WHERE etag = ANY(:h) AND status <> 'deleted'
                """
                        ),
                        {"h": sha_list, "d": document_id},
                    )
                ).all()
            }
        purge = sorted(set(sha_list) - shared)

    for build in await services.builds(refresh=True):
        for version_id in version_ids:
            await services.vectors.delete(build.collection, version_filter(version_id))

    async with transaction() as conn:
        await conn.execute(text("DELETE FROM chunks WHERE version_id = ANY(CAST(:v AS uuid[]))"), {"v": version_ids})
        await conn.execute(text("DELETE FROM version_access WHERE version_id = ANY(CAST(:v AS uuid[]))"), {"v": version_ids})
        await conn.execute(text("DELETE FROM doc_references WHERE from_version_id = ANY(CAST(:v AS uuid[]))"), {"v": version_ids})
        await conn.execute(text("DELETE FROM versions WHERE document_id = :d"), {"d": document_id})
        await conn.execute(text("DELETE FROM documents WHERE id = :d"), {"d": document_id})
        if purge:
            await conn.execute(text("DELETE FROM pages WHERE blob_sha256 = ANY(:h)"), {"h": purge})
            await conn.execute(text("DELETE FROM blobs WHERE sha256 = ANY(:h)"), {"h": purge})

    cache_root = Path(services.settings.cache_dir) / "pages"
    for sha in purge:
        await services.objects.delete(blob_key(sha))
        page_cache = cache_root / sha[:2]
        if page_cache.is_dir():
            for cached in page_cache.glob(f"{sha}-*"):
                cached.unlink(missing_ok=True)

    return {"versions_removed": len(version_ids), "items_removed": len(item_ids), "files_removed": len(purge)}
