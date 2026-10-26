# Scaling to 5 TB (and 10 TB)

> **Status.** The architecture is designed for this size and the scaling mechanisms are exercised in tests, but this
> build has **not been run against 5 TB**. The figures below are planning estimates from stated assumptions, not
> measurements. Run a 5% pilot (250 GB, a representative sample) first and replace the assumptions with your numbers.
>
> **Updated 2026-10-23:** a side-by-side sizing for 2, 5 and 10 TB is in
> [recommendations.md §2](recommendations.md#2-scaling-from-2-tb-to-10-tb). It covers RAM, disk and first-load time, and
> lists what to change before a load of that size: Qdrant shards and quantisation, Postgres partitions and replica,
> GPU embedding, separate worker pools, incremental reconcile, and two-stage retrieval. Three facts from the code
> matter here:
>
> - `QDRANT_SHARDS` takes effect only when a collection is created.
> - `chunks` and `pages` have 16 hash partitions.
> - The daily `reconcile` makes one Qdrant count per version.

## From terabytes of files to a search index

Files are not what is indexed; text is. Drawings (CAD exports, large PDFs, images) are large but carry little text;
specifications and reports are small and text-dense. Assumptions for planning:

| Assumption | Value | Why it matters |
|---|---|---|
| Average file size | 2 MB | ⇒ about 2.5 million files |
| Files with extractable or OCR-able text | 70% | The rest (photos, models) are stored, not indexed |
| Average text chunks per indexed file | 15–30 | At 380 tokens per chunk |
| **Chunks** | **25–50 million** | Sizing driver for Qdrant and Postgres |
| Duplicate files (same hash) | 20–30% | Extracted and embedded once |
| Scanned pages needing OCR | 15% of pages | Sizing driver for ingestion time |

## Storage

| Store | Estimate | Notes |
|---|---|---|
| Originals (object store) | 5 TB + 10% | S3-compatible (MinIO, Ceph, cloud) or a large volume; content-addressed, so duplicates cost nothing |
| Qdrant vectors, `multilingual-e5-small` (384-d) | ~25–50 GB int8 in RAM + 75–150 GB float on disk | With `QDRANT_QUANTIZATION=int8`, `QDRANT_ON_DISK=true` |
| Qdrant vectors, `bge-m3` (1024-d) | ~65–130 GB int8 in RAM + 200–400 GB on disk | Roughly 2.7× the small model |
| Qdrant sparse + payload | 30–80 GB | Payloads carry the text for expansion; keep on NVMe |
| Postgres (pages, chunks, ACLs, jobs) | 300 GB – 1 TB | Chunk text is the bulk; traces are partitioned and expire (`MONITOR_RETENTION_DAYS`) |
| Page images | on demand, cached | Rendered server-side from the original, cached under `/data/cache` |

Qdrant memory is the figure to size first: quantised vectors plus HNSW graphs must fit in RAM for fast search; the full
vectors and payload sit on disk. Plan 64–128 GB RAM for a single Qdrant node at 25–50 million 384-d chunks, then add
shards (`QDRANT_SHARDS`) before adding RAM beyond that.

## Throughput: how long does the first load take?

Ingestion cost is dominated by OCR and embedding, both of which scale linearly with workers.

| Work | Rough cost per unit (CPU) | For 50 M chunks / 15% OCR pages |
|---|---|---|
| PDF text extraction | milliseconds per page | negligible |
| RapidOCR on a scanned page | 1–3 s per page per core | The long pole; use `OCR=azure_di/...` or add cores |
| Embedding with `multilingual-e5-small` | 30–100 chunks/s per 8 cores | ~6–20 days on one 8-core node |
| Embedding with a GPU or a cloud embedder | 500–5,000 chunks/s | hours to a couple of days |

Time scales down with `WORKER_REPLICAS × WORKER_CONCURRENCY`. For the first load, use a GPU for the model server or a
cloud embedder, then switch back if you prefer local models for steady state (a switch means an index rebuild). The
numbers above are order-of-magnitude figures; measure them in the pilot with `scripts/status.sh` and the Monitor page
(documents per hour, queue depth by lane).

Steady state is much lighter: only changed files are re-processed (delta sync for SharePoint and OneDrive, size and
modification time for folders and shares).

## What scales how

| Component | Scale-out | Limit to watch |
|---|---|---|
| `redis` | `REDIS_MAXMEMORY` (cache size, LRU), `REDIS_MEM` | Nothing to scale at this size: one instance serves every API and worker replica. At very large scale use a managed Redis and set `REDIS_URL` |
| `backend` | `API_WORKERS`, more replicas behind nginx | Postgres connections (`DB_POOL_SIZE`; use PgBouncer with `DB_PGBOUNCER=true`) |
| `worker` | `WORKER_REPLICAS`, `WORKER_CONCURRENCY` | Model server throughput, provider rate limits (handled by cooldowns) |
| `model-server` | More replicas behind a service name, GPU build | CPU threads (`MODEL_THREADS`) |
| Postgres | Bigger box, read replica for reporting; partitioned monitoring tables | Chunk table size, queue polling (`FOR UPDATE SKIP LOCKED`, lane-indexed) |
| Qdrant | `QDRANT_SHARDS`, `QDRANT_REPLICAS`, on-disk vectors, quantisation | RAM for quantised vectors and HNSW |
| Object store | S3-compatible, effectively unbounded | Network throughput during first load |

The queue is Postgres on purpose: it gives transactional enqueue (a document and its jobs commit together), simple
inspection, and throughput well beyond what OCR and embedding can consume (thousands of jobs per second against
tens of jobs per second of real work). If an installation ever outgrows it, the queue interface in `rag.jobs.queue` is
the only thing to replace.

## Query latency at scale

Search latency depends on the number of **visible** chunks per query, not on the library size, because the filter is
applied inside the HNSW search. Expected on a correctly sized Qdrant node: tens of milliseconds for the four legs,
tens to a few hundred for reranking 40 candidates on CPU, plus the answer model (1–4 s to first token for Haiku-class
models, streaming after that). On the demo library the whole non-LLM path takes ~1.4 s on 2 CPUs; at 5 TB expect the
retrieval part to be dominated by reranking, not by the index.

## First-load checklist

1. Run the pilot; confirm chunks per file, OCR share and embeddings per second.
2. Put Postgres and Qdrant on NVMe (see "Mounts and large disks" below for the bind mounts).
3. Decide the embedder **before** the full load; changing it later means re-embedding everything (it can run in the
   background without downtime, but it costs the same time again).
4. Add sources in priority order; the library is searchable as it fills. Bulk work yields to interactive uploads.
5. Watch the Monitor page: queue depth, dead letters, provider errors, and spend if paid OCR or vision is on.
6. Take a backup after the first load completes (`scripts/backup.sh --with-index`).

## Mounts and large disks

Production is `docker-compose.yml` alone. To put the large data on dedicated disks, more ingestion capacity, or a mounted share
for a "Server folder" source, add a file of your own next to it, for example `docker-compose.local.yml`, and start with
`docker compose -f docker-compose.yml -f docker-compose.local.yml up -d` (create the folders first: Postgres runs as uid 70,
Qdrant as root, the backend as uid 10001):

```yaml
volumes:
  pgdata:     { driver: local, driver_opts: { type: none, o: bind, device: /mnt/fast-ssd/rag/postgres } }
  qdrant_data: { driver: local, driver_opts: { type: none, o: bind, device: /mnt/fast-ssd/rag/qdrant } }
  data:       { driver: local, driver_opts: { type: none, o: bind, device: /mnt/bulk/rag/data } }

services:
  worker:                      # more ingestion capacity
    deploy: { replicas: 4 }
    environment: { WORKER_CONCURRENCY: "6" }
    volumes: [ "data:/data", "/mnt/nas/projects:/sources/projects:ro" ]   # a NAS folder for a "Server folder" source
  backend:                     # the API needs the same mount for the Test button on the Sources page
    volumes: [ "data:/data", "/mnt/nas/projects:/sources/projects:ro" ]
```
