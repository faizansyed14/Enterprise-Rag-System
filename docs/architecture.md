# Architecture

## Services

```
                 browser
                    │  HTTPS
              ┌─────▼─────┐
              │   nginx   │  only published ports; /api → backend, /metrics blocked, rest → frontend
              └──┬─────┬──┘
        ┌────────▼┐   ┌▼────────┐
        │ frontend│   │   backend   │  FastAPI, cookie sessions, SSE chat stream
        │ Next.js │   └──┬───┬──┘
        └─────────┘      │   │
   ┌─────────────────────┘   └──────────────────────┐
┌──▼───────┐  ┌───────────┐  ┌──────────────┐  ┌────▼──────┐
│ Postgres │  │  Qdrant   │  │ model-server │  │ LLM APIs  │  OpenRouter / Anthropic / OpenAI / Azure
│ truth +  │  │ vectors + │  │ ONNX embed,  │  │ (optional)│
│ job queue│  │ filters   │  │ rerank, NLI  │  └───────────┘
└──▲───────┘  └─────▲─────┘  └──────▲───────┘
   │                │               │
┌──┴────────────────┴───────────────┴──┐   worker-control (permissions, deletions)
│ workers: fetch → extract → enrich →  │   worker × N     (interactive + bulk ingestion)
│ chunk → embed → publish              │   scheduler      (sync, reconcile, alerts, retention)
└──────────────────────────────────────┘
```

| Service | Role | State |
|---|---|---|
| `frontend` | Next.js server: the UI on port 3000, and it forwards `/api` to the API | none |
| `backend` | Authentication, search, chat, documents, admin, `/metrics` | none (stateless, scale with `API_WORKERS`) |
| `worker` | Every background job: reading files, OCR, chunking, embedding, publishing, permission and deletion jobs | none |
| `scheduler` | Source sync, permission refresh, reconcile, alert evaluation, retention | none |
| `postgres` | Source of truth: documents, versions, pages, chunks, ACLs, jobs, traces | volume |
| `qdrant` | Search index (dense + sparse + payload filters), alias `chunks_live` | volume |
| `model-server` | Local ONNX embedder, reranker and pair classifier; token-protected | models volume |
| `nginx` | The one published port (3000): `/api` to the API, the rest to `frontend`; blocks `/metrics`; upload limit | none |

Postgres is the truth and Qdrant is a rebuildable projection of it: the index can be thrown away and rebuilt from
Postgres at any time (`rag index rebuild`), and a daily reconcile job repairs differences.

## Ingestion pipeline

A document becomes searchable through six idempotent stages, each a job on the Postgres queue.

| Stage | Does | Idempotency key |
|---|---|---|
| fetch | Reads the original from the source, stores it content-addressed (`sha256`) in the object store | blob hash |
| extract | PDF text with pdfium; OCR (RapidOCR or a cloud service) for pages without text; Office, mail, HTML, image formats | blob hash (a file is extracted once, however many documents point at it) |
| enrich | Identifiers, title block, cross-references, project and type from text and folder rules; an LLM fills only the gaps | version |
| chunk | Structure-aware chunks of ~380 tokens with 15% overlap, each carrying its heading and document header | version + chunker version |
| embed | Dense vectors from the configured embedder, BM25 sparse vector, written to the **build** collection | chunk id |
| publish | In one Postgres transaction the version becomes current; then the Qdrant payload flips visibility | version |

Design points that matter at scale:

- **Content-addressed storage.** The same file in ten folders is stored and extracted once; each location is its own
  document with its own permissions.
- **Version rows, not overwrites.** A changed file creates a new version; the old one stays queryable ("what did
  revision 1 say") until retention removes it. Access rules attach to the version.
- **Lanes.** `control` (permission changes, deletions) is served by a dedicated worker so it never waits behind a bulk
  import; `interactive` (user uploads) runs ahead of `bulk` (source syncs, re-embedding).
- **Retry policy.** Rate limits wait for `Retry-After` without spending an attempt; transient errors back off
  exponentially with jitter and become dead letters after the limit; permanent errors (corrupt, encrypted,
  unsupported) are skipped with the reason shown on the document. Leases expire, so a crashed worker's job is picked
  up again.
- **Index builds.** Changing the embedder creates a new Qdrant collection alongside the live one. Search keeps using
  the live build until the new one is complete; `rag index activate` moves the alias atomically and
  `rag index rollback` moves it back.

## Question flow

1. **Guardrails** reject empty or abusive input and mark prompt-injection attempts.
2. **Query router** (rules first, a fast LLM only for follow-ups and translation) classifies the question
   (identifier, keyword, question, non-English), extracts filters (project, type) and decides
   whether the question is hard enough for the stronger model.
3. **Retrieval** (see [retrieval.md](retrieval.md)) returns passages with page numbers and their parent documents.
4. **Agent loop** lets the model call a small set of read-only tools (`search`, `search_within`, `read_pages`, `open_reference`), each working inside the user's access and limited by `AGENT_MAX_STEPS`
   for multi-step questions; the research phase reports status to the UI, the answer phase streams tokens.
5. **Citations** are parsed from the answer; each is matched to the best supporting span on its page and the quote is
   verified against the page text.
6. **Groundedness** checks each sentence against its cited passages; unsupported sentences are flagged.
7. The answer, sources, model used and timings are stored with the conversation and traced for the Monitor page.

## Data model (Postgres)

| Group | Tables |
|---|---|
| Identity | `users`, `sessions`, `user_principals`, `site_groups`, `audit_log` |
| Content | `sources`, `source_items`, `blobs`, `documents`, `versions`, `version_access`, `pages`, `ocr_cache`, `chunks`, `doc_references` |
| Chat | `conversations`, `messages` |
| Work | `jobs`, `provider_cooldowns`, `rate_limits`, `daily_budgets`, `usage_daily` |
| Models and index | `index_builds`, `role_overrides`, `model_switches` |
| Monitoring | `traces`, `spans`, `metric_minutes`, `process_stats`, `alerts` |

## Plugin architecture

Every replaceable capability (LLM, embedder, reranker, groundedness judge, parser, OCR, connector, vector store,
object store) is a factory registered under a capability and a provider name. A `.env` role line selects the provider
and model; `rag.plugins.PluginRegistry.create` builds it, wraps it so every call is metered and traced, and reports
missing keys or packages before the first request instead of during it. Adding a provider is one module with a
`@register(...)` decorated factory; nothing else changes.

## Frontend

Next.js 16 (App Router, React 19, standalone output) with Tailwind tokens as CSS variables, Inter and Cormorant
Garamond. The web server only serves the UI; the browser talks to `/api` through nginx with the session cookie and a
CSRF header. Chat uses a `fetch` stream (Server-Sent Events over POST). Charts are small hand-written SVG components.
