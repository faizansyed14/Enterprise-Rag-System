# Commands

Everything below runs from the repository root. The scripts need Docker with Compose v2 and bash (on Windows, use
WSL2). `make <target>` runs the same scripts; `make help` lists them.

## Development and production: which file runs what

| | File(s) | Sizing |
|---|---|---|
| **Production** | `docker-compose.yml` | Large host (32+ vCPU, 128+ GB RAM): Postgres 32 GB, Qdrant 64 GB, model server 12 GB, 4 parsing workers, 2 embedding workers, 4 API processes. Every number is a variable (see "Production sizing" in `.env.example`). |
| **Local development** | `docker-compose.yml` + `docker-compose.dev.yml` | Small machine (8 GB Docker VM): memory-capped containers, your source code mounted into them, Postgres/Qdrant/model server on localhost ports, only the API holds the OpenRouter key. |

On a development machine put these two lines in `.env` and plain `docker compose ...` uses both files. On a production server
leave them out, and it uses `docker-compose.yml` alone:

```
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
```

### One script for both: `scripts/run.sh <dev|prod> <action>`

It picks the compose files itself, whatever `COMPOSE_FILE` says in `.env`.

| What | Development | Production |
|---|---|---|
| Build and start | `scripts/run.sh dev up` | `scripts/run.sh prod up` |
| More workers | `scripts/run.sh dev up --scale worker=2` | `scripts/run.sh prod up --scale worker=8` |
| Apply new code | `scripts/run.sh dev update` (restarts the Python services; rebuilds the frontend image if the frontend changed, or `REBUILD_WEB=1`) | `scripts/run.sh prod update` (git pull, rebuild, recreate; migrations run first) |
| Recreate after editing `.env` | `scripts/run.sh dev restart` or `... restart backend worker` | `scripts/run.sh prod restart` |
| Status, configuration check, queue | `scripts/run.sh dev status` | `scripts/run.sh prod status` |
| Logs | `scripts/run.sh dev logs backend worker` | `scripts/run.sh prod logs backend` |
| Stop (data kept) / remove containers (data kept) | `scripts/run.sh dev stop` / `down` | `scripts/run.sh prod stop` / `down` |
| Show the memory limit and copies of each service | `scripts/run.sh dev check` | `scripts/run.sh prod check` |

On a production server remove `WORKER_REPLICAS`, `EMBED_WORKER_REPLICAS`, `API_WORKERS` and `MODEL_THREADS` from `.env` (or set
them to the production values in the "Production sizing" block of `.env.example`); a development `.env` pins them low.

### Local development (the same things by hand)

| What | Command |
|---|---|
| Start everything (builds on first run) | `docker compose up -d` |
| Same, without relying on `.env` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d` |
| Stop everything (data is kept) | `docker compose stop` |
| Stop and remove containers (data volumes are kept) | `docker compose down` |
| What is running | `docker compose ps` |
| Follow logs | `docker compose logs -f backend worker` |
| Python code changed | `docker compose restart backend worker scheduler` (the source is mounted; no rebuild) |
| Frontend code changed | `docker compose build frontend && docker compose up -d frontend` |
| `.env` changed | `docker compose up -d` (recreates only what changed) |
| API and workers from source with hot reload (Docker only for Postgres, Qdrant, models) | `scripts/dev.sh` |
| Throwaway databases and all checks | `scripts/test.sh` |
| Load the evaluation corpus as sources | `docker compose exec backend rag sources add-folder --name eval --path /sources/eval/arxiv` |
| Re-read stored PDFs with the current parsers | `docker compose exec backend rag reparse --format pdf` |

### Production

```bash
# first time on the server (creates .env with generated secrets; set PUBLIC_URL, OPENROUTER_API_KEY)
scripts/start.sh

# or by hand
docker compose up -d --build                 # production file only: COMPOSE_FILE must not be set in .env

# update to a new version
git pull && docker compose build && docker compose up -d      # migrations run first, automatically

# more capacity (or set WORKER_REPLICAS / EMBED_WORKER_REPLICAS in .env)
docker compose up -d --scale worker=8

docker compose ps                            # status
docker compose logs -f --tail 100 backend        # logs
scripts/backup.sh                            # database, originals and .env (see docs/operations.md)
docker compose stop                          # stop, data kept
```

Check a file before using it: `docker compose config --quiet` (prints nothing when valid) and `docker compose config | grep mem_limit`
shows the limits that apply.

## Sensitive values and outside models

| What | Command |
|---|---|
| See what a cloud model would receive from a document (bank and id numbers shown as placeholders) | `docker compose exec backend rag privacy check "Bank Details"` |
| Print every chunk of it, not only those with a masked value | `docker compose exec backend rag privacy check "Bank Details" --full` |
| Recent calls to cloud models and how many values the shield masked in each (a count, never a value) | `docker compose exec backend rag privacy calls --hours 24` |
| Treat documents holding bank or id numbers as local-only (set `PRIVACY_AUTO_LOCAL_ONLY=true` in `.env`), then apply it to stored files | `scripts/run.sh dev restart` then `docker compose exec backend rag reprocess` |

The Documents view shows the stored text from your own database; it is not what is sent. See "Bank accounts, cards and ids with
outside models" in `docs/security.md`.

## Start, stop, restart

| What | Command |
|---|---|
| Build and start everything (first run creates `.env`, downloads the local models, prints the admin password) | `scripts/start.sh` |
| Start with more ingestion workers | `scripts/start.sh --scale worker=3` |
| Stop everything (data is kept) | `scripts/stop.sh` |
| Stop and **delete all data** (asks first) | `scripts/stop.sh --purge` |
| Restart after editing `.env` | `scripts/restart.sh` |
| Restart some services only | `scripts/restart.sh backend worker` |
| Containers, configuration check, queue | `scripts/status.sh` |
| Follow all logs / some services | `scripts/logs.sh` / `scripts/logs.sh backend worker` |

The app answers on `http://localhost:3000`. Sign in with `BOOTSTRAP_ADMIN_EMAIL` and the password
`scripts/start.sh` printed (also in `.env` as `BOOTSTRAP_ADMIN_PASSWORD`; change it after signing in and clear it).

