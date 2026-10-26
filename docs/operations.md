# Operations

Commands are in [commands.md](../commands.md). This page covers the routines and what to do when something is wrong.

## First installation

1. Install Docker with Compose v2 (Linux, or WSL2 on Windows). Give Docker enough memory: 8 GB for a trial, far more
   for a real library ([scaling.md](scaling.md)).
2. `scripts/start.sh`. It creates `.env` from `.env.example`, generates the secrets, downloads the local models into
   the `models` volume, builds the images, starts the stack, waits for the API and runs `rag check`.
3. Put real values in `.env` (keys, `PUBLIC_URL`, `AUTH_MODE`) and `scripts/restart.sh`.
4. Sign in, change the admin password, clear `BOOTSTRAP_ADMIN_PASSWORD`.
5. Add sources (Sources page), watch ingestion on Monitor.
6. Optionally `scripts/demo.sh` first, to see the whole thing work on sample documents before connecting real ones.

## Daily routine

Open **Monitor**. Healthy looks like: no open alerts, queues draining, all processes online, spend under budget. An
alert webhook means you do not need to look daily.

## Upgrades

```bash
git pull                         # or unpack the new release over the old
scripts/backup.sh                # always before an upgrade
scripts/start.sh                 # rebuilds images, applies migrations (the migrate service), restarts
```

Migrations are forward-only and run before any service starts (the API applies them when it starts). If a new release
changes the chunker, analyzer or embedder version, `rag index status` shows a *configured* build that differs from the *live* one; run
`rag index rebuild` — search keeps using the old index until the new one is complete, and `rag index rollback` returns
to it.

## Changing the embedder

1. Edit `EMBEDDER` (and `LOCAL_MODELS` / `RERANKER` if needed); `scripts/models.sh pull` downloads local models.
2. `scripts/restart.sh`; `rag index rebuild` (or **Models → Search index → Rebuild**). It runs on the bulk lane.
3. When the build reaches 100%, run `rag eval run` against your golden set on both builds if you want proof, then
   `rag index activate <build-id>`. Roll back with `rag index rollback`.

## After improving how documents are read or described

Parsing, metadata and chunking run once per file, so an improvement does not reach documents that are already in the library
until they are processed again. Nothing is fetched from the source in either case; the stored copy is used.

| Changed | Run | What it does |
|---|---|---|
| Metadata rules, document identity, chunk sizes or heading handling | `rag reprocess [--source NAME]` | Takes the stored page text, redoes metadata and chunks, embeds only chunks whose text is new |
| The parser itself: layout, headings, tables, OCR, ligature repair | `rag reparse [--source NAME] [--format pdf]` | Reads each stored file again with the current parsers, then does everything `reprocess` does |

Both run on the bulk lane behind interactive work. Chunks with unchanged text keep their stored vectors (matched by content
hash), so the cost is the parsing plus the embedding of text that actually changed. A document's old chunks keep answering
searches until its new chunks replace them.

## Backup and restore

`scripts/backup.sh` writes `backups/<timestamp>/` with a Postgres dump, the stored originals (`data.tar`) and a copy of
`.env`. Postgres is the source of truth for everything except the original files; the search index is rebuilt from it
(`--with-index` additionally snapshots Qdrant to shorten recovery at scale).

For a 5 TB library:

- `data.tar` is only practical for small installations. With `OBJECT_STORE_URL=s3://...` the originals live in the object
  store: protect them there (versioning, replication) and use `backup.sh` for the database and settings only.
- Schedule Postgres backups on your infrastructure (base backup plus WAL archiving) rather than relying on dumps.
- Test a restore on a spare machine at least once before relying on the backups.

`backups/` contains `.env` and every document: treat it as secret and encrypt it.

## Routine tasks

| Task | How |
|---|---|
| Add a user | `scripts/admin.sh someone@company.com --user`, or Entra sign-in (users appear at first sign-in) |
| Disable a user | Users page → disable (sessions end at once) |
| Add capacity | `scripts/start.sh --scale worker=4`, or `WORKER_REPLICAS` and `WORKER_CONCURRENCY` in `.env` |
| Rotate `SECRET_KEY` | Edit `.env`, `scripts/restart.sh` (everyone signs in again) |
| Rotate the Qdrant key / DB password | Change in `.env` **and** in the running service (`ALTER USER` / recreate the `qdrant` container with the new key), then restart |
| Retry failed work | Monitor → Failed jobs, or `rag jobs retry-dead` |
| Remove a source | Sources → delete. Its documents leave search quickly; each released version purges its pages, vectors and original at once when nothing else uses that file (`purge_unused_file`), and `gc_blobs` (every 6 h) is the backstop. |
| Free disk | Originals of deleted documents are removed when their version is released, or by `gc_blobs` (every 6 h, up to 500 files per run) |

## Troubleshooting

| Symptom | Check |
|---|---|
| `scripts/start.sh` stops at "Set POSTGRES_PASSWORD" | `.env` is missing or hand-edited; delete the empty line or rerun, the script generates missing secrets |
| Page loads but sign-in says "origin not allowed" | `PUBLIC_URL` and `ALLOWED_ORIGINS` must contain the address in the browser bar |
| Sign-in blocked | Failed attempts per address are limited (`LOGIN_ATTEMPTS_PER_15MIN`), and an account locks for 15 minutes after 8 failures |
| Answers say the model is not configured | `scripts/status.sh` or Models page: a role is missing its key. `rag models test LLM_ANSWER` shows the exact error |
| "Not found" for something that exists | Is the file ingested (Documents page, status)? Is it visible to this user (`rag ask "..." --as user@company.com` searches with that user's access)? |
| A file never appears | Monitor → Failed jobs shows the reason: encrypted, corrupt, unsupported, or too large (`MAX_FILE_MB`) |
| Queue is growing | Processes online? Provider rate limits (cooldowns are visible in Monitor)? Add workers |
| Search got slower after a big import | Qdrant is still indexing; check CPU, and that vectors are quantised and on NVMe |
| Disk full | `docker system df`; Postgres WAL and Qdrant storage are the large ones; see the disk note in [scaling.md](scaling.md) |
| Containers restart in a loop | `scripts/logs.sh <service>`; the API failing its migration blocks the workers |
| Grafana empty | `scripts/monitoring.sh status`; `PROMETHEUS_METRICS=true`; Prometheus target `rag-backend` is "up" |

## Data retention and deletion

- Deleting a document at its source removes it from search at the next sync; its text and vectors are removed by a
  control-lane job, and its original right after, once nothing refers to the blob (`gc_blobs` catches anything left).
- Conversations belong to their user and can be deleted by them.
- Traces expire after `MONITOR_RETENTION_DAYS`, rollups after `METRIC_RETENTION_DAYS`, finished jobs and expired sessions
  are cleaned hourly.
