"""The ingestion pipeline end to end on the demo corpus: real files, real Postgres, real Qdrant, real OCR."""

from __future__ import annotations

from pathlib import Path

from qdrant_client import models
from sqlalchemy import select, text, update

from rag.db import schema
from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.providers.vectorstore.qdrant import match_value

from .conftest import add_source, run_jobs


async def _sync(source_id: str) -> None:
    async with transaction() as conn:
        await queue.enqueue(conn, NewJob("sync_source", {"source_id": source_id}, lane="interactive"))


async def _ingest(services, corpus: Path) -> tuple[str, str]:  # noqa: ANN001
    public = await add_source("Project library", corpus / "public", ["everyone"])
    restricted = await add_source("Commercial", corpus / "restricted", ["lgroup:commercial"])
    await _sync(public)
    await _sync(restricted)
    await run_jobs(services)
    return public, restricted


async def _scalar(sql: str, **params: object) -> object:
    async with transaction() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def test_every_file_becomes_a_live_searchable_version(services, copy_corpus) -> None:  # noqa: ANN001
    await _ingest(services, copy_corpus)

    assert await _scalar("SELECT count(*) FROM jobs WHERE status IN ('dead', 'failed')") == 0
    bad = await _scalar("SELECT string_agg(path || ': ' || coalesce(error, ''), '; ') FROM source_items "
                        "WHERE status <> 'active'")
    assert bad is None, bad
    # 13 files plus the PDF attached to the e-mail.
    assert await _scalar("SELECT count(*) FROM source_items") == 14
    assert await _scalar("SELECT count(*) FROM versions WHERE state <> 'live'") == 0

    build = await services.live_build()
    expected = await _scalar("SELECT sum(chunk_count) FROM versions WHERE state = 'live'")
    published = await services.vectors.count(build.collection, models.Filter(must=[match_value("published", True)]),
                                             exact=True)
    assert published == expected > 0


async def test_identity_and_metadata(services, copy_corpus) -> None:  # noqa: ANN001
    await _ingest(services, copy_corpus)
    async with transaction() as conn:
        spec = (await conn.execute(text(
            "SELECT d.id, d.project_ids, d.doc_type, count(v.id) AS versions "
            "FROM documents d JOIN versions v ON v.document_id = d.id "
            "WHERE d.doc_key = 'num:marina-tower:SPEC-033000' GROUP BY d.id"))).first()
        assert spec is not None and spec.versions == 1
        assert spec.project_ids == ["Marina Tower"] and spec.doc_type == "specification"
        # Same section number in another project is a different document.
        harbour = (await conn.execute(text(
            "SELECT count(*) FROM documents WHERE doc_key = 'num:harbour-view:SPEC-033000'"))).scalar()
        assert harbour == 1
        drawing = (await conn.execute(text(
            "SELECT d.doc_number, d.doc_type FROM documents d "
            "JOIN versions v ON v.id = d.current_version_id WHERE d.doc_number LIKE 'PRJ1-%'"))).first()
        assert drawing.doc_number == "PRJ1-ABC-ZZ-03-DR-S-2001"
        assert drawing.doc_type == "drawing"
        tb = (await conn.execute(text("SELECT count(*) FROM chunks WHERE kind = 'title_block'"))).scalar()
        assert tb >= 1