## Monitoring (optional Prometheus and Grafana)

The built-in Monitor page needs neither.

| What | Command |
|---|---|
| Start Prometheus and Grafana | `scripts/monitoring.sh start` |
| Stop them (their data is kept) | `scripts/monitoring.sh stop` |
| Their status | `scripts/monitoring.sh status` |
| Prometheus | <http://localhost:9090> (localhost only) |
| Grafana | <http://localhost:3001>, user `admin`, password `GRAFANA_ADMIN_PASSWORD` from `.env`; dashboard *ALAIN overview* |
| Start / stop only Prometheus | `docker compose --profile monitoring up -d prometheus` / `docker compose --profile monitoring stop prometheus` |
| Start / stop only Grafana | `docker compose --profile monitoring up -d grafana` / `docker compose --profile monitoring stop grafana` |

## Virus scanning of uploads (optional)

```bash
docker compose --profile clamav up -d clamav     # first start downloads signatures (a few minutes)
# in .env:  CLAMAV_URL=tcp://clamav:3310
scripts/restart.sh backend
```

## Demo library and evaluation

| What | Command |
|---|---|
| Load the sample library and demo users (`admin@`, `engineer@`, `commercial@demo.local`, password `ChangeMe-Demo-2025`) | `scripts/demo.sh` |
| Score the demo golden questions | `scripts/eval.sh` |
| Score your own set (`/data/eval/<name>.yaml`, see docs/evaluation.md) | `scripts/eval.sh <name>` |
| Ask from the terminal as a given user | `docker compose exec backend rag ask "What is the title of drawing A-101?" --as engineer@demo.local` |

## Models

| What | Command |
|---|---|
| Models the local server knows, and which are downloaded | `scripts/models.sh list` |
| Download the models in `LOCAL_MODELS` | `scripts/models.sh pull` |
| Download others | `scripts/models.sh pull bge-m3 bge-reranker-v2-m3` |
| Test one role with a real call (role name as in `.env`) | `docker compose exec backend rag models test LLM_ANSWER` |
| Check configuration, models, database, Qdrant, object store, index (and Redis) | `docker compose exec backend rag check` |
| Cache hit rates, entries and memory | `docker compose exec backend rag cache stats` |
| Make every cached answer out of date (after a manual database change, say) | `docker compose exec backend rag cache clear` |
| Which model each role uses | `docker compose exec backend rag models list` |

Switching a model: edit its line in `.env` and run `scripts/restart.sh`. Answer models can also be switched live on the
Models page. Changing `EMBEDDER` needs an index rebuild (below); search keeps using the old index until the new one is
ready.

## Search index

| What | Command |
|---|---|
| Live index, configured index, builds | `docker compose exec backend rag index status` |
| Rebuild after changing `EMBEDDER` (runs in the background on the bulk lane) | `docker compose exec backend rag index rebuild` |
| Switch search to a finished build | `docker compose exec backend rag index activate <build-id>` |
| Go back to the previous build | `docker compose exec backend rag index rollback` |

## Sources and jobs

| What | Command |
|---|---|
| List sources | `docker compose exec backend rag sources list` |
| Add a server folder (mounted into the containers) | `docker compose exec backend rag sources add-folder --name "Projects" --path /sources/projects` |
| Sync a source now | `docker compose exec backend rag sources sync "Projects"` (by source name; add `--full` to re-read everything) |
| Queue by lane | `docker compose exec backend rag jobs stats` |
| Retry jobs that gave up | `docker compose exec backend rag jobs retry-dead` |

SharePoint, OneDrive and Windows file shares are added on the Sources page (see docs/connectors.md).

## Users

| What | Command |
|---|---|
| Create an administrator (asks for a password) | `scripts/admin.sh you@company.com` |
| Create an ordinary user | `scripts/admin.sh someone@company.com --user` |
| Create a user in groups | `docker compose exec backend rag users create --email x@company.com --groups commercial,site` |

## Database

| What | Command |
|---|---|
| Apply migrations (also done automatically when the API starts) | `docker compose exec backend rag db upgrade` |
| Open psql | `docker compose exec postgres psql -U rag -d rag` |

## Backup and restore

| What | Command |
|---|---|
| Back up the database, originals and `.env` to `backups/<timestamp>/` | `scripts/backup.sh` |
| Also snapshot the search index | `scripts/backup.sh --with-index` |
| Restore (then rebuilds the index) | `scripts/restore.sh backups/<timestamp>` |

## Development and tests

| What | Command |
|---|---|
| Run from source with hot reload (Docker only for Postgres, Qdrant, model server) | `scripts/dev.sh` |
| Only the Docker services for development | `scripts/dev.sh infra` |
| Everything: ruff, bandit, pytest, frontend typecheck, lint, build | `scripts/test.sh` |
| Backend only (extra arguments go to pytest) | `scripts/test.sh backend -k retrieval` |
| Frontend only | `scripts/test.sh frontend` |
| Screenshots of every page (stack running with the demo library) | `cd frontend && BASE_URL=http://localhost:3000 npm run screenshots` |
| Dependency audits | `cd backend && uv run pip-audit` and `cd frontend && npm audit --omit=dev` |
