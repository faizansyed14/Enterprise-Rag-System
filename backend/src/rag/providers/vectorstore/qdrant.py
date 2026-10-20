"""Qdrant: one collection per index build, reached through an alias.

Collection names record everything that changes the vectors: `chunks__<embedder>__<analyzer>__<chunker version>`.
Switching the embedder (or the analyzer, or the chunker) builds a new collection next to the live one while search
keeps using the old one; the alias `chunks_live` is then swapped in one atomic call. Rolling back is swapping it back.

Each point holds a dense vector ("dense": cosine, HNSW, int8 scalar quantisation kept in RAM with full vectors on
disk), a BM25 sparse vector ("bm25", IDF computed by Qdrant) and the chunk payload with indexed fields for ACL,
project, type and identifiers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from rag.config.settings import Settings

log = logging.getLogger(__name__)

# transport errors that mean "the pooled socket was already closed", not "the server is slow or unwell"
_STALE_CONNECTION = (httpx.ReadError, httpx.WriteError, httpx.ConnectError, httpx.RemoteProtocolError)

DENSE = "dense"
SPARSE = "bm25"

KEYWORD_FIELDS = ("version_id", "document_id", "family_id", "doc_number", "project_ids", "doc_type",
                  "spec_section", "language", "identifiers", "acl", "kind", "source_ids", "content_hash")
BOOL_FIELDS = ("published", "local_only")


def collection_name(prefix: str, embedder_slug: str, analyzer: str, chunker_version: str) -> str:
    raw = f"{prefix}__{embedder_slug}__{analyzer}__{chunker_version}"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)[:200]


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.alias = settings.qdrant_alias
        self.client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.secret("qdrant_api_key"),
                                        timeout=int(settings.qdrant_timeout_s), prefer_grpc=False)

    async def aclose(self) -> None:
        await self.client.close()

    @staticmethod
    async def _call(call: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        """Run one Qdrant request, repeating it when the connection from the pool turns out to be dead.

        The server closes idle connections after a few seconds; a request that happens to pick such a socket fails with a read
        error although nothing is wrong. Every request this class makes is safe to repeat (reads, upserts, payload writes
        and deletes by filter). Timeouts are not repeated: a slow query would only be slower."""
        for attempt in range(3):
            try:
                return await call(**kwargs)
            except ResponseHandlingException as exc:
                if attempt == 2 or not isinstance(exc.source, _STALE_CONNECTION):
                    raise
                log.info("qdrant connection was closed by the server; repeating the request (%s)", type(exc.source).__name__)
                await asyncio.sleep(0.1 * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- collections and aliases -------------------------------------------------------------------------------------
    async def ensure_collection(self, name: str, dims: int) -> bool:
        """Create the collection and its payload indexes if missing. Returns True if it was created."""
        if await self.client.collection_exists(name):
            return False
        try:
            await self._create(name, dims)
        except UnexpectedResponse as exc:
            if exc.status_code == 409:  # another worker created it a moment ago
                return False
            raise
        log.info("created Qdrant collection %s (%s dims)", name, dims)
        return True

    async def _create(self, name: str, dims: int) -> None:
        s = self.settings
        quantization = None
        if s.qdrant_quantization == "int8":
            quantization = models.ScalarQuantization(
                scalar=models.ScalarQuantizationConfig(type=models.ScalarType.INT8, quantile=0.99, always_ram=True))
        await self.client.create_collection(
            collection_name=name,
            vectors_config={DENSE: models.VectorParams(size=dims, distance=models.Distance.COSINE, on_disk=s.qdrant_on_disk,
                                                       hnsw_config=models.HnswConfigDiff(m=16, ef_construct=128))},
            sparse_vectors_config={SPARSE: models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=s.qdrant_on_disk), modifier=models.Modifier.IDF)},
            shard_number=s.qdrant_shards,
            replication_factor=s.qdrant_replicas,
            on_disk_payload=s.qdrant_on_disk,
            quantization_config=quantization,
            optimizers_config=models.OptimizersConfigDiff(default_segment_number=2),
        )
        for field in KEYWORD_FIELDS:
            await self.client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
        for field in BOOL_FIELDS:
            await self.client.create_payload_index(name, field, models.PayloadSchemaType.BOOL)
        await self.client.create_payload_index(name, "page_start", models.PayloadSchemaType.INTEGER)
        await self.client.create_payload_index(name, "chunk_no", models.PayloadSchemaType.INTEGER)

    async def alias_target(self, alias: str | None = None) -> str | None:
        alias = alias or self.alias
        response = await self.client.get_aliases()
        for item in response.aliases:
            if item.alias_name == alias:
                return item.collection_name
        return None

    async def point_alias(self, collection: str, alias: str | None = None) -> None:
        """Atomically point the alias at `collection` (create, or delete-and-create in one request)."""
        alias = alias or self.alias
        operations: list[Any] = []
        if await self.alias_target(alias):
            operations.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
        operations.append(models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)))
        await self.client.update_collection_aliases(change_aliases_operations=operations)

    async def drop_collection(self, name: str) -> None:
        await self.client.delete_collection(name)

    async def collections(self) -> list[str]:
        response = await self.client.get_collections()
        return [c.name for c in response.collections]

    async def info(self, name: str) -> dict[str, Any]:
        info = await self.client.get_collection(name)
        return {"points": info.points_count or 0, "indexed_vectors": info.indexed_vectors_count or 0,
                "status": str(info.status), "segments": info.segments_count}

    # ---- writes --------------------------------------------------------------------------------------------------------
    async def upsert(self, collection: str, points: Sequence[models.PointStruct]) -> None:
        if points:
            await self._call(self.client.upsert, collection_name=collection, points=list(points), wait=True)

    async def set_payload(self, collection: str, payload: dict[str, Any], flt: models.Filter) -> None:
        await self._call(self.client.set_payload, collection_name=collection, payload=payload, points=flt, wait=True)

    async def delete(self, collection: str, flt: models.Filter) -> None:
        await self._call(self.client.delete, collection_name=collection,
                         points_selector=models.FilterSelector(filter=flt), wait=True)

    # ---- reads ---------------------------------------------------------------------------------------------------------
    async def count(self, collection: str, flt: models.Filter | None = None, *, exact: bool = False) -> int:
        result = await self._call(self.client.count, collection_name=collection, count_filter=flt, exact=exact)
        return result.count

    async def search_batch(self, collection: str, requests: list[models.QueryRequest]) -> list[list[Hit]]:
        responses = await self._call(self.client.query_batch_points, collection_name=collection, requests=requests)
        return [[Hit(id=str(p.id), score=float(p.score), payload=p.payload or {}) for p in r.points]
                for r in responses]

    async def scroll_ids(self, collection: str, flt: models.Filter, limit: int = 10_000) -> list[str]:
        ids: list[str] = []
        offset = None
        while True:
            points, offset = await self._call(self.client.scroll, collection_name=collection, scroll_filter=flt, limit=1000,
                                              offset=offset, with_payload=False, with_vectors=False)
            ids.extend(str(p.id) for p in points)
            if offset is None or len(ids) >= limit:
                return ids

    async def dense_by_hash(self, collection: str, hashes: Sequence[str]) -> dict[str, list[float]]:
        """Dense vectors already stored for chunks with these content hashes (same text, same embedder = same vector).
        One indexed lookup per batch; callers embed only what is missing."""
        if not hashes:
            return {}
        points, _ = await self._call(
            self.client.scroll, collection_name=collection,
            scroll_filter=models.Filter(must=[match_any("content_hash", list(hashes))]),
            limit=len(hashes) * 2, with_payload=["content_hash"], with_vectors=[DENSE])
        found: dict[str, list[float]] = {}
        for point in points:
            vector = (point.vector or {}).get(DENSE) if isinstance(point.vector, dict) else point.vector
            digest = (point.payload or {}).get("content_hash")
            if digest and vector is not None and digest not in found:
                found[digest] = list(vector)
        return found

    async def retrieve(self, collection: str, ids: Sequence[str | uuid.UUID]) -> list[Hit]:
        points = await self._call(self.client.retrieve, collection_name=collection, ids=[str(i) for i in ids],
                                  with_payload=True)
        return [Hit(id=str(p.id), score=0.0, payload=p.payload or {}) for p in points]


# ---- filter helpers ------------------------------------------------------------------------------------------------------


def match_any(key: str, values: Sequence[str]) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchAny(any=list(values)))


def match_value(key: str, value: Any) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def version_filter(version_id: str | uuid.UUID) -> models.Filter:
    return models.Filter(must=[match_value("version_id", str(version_id))])


def document_filter(document_id: str | uuid.UUID) -> models.Filter:
    return models.Filter(must=[match_value("document_id", str(document_id))])
