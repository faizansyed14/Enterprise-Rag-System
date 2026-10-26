# Configuration

`.env` is the single source of truth. `scripts/start.sh` copies `.env.example` on the first run and fills in random
secrets. `.env.example` documents every setting next to its default; this page explains the ideas.

## Model roles

A role is one line: `ROLE=<provider>/<model>`.

| Role | Used for | Optional |
|---|---|---|
| `LLM_ANSWER` | Everyday answers | no |
| `LLM_ANSWER_HARD` | Multi-part, comparison and cross-document questions | falls back to `LLM_ANSWER` |
| `LLM_ANSWER_FALLBACK` | Takes over when the answer model fails before writing anything | yes |
| `LLM_ROUTER` | Follow-up rewriting, translation, filters from the question | `none` = rules only |
| `LLM_ENRICH` | Fills metadata gaps during ingestion | `none` = rules only |
| `VISION` | Reads drawing sheets and pages OCR cannot | `none` = skip |
| `CAD_VISION` | Analyses AutoCAD drawings (.dwg, .dxf) with its own prompt: high-resolution colour tiles, one request per sheet, a structured Markdown report (see [parsing.md](parsing.md#autocad-drawings-dwg-dxf)). Default `openrouter/anthropic/claude-sonnet-5.5`; switchable on the Models page | `none` = the short `VISION` description |
| `EMBEDDER` | Dense vectors | no (changing it needs an index rebuild) |
| `RERANKER` | Cross-encoder reranking | yes |
| `GROUNDEDNESS` | Sentence-versus-passage check | `builtin/lexical` is offline |
| `PARSER`, `OCR` | PDF reading and OCR | – |

Providers: `local`, `openrouter`, `openai`, `anthropic`, `azure_openai`, `openai_compat` (vLLM, LM Studio, any
OpenAI-compatible endpoint), `cohere`, `mistral`, `azure_di`, `llamaparse`, `builtin`, `extractive` (offline), `none`.
`@n` after an embedding model sets output dimensions (`openai/text-embedding-3-large@1024`).

Local and cloud mix freely, role by role. Typical profiles:

| Profile | Lines |
|---|---|
| Development (default) | local embedder/reranker/OCR, answers through OpenRouter with Claude Haiku 5.5 and Sonnet 5.5 for hard questions |
| No keys at all | `extractive/default` for the three answer roles, `none` for router, enrich and vision |
| Anthropic direct | `anthropic/claude-haiku-5-5`, `anthropic/claude-sonnet-5-5` |
| Azure | `azure_openai/<deployment>`, `OCR=azure_di/prebuilt-read` |
| Arabic + English, stronger search | `EMBEDDER=local/bge-m3`, `RERANKER=local/bge-reranker-v2-m3`, `RERANKER_LANGUAGES=*` |

Switching an answer model does not need a restart: admins can switch roles on the **Models** page (stored in
`role_overrides`, audited in `model_switches`). `MODEL_CHOICES` lists the models ordinary users may pick in the chat
box. The model that wrote each answer is shown under it when `SHOW_MODEL_BADGE=true`.

Run `rag models list` to see what each role resolves to, `rag models test LLM_ANSWER` to make one real call with a role's model, and `rag check` to check configuration, models, database, Qdrant and the index in one go.

### Cost control

`DAILY_BUDGET_USD` raises an alert at 80% and the Monitor page shows spend per role and model. Prices for models the
built-in table does not know come from `MODEL_PRICES_FILE` (YAML, USD per million tokens, e.g. `anthropic/claude-haiku-5-5: {input: 1.00, output: 5.00, cached_input: 0.10}`). `VISION_PAGES_PER_DAY` and `CLOUD_OCR_PAGES_PER_DAY` cap paid page reading.

## Local models

The model server loads the models listed in `LOCAL_MODELS` from the `models` volume. Download them once with
`scripts/models.sh pull`. Known models:

| Model | Kind | Notes |
|---|---|---|
| `multilingual-e5-small` | embedder, 384-d | default; Arabic and English |
| `multilingual-e5-base`, `-large` | embedder | 768-d, 1024-d |
| `bge-m3` | embedder, 1024-d | strongest multilingual option here |
| `all-MiniLM-L6-v2`, `bge-small-en-v1.5` | embedder, English | fast |
| `ms-marco-MiniLM-L-6-v2` | reranker | English only (`RERANKER_LANGUAGES=en`) |
| `bge-reranker-v2-m3` (+ `-int8`) | reranker | multilingual |
| `mmarco-mMiniLMv2-L12` (+ `-int8`) | reranker | **default**; multilingual (Arabic and English) |
| `multilingual-e5-small-int8` | embedder, 384-d | quantised copy of the default |

Queries in a language the reranker does not read skip reranking and keep the fused order.

## Folder rules (`PATH_RULES_FILE`)

Project and document type are read from folder names by default. A YAML file adds site-specific rules.
Each rule is a regular expression on the full path with named groups, fixed values, or both:

```yaml
projects: [Harbour View, Tower B]          # names to recognise in paths and questions
rules:
  - match: '(?i)^/?Projects/(?P<project>P\d{4})[^/]*/'
  - match: '(?i)/02[_ -]Structural/'
    set: {doc_type: drawing}
  - match: '(?i)/Archive/'
    set: {status: archived}
```

`KNOWN_PROJECTS` takes a comma-separated list for the simple case. Folder-derived values carry a confidence of 0.9 for
explicit rules and 0.6 for guesses; values read from the document itself win over guesses.

## Fusion weights

Search combines three legs — dense (meaning), sparse (BM25 words) and exact (identifiers). The weights depend on the
query type. The defaults lean on the words because a construction library is full of near-identical documents that
meaning alone cannot tell apart (measured in [retrieval.md](retrieval.md#near-duplicate-libraries-and-the-fusion-weights)):

| Query type | dense | sparse | exact |
|---|---|---|---|
| identifier (`A-101`, `RFI-023`) | 0.25 | 0.35 | 0.40 |
| keyword | 0.10 | 0.90 | 0.30 |
| question | 0.15 | 0.85 | 0.25 |
| non_english | 0.70 | 0.30 | 0.25 |

Override any of them with JSON: `FUSION_WEIGHTS={"question": {"dense": 0.5, "sparse": 0.5}}`. Change weights only
together with an evaluation run ([evaluation.md](evaluation.md)); the Retrieval Lab page shows each leg's contribution
for a query.

## Other settings worth knowing

| Setting | Meaning |
|---|---|
| `EXACT_SEARCH_BELOW` | Below this many visible chunks the search scans exactly instead of using HNSW (perfect recall for small or heavily filtered scopes) |
| `DENSE_TOP_K`, `SPARSE_TOP_K`, `EXACT_TOP_K`, `RERANK_TOP_N` | Candidates per leg and how many are reranked |
| `EVIDENCE_PASSAGES`, `EVIDENCE_PER_DOCUMENT` | How much evidence the model sees, and the cap per document |
| `CHUNK_TOKENS`, `CHUNK_OVERLAP` | Chunk size. A change affects only files read after it; run `rag reprocess` to re-chunk the existing library. |
| `QDRANT_QUANTIZATION` | `int8` (default) cuts vector memory 4× with negligible recall loss; `none` disables |
| `QDRANT_ON_DISK` | Keep original vectors on disk; quantised copies stay in RAM |
| `OBJECT_STORE_URL` | `file:///data/originals` or `s3://bucket/prefix` (S3, MinIO, Ceph, R2) |
| `DB_PGBOUNCER` | Set when PgBouncer in transaction mode is in front of Postgres |
| `AUTH_MODE` | `local`, `entra` or `both` |
| `APP_ENV=prod` | Refuses to start with a weak `SECRET_KEY`, enables secure cookies |
| `READ_FILES_MAX_DOCS`, `READ_FILES_BUDGET_CHARS` | How many documents (40) and characters (400,000) a `read_files` answer may read in full. A model on the local server uses 3 and 60,000. |
| `AGGREGATE_MAX_DOCS` and the other `AGGREGATE_*` settings | Limits of the "list all …" map-reduce: documents (60), characters per document, batch size, parallel calls |
| `SUFFICIENCY_CHECK`, `STRATEGY_PLANNER` | The evidence-coverage check before writing, and the model-chosen answer strategy |
| `VISION_FALLBACK` | Vision model used when `VISION` fails (for example a 429); default `openrouter/anthropic/claude-haiku-5.5` |
| `VISION_CONCURRENCY`, `VISION_RETRIES` | Parallel vision calls per process (3) and retries before the fallback (1) |
| `UPLOAD_MAX_MB`, `UPLOAD_ZIP_MAX_MB`, `UPLOAD_STAGING_DIR` | Single-file limit (200 MB), ZIP folder limit (4,096 MB; nginx allows 5 GB), where ZIPs are unpacked for selection |
| `SKIP_EXTENSIONS` | Extensions that connector sources record as skipped instead of reading (default `jpg,jpeg,png`). Uploads ignore it. |
| `CHAT_DETAILS` | Who sees the live per-answer Details panel: `all` (default) or `admin` |
| `PRIVACY_MASK_FOR_MODELS`, `PRIVACY_REDACT_STORED`, `PRIVACY_AUTO_LOCAL_ONLY` | Mask values sent to cloud models (on); remove them from stored text (off); mark documents that contain them as local-only (off) |
| `STORE_TEXT_IN_INDEX` | Keep chunk text in Qdrant payloads (on). Set it off above about 5 TB to halve Qdrant disk; text is then loaded from Postgres. |
| `MIN_DENSE`, `MIN_SPARSE`, `MIN_COVERAGE`, `MIN_RERANK` | The "not in the library" gate (only `MIN_RERANK=0.02` is on by default) |
| `COOKIE_SECURE` | Secure session cookie; defaults to true when `APP_ENV=prod` |

`GUARDRAILS` is reserved for an optional stronger guardrail and is not read yet: the built-in checks always run ([dead-code.md](dead-code.md)). `ANALYZER_VERSION` was removed on 2026-10-23 because it had no effect.
