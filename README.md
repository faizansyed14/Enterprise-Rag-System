# Enterprise RAG

Question answering over a construction document library (specifications, drawings, RFIs, submittals, minutes, BOQs,
reports) with cited, page-level evidence. Built for a library of about 5 TB, with a hard rule: **every answer shows
the document and page it came from, and the user can only ever see what their own access allows.**


## What it does

- **Finds the right document first.** Dense vectors, BM25 sparse vectors and an exact identifier leg (drawing numbers,
  RFI numbers, clause numbers) are searched together and fused per query type, then reranked. Near-duplicate copies of
  the same document are grouped into a family so the register copy wins, not a stray copy in an archive folder.
- **Answers only from evidence.** Each answer sentence carries a citation; quotes are verified against the page text,
  groundedness is checked, and when the library does not answer the question the system says so.
- **Respects access control.** Permissions are read from SharePoint/OneDrive (users, groups, site groups, sharing
  links), stored per document version and enforced inside the vector search filter — and re-checked in Postgres before
  anything is shown.
- **Mixes local and cloud models line by line.** One `.env` line per role: `LLM_ANSWER=openrouter/anthropic/claude-haiku-5.5`,
  `EMBEDDER=local/multilingual-e5-small`, `OCR=azure_di/prebuilt-read`. The active model is shown on every answer.
- **Watches itself.** A built-in Monitor page (traces, latency, queue, spend, alerts) needs nothing else running.
  Prometheus and Grafana are optional.

## Run it

Requirements: Docker with Compose v2, bash (WSL2 on Windows), about 8 GB of free RAM.

```bash
scripts/start.sh          # creates .env with random secrets, downloads the local models, starts everything
scripts/demo.sh           # optional: loads a sample library and demo users
open http://localhost     # sign in with the admin e-mail and password start.sh printed
```

Add an `OPENROUTER_API_KEY` (or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) to `.env` and run `scripts/restart.sh` to get
LLM-written answers. Without any key, set the answer roles to `extractive/default` (see the end of `.env.example`)
and answers are assembled from the retrieved sentences — useful for a first look and for tests.

Every command for starting, stopping, monitoring, backing up and testing is in **[commands.md](commands.md)**.

## Documentation

| Document | Contents |
|---|---|
| [commands.md](commands.md) | Every command: start, stop, Prometheus, backup, users, tests |
| [docs/architecture.md](docs/architecture.md) | Services, data flow, ingestion pipeline, data model |
| [docs/configuration.md](docs/configuration.md) | `.env` roles, providers, path rules, fusion weights |
| [docs/retrieval.md](docs/retrieval.md) | How a question becomes cited evidence, and why |
| [docs/parsing.md](docs/parsing.md) | PDF, scan, table and image reading; measured comparison; Docling status |
| [docs/scaling.md](docs/scaling.md) | Sizing for 5 TB, what scales how, what is not yet proven |
| [docs/security.md](docs/security.md) | Authentication, access control, uploads, hardening checklist |
| [docs/connectors.md](docs/connectors.md) | SharePoint, OneDrive, Windows shares, folders; Entra app registration |
| [docs/monitoring.md](docs/monitoring.md) | Monitor page, alerts, Prometheus and Grafana |
| [docs/evaluation.md](docs/evaluation.md) | Golden question sets and the metrics |
| [docs/operations.md](docs/operations.md) | Upgrades, backups, index rebuilds, troubleshooting |
| [docs/deviations.md](docs/deviations.md) | Where this build differs from the design document, and what is unverified |

## Repository layout

```
backend/            Python 3.12 service (FastAPI API, workers, scheduler, CLI) and the local ONNX model server
frontend/           Next.js 16 web app
deploy/             nginx edge proxy, Prometheus and Grafana provisioning
scripts/            Run, stop, monitor, back up, test (all wrap docker compose)
tests/              unit, integration, contract, model-server and evaluation tests
docs/               this documentation
docker-compose.yml     production stack
docker-compose.dev.yml local development (add it with COMPOSE_FILE in .env, or -f)
```

## Tests

`scripts/test.sh` runs ruff, bandit, the backend suite (unit, integration against real Postgres and Qdrant, contract
tests for the cloud providers and Microsoft Graph with recorded responses, model server, evaluation) and the frontend
typecheck, lint and production build. Tests that would need paid API keys use recorded or simulated responses; nothing
in the suite calls a paid service.

## Licences and marks

Service and model-vendor marks in the interface are drawn from open icon packages at build time (Simple Icons, Lobe
Icons, Fluent UI branded icons). They remain trademarks of their owners and are used only to identify the service.
Run `npm audit` and `uv run pip-audit` before each release.
