"""Fixtures for tests that run the real pipeline: services, a clean Qdrant, the demo corpus and a worker."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rag.config.settings import Settings
from rag.db import schema
from rag.db.engine import transaction
from rag.jobs.handlers import load_handlers
from rag.jobs.worker import Worker
from rag.services import Services


async def reset_qdrant(services: Services) -> None:
    prefix = services.settings.qdrant_collection_prefix
    for alias in (await services.vectors.client.get_aliases()).aliases:
        if alias.alias_name == services.settings.qdrant_alias:
            from qdrant_client import models

            await services.vectors.client.update_collection_aliases(change_aliases_operations=[
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias.alias_name))])
    for name in await services.vectors.collections():
        if name.startswith(prefix):
            await services.vectors.drop_collection(name)


@pytest.fixture
async def services(settings: Settings, db, originals_dir):  # noqa: ANN001, ANN201
    load_handlers()
    svc = Services.create(settings)
    await reset_qdrant(svc)
    yield svc
    await svc.aclose()


async def add_source(name: str, root: Path, acl: list[str], kind: str = "filesystem") -> str:
    from sqlalchemy.dialects.postgresql import insert

    async with transaction() as conn:
        row = await conn.execute(insert(schema.sources).values(
            name=name, kind=kind, config={"root": str(root)} if kind == "filesystem" else {},
            default_acl=acl).returning(schema.sources.c.id))
        return str(row.scalar_one())


async def run_jobs(services: Services, *, rounds: int = 3) -> None:
    """Run workers until nothing is runnable (jobs waiting on another job are pulled forward between rounds)."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    for _ in range(rounds):
        await Worker(services, concurrency=4, idle_poll_s=0.05, lease_seconds=120).run(until_idle=True)
        async with transaction() as conn:
            result = await conn.execute(update(schema.jobs).where(schema.jobs.c.status == "queued")
                                        .values(run_after=datetime.now(UTC)).returning(schema.jobs.c.id))
            if not result.all():
                return


@pytest.fixture
def copy_corpus(corpus_root: Path, tmp_path: Path) -> Path:
    target = tmp_path / "corpus"
    shutil.copytree(corpus_root, target)
    return target
