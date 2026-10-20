"""Scale proof: inflate the LIVE index with synthetic distractor chunks and measure what happens to retrieval.

The synthetic chunks are made to compete for real:
  dense    a normalised mix of two real chunk vectors plus noise (so they sit on the real embedding manifold, with no exact
           copies of anything)
  sparse   the keyword vector of a random real chunk with 30 % of its terms dropped and a few random terms added
  postgres each belongs to one of many synthetic documents that exist in `documents`/`versions`/`version_access`, so they pass
           the access re-check and appear in results as wrong documents exactly as real distractors would
  acl      everyone can read them (they must be able to push real answers out of the candidate lists)

  python /eval/scale_inject.py inject 1000000       add that many chunks (resumable: counts what is already there)
  python /eval/scale_inject.py clean                remove every synthetic point, version and document
  python /eval/scale_inject.py recall               ANN recall@10 vs an exact scan, latency, for several access-list sizes

Run `rag eval bench` before, during and after to see whether real questions still find their pages.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
import uuid

import numpy as np
import psycopg
from qdrant_client import models

from rag.config.settings import get_settings
from rag.jobs.handlers import load_handlers
from rag.providers.vectorstore.qdrant import DENSE, SPARSE, match_any, match_value
from rag.services import Services

TAG = "synthetic"
NS = uuid.UUID("5e1d3c3a-71c9-4b8c-8a0f-0b6a8c2f9d11")
CHUNKS_PER_DOC = 500


async def real_basis(services, build):
    """Real chunk vectors and keyword vectors to build the synthetic ones from."""
    vectors, sparse, payloads = [], [], []
    offset = None
    while True:
        points, offset = await services.vectors.client.scroll(
            build.collection, limit=500, offset=offset, with_vectors=True, with_payload=["language", "kind"],
            scroll_filter=models.Filter(must_not=[match_value(TAG, True)]))
        for p in points:
            vec = p.vector
            if isinstance(vec, dict) and DENSE in vec and SPARSE in vec:
                vectors.append(vec[DENSE])
                sp = vec[SPARSE]
                sparse.append((list(sp.indices), list(sp.values)))
                payloads.append(p.payload or {})
        if offset is None:
            return np.asarray(vectors, dtype=np.float32), sparse, payloads


async def count_synthetic(services, build) -> int:
    return await services.vectors.count(build.collection, models.Filter(must=[match_value(TAG, True)]), exact=True)


async def inject(total: int) -> None:
    s = get_settings()
    load_handlers()
    services = Services.create(s)
    build = await services.live_build()
    dsn = s.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    basis, sparse_basis, _ = await real_basis(services, build)
    rng = np.random.default_rng(11)
    prng = random.Random(11)
    print(f"real basis: {len(basis)} chunks", flush=True)
    have = await count_synthetic(services, build)
    docs_needed = (total + CHUNKS_PER_DOC - 1) // CHUNKS_PER_DOC
    started = time.perf_counter()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("INSERT INTO blobs (sha256, size, format, storage_key, extracted_at) VALUES ('synthetic', 0, 'pdf', 'synthetic', now()) ON CONFLICT DO NOTHING")
    done = have
    doc_index = have // CHUNKS_PER_DOC
    while done < total:
        doc_id, version_id = uuid.uuid5(NS, f"doc{doc_index}"), uuid.uuid5(NS, f"ver{doc_index}")
        n = min(CHUNKS_PER_DOC, total - done)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("INSERT INTO documents (id, doc_key, title, project_ids, status) VALUES (%s, %s, %s, %s, 'active') ON CONFLICT DO NOTHING",
                      (doc_id, f"synthetic:{doc_index}", f"Synthetic document {doc_index}", [f"synthetic-{doc_index % 40}"]))
            c.execute("INSERT INTO versions (id, document_id, blob_sha256, state, chunk_count, page_count) VALUES (%s, %s, 'synthetic', 'live', %s, 50) ON CONFLICT DO NOTHING",
                      (version_id, doc_id, n))
            c.execute("INSERT INTO version_access (version_id, principal) VALUES (%s, 'everyone') ON CONFLICT DO NOTHING", (version_id,))
        a = rng.integers(0, len(basis), n), rng.integers(0, len(basis), n)
        mix = rng.uniform(0.3, 0.7, (n, 1)).astype(np.float32)
        dense = mix * basis[a[0]] + (1 - mix) * basis[a[1]] + rng.normal(0, 0.03, (n, basis.shape[1])).astype(np.float32)
        dense /= np.linalg.norm(dense, axis=1, keepdims=True)
        points = []
        for i in range(n):
            idx, val = sparse_basis[int(rng.integers(0, len(sparse_basis)))]
            keep = [j for j in range(len(idx)) if prng.random() > 0.3] or [0]
            indices = [idx[j] for j in keep] + [prng.getrandbits(31) for _ in range(2)]
            values = [val[j] for j in keep] + [1.0, 1.0]
            points.append(models.PointStruct(
                id=str(uuid.uuid5(NS, f"p{doc_index}:{i}")),
                vector={DENSE: dense[i].tolist(), SPARSE: models.SparseVector(indices=indices, values=values)},
                payload={TAG: True, "version_id": str(version_id), "document_id": str(doc_id), "family_id": str(doc_id),
                         "doc_number": "", "title": f"Synthetic document {doc_index}", "project_ids": [f"synthetic-{doc_index % 40}"],
                         "doc_type": "", "spec_section": "", "acl": ["everyone"], "published": True,
                         "local_only": False, "file_name": f"synthetic-{doc_index}.pdf", "chunk_no": i, "page_start": 1 + i // 10,
                         "page_end": 1 + i // 10, "kind": "text", "section_path": "", "language": "en", "identifiers": [],
                         "content_hash": uuid.uuid5(NS, f"h{doc_index}:{i}").hex}))
        await services.vectors.client.upsert(build.collection, points=points, wait=False)
        done += n
        doc_index += 1
        if doc_index % 10 == 0:
            rate = (done - have) / (time.perf_counter() - started)
            print(f"{done:>9} / {total}  ({rate:,.0f} chunks/s)", flush=True)
    await services.aclose()


async def clean() -> None:
    s = get_settings()
    load_handlers()
    services = Services.create(s)
    build = await services.live_build()
    await services.vectors.delete(build.collection, models.Filter(must=[match_value(TAG, True)]))
    dsn = s.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DELETE FROM documents WHERE doc_key LIKE 'synthetic:%'")  # cascades to versions and version_access
        c.execute("DELETE FROM blobs WHERE sha256 = 'synthetic'")
    print("synthetic points, versions and documents removed")
    await services.aclose()


async def recall() -> None:
    """ANN recall@10 of the dense leg against an exact scan, and latency, for access lists of different sizes."""
    s = get_settings()
    load_handlers()
    services = Services.create(s)
    build = await services.live_build()
    embedder = services.embedder_for(build)
    import json

    questions = [json.loads(line)["q"] for line in open("/eval/golden_paraphrase.jsonl", encoding="utf-8") if line.strip() and not line.startswith("#")][:60]
    vectors = [await embedder.embed_query(q) for q in questions]
    total = await services.vectors.count(build.collection, None, exact=False)
    print(f"collection {build.collection}: {total:,} points")
    scenarios = {
        "everyone (all points)": ["everyone"],
        "one small project": ["everyone", "none"],
    }
    for label, principals in scenarios.items():
        flt = models.Filter(must=[match_any("acl", principals), match_value("published", True)])
        if label.startswith("one small"):
            flt = models.Filter(must=[match_any("acl", principals), match_any("project_ids", ["synthetic-3"]), match_value("published", True)])
        visible = await services.vectors.count(build.collection, flt, exact=False)
        hit, t_ann, t_exact = [], [], []
        for v in vectors:
            t = time.perf_counter()
            ann = await services.vectors.client.query_points(build.collection, query=v, using=DENSE, query_filter=flt, limit=10, with_payload=False,
                search_params=models.SearchParams(hnsw_ef=128, quantization=models.QuantizationSearchParams(rescore=True, oversampling=s.qdrant_oversampling)))
            t_ann.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            exact = await services.vectors.client.query_points(build.collection, query=v, using=DENSE, query_filter=flt, limit=10, with_payload=False,
                search_params=models.SearchParams(exact=True))
            t_exact.append((time.perf_counter() - t) * 1000)
            hit.append(len({p.id for p in ann.points} & {p.id for p in exact.points}) / 10)
        print(f"{label:24} visible {visible:>10,}  recall@10 {np.mean(hit):.3f}  ANN p50 {np.percentile(t_ann, 50):6.1f} ms p95 {np.percentile(t_ann, 95):6.1f} ms  "
              f"exact scan p50 {np.percentile(t_exact, 50):7.1f} ms")
    await services.aclose()


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "inject":
        asyncio.run(inject(int(sys.argv[2])))
    elif command == "clean":
        asyncio.run(clean())
    else:
        asyncio.run(recall())
