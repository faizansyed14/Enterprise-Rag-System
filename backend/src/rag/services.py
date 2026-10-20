"""Process-wide services shared by the API, workers and CLI: settings, models, vector store, object store, analyzers."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update

from rag.config.settings import Settings, get_settings
from rag.db import schema
from rag.db.engine import transaction
from rag.ingestion.chunker import Chunker
from rag.metadata.extract import MetadataExtractor
from rag.metadata.path_rules import PathRules
from rag.plugins.hub import ModelHub
from rag.providers.storage.objects import ObjectStore, make_object_store
from rag.providers.vectorstore.qdrant import VectorStore
from rag.text.bm25 import BM25Analyzer
from rag.text.tokens import TokenCounter

log = logging.getLogger(__name__)


@dataclass
class IndexBuild:
    id: Any
    collection: str
    embedder: str
    dims: int
    analyzer: str
    chunker_version: str
    status: str


@dataclass
class Services:
    settings: Settings
    hub: ModelHub
    vectors: VectorStore
    objects: ObjectStore
    analyzer: BM25Analyzer
    extractor: MetadataExtractor
    _chunker: Chunker | None = None
    _builds: list[IndexBuild] = field(default_factory=list)
    _builds_at: float = 0.0
    _projects: list[str] = field(default_factory=list)
    _projects_at: float = 0.0

    @classmethod
    def create(cls, settings: Settings | None = None) -> Services:
        settings = settings or get_settings()
        rules = PathRules.load(settings.path_rules_file or None, settings.known_project_list)
        return cls(
            settings=settings,
            hub=ModelHub(settings),
            vectors=VectorStore(settings),
            objects=make_object_store(settings),
            analyzer=BM25Analyzer(),
            extractor=MetadataExtractor(rules),
        )

    async def aclose(self) -> None:
        await self.hub.aclose()
        await self.vectors.aclose()

    # ---- chunking -------------------------------------------------------------------------------------------------------
    async def chunker(self) -> Chunker:
        if self._chunker is None:
            tokenizer = await self._embedder_tokenizer()
            counter = TokenCounter(tokenizer)
            s = self.settings
            self._chunker = Chunker(counter, chunk_tokens=s.chunk_tokens, header_tokens=s.chunk_header_tokens,
                                    overlap=s.chunk_overlap, min_tokens=s.chunk_min_tokens)
            log.info("chunk sizing uses %s token counts", "exact" if counter.exact else "estimated")
        return self._chunker

    async def _embedder_tokenizer(self) -> str | None:
        """Fetch the local embedder's tokenizer.json once so chunks are sized in its real tokens."""
        spec = self.hub.spec("EMBEDDER")
        if spec.provider != "local":
            return None
        target = Path(self.settings.cache_dir) / "tokenizers" / f"{spec.model}.json"
        if target.is_file():
            return str(target)
        try:
            from rag.providers.local_models import LocalModelClient

            client = LocalModelClient(self.settings)
            try:
                response = await client._client.get(f"/models/{spec.model}/tokenizer.json")
                if response.status_code == 200:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(response.content)
                    return str(target)
            finally:
                await client.aclose()
        except Exception:  # noqa: BLE001 - estimates are a safe fallback
            log.info("model server tokenizer not available; chunk sizes are estimated")
        return None

    # ---- index builds ---------------------------------------------------------------------------------------------------
    async def builds(self, *, refresh: bool = False) -> list[IndexBuild]:
        if refresh or not self._builds or time.monotonic() - self._builds_at > 10:
            async with transaction() as conn:
                rows = (await conn.execute(
                    select(schema.index_builds).where(schema.index_builds.c.status.in_(["live", "building", "ready"]))
                )).all()
            self._builds = [IndexBuild(r.id, r.collection, r.embedder, r.embedder_dims, r.analyzer, r.chunker_version,
                                       r.status) for r in rows]
            self._builds_at = time.monotonic()
        return self._builds

    async def live_build(self) -> IndexBuild:
        builds = await self.builds()
        live = [b for b in builds if b.status == "live"]
        if not live:
            from rag.ingestion.index_builds import ensure_live_build

            await ensure_live_build(self)
            live = [b for b in await self.builds(refresh=True) if b.status == "live"]
        return live[0]

    async def write_builds(self) -> list[IndexBuild]:
        """Builds that new and changed documents are written to (the live one plus any being built)."""
        await self.live_build()
        return [b for b in await self.builds() if b.status in {"live", "building", "ready"}]

    def embedder_for(self, build: IndexBuild) -> Any:
        return self.hub.get("EMBEDDER", build.embedder)

    async def known_projects(self) -> list[str]:
        """Project names the query router recognises in questions: KNOWN_PROJECTS plus every project that documents
        are filed under (refreshed every five minutes)."""
        if not self._projects_at or time.monotonic() - self._projects_at > 300:
            try:
                async with transaction() as conn:
                    rows = (await conn.execute(text(
                        "SELECT DISTINCT p FROM documents, unnest(project_ids) AS p WHERE status = 'active' "
                        "AND length(p) >= 3 LIMIT 2000"))).all()
                found = [r[0] for r in rows]
            except Exception:  # noqa: BLE001 - fall back to the configured list
                found = []
            self._projects = sorted(set(self.settings.known_project_list) | set(found))
            self._projects_at = time.monotonic()
        return self._projects

    # ---- failure bookkeeping (called by the worker when a job will not be retried) ---------------------------------------
    async def on_job_failed(self, job: Any, status: str, detail: str) -> None:
        message = detail.splitlines()[0][:1000] if detail else status
        payload = job.payload or {}
        async with transaction() as conn:
            if job.kind in {"fetch", "enrich"} and payload.get("item_id"):
                await conn.execute(update(schema.source_items).where(schema.source_items.c.id == payload["item_id"])
                                   .values(status="skipped" if status == "failed" else "error",
                                           skip_reason=message if status == "failed" else None, error=message))
            elif job.kind == "extract" and payload.get("sha256"):
                await conn.execute(update(schema.blobs).where(schema.blobs.c.sha256 == payload["sha256"])
                                   .values(extract_error=message))
                await conn.execute(update(schema.source_items)
                                   .where(schema.source_items.c.content_sha256 == payload["sha256"])
                                   .values(status="skipped" if status == "failed" else "error",
                                           skip_reason=message, error=message))
            elif job.kind in {"chunk", "embed", "publish"} and payload.get("version_id"):
                await conn.execute(update(schema.versions).where(schema.versions.c.id == payload["version_id"])
                                   .values(state="failed", error=message))
