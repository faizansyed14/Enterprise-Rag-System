"""Ingestion as small, idempotent, checkpointed jobs:

  sync_source -> fetch (per item) -> extract (per unique file content) -> enrich (per item) -> chunk -> embed -> publish
  control lane: delete_item, update_acl, refresh_acl, release_version   (access changes apply within seconds)

Every step can be re-run safely: blobs are content-addressed, pages are keyed by blob, chunk ids are derived from
(version, chunk number), points are upserted by chunk id, and a version only becomes visible in `publish`, in one
Postgres transaction. A failure anywhere leaves the previous live version searchable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from qdrant_client import models
from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from rag.cache import get_cache
from rag.connectors.base import RemoteItem, acl_hash, make_connector
from rag.db import schema
from rag.db.engine import transaction
from rag.errors import NotReady, PermanentError
from rag.ingestion import protection
from rag.ingestion.chunker import HeaderFields, PageForChunking
from rag.ingestion.formats import detect_format, parse_document, skip_reason
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.jobs.worker import JobContext, handler
from rag.metadata.extract import PageInput, fill_gaps_with_llm
from rag.metadata.identifiers import identifier_key
from rag.metadata.references import find_references
from rag.providers.storage.objects import blob_key
from rag.providers.vectorstore.qdrant import DENSE, SPARSE, match_value, version_filter
from rag.text.minhash import NEAR_DUPLICATE_JACCARD, band_keys, signature, similarity

log = logging.getLogger(__name__)
S = schema
SI = schema.source_items.c
V = schema.versions.c
D = schema.documents.c


# A document number from a source or a title block (confidence at or above this) identifies the document by itself; one
# read from a file name or the opening text needs corroboration from the text (similarity at or above IDENTITY_MIN_SIMILARITY).
STRONG_IDENTITY = 0.88
IDENTITY_MIN_SIMILARITY = 0.2


def _slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


# Later stages run first: a document that is nearly done is finished before another one is started, so work in progress
# stays small and documents become searchable as soon as they can instead of all at the end of the backlog.
def holds_sensitive_values(pages: list[Any], kinds: tuple[str, ...]) -> bool:
    """Do the stored pages (text and table cells) contain a bank account, card, IBAN or id number?"""
    from rag.privacy.shield import has_sensitive

    for page in pages:
        if has_sensitive(page.text or "", kinds):
            return True
        for table in page.tables or []:
            for row in table.get("rows", []):
                if has_sensitive(" | ".join(str(c) for c in row), kinds) or any(has_sensitive(str(c), kinds) for c in row):
                    return True
    return False


def redact_pages(pages: list[Any], kinds: tuple[str, ...]) -> int:
    """Remove bank and id numbers from parsed pages (text, table cells, OCR lines, title block). Returns how many values went."""
    from rag.privacy.shield import find, redact

    removed = 0
    for page in pages:
        removed += len(find(page.text, kinds))
        page.text = redact(page.text, kinds)
        for table in page.tables:
            for row in table.rows:
                for i, cell in enumerate(row):
                    if cell:
                        removed += len(find(cell, kinds))
                        row[i] = redact(cell, kinds)
        for line in page.lines:
            removed += len(find(line.text, kinds))
            line.text = redact(line.text, kinds)
        page.title_block = {k: redact(v, kinds) for k, v in page.title_block.items()}
    return removed


STAGE_PRIORITY = {"fetch": 0, "extract": 5, "enrich": 10, "chunk": 20, "embed": 30, "publish": 40}


async def _enqueue(conn: AsyncConnection, ctx: JobContext, kind: str, payload: dict[str, Any], *,
                   lane: str | None = None, dedupe: str | None = None, **extra: Any) -> None:
    priority = ctx.job.priority - STAGE_PRIORITY.get(ctx.job.kind, 0) + STAGE_PRIORITY.get(kind, 0)
    await queue.enqueue(conn, NewJob(kind, payload, lane=lane or ctx.job.lane, dedupe_key=dedupe,
                                     trace_id=ctx.job.trace_id, priority=priority, **extra),
                        default_max_attempts=ctx.settings.job_max_attempts)


# ======================================================================================================================
# sync
# ======================================================================================================================


@handler("sync_source")
async def sync_source(ctx: JobContext) -> None:
    services = ctx.services
    source_id = ctx.payload["source_id"]
    full = bool(ctx.payload.get("full"))
    async with transaction() as conn:
        src = (await conn.execute(select(S.sources).where(S.sources.c.id == source_id))).first()
        if src is None or not src.enabled:
            return
        await conn.execute(update(S.sources).where(S.sources.c.id == source_id)
                           .values(last_sync_started_at=datetime.now(UTC), last_sync_status="running",
                                   last_sync_error=None))
    connector = make_connector(src.kind, dict(src.config), services.settings)
    started = datetime.now(UTC)
    stats = Counter()
    cursor = {} if full else dict(src.cursor or {})
    complete = False
    try:
        async for change in connector.changes(cursor):
            complete = complete or change.complete_listing
            queued, deleted, acl = await _apply_changes(ctx, src, change.items, started, change.complete_listing)
            stats.update({"seen": len(change.items), "queued": queued, "deleted": deleted, "acl_checks": acl})
            cursor = change.cursor
            await ctx.checkpoint(seen=stats["seen"])
        if complete:
            stats["deleted"] += await _delete_unseen(ctx, source_id, started)
        async with transaction() as conn:
            totals = (await conn.execute(
                select(func.count(), func.coalesce(func.sum(SI.size), 0))
                .where(SI.source_id == source_id, SI.status != "deleted"))).first()
            await conn.execute(update(S.sources).where(S.sources.c.id == source_id).values(
                cursor=cursor, last_sync_finished_at=datetime.now(UTC), last_sync_status="ok",
                stats={**dict(stats), "items": int(totals[0]), "bytes": int(totals[1])}))
    except Exception as exc:
        async with transaction() as conn:
            await conn.execute(update(S.sources).where(S.sources.c.id == source_id).values(
                last_sync_status="error", last_sync_error=f"{type(exc).__name__}: {exc}"[:1000],
                last_sync_finished_at=datetime.now(UTC)))
        raise
    finally:
        await connector.aclose()


async def _apply_changes(ctx: JobContext, src: Any, items: list[RemoteItem], started: datetime,
                         complete_listing: bool) -> tuple[int, int, int]:
    if not items:
        return 0, 0, 0
    now = datetime.now(UTC)
    async with transaction() as conn:
        existing = {r.external_id: r for r in (await conn.execute(
            select(SI.id, SI.external_id, SI.etag, SI.status, SI.content_sha256)
            .where(SI.source_id == src.id, SI.external_id.in_([i.external_id for i in items])))).all()}
        deletes: list[Any] = []
        rows: list[dict[str, Any]] = []
        for item in items:
            old = existing.get(item.external_id)
            if item.deleted:
                if old is not None and old.status != "deleted":
                    deletes.append(old.id)
                continue
            rows.append({"source_id": src.id, "external_id": item.external_id, "path": item.path, "name": item.name,
                         "etag": item.etag, "size": item.size, "modified_at": item.modified_at, "mime": item.mime,
                         "fields": item.fields or {}, "last_seen_at": now})
        ids: dict[str, Any] = {}
        if rows:
            stmt = insert(S.source_items).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=[SI.source_id, SI.external_id],
                set_={
                    "path": stmt.excluded.path, "name": stmt.excluded.name, "etag": stmt.excluded.etag,
                    "size": stmt.excluded.size, "modified_at": stmt.excluded.modified_at, "mime": stmt.excluded.mime,
                    "fields": stmt.excluded.fields, "last_seen_at": stmt.excluded.last_seen_at,
                    "status": text("CASE WHEN source_items.etag IS DISTINCT FROM excluded.etag "
                                   "OR source_items.status = 'deleted' THEN 'active' ELSE source_items.status END"),
                },
            ).returning(SI.id, SI.external_id)
            ids = {r.external_id: r.id for r in (await conn.execute(stmt)).all()}
        jobs: list[NewJob] = []
        acl_checks = 0
        for item in items:
            if item.deleted or item.external_id not in ids:
                continue
            item_id = ids[item.external_id]
            old = existing.get(item.external_id)
            changed = old is None or old.etag != item.etag or old.status in {"deleted", "error"} \
                or old.content_sha256 is None
            if changed:
                jobs.append(NewJob("fetch", {"item_id": str(item_id)}, lane=ctx.job.lane,
                                   dedupe_key=f"fetch:{item_id}:{item.etag}", source_id=src.id,
                                   trace_id=ctx.job.trace_id))
            elif not complete_listing:
                # Listed by a delta query with unchanged content: its permissions may have changed.
                jobs.append(NewJob("refresh_acl", {"item_id": str(item_id)}, lane="control",
                                   dedupe_key=f"refresh_acl:{item_id}", source_id=src.id))
                acl_checks += 1
        jobs.extend(NewJob("delete_item", {"item_id": str(item_id)}, lane="control",
                           dedupe_key=f"delete_item:{item_id}", source_id=src.id) for item_id in deletes)
        await queue.enqueue_many(conn, jobs, default_max_attempts=ctx.settings.job_max_attempts)
    return len(jobs) - len(deletes) - acl_checks, len(deletes), acl_checks


async def _delete_unseen(ctx: JobContext, source_id: Any, started: datetime) -> int:
    async with transaction() as conn:
        rows = (await conn.execute(
            update(S.source_items)
            .where(SI.source_id == source_id, SI.status != "deleted", SI.last_seen_at < started,
                   SI.parent_item_id.is_(None))
            .values(status="deleted").returning(SI.id))).all()
        await queue.enqueue_many(conn, [NewJob("delete_item", {"item_id": str(r.id)}, lane="control",
                                               dedupe_key=f"delete_item:{r.id}", source_id=source_id) for r in rows])
    return len(rows)


# ======================================================================================================================
# fetch and extract
# ======================================================================================================================


def _remote(item: Any) -> RemoteItem:
    return RemoteItem(external_id=item.external_id, path=item.path, name=item.name, etag=item.etag, size=item.size,
                      modified_at=item.modified_at, mime=item.mime)


@handler("fetch")
async def fetch(ctx: JobContext) -> None:
    services = ctx.services
    item_id = ctx.payload["item_id"]
    async with transaction() as conn:
        row = (await conn.execute(
            select(S.source_items, S.sources.c.kind, S.sources.c.config, S.sources.c.default_acl)
            .join(S.sources, S.sources.c.id == SI.source_id).where(SI.id == item_id))).first()
    if row is None or row.status == "deleted":
        return
    item = row
    reason = skip_reason(item.name, services.settings.skip_extension_set) if row.kind != "upload" else None
    if reason:
        raise PermanentError(reason)  # a connector's file of a type in SKIP_EXTENSIONS is skipped, with this reason shown
    acl: list[str] | None = None
    if row.kind == "upload" or item.parent_item_id is not None:
        sha = item.etag or ""
        data = await services.objects.get(blob_key(sha))
        if row.kind == "upload":
            acl = list(item.acl or row.default_acl or [])
        else:
            async with transaction() as conn:
                parent = (await conn.execute(select(SI.acl).where(SI.id == item.parent_item_id))).first()
            acl = list(parent.acl) if parent else list(row.default_acl or [])
    else:
        connector = make_connector(row.kind, dict(row.config), services.settings)
        try:
            data = await connector.download(_remote(item))
            acl = await connector.permissions(_remote(item))
        finally:
            await connector.aclose()
        if acl is None:
            acl = list(row.default_acl or [])
    if len(data) > services.settings.max_file_mb * 1024 * 1024:
        raise PermanentError(f"file is larger than MAX_FILE_MB={services.settings.max_file_mb}")
    sha = hashlib.sha256(data).hexdigest()
    locked = protection.detect(data, item.name)
    if locked:
        raise PermanentError(locked)  # shown as "Protected: ..." on the document queue instead of a failed read later
    fmt = detect_format(data, item.name)  # raises PermanentError for unsupported types
    key = blob_key(sha)
    if not await services.objects.exists(key):
        await services.objects.put(key, data)
    async with transaction() as conn:
        await conn.execute(insert(S.blobs).values(sha256=sha, size=len(data), mime=item.mime or "application/octet-stream",
                                                   format=fmt, storage_key=key).on_conflict_do_nothing())
        blob = (await conn.execute(select(S.blobs.c.extracted_at, S.blobs.c.extract_error)
                                   .where(S.blobs.c.sha256 == sha))).first()
        await conn.execute(update(S.source_items).where(SI.id == item_id).values(
            content_sha256=sha, fetched_at=datetime.now(UTC), acl=acl, acl_hash=acl_hash(acl), error=None,
            skip_reason=None, status="active"))
        if blob.extracted_at is None and blob.extract_error is None:
            await _enqueue(conn, ctx, "extract", {"sha256": sha, "name": item.name}, dedupe=f"extract:{sha}")
        else:
            # Already read (same bytes seen before): go straight on. Otherwise `extract` starts the enrichment of every
            # item that waits for this file when it finishes, so nothing polls for it in the meantime.
            await _enqueue(conn, ctx, "enrich", {"item_id": str(item_id)}, dedupe=f"enrich:{item_id}:{sha}",
                           source_id=item.source_id)


@handler("extract")
async def extract(ctx: JobContext) -> None:
    services = ctx.services
    sha = ctx.payload["sha256"]
    async with transaction() as conn:
        blob = (await conn.execute(select(S.blobs).where(S.blobs.c.sha256 == sha))).first()
    force = bool(ctx.payload.get("force"))  # `rag reparse`: read the stored file again with the current parsers
    if blob is None or (blob.extracted_at is not None and not force):
        return
    data = await services.objects.get(blob.storage_key)
    await services.hub.refresh_overrides()  # a vision model chosen on the Models page applies to the next file
    pdf_parser, ocr = services.hub.reading(ctx.payload.get("ocr"))  # "local" / "vision" when a person asked for one file
    parsed = await parse_document(data, ctx.payload.get("name") or f"file.{blob.format or 'bin'}", pdf_parser=pdf_parser, ocr=ocr)
    if services.settings.privacy_redact_stored:
        redact_pages(parsed.pages, services.settings.privacy_kind_list)  # the values are never stored, searched or shown
    if parsed.previews:  # CAD sheets drawn while reading become the viewer's page images at once
        from rag.ingestion.cad import prime_previews

        try:
            await asyncio.to_thread(prime_previews, services.settings.cache_dir, sha, parsed.previews)
        except OSError:
            log.warning("could not store the drawing previews of %s", sha, exc_info=True)
    attachments: list[dict[str, Any]] = []
    for name, content in parsed.attachments:
        try:
            fmt = detect_format(content, name)
        except PermanentError:
            continue
        a_sha = hashlib.sha256(content).hexdigest()
        a_key = blob_key(a_sha)
        if not await services.objects.exists(a_key):
            await services.objects.put(a_key, content)
        async with transaction() as conn:
            await conn.execute(insert(S.blobs).values(sha256=a_sha, size=len(content), format=fmt,
                                                       storage_key=a_key).on_conflict_do_nothing())
        attachments.append({"name": name, "sha256": a_sha, "size": len(content)})
    methods = Counter(p.method for p in parsed.pages)
    rows = [{
        "blob_sha256": sha, "page_no": p.number, "text": p.text, "method": p.method, "language": p.language,
        "label": p.label, "width": p.width, "height": p.height, "is_drawing": p.is_drawing,
        "title_block": p.title_block, "tables": [{"rows": t.rows, "header_rows": t.header_rows} for t in p.tables],
        "lines": [{"text": ln.text, "box": list(ln.box) if ln.box else None, "confidence": ln.confidence}
                  for ln in p.lines][:2000],
        "quality": p.quality, "image_hash": p.image_hash,
    } for p in parsed.pages]
    kept = {"title", "subject", "author", "header", "title_guess", "outline_source"}
    properties = {k: v for k, v in (parsed.properties or {}).items() if k in kept}
    if parsed.title:
        properties.setdefault("title", parsed.title)
    async with transaction() as conn:
        await conn.execute(delete(S.pages).where(S.pages.c.blob_sha256 == sha))
        for i in range(0, len(rows), 200):
            await conn.execute(insert(S.pages), rows[i : i + 200])
        await conn.execute(
            update(S.blobs)
            .where(S.blobs.c.sha256 == sha)
            .values(
                extracted_at=datetime.now(UTC),
                page_count=len(rows),
                format=parsed.format,
                extract_method=",".join(f"{k}:{v}" for k, v in methods.most_common()),
                extract_warnings=parsed.warnings[:200],
                extract_error=None,
                properties=properties,
                attachments=attachments,
            )
        )
        waiting = (await conn.execute(select(SI.id, SI.source_id).where(SI.content_sha256 == sha, SI.status != "deleted"))).all()
        for row in waiting:
            # a forced read must also chunk and embed again, or the new text never reaches the index
            await _enqueue(
                conn,
                ctx,
                "enrich",
                {"item_id": str(row.id), **({"force": True} if force else {})},
                dedupe=f"enrich:{row.id}:{sha}" + (":force" if force else ""),
                source_id=row.source_id,
            )


# ======================================================================================================================
# enrich: metadata, document identity, versions, access, families, references
# ======================================================================================================================


async def _pages(conn: AsyncConnection, sha: str) -> list[Any]:
    return (await conn.execute(select(S.pages).where(S.pages.c.blob_sha256 == sha).order_by(S.pages.c.page_no))).all()


async def refresh_version_access(conn: AsyncConnection, version_id: Any) -> list[str]:
    """version_access = union of the ACLs of the active items whose current content is this version."""
    rows = (await conn.execute(select(SI.acl).where(SI.version_id == version_id, SI.status == "active"))).all()
    principals = sorted({p for r in rows for p in (r.acl or [])})
    await conn.execute(delete(S.version_access).where(S.version_access.c.version_id == version_id))
    if principals:
        await conn.execute(insert(S.version_access), [{"version_id": version_id, "principal": p} for p in principals])
    return principals


async def settle_document(conn: AsyncConnection, document_id: Any) -> None:
    """Point the document at its newest live version, or mark it deleted when none is live.

    The document row is locked first, so two versions published at the same moment cannot disagree."""
    await conn.execute(select(D.id).where(D.id == document_id).with_for_update())
    live = (
        await conn.execute(select(V.id).where(V.document_id == document_id, V.state == "live").order_by(V.created_at.desc()))
    ).all()
    if not live:
        await conn.execute(
            update(S.documents)
            .where(D.id == document_id)
            .values(current_version_id=None, status="deleted", updated_at=datetime.now(UTC))
        )
        return
    await conn.execute(
        update(S.documents)
        .where(D.id == document_id)
        .values(current_version_id=live[0].id, status="active", updated_at=datetime.now(UTC))
    )


async def assign_family(conn: AsyncConnection, document_id: Any) -> dict[str, str]:
    """Merge the document's family with the families of its near-duplicates (MinHash LSH candidates, then the
    estimated Jaccard similarity). The merged family id is the smallest id involved, so the result does not depend on
    which document was published first. Returns {document_id: family_id} for every document whose family changed."""
    own = (
        await conn.execute(
            select(V.minhash, V.minhash_bands)
            .where(V.document_id == document_id, V.state != "deleted", V.minhash.is_not(None))
            .order_by(V.updated_at.desc())
            .limit(1)
        )
    ).first()
    current = (await conn.execute(select(D.family_id).where(D.id == document_id))).scalar_one()
    members: set[Any] = {document_id}
    families: set[Any] = {current} if current else set()
    if own is not None:
        candidates = (
            await conn.execute(
                select(V.document_id, V.minhash)
                .where(V.document_id != document_id, V.state != "deleted", V.minhash_bands.overlap(own.minhash_bands))
                .limit(200)
            )
        ).all()
        near = {c.document_id for c in candidates if similarity(c.minhash, own.minhash) >= NEAR_DUPLICATE_JACCARD}
        if near:
            rows = (await conn.execute(select(D.id, D.family_id).where(D.id.in_(near)))).all()
            members |= {r.id for r in rows}
            families |= {r.family_id for r in rows if r.family_id}
    target = min(members | families, key=str)
    changed = (
        await conn.execute(
            update(S.documents)
            .where((D.id.in_(members) | D.family_id.in_(families or {target})) & (D.family_id.is_distinct_from(target)))
            .values(family_id=target)
            .returning(D.id)
        )
    ).all()
    return {str(r.id): str(target) for r in changed}


@handler("enrich")
async def enrich(ctx: JobContext) -> None:
    services = ctx.services
    item_id = ctx.payload["item_id"]
    async with transaction() as conn:
        item = (
            await conn.execute(
                select(S.source_items, S.sources.c.config.label("source_config"), S.sources.c.kind.label("source_kind"))
                .join(S.sources, S.sources.c.id == SI.source_id)
                .where(SI.id == item_id)
            )
        ).first()
        if item is None or item.status == "deleted" or not item.content_sha256:
            return
        blob = (await conn.execute(select(S.blobs).where(S.blobs.c.sha256 == item.content_sha256))).first()
        if blob is None:
            raise NotReady("waiting for the file to be stored")
        if blob.extract_error:
            raise PermanentError(f"could not read the file: {blob.extract_error}")
        if blob.extracted_at is None:
            await _enqueue(conn, ctx, "extract", {"sha256": blob.sha256, "name": item.name}, dedupe=f"extract:{blob.sha256}")
            raise NotReady("waiting for text extraction", retry_after=5)
        pages = await _pages(conn, blob.sha256)

    from rag.privacy.shield import bank_kinds

    config = dict(item.source_config or {})
    if (
        services.settings.privacy_auto_local_only
        and not config.get("local_only")
        and holds_sensitive_values(pages, bank_kinds(services.settings.privacy_kind_list))
    ):
        config["local_only"] = True  # this document stays away from cloud models (see docs/security.md)
        log.info("item %s holds bank or id numbers: treated as local-only", item_id)
    page_inputs = [PageInput(p.page_no, p.text, p.is_drawing, dict(p.title_block or {})) for p in pages]
    properties = dict(blob.properties or {})
    md = services.extractor.extract(
        path=item.path or item.name, source_fields=dict(item.fields or {}), pages=page_inputs, properties=properties
    )
    if properties.get("header"):
        services.extractor._from_content(md, properties["header"])  # running headers carry document numbers
    await services.hub.refresh_overrides()  # metadata filling chosen on the Models page applies to the next document
    if not config.get("local_only") and not services.hub.spec("LLM_ENRICH").is_off:
        enrich_model = services.hub.try_get("LLM_ENRICH")
        if enrich_model is not None:
            try:
                await fill_gaps_with_llm(enrich_model, md, "\n".join(p.text for p in pages[:2])[:6000])
            except Exception:
                log.warning("LLM enrichment failed for item %s", item_id, exc_info=True)

    project = md.get("project") or config.get("project") or ""
    number_key = md.doc_number_key if md.confidence("doc_number") >= 0.6 else None
    doc_key = f"num:{_slug(project)}:{number_key}" if number_key else f"item:{item_id}"
    full_text = "\n".join(p.text for p in pages)
    fingerprint = signature(full_text)
    title = md.get("title") or item.name

    async with transaction() as conn:
        if number_key and md.confidence("doc_number") < STRONG_IDENTITY and fingerprint is not None:
            # A number read from a file name is a weak identity: "SP 800" is a series, "Report-2" is not unique. Filing a
            # new file under an existing document replaces the older one in search, so it is only done
            # when the text of the two is similar. A wrong split shows a duplicate; a wrong merge hides a document.
            known = (
                await conn.execute(
                    select(V.minhash)
                    .join(S.documents, D.id == V.document_id)
                    .where(D.doc_key == doc_key, V.blob_sha256 != blob.sha256, V.minhash.is_not(None), V.state != "deleted")
                    .order_by(V.updated_at.desc())
                    .limit(1)
                )
            ).first()
            if known is not None and similarity(known.minhash, fingerprint) < IDENTITY_MIN_SIMILARITY:
                log.info("item %s: number %s is shared with an unrelated document; filed on its own", item_id, number_key)
                doc_key, number_key = f"item:{item_id}", None
        doc_id = (
            await conn.execute(
                insert(S.documents)
                .values(
                    doc_key=doc_key,
                    doc_number=md.get("doc_number") if number_key else None,
                    title=title,
                    project_ids=[project] if project else [],
                    local_only=bool(config.get("local_only")),
                )
                .on_conflict_do_update(index_elements=[D.doc_key], set_={"updated_at": datetime.now(UTC)})
                .returning(D.id)
            )
        ).scalar_one()
        values = {
            "title": title,
            "language": md.language,
            "metadata": md.as_json(),
            "identifiers": md.identifiers[:60],
            "page_count": len(pages),
            "updated_at": datetime.now(UTC),
            "minhash": fingerprint,
            "minhash_bands": band_keys(fingerprint) if fingerprint else None,
        }
        stmt = insert(S.versions).values(document_id=doc_id, blob_sha256=blob.sha256, **values)
        version = (
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[V.document_id, V.blob_sha256],
                    set_={
                        **values,
                        "state": text("CASE WHEN versions.state = 'deleted' " "THEN 'pending' ELSE versions.state END"),
                    },
                ).returning(V.id, V.state)
            )
        ).first()
        previous = item.version_id
        # an item skipped earlier (a format the worker could not read then) is healthy once its file has been read
        await conn.execute(
            update(S.source_items)
            .where(SI.id == item_id)
            .values(version_id=version.id, status="active", error=None, skip_reason=None)
        )
        principals = await refresh_version_access(conn, version.id)
        await _refresh_document(conn, doc_id, md, project, bool(config.get("local_only")))
        await conn.execute(delete(S.doc_references).where(S.doc_references.c.from_version_id == version.id))
        own = {number_key} if number_key else set()
        refs = find_references([(p.page_no, p.text) for p in pages], own)
        if refs:
            await conn.execute(
                insert(S.doc_references).on_conflict_do_nothing(),
                [
                    {
                        "from_version_id": version.id,
                        "target": r.target,
                        "kind": r.kind,
                        "page_no": r.page_no,
                        "context": r.context,
                    }
                    for r in refs
                ],
            )
        await _children(conn, ctx, item, blob)
        if previous is not None and previous != version.id:
            await _enqueue(
                conn, ctx, "release_version", {"version_id": str(previous)}, lane="control", dedupe=f"release_version:{previous}"
            )
        if version.state in {"pending", "failed", "deleted"} or ctx.payload.get("force"):
            # `force` (rag reprocess): chunk again even though the version is live, so improved metadata and chunking
            # reach the index. The old chunks keep answering until the new ones replace them.
            await _enqueue(
                conn,
                ctx,
                "chunk",
                {"version_id": str(version.id)},
                dedupe=f"chunk:{version.id}" + (f":force:{ctx.job.id}" if ctx.payload.get("force") else ""),
                version_id=version.id,
            )
        elif version.state == "live":
            await _enqueue(
                conn,
                ctx,
                "update_acl",
                {"version_id": str(version.id)},
                lane="control",
                dedupe=f"update_acl:{version.id}",
                version_id=version.id,
            )
    log.debug("enriched item %s -> document %s (%s principals)", item_id, doc_key, len(principals))


async def _refresh_document(conn: AsyncConnection, doc_id: Any, md: Any, project: str, local_only: bool) -> None:
    values: dict[str, Any] = {"updated_at": datetime.now(UTC), "local_only": local_only}
    for name in ("doc_type", "spec_section"):
        if md.get(name):
            values[name] = md.get(name)
    if md.get("title"):
        values["title"] = md.get("title")
    await conn.execute(update(S.documents).where(D.id == doc_id).values(**values))
    if project:
        await conn.execute(
            text(
                "UPDATE documents SET project_ids = (SELECT array_agg(DISTINCT p ORDER BY p) "
                "FROM unnest(array_append(project_ids, CAST(:p AS text))) AS p) WHERE id = :id"
            ),
            {"p": project, "id": doc_id},
        )


async def _children(conn: AsyncConnection, ctx: JobContext, item: Any, blob: Any) -> None:
    """E-mail attachments and zip members become child items of the item that contains them."""
    for att in blob.attachments or []:
        external_id = f"{item.external_id}#{att['name']}"
        row = (
            await conn.execute(
                insert(S.source_items)
                .values(
                    source_id=item.source_id,
                    external_id=external_id,
                    path=f"{item.path}/{att['name']}",
                    name=att["name"].rsplit("/", 1)[-1],
                    parent_item_id=item.id,
                    etag=att["sha256"],
                    size=att.get("size"),
                    last_seen_at=datetime.now(UTC),
                )
                .on_conflict_do_update(
                    index_elements=[SI.source_id, SI.external_id],
                    set_={"etag": att["sha256"], "last_seen_at": datetime.now(UTC), "status": "active"},
                )
                .returning(SI.id, SI.content_sha256)
            )
        ).first()
        if row.content_sha256 != att["sha256"]:
            await _enqueue(
                conn, ctx, "fetch", {"item_id": str(row.id)}, dedupe=f"fetch:{row.id}:{att['sha256']}", source_id=item.source_id
            )


# ======================================================================================================================
# chunk, embed, publish
# ======================================================================================================================


async def _version_context(conn: AsyncConnection, version_id: Any) -> tuple[Any, Any]:
    row = (await conn.execute(select(S.versions).where(V.id == version_id))).first()
    if row is None:
        return None, None
    doc = (await conn.execute(select(S.documents).where(D.id == row.document_id))).first()
    return row, doc


def _header_fields(version: Any, doc: Any) -> HeaderFields:
    md = version.metadata or {}
    number = doc.doc_number or (md.get("doc_number") or {}).get("value") or ""
    return HeaderFields(
        project=(doc.project_ids or [""])[0],
        doc_number=number,
        title=version.title or doc.title or "",
        doc_number_key=identifier_key(number) if doc.doc_number else None,
    )


@handler("chunk")
async def chunk(ctx: JobContext) -> None:
    services = ctx.services
    version_id = ctx.payload["version_id"]
    async with transaction() as conn:
        version, doc = await _version_context(conn, version_id)
        if version is None or version.state == "deleted":
            return
        pages = await _pages(conn, version.blob_sha256)
    chunker = await services.chunker()
    inputs = [
        PageForChunking(
            number=p.page_no,
            text=p.text,
            tables=list(p.tables or []),
            label=p.label,
            is_drawing=p.is_drawing,
            title_block=dict(p.title_block or {}),
            language=p.language,
        )
        for p in pages
    ]
    chunks = chunker.chunk(inputs, _header_fields(version, doc), version_id=version.id)
    rows = [
        {
            "version_id": version.id,
            "chunk_no": c.chunk_no,
            "id": c.id,
            "document_id": doc.id,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "section_path": c.section_path,
            "kind": c.kind,
            "header": c.header,
            "text": c.text,
            "token_count": c.token_count,
            "language": c.language,
            "content_hash": c.content_hash,
        }
        for c in chunks
    ]
    async with transaction() as conn:
        await conn.execute(delete(S.chunks).where(S.chunks.c.version_id == version.id))
        for i in range(0, len(rows), 500):
            await conn.execute(insert(S.chunks), rows[i : i + 500])
        await conn.execute(
            update(S.versions)
            .where(V.id == version.id)
            .values(state="chunked", chunk_count=len(rows), error=None, updated_at=datetime.now(UTC))
        )
        await _enqueue(conn, ctx, "embed", {"version_id": str(version.id)}, dedupe=f"embed:{version.id}", version_id=version.id)


async def point_payloads(conn: AsyncConnection, version: Any, doc: Any) -> tuple[list[Any], dict[str, Any]]:
    chunks = (await conn.execute(select(S.chunks).where(S.chunks.c.version_id == version.id).order_by(S.chunks.c.chunk_no))).all()
    acl = [
        r.principal
        for r in (
            await conn.execute(select(S.version_access.c.principal).where(S.version_access.c.version_id == version.id))
        ).all()
    ]
    items = (await conn.execute(select(SI.source_id, SI.name).where(SI.version_id == version.id, SI.status == "active"))).all()
    base = {
        "version_id": str(version.id),
        "document_id": str(doc.id),
        "family_id": str(doc.family_id or doc.id),
        "doc_number": doc.doc_number or "",
        "title": version.title or doc.title,
        "project_ids": list(doc.project_ids or []),
        "doc_type": doc.doc_type or "",
        "spec_section": doc.spec_section or "",
        "acl": acl,
        "published": version.state == "live",
        "source_ids": sorted({str(i.source_id) for i in items}),
        "local_only": bool(doc.local_only),
        "file_name": items[0].name if items else "",
    }
    return chunks, base


@handler("embed")
async def embed(ctx: JobContext) -> None:
    services = ctx.services
    version_id = ctx.payload["version_id"]
    build_id = ctx.payload.get("build_id")
    async with transaction() as conn:
        version, doc = await _version_context(conn, version_id)
        if version is None or version.state == "deleted":
            return
        chunks, base = await point_payloads(conn, version, doc)
    builds = await services.write_builds()
    if build_id:
        builds = [b for b in await services.builds(refresh=True) if str(b.id) == str(build_id)]
    batch = max(8, services.settings.embedder_batch * 2)
    for build in builds:
        embedder = services.embedder_for(build)
        start = int(ctx.progress.get(str(build.id), 0))
        for i in range(start, len(chunks), batch):
            part = chunks[i : i + batch]
            dense = await _dense_vectors(services, build, embedder, part)
            points = []
            for c, vector in zip(part, dense, strict=True):
                sparse = services.analyzer.document_vector(f"{c.header}\n{c.text}")
                points.append(
                    models.PointStruct(
                        id=str(c.id),
                        vector={DENSE: vector, SPARSE: models.SparseVector(indices=sparse.indices, values=sparse.values)},
                        payload={
                            **base,
                            "chunk_id": str(c.id),
                            "chunk_no": c.chunk_no,
                            "page_start": c.page_start,
                            "page_end": c.page_end,
                            "kind": c.kind,
                            "section_path": c.section_path,
                            **({"header": c.header, "text": c.text} if services.settings.store_text_in_index else {}),
                            "language": c.language or "und",
                            "identifiers": _chunk_identifiers(c, base),
                            "content_hash": c.content_hash,
                        },
                    )
                )
            await services.vectors.upsert(build.collection, points)
            await ctx.checkpoint(**{str(build.id): i + len(part)})
        # A re-chunked version may now have fewer chunks: remove the leftovers.
        await services.vectors.delete(
            build.collection,
            models.Filter(
                must=[
                    match_value("version_id", str(version.id)),
                    models.FieldCondition(key="chunk_no", range=models.Range(gte=len(chunks))),
                ]
            ),
        )
    async with transaction() as conn:
        if build_id:
            await conn.execute(
                update(S.index_builds)
                .where(S.index_builds.c.id == build_id)
                .values(done_versions=S.index_builds.c.done_versions + 1)
            )
            return
        await conn.execute(
            update(S.versions)
            .where(V.id == version.id, V.state != "deleted")
            .values(state="embedded" if version.state != "live" else "live", updated_at=datetime.now(UTC))
        )
        await _enqueue(
            conn, ctx, "publish", {"version_id": str(version.id)}, dedupe=f"publish:{version.id}", version_id=version.id
        )


async def _dense_vectors(services: Any, build: Any, embedder: Any, part: list[Any]) -> list[list[float]]:
    """Embed a batch of chunks, taking the vector of any chunk whose exact text (header included) is already in this
    build's index instead of computing it again: boilerplate repeated across documents, copies, and the unchanged
    chunks of an edited file that did not change cost nothing. Any problem with the lookup just means embedding everything."""
    texts = [f"{c.header}\n{c.text}" if c.header else c.text for c in part]
    known: dict[str, list[float]] = {}
    try:
        known = await services.vectors.dense_by_hash(build.collection, [c.content_hash for c in part if c.content_hash])
    except Exception:  # noqa: BLE001 - an optimisation only
        log.debug("embedding reuse lookup failed", exc_info=True)
    missing = [i for i, c in enumerate(part) if c.content_hash not in known]
    fresh = await embedder.embed_documents([texts[i] for i in missing]) if missing else []
    computed = dict(zip(missing, fresh, strict=True))
    return [computed[i] if i in computed else known[c.content_hash] for i, c in enumerate(part)]


def _chunk_identifiers(chunk: Any, base: dict[str, Any]) -> list[str]:
    from rag.metadata.identifiers import identifier_keys, label_keys

    ids = identifier_keys(f"{chunk.header}\n{chunk.text}", limit=30)
    ids += [
        k for k in label_keys(getattr(chunk, "section_path", "") or "") if k not in ids
    ]  # the chunk's own caption: "Table 11"
    if base.get("doc_number"):
        key = identifier_key(base["doc_number"])
        if key not in ids:
            ids.insert(0, key)
    return ids


async def mark_published(services: Any, version_id: str) -> None:
    """Tell the index this version is searchable (its chunks were written with published = false)."""
    for build in await services.write_builds():
        await services.vectors.set_payload(build.collection, {"published": True}, version_filter(version_id))


@handler("publish")
async def publish(ctx: JobContext) -> None:
    services = ctx.services
    version_id = ctx.payload["version_id"]
    async with transaction() as conn:
        version = (await conn.execute(select(S.versions).where(V.id == version_id).with_for_update())).first()
        if version is None or version.state == "deleted":
            return
        if version.state not in {"embedded", "live"}:
            raise NotReady(f"version is {version.state}; waiting for embedding")
        await conn.execute(
            update(S.versions)
            .where(V.id == version_id)
            .values(state="live", live_at=func.coalesce(V.live_at, func.now()), error=None)
        )
        await settle_document(conn, version.document_id)
        family_changes = await assign_family(conn, version.document_id)
        # Another publish may have merged this document's family while its chunks were still being embedded; those
        # chunks then carry the family they were written with. Always bring this document's payload up to date.
        own_family = (await conn.execute(select(D.family_id).where(D.id == version.document_id))).scalar_one()
        family_changes.setdefault(str(version.document_id), str(own_family or version.document_id))
        doc = (await conn.execute(select(D.doc_number).where(D.id == version.document_id))).first()
        if doc and doc.doc_number:
            await conn.execute(
                update(S.doc_references)
                .where(
                    S.doc_references.c.target == identifier_key(doc.doc_number), S.doc_references.c.target_document_id.is_(None)
                )
                .values(target_document_id=version.document_id)
            )
    await mark_published(services, str(version_id))
    await get_cache(services.settings).bump_epoch()  # new content: cached answers may be out of date
    for build in await services.write_builds():
        for document_id, family_id in family_changes.items():
            await services.vectors.set_payload(
                build.collection, {"family_id": family_id}, models.Filter(must=[match_value("document_id", document_id)])
            )


# ======================================================================================================================
# access changes and deletions (control lane)
# ======================================================================================================================


async def _push_acl(services: Any, version_id: Any, principals: list[str]) -> None:
    for build in await services.write_builds():
        await services.vectors.set_payload(build.collection, {"acl": principals}, version_filter(version_id))


@handler("update_acl")
async def update_acl(ctx: JobContext) -> None:
    version_id = ctx.payload["version_id"]
    async with transaction() as conn:
        state = (await conn.execute(select(V.state).where(V.id == version_id))).scalar_one_or_none()
        if state is None or state == "deleted":
            return
        principals = await refresh_version_access(conn, version_id)
    await _push_acl(ctx.services, version_id, principals)
    await get_cache(ctx.services.settings).bump_epoch()  # who may see what has changed


@handler("refresh_acl")
async def refresh_acl(ctx: JobContext) -> None:
    services = ctx.services
    item_id = ctx.payload["item_id"]
    async with transaction() as conn:
        row = (
            await conn.execute(
                select(S.source_items, S.sources.c.kind, S.sources.c.config, S.sources.c.default_acl)
                .join(S.sources, S.sources.c.id == SI.source_id)
                .where(SI.id == item_id)
            )
        ).first()
    if row is None or row.status != "active":
        return
    if row.kind == "upload" or row.parent_item_id is not None:
        return  # uploads carry their own sharing; attachments follow their parent item
    connector = make_connector(row.kind, dict(row.config), services.settings)
    try:
        acl = await connector.permissions(_remote(row))
    finally:
        await connector.aclose()
    acl = list(row.default_acl or []) if acl is None else acl
    if acl_hash(acl) == row.acl_hash:
        return
    async with transaction() as conn:
        await conn.execute(update(S.source_items).where(SI.id == item_id).values(acl=acl, acl_hash=acl_hash(acl)))
        if row.version_id is None:
            return
        principals = await refresh_version_access(conn, row.version_id)
    await _push_acl(services, row.version_id, principals)


@handler("delete_item")
async def delete_item(ctx: JobContext) -> None:
    item_id = ctx.payload["item_id"]
    async with transaction() as conn:
        row = (await conn.execute(select(SI.id, SI.version_id).where(SI.id == item_id))).first()
        if row is None:
            return
        await conn.execute(update(S.source_items).where(SI.id == item_id).values(status="deleted", version_id=None))
        children = (
            await conn.execute(
                update(S.source_items)
                .where(SI.parent_item_id == item_id, SI.status != "deleted")
                .values(status="deleted")
                .returning(SI.id)
            )
        ).all()
        for child in children:
            await _enqueue(
                conn, ctx, "delete_item", {"item_id": str(child.id)}, lane="control", dedupe=f"delete_item:{child.id}:children"
            )
    if row.version_id is not None:
        await release_version(ctx.services, row.version_id)


@handler("release_version")
async def release_version_job(ctx: JobContext) -> None:
    await release_version(ctx.services, ctx.payload["version_id"])


async def release_version(services: Any, version_id: Any) -> None:
    """Drop a version that no active item points to any more; otherwise just re-derive its access."""
    async with transaction() as conn:
        version = (await conn.execute(select(S.versions).where(V.id == version_id).with_for_update())).first()
        if version is None or version.state == "deleted":
            return
        active = (
            await conn.execute(
                select(func.count()).select_from(S.source_items).where(SI.version_id == version_id, SI.status == "active")
            )
        ).scalar_one()
        if active:
            principals = await refresh_version_access(conn, version_id)
        else:
            principals = []
            await conn.execute(update(S.versions).where(V.id == version_id).values(state="deleted", updated_at=datetime.now(UTC)))
            await conn.execute(delete(S.version_access).where(S.version_access.c.version_id == version_id))
            await conn.execute(delete(S.chunks).where(S.chunks.c.version_id == version_id))
            await conn.execute(delete(S.doc_references).where(S.doc_references.c.from_version_id == version_id))
            await settle_document(conn, version.document_id)
    builds = list(await services.builds(refresh=True))
    if active:
        await _push_acl(services, version_id, principals)
        return
    for build in builds:
        await services.vectors.delete(build.collection, version_filter(version_id))
    await purge_unused_file(services, version.blob_sha256)
    await get_cache(services.settings).bump_epoch()


async def purge_unused_file(services: Any, sha: str) -> bool:
    """Remove a stored file, its pages, its deleted version rows and its page images, at once, when no live version and
    no active item uses it any more (the daily garbage collector is only the backstop). Returns whether it was removed."""
    async with transaction() as conn:
        used = (
            await conn.execute(
                text(
                    """
            SELECT EXISTS (SELECT 1 FROM versions WHERE blob_sha256 = :s AND state <> 'deleted')
                OR EXISTS (SELECT 1 FROM source_items WHERE content_sha256 = :s AND status <> 'deleted')
                OR EXISTS (SELECT 1 FROM source_items WHERE etag = :s AND status <> 'deleted')
            """
                ),
                {"s": sha},
            )
        ).scalar_one()
        if used:
            return False
        document_ids = [
            r[0]
            for r in (
                await conn.execute(text("SELECT DISTINCT document_id FROM versions WHERE blob_sha256 = :s"), {"s": sha})
            ).all()
        ]
        await conn.execute(delete(S.pages).where(S.pages.c.blob_sha256 == sha))
        await conn.execute(delete(S.versions).where(V.blob_sha256 == sha, V.state == "deleted"))
        await conn.execute(delete(S.blobs).where(S.blobs.c.sha256 == sha))
        if document_ids:  # a document whose only version was this file goes with it
            await conn.execute(
                text(
                    "DELETE FROM documents d WHERE d.id = ANY(CAST(:ids AS uuid[])) "
                    "AND NOT EXISTS (SELECT 1 FROM versions v WHERE v.document_id = d.id)"
                ),
                {"ids": [str(i) for i in document_ids]},
            )
    await services.objects.delete(blob_key(sha))
    cache = Path(services.settings.cache_dir) / "pages" / sha[:2]
    if cache.is_dir():
        for cached in cache.glob(f"{sha}-*"):
            cached.unlink(missing_ok=True)
    return True


# ======================================================================================================================
# housekeeping
# ======================================================================================================================


@handler("resume_stalled")
async def resume_stalled(ctx: JobContext) -> None:
    """Items whose file has been read but that never got their enrichment (an enqueue lost to a race or a restart):
    queue it now. Idempotent; finds nothing in a healthy system."""
    async with transaction() as conn:
        rows = await conn.execute(
            text(
                """
            SELECT i.id, i.source_id, i.content_sha256 FROM source_items i
            JOIN blobs b ON b.sha256 = i.content_sha256 AND b.extracted_at IS NOT NULL
            WHERE i.status = 'active' AND i.version_id IS NULL AND i.fetched_at < now() - interval '2 minutes'
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind IN ('enrich', 'chunk', 'embed', 'publish')
                              AND j.status IN ('queued', 'running') AND j.payload->>'item_id' = i.id::text)
            LIMIT 500"""
            )
        )
        found = rows.all()
        for row in found:
            await _enqueue(
                conn,
                ctx,
                "enrich",
                {"item_id": str(row.id)},
                dedupe=f"enrich:{row.id}:{row.content_sha256}",
                source_id=row.source_id,
            )
    if found:
        log.info("resumed %s items that were waiting for enrichment", len(found))


@handler("gc_blobs")
async def gc_blobs(ctx: JobContext) -> None:
    """Remove stored files and pages no version or item uses any more (after a day, in batches)."""
    services = ctx.services
    cutoff = datetime.now(UTC) - timedelta(days=1)
    async with transaction() as conn:
        rows = (
            await conn.execute(
                text(
                    """
            SELECT b.sha256, b.storage_key FROM blobs b
            WHERE b.created_at < :cutoff
              AND NOT EXISTS (SELECT 1 FROM versions v WHERE v.blob_sha256 = b.sha256 AND v.state <> 'deleted')
              AND NOT EXISTS (SELECT 1 FROM source_items i WHERE i.content_sha256 = b.sha256 AND i.status <> 'deleted')
              AND NOT EXISTS (SELECT 1 FROM source_items i WHERE i.etag = b.sha256 AND i.status <> 'deleted')
            LIMIT 500
            """
                ),
                {"cutoff": cutoff},
            )
        ).all()
    for sha, key in rows:
        async with transaction() as conn:
            await conn.execute(delete(S.pages).where(S.pages.c.blob_sha256 == sha))
            await conn.execute(delete(S.versions).where(V.blob_sha256 == sha, V.state == "deleted"))
            await conn.execute(delete(S.blobs).where(S.blobs.c.sha256 == sha))
        await services.objects.delete(key)
    if rows:
        log.info("removed %s unused files", len(rows))


@handler("reconcile")
async def reconcile(ctx: JobContext) -> None:
    """Compare Postgres with the live index and repair differences (missing or stale points, unpublished payloads)."""
    services = ctx.services
    build = await services.live_build()
    repaired = 0
    batch = int(ctx.payload.get("batch", 500))
    offset = int(ctx.progress.get("offset", 0))
    while True:
        async with transaction() as conn:
            rows = (
                await conn.execute(
                    select(V.id, V.chunk_count, V.state)
                    .where(V.state.in_(["live", "deleted"]))
                    .order_by(V.id)
                    .offset(offset)
                    .limit(batch)
                )
            ).all()
        if not rows:
            break
        for row in rows:
            count = await services.vectors.count(build.collection, version_filter(row.id), exact=True)
            if row.state == "live" and count != row.chunk_count:
                async with transaction() as conn:
                    await _enqueue(
                        conn, ctx, "embed", {"version_id": str(row.id)}, lane="bulk", dedupe=f"embed:{row.id}", version_id=row.id
                    )
                repaired += 1
            elif row.state == "deleted" and count:
                await services.vectors.delete(build.collection, version_filter(row.id))
                repaired += 1
        offset += len(rows)
        await ctx.checkpoint(offset=offset, repaired=repaired)
    if repaired:
        from rag.monitoring.alerts import raise_alert

        await raise_alert(
            "reconcile_repairs",
            "warning",
            f"Reconcile repaired {repaired} versions in {build.collection}",
            {"repaired": repaired},
        )


@handler("finish_rebuild")
async def finish_rebuild(ctx: JobContext) -> None:
    from rag.ingestion.index_builds import activate

    build_id = ctx.payload["build_id"]
    async with transaction() as conn:
        pending = (
            await conn.execute(
                select(func.count())
                .select_from(S.jobs)
                .where(
                    and_(
                        S.jobs.c.kind == "embed",
                        S.jobs.c.status.in_(["queued", "running"]),
                        S.jobs.c.payload["build_id"].astext == str(build_id),
                    )
                )
            )
        ).scalar_one()
        dead = (
            await conn.execute(
                select(func.count())
                .select_from(S.jobs)
                .where(
                    and_(
                        S.jobs.c.kind == "embed", S.jobs.c.status == "dead", S.jobs.c.payload["build_id"].astext == str(build_id)
                    )
                )
            )
        ).scalar_one()
    if pending:
        raise NotReady(f"rebuild still has {pending} versions to embed", retry_after=60)
    if dead:
        raise PermanentError(f"rebuild has {dead} failed versions; retry them from the Monitor page, then activate")
    await activate(ctx.services, uuid.UUID(str(build_id)))
