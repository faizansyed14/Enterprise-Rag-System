# Codebase map: what every file does

This map is a reference you can search, ordered by folder. The line counts are from 2026-10-23. ⚠ marks a file or part
that is unused today: either kept on purpose for a planned improvement, or (empty packages, `crawl.py`) waiting to be deleted. Details in [dead-code.md](dead-code.md). For how the files work together, read
[architecture.md](architecture.md) and [data-flows.md](data-flows.md).

## Repository root

| Path | Purpose |
|---|---|
| `README.md` | Overview, quick start, documentation index |
| `commands.md` | Every operational command (start, stop, logs, backup, users, tests, dev compose) |
| `Makefile` | Thin `make` targets wrapping `scripts/*.sh` |
| `docker-compose.yml` | Production stack: postgres, qdrant, redis, model-server, backend, worker ×`WORKER_REPLICAS`, scheduler, frontend, nginx; profiles `monitoring` (Prometheus, Grafana) and `clamav` |
| `docker-compose.dev.yml` | Small-machine overrides: memory and CPU caps, localhost ports, source bind-mounts, paid keys blanked except OpenRouter, test Postgres and Qdrant (`profile test`) |
| `.env.example` | Every setting with its default (copied to `.env` by `scripts/start.sh`) |
| `pytest.ini` | Test paths and markers |
| `evalcorpus/` | Evaluation documents (arxiv, core4, docx, drawings, irs, nist, uae), mounted read-only at `/sources/eval` in dev |
| `eval/` | Ad-hoc evaluation scripts (see the [eval](#eval-scripts) section) |

## `backend/` packaging

| Path | Purpose |
|---|---|
| `backend/Dockerfile` | Multi-stage image: uv install from `uv.lock`; LibreDWG 0.14 build stage (`dwg2dxf`, sha256-pinned, `LIBREDWG_JOBS=1`); optional LibreOffice (`INSTALL_LIBREOFFICE`); runtime adds libcairo2 and DejaVu fonts; non-root uid 10001 |
| `backend/pyproject.toml`, `uv.lock` | Dependencies (FastAPI, SQLAlchemy+psycopg3, qdrant-client, pypdfium2, pdfplumber, rapidocr, python-docx/openpyxl/python-pptx, ezdxf, cairosvg, argon2 …) and extras; console script `rag` |
| `backend/alembic.ini`, `alembic/env.py` | Migration runner (sync psycopg; ignores partition child tables) |
| `backend/alembic/versions/0001_initial_schema.py` | The single squashed migration: all 32 tables, partitions, indexes |

## `backend/src/rag/` top level

| File | Purpose |
|---|---|
| `__init__.py` | Version string |
| `services.py` | `Services` container passed to every request and job. Holds the settings, `ModelHub`, `VectorStore`, `ObjectStore`, `BM25Analyzer` and `MetadataExtractor`. Provides the lazy chunker (exact tokenizer from the model server), cached `builds()`, `live_build()` and `write_builds()`, `known_projects()`, and `on_job_failed()`, which marks the item, blob or version failed with the reason. |
| `cache.py` | Optional Redis cache: query vectors, rerank scores, answers, epoch counter, rate-limit windows and stats. 15 s circuit breaker, `rag:` key prefix. |
| `errors.py` | `PermanentError`, `TransientError`, `RateLimited(retry_after)`, `NotReady`, `LeaseLost`, `ConfigurationError` |
| `logging_setup.py` | Text or JSON logs. PII-masking filter when `MASK_PII_IN_LOGS`. |
| `cli.py` | `rag` command: `api`, `worker`, `scheduler`, `db upgrade`, `check`, `users create/set-password`, `sources list/add-folder/purge/sync`, `reprocess`, `reparse`, `demo seed`, `ask`, `index status/rebuild/activate/rollback`, `cache stats/clear`, `eval run/bench`, `models list/pull/test`, `jobs stats/retry-dead`, `privacy check/calls` |

### `config/`

| File | Purpose |
|---|---|
| `settings.py` | Every setting, read with pydantic-settings from the environment or `.env`. It finds `.env` by walking up from the working directory, or from `RAG_ENV_FILE`. Defaults: fusion weights per query type, the roles, retrieval knobs (top-k, rerank blend, boosts, gates), answer strategy limits (`READ_FILES_MAX_DOCS` 40, `READ_FILES_BUDGET_CHARS` 400k, aggregate limits), vision limits and fallback, ingestion (chunk sizes, OCR, layout, budgets, worker), uploads (`UPLOAD_ZIP_MAX_MB`, `SKIP_EXTENSIONS`), privacy, Redis and `CHAT_DETAILS`. Production checks run on startup. `GUARDRAILS` is reserved and not read yet. |
| `roles.py` | Parses a role line `provider/model[@dims][?k=v]` into a `RoleSpec`. Lists the 12 roles and their capabilities, which roles the UI may switch, and which providers count as local. |
| `startup_checks.py` | The `rag check` and startup checks: role keys, local models, DB migration, Qdrant alias, object store write probe, index rebuild needed, production cookie and ZDR rules, Redis. |

### `db/`

| File | Purpose |
|---|---|
| `engine.py` | Async SQLAlchemy Core engine (psycopg3) with `statement_timeout` and PgBouncer mode. `transaction()` and `connection()`. Partition upkeep: 16 hash partitions for pages and chunks, daily partitions for traces and spans, created ahead and dropped after retention. `initial_partition_ddl` builds the first partitions for the Alembic migration. |
| `schema.py` | SQLAlchemy `Table` definitions for all 32 tables (see [architecture §6](architecture.md#6-data-model-postgres-32-tables-one-squashed-migration-0001_initial_schemapy)) |
| `repositories/__init__.py` | ⚠ Empty package |

`domain/__init__.py` is also an empty package (⚠).

### `jobs/`

| File | Purpose |
|---|---|
| `queue.py` | Postgres queue with three lanes (control, interactive, bulk). `enqueue` and `enqueue_many` merge on `dedupe_key` while a job is active. `lease` uses FOR UPDATE SKIP LOCKED. Also: heartbeat, progress, complete, fail (backoff or dead), release, `requeue_expired`, retry, cancel, `cleanup_finished`, stats. `get_job` is kept for future use. |
| `worker.py` | `@handler(kind)` registry, `JobContext` (payload, checkpoint, enqueue) and the `Worker` loop: concurrency slots, lease, a heartbeat task, cooldowns on `RateLimited`, final-failure hook, and an `until_idle` mode. ⚠ `registered_kinds` is unused. |
| `handlers.py` | Imports the modules that register handlers: pipeline, security.sync, monitoring.tick and evaluation.runner. |
| `retry_policy.py` | Classifies exceptions as rate_limited, transient or permanent. Backoff is 10 s·2ⁿ, capped at 1 h, with jitter. Parses `Retry-After`. |
| `rate_limits.py` | Provider cooldowns (`set_cooldown`) and daily page budgets (`take_budget`). ⚠ `check_cooldown` and `Pacer` are never called. |
| `scheduler.py` | `PERIODIC` table, per-slot dedupe, due source syncs, Entra principal syncs. `_HANDLED_SLOTS` makes each periodic slot run once. |

### `ingestion/`

| File | Purpose |
|---|---|
| `pipeline.py` | All ingestion and housekeeping handlers: `sync_source`, `fetch`, `extract`, `enrich`, `chunk`, `embed`, `publish`, `update_acl`, `refresh_acl`, `delete_item`, `release_version`, `resume_stalled`, `gc_blobs`, `reconcile`, `finish_rebuild`. Helpers include `refresh_version_access`, `settle_document`, `assign_family` (MinHash LSH), `point_payloads`, `_dense_vectors` (vector reuse by hash) and `purge_unused_file`. |
| `formats.py` | Format detection (magic bytes first, then extension). `SUPPORTED` and `CAD_EXTENSIONS`. Readers for docx, xlsx/xlsm (header detection, 2,000 rows per page), csv, pptx, html, eml, msg, zip (bomb limits), text and images (OCR). Also legacy Office through LibreOffice, `read_cad`, `skip_reason`, and the `parse_document` dispatcher. |
| `formats_extra.py` | Native readers for RTF, ODT and Primavera XER. |
| `cad.py` | DWG and DXF: magic-byte detection, `dwg2dxf` conversion (180 s timeout), ezdxf recover-read, sheets (model space plus layouts), sheet text (text, mtext, dimensions, attributes, layers, blocks), `title_block_from`, gap-based extents trimming, rendering to SVG and then PNG (cairosvg), `render_page` for the viewer (black on white), a high-resolution colour render for the analysis, `read_full`, `prime_previews`, and `describe_sheets` (the short description, with budget and privacy checks). |
| `cad_analysis.py` | The full AutoCAD sheet analysis: the CAD system and user prompts, cropping and tiling a drawn sheet (`make_tiles`), the one-request message (`build_messages`), retry on an empty reply, report front matter into the title block, and `analyze_sheets` (budget and privacy checks, fallback to the short description). |
| `chunker.py` | Structure-aware chunker: headings become the section path, chunks stay within a page, tables become labelled rows, drawings get a title-block chunk, every chunk gets a header, sentence overlap, small chunks are merged, chunk ids are uuid5, and each chunk has a content hash. ⚠ `Chunk.identifiers` is computed but not stored; `embed` computes it again. |
| `index_builds.py` | `configured_build`, `ensure_live_build`, `status`, `start_rebuild`, `activate` (alias swap and epoch bump), `rollback` |
| `ocr_cache.py` | `ocr_cache` get and put by image hash and engine |
| `archive.py` | ZIP upload staging: `extract` (nested ZIPs, manifest, owner, protected flag), `load`, `remove`, `sweep` |
| `protection.py` | Detects password-protected files: PDF user password or rights, Office encryption (msoffcrypto/OLE), ZIP encryption flag. Errors use the prefix `Protected: `. |

### `metadata/`

| File | Purpose |
|---|---|
| `extract.py` | `MetadataExtractor`: field values ranked by trust (source fields > ISO 19650 name > title block > content > filename > path > properties). `fill_gaps_with_llm` for `LLM_ENRICH`. |
| `file_naming.py` | Metadata from file names (ISO 19650, "A-101 Rev C – title", RFI-023, spec sections) |
| `identifiers.py` | `identifier_key` folding (A-101 = A101 = a 101; RFI #023 = RFI-23; 03 30 00 = 033000), `extract_identifiers`, `label_keys` ("Table 11") |
| `path_rules.py` | Folder rules (`PATH_RULES_FILE` YAML, known projects) |
| `references.py` | Cross-references ("refer to drawing A-102") stored as `doc_references` |
| `title_block.py` | Label and value pairs in drawing title blocks (English and Arabic). |
| `vocabularies.py` | Document types (ISO 19650 codes plus English and Arabic words) and CSI divisions. ⚠ `division_for_spec` is unused. |
| `dates.py` | Day-first date parsing. ⚠ `find_document_date` and `to_iso` are unused. |

### `text/`

| File | Purpose |
|---|---|
| `bm25.py` | BM25 analyzer producing Qdrant sparse vectors. Qdrant applies IDF. English and Arabic stemming, stop words, identifier tokens, murmur3 ids. |
| `language.py` | Detects the language from script ratios and stop words. Checks whether the reranker reads that language. |
| `normalize.py` | `clean_text`, Arabic folding, digits. (`has_arabic` is kept for future use.) |
| `ligatures.py` | Repairs glyph codes that fonts lost (ff, fi, tt, Qu …) by testing candidates against a dictionary. |
| `minhash.py` | 64-value MinHash, 16 LSH bands, similarity estimate |
| `ocr_spacing.py` | Puts back spaces OCR dropped (wordninja), leaving identifiers alone |
| `tokens.py` | Token counting: exact with tokenizer.json, otherwise estimated. ⚠ `count_many` and `counter_for` are unused. |

### `retrieval/`

| File | Purpose |
|---|---|
| `query_router.py` | Rule-based plan (type, identifiers, project filter, doc-type boost, hard flag, follow-up merge). Optional `LLM_ROUTER` refinement (standalone question, English translation, complexity) within a timeout. |
| `retriever.py` | Hybrid search (see [data-flows §3](data-flows.md#inside-retrieversearch)): legs, fusion, family demotion, name and phrase boosts, adaptive rerank, page boost, Postgres access re-check, documents, relevance gate, passage choice, small-to-big expansion, inspector details. |
| `relevance.py` | Signals (dense, sparse, coverage, rerank, identifier) and `is_off_topic` |
| `types.py` | `SearchFilters`, `QueryPlan`, `Candidate`, `Passage`, `DocumentHit`, `RetrievalResult`. ⚠ `restricted_local_only` is always 0. |

### `answering/`

| File | Purpose |
|---|---|
| `agent.py` | `Answerer.answer`: the whole question flow as SSE events (guardrails, cache, plan, model choice, search, strategy, research loop, sufficiency check, streamed answer with fallback, arithmetic, citations, groundedness, done, cache). Also writes the inspector notes for the Details panel. |
| `strategy.py` | Strategy planner prompt (`lookup`, `read_files`, `list_all`, `multi_part`), `fair_shares` budget split, `read_documents` (one SQL with a running sum), `search_parts`, `documents_named_in`, `check_sufficiency` |
| `aggregate.py` | Map-reduce for "list all …" questions: subject-stem candidates plus search results, batched model reading, page verdicts, passages |
| `tools.py` | Research tools for tool-capable models: `search`, `search_within`, `read_pages`, `open_reference`. All of them check access. |
| `citations.py` | Verifies every `[n]` against passage n (RapidFuzz span, threshold 60, numbers must appear or be derived) |
| `grounding.py` | Groundedness score from the judge, with a lexical fallback; uncited long sentences count against the answer |
| `arithmetic.py` | Works out unfinished sums exactly (Decimal) and recognises derived numbers |
| `prompts.py` | `ANSWER_SYSTEM`, `RESEARCH_SYSTEM`, evidence formatting in tags, NOT_FOUND and CLOSEST texts (English and Arabic) |

### `api/`

| File | Purpose |
|---|---|
| `app.py` | `create_app`: lifespan (services, bootstrap admin from `BOOTSTRAP_ADMIN_*`), CORS, security headers and timing middleware, error handlers, routers under `/api/v1`, `/metrics` |
| `main.py` | `app = create_app()` for uvicorn |
| `deps.py` | Session from the cookie, Origin check, CSRF double submit, `AdminDep`, `PrincipalsDep`, paging |
| `sse.py` | Server-sent event formatting |
| `routers/auth.py` | Login (rate-limited, lockout), logout, me, config, Entra OIDC login and callback |
| `routers/chat.py` | `POST /chat` (SSE with a producer task and queue, trace events when `CHAT_DETAILS` allows), conversations CRUD, feedback, `/chat/models` (with the OpenRouter catalogue) |
| `routers/documents.py` | List and facets; processing queue (300 shown, `more`); reprocess and retry; detail; privacy report; processing log; page text, page image (PDF, Office preview, CAD, image) and highlights; original file (inline only for safe types, with nosniff) and download; delete and bulk delete; project edit (Postgres and Qdrant). |
| `routers/uploads.py` | Upload options, single upload, ZIP extract, commit and discard, status, item |
| `routers/sources.py` | Admin source CRUD, test, sync now |
| `routers/models.py` | Roles and overrides, model test, OpenRouter options, switch history, index status, rebuild, activate, rollback |
| `routers/monitor.py` | Summary, timeseries, live stream, traces, jobs list, retry, cancel |
| `routers/admin.py` | Retrieval debug (Lab), users CRUD, evaluation sets and runs |
| `routers/health.py` | `/health`, `/health/ready` (DB and Qdrant), `/health/details` (admin), `/metrics` |

### `security/`

| File | Purpose |
|---|---|
| `auth.py` | Argon2id passwords, lockout (8 attempts / 15 min), sessions (SHA-256 stored, TTL and idle timeout), CSRF, signed cookies, Entra OIDC (PKCE, state, nonce, JWKS) |
| `principals.py` | A user's principals: everyone, `user:`, `upn:`, `group:`, `lgroup:`, `sitegroup:` |
| `sync.py` | `sync_principals` (Graph transitive groups) and `sync_site_groups` (SharePoint REST) jobs |
| `guardrails.py` | Question checks, evidence tag neutralising, PII masking for logs |
| `rate_limit.py` | Fixed-window counters in Postgres (`rate_limits`) for login, chat and uploads |
| `uploads.py` | `validate_upload` (content sniffing, OOXML bomb limits), `safe_filename`, `clamav_scan` |
| `audit.py` | `audit_log` writer |

### `privacy/`

| File | Purpose |
|---|---|
| `shield.py` | Finds bank values, cards, IBAN, SWIFT, routing codes, national ids, e-mail, phone, passport, Stripe ids and custom patterns. `Vault` replaces them with placeholders and restores them. `ShieldedChat` wraps cloud chat models (also tool arguments and streamed placeholders). `redact` removes values permanently. |
| `report.py` | Plain-language privacy findings per document, and what a model would see |

### `plugins/`

| File | Purpose |
|---|---|
| `capabilities.py` | Protocols and data types: ChatModel, Embedder, Reranker, GroundednessJudge, OCREngine, DocumentParser, ParsedDocument, ParsedPage, `previews` … |
| `registry.py` | `(Capability, provider) → factory` registry with required keys and a local flag; `load_plugins` |
| `hub.py` | `ModelHub`: one model per role, Postgres overrides, model choices, privacy shield and metering wrappers, the `reading()` PDF router with OCR, vision and budgets, `vision()` with `FallbackVision` |
| `metered.py` | `MeteredChat` and `MeteredEmbedder`: usage, cost, spans and inspector entries. ⚠ `unwrap` is unused. |

### `providers/`

| File | Purpose |
|---|---|
| `http.py` | Shared httpx client, status → error mapping, bounded retries |
| `local_models.py` | Client for the model server (embed, rerank, classify pairs, tokenizer) |
| `openrouter_catalog.py` | Public OpenRouter model list, cached 1 h, curated for the chat box |
| `llm/openai_compat.py` | OpenRouter, OpenAI, Azure OpenAI and any OpenAI-compatible chat or embedder (ZDR routing, usage reporting, tools, streaming) |
| `llm/anthropic.py` | Anthropic Messages API chat |
| `llm/extractive.py` | Offline answerer that only quotes the passages |
| `llm/pricing.py` | Prices from `MODEL_PRICES_FILE` |
| `embedders/local.py`, `hashing.py`, `remote.py` | Model-server embedder (e5 prefixes); hashing embedder for tests; Cohere |
| `rerankers/local.py`, `cohere.py`, `lexical.py` | Model-server cross-encoder; Cohere Rerank; offline overlap |
| `groundedness/judges.py` | Lexical, HHEM (model server) and LLM judges |
| `ocr/rapidocr.py` | Local RapidOCR with reading-order lines and boxes |
| `ocr/cloud.py` | Azure Document Intelligence read, Mistral OCR |
| `ocr/vision.py` | `VisionOCR` (pages and tables through a vision model, concurrency slots, retries) and `FallbackVision` |
| `parsers/pdf.py` | `local/pdfium` page router: text layer, OCR, mixed pages, pdfplumber tables, drawing title blocks, vision for drawings, layout outline, ligature repair, table flow |
| `parsers/structure.py` | Running headers and footers, heading levels and title from typography |
| `parsers/table_flow.py`, `table_quality.py` | Tables that continue across pages; tidying or rejecting broken tables |
| `parsers/office_preview.py` | PNG previews of Word, Excel and PowerPoint pages from the stored text and tables |
| `parsers/layout_runner.py` | Runs the layout engine in a killable process pool |
| `parsers/pdfium_thread.py` | Single pdfium thread per process. ⚠ `run_pdfium_sync` is unused. |
| `parsers/cloud.py` | LlamaParse, Azure layout and Mistral PDF parsers |
| `parsers/layout/` | Layout engine (about 17k lines) brought over from the earlier code base: character-level pdfium parse (`parser_pdfium_charlevel/`, `parser_pdfium_parallel.py`), `clustering/`, `columns/`, `blocks/`, `classification/` (headers, footers, TOC, boilerplate, body), `heading_detection/`, `title/`, `labels/` (captions), `outline/`, `outline_assembly/`, `embedded_toc.py`, `stats/`, `tokens/`, `phases/`, `model/`, and `data/*.json` dictionaries. The entry point is `adapter.analyse` → `LayoutOutline`. Only its outline is used; page text comes from pdfium. |
| `storage/objects.py` | Content-addressed object store: local disk or S3 (`sha256/aa/bb/<sha>`) |
| `vectorstore/qdrant.py` | `VectorStore`: collections (dense cosine HNSW m16/ef128, int8 quantisation in RAM, sparse with the IDF modifier, payload indexes), alias, upsert, set_payload, delete, count, query_batch, `dense_by_hash`, stale-connection retry. ⚠ `scroll_ids` and `document_filter` are unused. |

The `guardrails/` folder holds only an empty `__init__.py` (⚠).

### `monitoring/`

| File | Purpose |
|---|---|
| `tracing.py` | `trace` and `span` context managers in context variables. ⚠ `current_trace` and `current_span_id` are unused. |
| `recorder.py` | Batched writes of spans, traces, metric minutes, usage and process stats; latency histograms |
| `alerts.py` | Alert rules (control lane stuck, dead letters, budget at 80%, error spike, worker missing), webhook notification, open alerts |
| `tick.py` | `janitor`, `monitor_tick` and `maintenance` handlers (maintenance includes the ZIP staging sweep) |
| `prometheus.py` | `/metrics` exposition from the database rollups |
| `inspector.py` | Per-answer live trace for the Details panel: notes, purposes, model calls (masked messages), embeddings. Never stored. |

### `evaluation/`

| File | Purpose |
|---|---|
| `golden.py` | Golden sets (YAML in `EVAL_DIR`, built-in `demo`) |
| `runner.py` | `eval_run` job: retrieval and answer scoring stored per question |
| `retrieval_bench.py` | JSONL retrieval benchmark (page@k, MRR), gate calibration. `gate_effect` is used only by tests. |
| `demo_corpus.py` | Builds the demo library for `rag demo seed`. |
| `bench_corpus.py` | Five generated document kinds for parsing tests |

### `connectors/`

| File | Purpose |
|---|---|
| `base.py` | Connector contract (`changes`, `download`, `permissions`), `RemoteItem`, `ChangeSet`, `acl_hash`, `@connector_kind` registry, `make_connector` |
| `upload.py` | `upload` kind: sync does nothing; downloads come from the object store |
| `filesystem.py` | Folder on disk or a mounted share (include and exclude globs, size+mtime etag) |
| `smb.py` | SMB 2/3 share without mounting (password from an environment variable; default ACL) |
| `sharepoint.py` | SharePoint and OneDrive through Graph delta, mapping per-item permissions to principals |
| `graph_client.py` | App-only Graph and SharePoint REST tokens; 429 → `RateLimited(provider="graph")` |
| `legacy_adapter.py` | Presents the 27 legacy connectors as connectors of this system (the scan runs in a thread) |
| `legacy/__init__.py` | `LEGACY_CONNECTORS` map: s3, azure_blob, gcs, webdav, ftp, sftp, gdrive, dropbox, box, confluence, jira, notion, slack, discord, teams, outlook_mail, github, gitlab, bitbucket, zendesk, asana, airtable, gmail, imap, sql, web |
| `legacy/base.py`, `_compat.py`, `_http.py`, `_objects.py` | Old contract (`SourceFile`, `Change`, `ScanBatch`, `AclRules`), HTTP helper, shared object-store crawler |
| `legacy/<kind>.py` | One file per kind listed above. `graph.py` and `graph_common.py` are the base for `graph_mail_teams.py`. |
| `legacy/crawl.py` | ⚠ Shared crawl engine that nothing imports |

## `backend/model_server/`

| File | Purpose |
|---|---|
| `src/rag_model_server/app.py` | FastAPI: `/health`, `/models`, `/models/{name}/tokenizer.json`, `/embed`, `/rerank`, `/classify_pairs`. Optional bearer token `LOCAL_MODELS_TOKEN`; input limits. |
| `engine.py` | ONNX Runtime sessions, tokenizers, pooling (mean or CLS), cross-encoder scoring, model registry |
| `catalog.py` | Known models with Hugging Face sources: multilingual-e5 small (int8), base and large, bge-m3, MiniLM, bge-small-en, ms-marco MiniLM, bge-reranker-v2-m3 (int8), mmarco-mMiniLMv2-L12 (int8) |
| `cli.py` | `rag-model-server serve`, `pull` and `list` |

## `frontend/`

| Path | Purpose |
|---|---|
| `next.config.mjs`, `tailwind.config.ts`, `tsconfig.json`, `eslint.config.mjs`, `postcss.config.mjs`, `package.json` | Build config: standalone output, Tailwind tokens, strict TS |
| `scripts/brand-icons.mjs` | Generates `components/brand/marks.generated.ts` from open icon packages at build time |
| `scripts/screenshots.mjs` | Playwright tour that saves screenshots of every page |
| `src/app/layout.tsx`, `globals.css` | Root layout, fonts, theme tokens (light and dark) |
| `src/app/login/page.tsx` | Local and Entra sign-in |
| `src/app/(app)/layout.tsx`, `page.tsx`, `c/[id]/page.tsx` | App shell, new chat, saved conversation |
| `src/app/(app)/documents/page.tsx`, `[id]/page.tsx` | Library list with filters, upload, processing queue, bulk delete; document page with versions, references, privacy and processing log |
| `src/app/(app)/sources/page.tsx` | Admin sources (native forms plus JSON config for the legacy kinds) |
| `src/app/(app)/models/page.tsx` | Roles, switches, model test, index builds |
| `src/app/(app)/monitor/page.tsx` | Live monitor: summary, charts, traces, queue, failed jobs, alerts |
| `src/app/(app)/lab/page.tsx` | Retrieval debug: legs, fusion, rerank, passages |
| `src/app/(app)/evaluation/page.tsx` | Evaluation sets and runs |
| `src/app/(app)/users/page.tsx` | Users admin |
| `src/app/(app)/error.tsx`, `not-found.tsx` | Error pages |
| `components/chat/chat-view.tsx` | Chat state, SSE handling (status, delta, citation, done, trace) |
| `components/chat/composer.tsx` | Question box, filters, model picker |
| `components/chat/messages.tsx`, `answer-text.tsx`, `activity.tsx` | Messages with citations, the live activity line, the Details button |
| `components/chat/details-panel.tsx` | Per-answer Details panel showing every step of the trace |
| `components/chat/source-card.tsx`, `source-preview.tsx` | Citation cards and the page preview with highlight |
| `components/documents/page-viewer.tsx` | Page image, extracted text pane and highlight overlay |
| `components/documents/upload-dialog.tsx` | File and ZIP upload: extract, tree, ticks, counts by format, protected lock, commit |
| `components/documents/processing-queue.tsx`, `processing.tsx`, `privacy.tsx`, `row-actions.tsx` | Queue view, per-document processing log, privacy report, row menu |
| `components/layout/app-shell.tsx`, `events.ts` | Sidebar, conversation list, navigation, shared events |
| `components/monitor/trace-drawer.tsx` | Trace span tree |
| `components/charts/line-chart.tsx` | SVG line chart |
| `components/brand/*` | Provider and model marks |
| `components/ui/*` | Button, fields, feedback, layout, overlay, toast primitives |
| `lib/api.ts` | `api()` fetch wrapper (CSRF header, errors) |
| `lib/sse.ts` | POST and server-sent events reader |
| `lib/hooks.ts`, `session.tsx`, `theme.ts`, `format.ts`, `roles.ts`, `types.ts`, `trace-store.ts` | Data hooks, session context, theme, formatting, role labels, API types, the Details trace store |
| `lib/sources.ts` | Citation URL helper (`originalUrl`). |

## `deploy/`

| File | Purpose |
|---|---|
| `nginx.conf` | Edge proxy (see [architecture §1](architecture.md#1-the-system-at-a-glance)) |
| `prometheus/prometheus.yml` | Scrapes `backend:8000/metrics` |
| `grafana/provisioning/*` | Datasource and dashboard provisioning |

## `scripts/`

| Script | Purpose |
|---|---|
| `start.sh` | First run: create `.env` with random secrets, pull models, build, start, run `rag check`, print the admin sign-in |
| `stop.sh`, `restart.sh`, `status.sh`, `logs.sh` | Stack lifecycle |
| `run.sh`, `lib.sh` | Shared compose selection and helpers |
| `admin.sh` | Create a local administrator or user |
| `demo.sh`, `eval.sh` | Demo library; run a golden set |
| `models.sh` | List or pull local models |
| `monitoring.sh` | Prometheus and Grafana profile |
| `backup.sh`, `restore.sh` | Database dump, originals, `.env`, optional Qdrant snapshot; restore and rebuild the index |
| `dev.sh` | Run from source with hot reload |
| `test.sh` | Ruff, bandit, the backend suites with throwaway Postgres and Qdrant, frontend typecheck, lint and build |

## `tests/`

| Folder | Covers |
|---|---|
| `unit/` | Aggregate, answering units, arithmetic, cache, CAD, citations, config, formats_extra, ligatures, OpenRouter catalogue, privacy shield, protection, Qdrant retry, read budget, relevance, rerank blend, scheduler, structure, table flow and quality, text and metadata, vision OCR, xlsx reader, ZIP upload |
| `integration/` | API, document kinds, ingestion, queue, retrieval and answering against real Postgres and Qdrant |
| `contracts/` | Cloud providers and Microsoft 365 with recorded responses |
| `connectors_legacy/` | One test file per legacy connector (about 19k lines) |
| `model_server/` | Model server API |
| `eval/` | Fusion weights on a near-duplicate corpus, golden sets |

## `eval/` scripts

| Script | Purpose |
|---|---|
| `ask.py`, `ask_full.py` | Ask a question as `BENCH_AS` (defaults to the bootstrap admin) and print the plan, legs and answer |
| `blend_sweep.py`, `rerank_sweep.sh` | Sweep the rerank blend settings over a question set |
| `gate_calibrate.py` | Calibrate the relevance gate thresholds |
| `failures.py`, `inspect_run.py` | List failed questions; inspect one evaluation run |
| `gen_golden.py`, `gen_table_golden.py` | Generate golden questions (text and tables) |
| `scale_inject.py` | Inject synthetic chunks to test search at scale |
| `mini_pytest.py` | Minimal test runner for containers without pytest |
| `setup_mini.py` | Create the mini evaluation sources and re-chunk existing documents |
| `golden_*.jsonl`, `g3.jsonl` | Question sets for `rag eval bench` (all, auto, construction, paraphrase, rerank, unanswerable) |
| `old_stack.py`, `*.json` | Comparison with the earlier system's retrieval stack, and saved benchmark results. **Git-ignored**: local only. |
