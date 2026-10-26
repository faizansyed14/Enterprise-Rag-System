# Recommendations: accuracy, scale (2 → 5 → 10 TB) and open issues

> Written 2026-10-23 after reading the whole code base. **Nothing here has been implemented.** Each recommendation names the
> code it touches, so it can be planned and tested. Any setting a recommendation adds does not exist yet (see section 5).
> The figures are planning estimates: replace them with measurements from a pilot (`eval/scale_inject.py`, the Monitor page).
> Where a recommendation would use a paid service, a local alternative is named, because the default stays "local models only".

## 0. The short list (do these first)

| # | Recommendation | Why | Effort |
|---|---|---|---|
| 1 | **Fix the three access and privacy gaps** in [section 4](#4-security-findings-still-open): delete/edit rights (BFLA), local-only documents reaching cloud models through research tools, and non-admins sharing uploads with `everyone` | These are correctness bugs in access control, and access control is the product's hard rule | S |
| 2 | **Decide the embedder and the Qdrant shard count before the big load** | Both are fixed at collection creation. Changing either later means re-embedding 50–100 M chunks. | S (decision) |
| 3 | **Split workers by job kind** (parse/OCR, embed, control) and give embedding a GPU (local TEI or the model server on GPU) | First load at 10 TB on CPU-only embedding takes weeks ([§2.3](#23-how-long-the-first-load-takes)) | M |
| 4 | **Make `reconcile` incremental** | Today it makes one exact Qdrant count per version every day. At 5 M versions that is 5 M calls a day. | S |
| 5 | **Build a golden set from real questions (300+) and gate releases on it** | Every accuracy change below must be measured. The tools exist (`rag eval bench`, `eval/*.py`). | M |
| 6 | **Calibrate the "not in the library" gate** | `MIN_DENSE`, `MIN_SPARSE` and `MIN_COVERAGE` are 0 (off). Only the English rerank gate (0.02) is active. | S |
| 7 | **Upgrade the embedder and reranker to stronger multilingual local models** (measured on the golden set) | `multilingual-e5-small` (384-d) is the weakest link for Arabic and paraphrases | M |
| 8 | **Add document-level (two-stage) retrieval** for 10 TB | Chunk-only search over 100 M chunks loses recall on broad questions | L |
| 9 | **Re-chunk on a `CHUNKER_VERSION` change** | `rag index rebuild` re-embeds the old chunk rows, so a chunker change never reaches the new collection | S |
| 10 | **Upgrade flagged dependencies** (Pillow, PyPDF2 → pypdf, certifi) and add `pip-audit` to CI | Supply-chain hygiene before go-live | S |

S = a day or two, M = about a week, L = several weeks.

---

## 1. What the system already does well

Industry systems are measured against these. This code base already has most of the core:

- Hybrid retrieval: dense, BM25 sparse, an exact-identifier leg and an English-translation sparse leg. Weights per query
  type are tuned on near-duplicate corpora.
- Cross-encoder reranking with an adaptive blend; reranker and vector caches in Redis.
- Access control inside the vector filter **and** re-checked in Postgres (fail-closed).
- Structure-aware, page-bounded chunks with headers; labelled table rows; tables that continue across pages; small-to-big
  expansion.
- Near-duplicate families (MinHash LSH), content-addressed blobs, embedding reuse by content hash.
- Agentic answering:
  - a strategy planner (lookup, read_files, list_all map-reduce, multi_part);
  - a sufficiency check that searches again when evidence is missing;
  - a tool-using research loop.
- Every citation verified against its passage (fuzzy span plus numbers); groundedness scoring; exact arithmetic.
- Privacy shield (placeholder vault) for cloud models, `local_only` documents, and page and vision budgets.
- Blue/green index builds (collection per build, alias swap, rollback).
- A Postgres job queue with lanes, leases, dedupe, backoff and dead letters.
- Observability: traces, rollups, alerts, Prometheus, and a live per-answer Details panel.

The recommendations below are about what changes as the library grows 5–20× and how to push accuracy further.

---

## 2. Scaling from 2 TB to 10 TB

### 2.1 What the data turns into

Assumptions, as in [scaling.md](scaling.md):

- 2 MB average file;
- 70% of files have readable text;
- 15–30 chunks per indexed file;
- 20–30% exact duplicates, which are stored and embedded once;
- 15% of pages scanned.

| | 2 TB | 5 TB | 10 TB |
|---|---|---|---|
| Files | ~1 M | ~2.5 M | ~5 M |
| Versions (documents) indexed | ~0.7 M | ~1.75 M | ~3.5 M |
| **Chunks (Qdrant points)** | **10–20 M** | **25–50 M** | **50–100 M** |
| Pages (assuming 20 per file) | ~20 M | ~50 M | ~100 M |
| Scanned pages to OCR | ~3 M | ~7.5 M | ~15 M |

### 2.2 Memory and disk per store (default `multilingual-e5-small`, 384-d, int8 in RAM)

Per point, in RAM:

- the int8 vector, 384 B;
- HNSW links at m = 16, about 130 B;
- payload indexes on 15 fields (keyword and bool), roughly 150–300 B.

That is about **0.7–0.8 KB of RAM per chunk**. On disk:

- the float vector, 1.5 KB;
- the payload, including chunk text (`STORE_TEXT_IN_INDEX=true`), about 2–3 KB;
- the sparse vector, about 0.5 KB.

| Store | 2 TB | 5 TB | 10 TB |
|---|---|---|---|
| Qdrant RAM (e5-small, int8) | 8–16 GB | 20–40 GB | 40–80 GB |
| Qdrant RAM (bge-m3 1024-d, int8) | 15–30 GB | 40–75 GB | 80–150 GB |
| Qdrant RAM (bge-m3 1024-d, binary quantisation + rescoring) | 4–8 GB | 10–20 GB | 20–40 GB |
| Qdrant disk (vectors + payload + sparse) | 40–80 GB | 100–200 GB | 200–400 GB |
| Qdrant disk with `STORE_TEXT_IN_INDEX=false` | −50% | −50% | −50% |
| Postgres (pages + chunks + indexes) | 100–200 GB | 250–500 GB | 0.5–1 TB |
| Object store (originals) | 2 TB × 0.75 | 5 TB × 0.75 | 10 TB × 0.75 |

### 2.3 How long the first load takes

| Work | Rate (order of magnitude) | 2 TB | 5 TB | 10 TB |
|---|---|---|---|---|
| Embedding, e5-small, CPU (8 cores) | 50–100 chunks/s | 1.5–4.5 days | 3–12 days | 6–23 days |
| Embedding, e5-small, 1 GPU (TEI or ONNX CUDA) | 2,000–4,000 chunks/s | 1–3 h | 2–7 h | 4–14 h |
| OCR, RapidOCR, CPU | ~2 s per page per core | 3 M pages ≈ 70 core-days | ≈ 175 core-days | ≈ 350 core-days |
| Layout engine (`PDF_LAYOUT=deep`) | 0.2–0.5 s per page | ~2–6 core-days | ~5–15 | ~10–30 |

**Conclusion:**

- At 5–10 TB, CPU-only embedding and OCR are the bottleneck.
- Embedding needs a GPU.
- OCR needs either many cores (32–64 dedicated to OCR workers) or a GPU OCR engine.
- Steady state afterwards is small: only changed files are processed.

### 2.4 Recommendations by component

#### Qdrant (the store to size first)

1. **Choose the shard count at creation.** `QDRANT_SHARDS` is applied only in `VectorStore._create`
   (`providers/vectorstore/qdrant.py`). Recommended:
   - 2 TB: 1 node, 2 shards;
   - 5 TB: 3-node cluster, 6 shards, replication factor 2;
   - 10 TB: 3–5 nodes, 12 shards, replication 2.

   Over-sharding a little is cheap. Re-sharding later means a rebuild.
2. **Use binary quantisation for 1024-d models.** If the embedder moves to bge-m3 or another model of 1024-d or more, add
   a `QDRANT_QUANTIZATION=binary` option. Search with `rescore=True` and oversampling 2–3 (the retriever already passes
   `QDRANT_OVERSAMPLING`). This cuts RAM 4× compared with int8 at a small recall cost; measure it.
3. **Bulk-load mode.** During the first load:
   - set `indexing_threshold` high (or 0 = no indexing) and re-enable it afterwards;
   - upsert with `wait=False` for bulk lanes (today it is always `wait=True` in `VectorStore.upsert`);
   - remove `default_segment_number=2` (too few segments for 100 M points) and let the optimiser decide, or set it to the
     CPU count.
4. **Use `STORE_TEXT_IN_INDEX=false` above 5 TB.** The retriever already loads text from Postgres for the top
   candidates (`Retriever._hydrate`). This halves Qdrant disk and speeds payload I/O. Keep it `true` below 2 TB.
5. **Tenant-aware indexing.** If projects are the natural partition, mark the `project_ids` payload index with
   `is_tenant=true` (Qdrant ≥ 1.11). Filtered searches inside one project then read a co-located subset.
6. **Use HNSW `ef` and top-k that grow with the corpus.** `hnsw_ef = max(128, 2·DENSE_TOP_K)` is fine to 20 M. At
   50–100 M:
   - raise `DENSE_TOP_K` and `SPARSE_TOP_K` to 100–150;
   - raise `hnsw_ef` to 256;
   - check recall@100 against exact search on a sample (`EXACT_SEARCH_BELOW` already gives exact scans for small scopes).
7. **Remove one round trip per question.** `Retriever.search` calls `vectors.count(...)` on every query just to decide
   between exact and HNSW search. Cache that count per (principal set, filter) for a minute, or decide only from the
   filter (a project or document filter means a small scope).

#### Postgres

1. **More hash partitions.** `chunks` and `pages` are hash-partitioned ×16 (`db/engine.py`, migration 0001). At 50–100 M
   chunks, use 64 partitions (≈1–2 M rows each), so vacuum and index builds stay quick. This needs a migration with
   data copy, so do it before the big load.
2. **Separate the job table's churn.** Each document creates about 6–8 job rows, so a first load at 10 TB writes
   ~30–40 M rows.
   - Keep `KEEP_DONE_JOBS_DAYS` small (1).
   - Make sure autovacuum keeps up on `jobs`: `autovacuum_vacuum_scale_factor=0.01` on that table.
   - Consider partitioning `jobs` by status or by day if leasing slows down. Watch `ix_jobs_ready` bloat.
3. **PgBouncer** in transaction mode (`DB_PGBOUNCER=true` is already supported). Workers × concurrency plus API
   workers × pool size exceed `max_connections` quickly.
4. **Read replica** for the read-heavy answer path: `_hydrate`, `_expand`, `read_documents`, the aggregator,
   `documents_named_in` and the Documents page. These are read-only and tolerate a second of replica lag. The access
   re-check must stay on the primary.
5. **Fuzzy-name search needs an index.** `documents_named_in` and the aggregator run `ILIKE ANY('%stem%')` over file
   names and titles. At 5 M items that is a sequential scan. Add `pg_trgm` GIN indexes on `source_items.name` and
   `versions.title`.
6. **Backups:** base backup plus WAL archiving (pgBackRest or WAL-G) instead of `pg_dump` above 2 TB, as
   [operations.md](operations.md) notes.

#### Workers and the queue

1. **Separate worker pools per kind** (supported today through `rag worker --lanes … --kinds …`):

   | Pool | Kinds | Resources |
   |---|---|---|
   | control | `update_acl`, `refresh_acl`, `delete_item`, `release_version`, `janitor`, `monitor_tick`, `sync_principals` | small, always on, `--lanes control` |
   | parse | `fetch`, `extract` | CPU-heavy, many cores, about 3 GB RAM per process (PDF layout and OCR) |
   | index | `enrich`, `chunk`, `embed`, `publish` | light CPU; embedding offloaded to the GPU model server |
   | housekeeping | `gc_blobs`, `reconcile`, `maintenance`, `resume_stalled`, `eval_run` | 1 small worker |

   The current dev setup runs every kind in two identical 2.2 GB workers. Worker-1 restarted from running out of memory
   on large files, and raising it to about 2.8 GB was already suggested. Separate pools let parse workers get large
   memory without paying for it everywhere.
2. **Faster embedding.** Run the model server with the ONNX Runtime CUDA provider, or put Hugging Face
   **text-embeddings-inference** (TEI, open source, local) in front of the same models. Then raise `EMBEDDER_BATCH`
   to 64–256.
3. **Back-pressure on syncs.** `sync_source` enqueues a `fetch` for every changed item at once. For a 5 M-file first
   sync, cap how many queued `fetch` jobs one source may have (for example 50,000) and let the sync resume from its
   cursor, so the queue table stays small.
4. **Incremental `reconcile`** (`pipeline.reconcile`). Today it counts the points of **every** live and deleted version
   every day.
   - Run it only for versions whose `updated_at` is later than the last run.
   - Once a week, run a random sample (for example 1%) of all versions.
   - Or use Qdrant's facet API on `version_id` to count many versions in one call.
   - Add `RECONCILE_EVERY_HOURS`.
5. **Raise `gc_blobs` throughput.** It removes at most 500 blobs every 6 hours (≈2,000 a day). After a large re-org or
   source removal at 10 TB, that is too slow. Loop until done, with a time budget per run.

#### Object store

At 5 TB and above, use `OBJECT_STORE_URL=s3://…` on MinIO (self-hosted, free), Ceph or a cloud bucket, with versioning
and replication. A single local volume of 10 TB is hard to back up and to move. The code already supports S3
(`providers/storage/objects.py`).

#### Model server

- Run separate replicas for embedding (bulk load) and reranking (interactive). This keeps answer latency stable while
  a bulk load runs.
- Rerank latency grows with `RERANK_TOP_N`. On a GPU, reranking 50 candidates costs about the same as 15 on CPU, and
  that is one of the accuracy levers in §3.

### 2.5 Keeping accuracy as the library grows

More documents means more near-duplicates and more look-alike distractors. The same question gets harder at 10 TB than
at 2 TB. What keeps it accurate:

1. **Filters before similarity.** The biggest measured gain in this code base is naming or filtering the project (97%
   hit@1 for every weight set, [retrieval.md](retrieval.md)). Extend the router to extract more filters:
   - discipline;
   - document type (today only a +0.08 boost);
   - date ranges ("RFIs from March");
   - revision ("latest revision");
   - source.

   An LLM self-query extraction (one small call, JSON out, validated against known values in Postgres) is the industry
   pattern.
2. **Two-stage retrieval** (§3.4): find the right documents first, then the chunks inside them.
3. **Family demotion and version state** already remove most duplicates. Tune `NEAR_DUPLICATE_JACCARD` (a constant, 0.75, in `text/minhash.py`; worth making a setting) on a
   sample of the real library.
4. **Re-measure** recall@k on a fixed golden set at each size step (1 M, 10 M, 50 M chunks). `eval/scale_inject.py` adds
   synthetic distractors for this.

---

## 3. Making answers more accurate (industry practice)

### 3.1 Measure first

- **Golden set from real questions:** 300–1,000, tagged by type (identifier, paraphrase, table, list-all, Arabic,
  unanswerable). The formats and runners exist: `evaluation/golden.py`, `retrieval_bench.py` and `eval/*.jsonl`.
- **Answer metrics on top of retrieval metrics:**
  - faithfulness (already partly covered by groundedness);
  - answer correctness (facts present or forbidden, already in `runner.score_answer`);
  - context precision and recall (RAGAS-style), judged by a local model.
- **Feed production feedback back.** `messages.feedback` (thumbs) is stored. Turn thumbs-down answers into
  golden-set candidates every week.
- **A regression gate in CI.** No change to weights, models or prompts merges if page@1 or answer correctness drops by
  more than one point.

### 3.2 Embeddings and reranking (local, no paid API)

| Today | Candidates to measure | Expected effect |
|---|---|---|
| `multilingual-e5-small` (384-d) | `bge-m3` (1024-d, dense + sparse + multi-vector), `multilingual-e5-large-instruct`, `Qwen3-Embedding-0.6B`, `jina-embeddings-v3` | Clearly better Arabic and paraphrase recall. Costs 2.7× RAM (use binary quantisation, §2.4). |
| `mmarco-mMiniLMv2-L12` reranker on the top 15 | `bge-reranker-v2-m3` (multilingual, already in the catalogue), `Qwen3-Reranker-0.6B`; rerank the **top 30–50** on GPU | Better ordering for paraphrases and tables, where the measured gains already come from reranking |
| BM25 sparse (`bm25v2`) | Keep BM25 (identifiers, exact words), **add** bge-m3's learned sparse vector or SPLADE as a further leg | Better term matching for morphology and synonyms |
| no late interaction | bge-m3 or ColBERT multi-vectors in a Qdrant multivector field, used only to re-score the top 100 | Most of a cross-encoder's precision at a fraction of its cost |

Any embedder change goes through the existing blue/green rebuild. Measure on the golden set before `index activate`.

### 3.3 Better chunks and context

1. **Contextual chunk headers.** Every chunk already carries `Project | Doc number | Title | Section path`, a cheap form
   of contextual retrieval. The full technique (Anthropic's contextual retrieval) adds one or two sentences generated
   per chunk that place it in its document. That costs one model call per chunk: 50–100 M calls at 10 TB. Do it only
   with a **local** small model, and only for document types where it is measured to help (specifications, contracts).
   This would add a setting, `CONTEXTUAL_CHUNKS=off|local`.
2. **Per-type chunking.** Spreadsheets and BOQs are better served as rows with column labels, which is already done.
   Large registers should go to **structured lookup**: put tables into Postgres and answer "list/total/filter" questions
   with generated SQL over that one table, instead of reading 2,000-row pages. `read_files` and the aggregator are the
   current answers to this, and they stop scaling past ~400k characters.
3. **Parent-document retrieval.** `_expand` adds ±1 neighbour on the same page under the same heading. For
   specifications and contracts, expanding to the whole clause or section (bounded by tokens) usually helps more than
   ±1 chunk.
4. **Drawings.** DWG and DXF sheets are now read (text, title block, layers, blocks, and a vision description when
   `VISION` is on). Add a chunk per drawing that lists the sheet register (number, title, revision) so "which drawings
   show X" questions hit one chunk.

### 3.4 Retrieval for very large libraries

1. **Two-stage (document → chunk) retrieval.**
   - Keep a second, small Qdrant collection with one vector per document (or per section). Use a summary or the first
     pages plus the title block, embedded at `publish`.
   - Stage 1 finds the top 50–200 documents.
   - Stage 2 searches chunks filtered to those `document_id`s. The `document_ids` filter already exists in
     `SearchFilters`.

   This is how 100 M-chunk systems keep broad questions accurate, and it makes the `list_all` candidate step cheaper too.
2. **Query expansion for paraphrases.**
   - Multi-query: 2–3 rewrites by `LLM_ROUTER`, searched in the same batch.
   - HyDE: embed a hypothetical answer.

   Today the router model only runs for follow-ups and translations. Gate this on the question type (`question`, not
   `identifier`) and measure. It costs one small call.
3. **Use the reference graph.** `doc_references` records "refer to drawing A-102" links. For multi-hop questions, add the
   referenced documents' best passages to the evidence after the first search. The `open_reference` tool does this only
   inside the research loop.
4. **Calibrate the relevance gate.** Run `eval/gate_calibrate.py` on the golden set (unanswerable questions included) and
   set `MIN_DENSE`, `MIN_SPARSE` and `MIN_COVERAGE` for a chosen loss rate. Today the gate is effectively the English
   rerank threshold only, so off-topic questions in Arabic, and short ones that skip reranking, are always answered from
   whatever was found.

### 3.5 Answer generation

- **Use a stronger model for hard and long-context questions.** `read_files` reads up to 40 documents / 400k characters,
  so `LLM_ANSWER_HARD` should be a long-context model. When the model is local, the limit already drops to 3 docs / 60k.
- **Fewer passages when they are decisive; more when they are spread.** `EVIDENCE_PASSAGES` is fixed at 12. Make it
  depend on the strategy and on the relevance signals.
- **Citations:** keep verification strict. Optionally reject or regenerate an answer when fewer than half of its
  citations verify, instead of only adding the "unverified" note.

---

## 4. Security findings still open

From the security audit in this conversation, plus what this read-through added. All are verified in the code.

| Severity | Finding | Where | Fix |
|---|---|---|---|
| **High** | **BFLA: any user who can *read* a document can delete it, change its project, or reprocess it.** This covers documents shared with `everyone`. | `api/routers/documents.py`: `delete_document`, `bulk_delete`, `update_document` use `_visible_versions` and `reprocess_document` uses `_version`: read checks only. A reprocess with `mode=vision` also spends vision budget. | Allow these actions for admins and for the uploader of the item (`source_items` in `Uploads/…` created by that user). Treat connector-sourced documents as read-only for non-admins. |
| **High** | **Local-only documents can reach a cloud model through the research tools.** `search` and `search_within` respect `exclude_local_only`, but `read_pages` and `open_reference` resolve documents with `_resolve`, which checks access but not `documents.local_only`. | `answering/tools.py` `_resolve` | Add `AND NOT d.local_only` when `plan.filters.exclude_local_only` is set, as `strategy.documents_named_in` and the aggregator already do. |
| Medium | **Non-admins can share uploads with `everyone`.** The admin-only check is unreachable, because every user's principals include `everyone`. | `api/routers/uploads.py` `_target` | Check `principal == EVERYONE and not user.is_admin` **before** the membership test. |
| Medium | `openai_compat` counts as a *local* provider, so the privacy shield is not applied. If `OPENAI_COMPAT_BASE_URL` points at a cloud service, bank values leave unmasked. | `config/roles.py` `LOCAL_PROVIDERS` | Treat it as local only when the base URL is a private address, or add `OPENAI_COMPAT_IS_LOCAL`. |
| Medium | Dependencies: Pillow 12.3.0 (advisories reported by pip-audit), PyPDF2 3.0.1 (unmaintained, used by the layout engine; move to `pypdf`), certifi | `backend/uv.lock` | Upgrade, re-lock, and run `pip-audit` in `scripts/test.sh` and CI. |
| Medium | Server-side request forgery: admin-configured connectors (`web`, `webdav`, `openai_compat` base URL) can be pointed at internal addresses. | `connectors/legacy/web.py` and the other HTTP-based legacy connectors | Refuse private and link-local ranges unless allow-listed. Admin-only today, which limits the risk. |
| Low | ClamAV is off by default (profile `clamav`, `CLAMAV_URL` empty) | `docker-compose.yml` | Turn it on wherever users upload files from outside. |
| Low | Production settings: `APP_ENV=prod`, `COOKIE_SECURE=true` (**correction:** the setting is `COOKIE_SECURE`, not `SECURE_COOKIES` as the earlier audit report said), HTTPS, `METRICS_TOKEN` | `.env` | Use the hardening checklist in [security.md](security.md). |
| Note | LibreDWG (`dwg2dxf`) is GPLv3. It runs as a separate program, so the application is not linked to it, but shipping the image to third parties carries GPL source-offer duties for that binary. | `backend/Dockerfile` | Legal check before distributing images outside the company. |

---

## 5. Smaller improvements found while reading

| Item | Where | Suggestion |
|---|---|---|
| A chunker change does not re-chunk on rebuild | `ingestion/index_builds.start_rebuild` queues `embed` with `build_id`; `embed` reads the existing `chunks` rows | When `chunker_version` differs from the live build, queue `chunk` with `force` (as `rag reprocess` does) and let the dual write fill the new build |
| Provider cooldowns are written but not read | `jobs/rate_limits.check_cooldown` has no callers | Call it before cloud model calls in bulk jobs (vision, enrich), so one 429 pauses every worker instead of each finding out on its own |
| `restricted_local_only` is always 0 | `retrieval/retriever.py` | Count local-only hits that were excluded, and tell the user "N documents could not be used with a cloud model" |
| `GUARDRAILS` does nothing yet (`ANALYZER_VERSION` was removed on 2026-10-23) | `config/settings.py` | Wire `GUARDRAILS` in (at least make `off` work), or document it as reserved ([dead-code.md](dead-code.md#2-unused-today-kept-on-purpose-useful-later)) |
| `gc_blobs` is capped at 500 per 6 h | `ingestion/pipeline.gc_blobs` | Loop with a time budget |
| Every search counts points first | `Retriever.search` | Cache the count, or decide from the filter |
| Fuzzy file-name matching uses `ILIKE '%…%'` | `strategy.documents_named_in`, `aggregate._candidates` | `pg_trgm` GIN indexes |
| Worker memory | `docker-compose.dev.yml` worker `mem_limit: 2200m` | 2.8–3 GB for parse workers; keep others small (§2.4) |
| Dead code | [dead-code.md](dead-code.md) | Clean-up done on 2026-10-23; three unused files still need deleting |

---

## 6. A suggested order

1. **Week 1:** the security fixes (§4 High and Medium), dependency upgrades, dead-code clean-up, and the chunker-rebuild
   fix. Build the golden set from real questions.
2. **Week 2:** calibrate the gate; measure bge-m3 and bge-reranker-v2-m3 against the current models on the golden set;
   decide the embedder.
3. **Week 3:** production topology:
   - Qdrant cluster with the chosen shard count and quantisation;
   - Postgres with PgBouncer, a replica and 64 partitions;
   - MinIO;
   - a GPU embedding service;
   - separate worker pools;
   - incremental `reconcile`.
4. **Pilot at 5% (≈250–500 GB):** measure chunks per file, OCR share, throughput, recall@k and latency. Correct §2.
5. **Full load in priority order.** The library is searchable while it fills.
6. **Then the accuracy upgrades** (§3.3, §3.4), each behind a setting and measured: two-stage retrieval, multi-query,
   parent-section expansion, and structured lookup for registers.