async def test_ocr_arabic_office_email_and_families(services, copy_corpus) -> None:  # noqa: ANN001
    await _ingest(services, copy_corpus)
    async with transaction() as conn:
        scan = (await conn.execute(text(
            "SELECT p.method, p.text FROM pages p JOIN source_items i ON i.content_sha256 = p.blob_sha256 "
            "WHERE i.name LIKE 'SI-014%'"))).first()
        assert scan.method == "ocr"
        assert "fire dampers at all shaft penetrations" in scan.text.lower()
        arabic = (await conn.execute(text(
            "SELECT v.language FROM versions v JOIN source_items i ON i.version_id = v.id "
            "WHERE i.name LIKE 'محضر%'"))).scalar()
        assert arabic == "ar"
        boq = (await conn.execute(text("SELECT text FROM chunks WHERE kind = 'table' AND text LIKE '%B-12%'"))).scalar()
        assert "Qty: 420" in boq and "Description: Reinforcement steel 16mm" in boq
        child = (await conn.execute(text(
            "SELECT c.name, c.status, c.version_id IS NOT NULL FROM source_items c "
            "WHERE c.parent_item_id IS NOT NULL"))).first()
        assert child.name == "SUB-084 Rebar shop drawings.pdf" and child.status == "active" and child[2]
        # The filed copy of RFI-023 (no project) is a separate document in the same family as the original.
        fams = (await conn.execute(text(
            "SELECT count(DISTINCT d.id), count(DISTINCT d.family_id) FROM documents d "
            "JOIN versions v ON v.document_id = d.id JOIN source_items i ON i.version_id = v.id "
            "WHERE i.name LIKE 'RFI-023%'"))).first()
        assert tuple(fams) == (2, 1)


async def test_restricted_documents_carry_only_their_acl(services, copy_corpus) -> None:  # noqa: ANN001
    await _ingest(services, copy_corpus)
    build = await services.live_build()
    points, _ = await services.vectors.client.scroll(
        build.collection, scroll_filter=models.Filter(must=[match_value("acl", "lgroup:commercial")]), limit=100,
        with_payload=True)
    assert points and all(p.payload["acl"] == ["lgroup:commercial"] for p in points)
    assert any("VO-15" in (p.payload.get("doc_number") or "") for p in points)


async def test_reingest_is_idempotent_and_deletions_propagate(services, copy_corpus) -> None:  # noqa: ANN001
    public, _ = await _ingest(services, copy_corpus)
    chunks_before = await _scalar("SELECT count(*) FROM chunks")
    await _sync(public)
    await run_jobs(services)
    assert await _scalar("SELECT count(*) FROM chunks") == chunks_before
    # 12 files + the e-mail attachment, each fetched once: the re-sync found nothing changed.
    assert await _scalar("SELECT count(*) FROM jobs WHERE kind = 'fetch' AND status = 'done'") == 13

    # Delete the spec: its version is deleted and its points leave the index.
    target = copy_corpus / "public" / "Marina Tower" / "Specifications" / "03 30 00 Cast-in-Place Concrete Rev 2.pdf"
    target.unlink()
    await _sync(public)
    await run_jobs(services)
    async with transaction() as conn:
        rows = (await conn.execute(text(
            "SELECT v.id, v.state FROM versions v JOIN documents d ON d.id = v.document_id "
            "WHERE d.doc_key = 'num:marina-tower:SPEC-033000'"))).all()
    assert [r.state for r in rows] == ["deleted"]
    build = await services.live_build()
    deleted_points = await services.vectors.count(
        build.collection, models.Filter(must=[match_value("version_id", str(rows[0].id))]), exact=True)
    assert deleted_points == 0


async def test_acl_change_reaches_the_index_through_the_control_lane(services, copy_corpus) -> None:  # noqa: ANN001
    _, restricted = await _ingest(services, copy_corpus)
    async with transaction() as conn:
        item = (await conn.execute(select(schema.source_items).where(
            schema.source_items.c.source_id == restricted))).first()
        await conn.execute(update(schema.source_items).where(schema.source_items.c.id == item.id)
                           .values(acl=["lgroup:commercial", "lgroup:directors"]))
        await queue.enqueue(conn, NewJob("update_acl", {"version_id": str(item.version_id)}, lane="control"))
    await run_jobs(services)
    build = await services.live_build()
    hits = await services.vectors.count(build.collection, models.Filter(must=[
        match_value("version_id", str(item.version_id)), match_value("acl", "lgroup:directors")]), exact=True)
    assert hits > 0
