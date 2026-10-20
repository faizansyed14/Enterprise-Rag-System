"""Index builds: create the first collection, rebuild into a new one when the embedder/analyzer/chunker changes,
swap the alias when the rebuild is complete, roll back by swapping it back.

    rag index status     what is live, what .env asks for, progress of any rebuild
    rag index rebuild    start building a collection for the current .env (search keeps using the live one)
    rag index activate   point search at a finished build (done automatically when a rebuild completes)
    rag index rollback   point search back at the previous build
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from rag.db import schema
from rag.db.engine import transaction
from rag.jobs import queue
from rag.jobs.queue import NewJob
from rag.providers.vectorstore.qdrant import collection_name

log = logging.getLogger(__name__)
B = schema.index_builds.c


def configured_build(services: Any) -> dict[str, Any]:
    """The build the current .env describes."""
    embedder = services.hub.embedder()
    s = services.settings
    return {
        "collection": collection_name(s.qdrant_collection_prefix, embedder.slug, services.analyzer.version,
                                      s.chunker_version),
        "embedder": services.hub.spec("EMBEDDER").label,
        "embedder_dims": int(embedder.dims),
        "analyzer": services.analyzer.version,
        "chunker_version": s.chunker_version,
    }


async def ensure_live_build(services: Any) -> str:
    """Make sure a live build exists and the alias points at it. Creates one from .env on first start."""
    async with transaction() as conn:
        row = (await conn.execute(select(schema.index_builds).where(B.status == "live"))).first()
    if row is not None:
        target = await services.vectors.alias_target()
        if target != row.collection:
            await services.vectors.ensure_collection(row.collection, row.embedder_dims)
            await services.vectors.point_alias(row.collection)
        return row.collection
    wanted = configured_build(services)
    await services.vectors.ensure_collection(wanted["collection"], wanted["embedder_dims"])
    async with transaction() as conn:
        await conn.execute(
            insert(schema.index_builds).values(**wanted, status="live", live_at=datetime.now(UTC))
            .on_conflict_do_update(index_elements=[B.collection], set_={"status": "live", "live_at": datetime.now(UTC)})
        )
    await services.vectors.point_alias(wanted["collection"])
    await services.builds(refresh=True)
    log.info("live index is %s", wanted["collection"])
    return wanted["collection"]


async def status(services: Any) -> dict[str, Any]:
    async with transaction() as conn:
        rows = (await conn.execute(select(schema.index_builds).order_by(B.created_at.desc()))).all()
        live_versions = (await conn.execute(
            select(func.count()).select_from(schema.versions).where(schema.versions.c.state == "live"))).scalar_one()
    wanted = configured_build(services)
    builds = []
    for r in rows:
        info: dict[str, Any] = {}
        try:
            info = await services.vectors.info(r.collection)
        except Exception:  # noqa: BLE001 - a retired collection may have been dropped
            info = {"points": None}
        builds.append({"id": str(r.id), "collection": r.collection, "embedder": r.embedder, "dims": r.embedder_dims,
                       "analyzer": r.analyzer, "chunker_version": r.chunker_version, "status": r.status,
                       "created_at": r.created_at.isoformat(), "live_at": r.live_at.isoformat() if r.live_at else None,
                       "total_versions": r.total_versions, "done_versions": r.done_versions,
                       "points": info.get("points")})
    live = next((b for b in builds if b["status"] == "live"), None)
    return {"configured": wanted, "live": live, "builds": builds, "live_versions": live_versions,
            "rebuild_needed": live is not None and live["collection"] != wanted["collection"]}


async def start_rebuild(services: Any) -> dict[str, Any]:
    wanted = configured_build(services)
    live = await services.live_build()
    if live.collection == wanted["collection"]:
        return {"started": False, "reason": "the live index already matches .env"}
    await services.vectors.ensure_collection(wanted["collection"], wanted["embedder_dims"])
    async with transaction() as conn:
        version_ids = [r[0] for r in (await conn.execute(
            select(schema.versions.c.id).where(schema.versions.c.state == "live"))).all()]
        result = await conn.execute(
            insert(schema.index_builds).values(**wanted, status="building", total_versions=len(version_ids))
            .on_conflict_do_update(index_elements=[B.collection],
                                   set_={"status": "building", "total_versions": len(version_ids), "done_versions": 0})
            .returning(B.id)
        )
        build_id = result.scalar_one()
        jobs = [NewJob("embed", {"version_id": str(v), "build_id": str(build_id), "rebuild": True}, lane="bulk",
                       priority=-1, dedupe_key=f"rebuild:{build_id}:{v}", version_id=v) for v in version_ids]
        await queue.enqueue_many(conn, jobs)
        await queue.enqueue(conn, NewJob("finish_rebuild", {"build_id": str(build_id)}, lane="bulk", priority=-2,
                                         dedupe_key=f"finish_rebuild:{build_id}"))
    await services.builds(refresh=True)
    return {"started": True, "build_id": str(build_id), "collection": wanted["collection"],
            "versions": len(version_ids)}


async def activate(services: Any, build_id: Any) -> dict[str, Any]:
    async with transaction() as conn:
        target = (await conn.execute(select(schema.index_builds).where(B.id == build_id))).first()
        if target is None:
            raise ValueError("unknown build")
        await conn.execute(update(schema.index_builds).where(B.status == "live").values(status="retired",
                                                                                         retired_at=datetime.now(UTC)))
        await conn.execute(update(schema.index_builds).where(B.id == build_id).values(status="live",
                                                                                       live_at=datetime.now(UTC)))
    await services.vectors.point_alias(target.collection)
    await services.builds(refresh=True)
    log.warning("search now uses %s", target.collection)
    return {"live": target.collection}


async def rollback(services: Any) -> dict[str, Any]:
    async with transaction() as conn:
        previous = (await conn.execute(
            select(schema.index_builds).where(B.status == "retired").order_by(B.retired_at.desc()).limit(1))).first()
    if previous is None:
        return {"rolled_back": False, "reason": "no retired build to roll back to"}
    await activate(services, previous.id)
    return {"rolled_back": True, "live": previous.collection}
