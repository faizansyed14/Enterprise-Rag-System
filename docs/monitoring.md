# Monitoring

Monitoring is built in. Open **Monitor** (administrators) for everything below; nothing else needs to run.
Prometheus and Grafana are optional extras for teams that already use them.

There is no Langfuse or other paid observability dependency. Everything is stored in your own Postgres.

## The Monitor page

| Section | Shows |
|---|---|
| Header strip | Open alerts, model spend today against the daily budget, live documents, processes online |
| Window | Last hour, 6 hours, day, week or month; figures update every 2 seconds in short windows (server-sent events) |
| Questions | Questions per period and how many failed or found nothing |
| Answer time | 95th-percentile answer time per period |
| Model calls | Every call to a model, by role and model: calls, errors, tokens, cost, average and slow latency |
| Calls over time | One small chart per kind of call, each on its own scale |
| Queues | Waiting, running, retrying and dead jobs per lane, and the age of the oldest runnable job |
| Processes | API, worker, control worker and scheduler heartbeats |
| Failed jobs | Files that could not be read, with the reason; retry or cancel |
| Traces | Recent questions with the plan, each retrieval leg, rerank, answer and verification step; open one to see its spans, timings, counts and the model used. Traces hold ids, timings, counts and model names, not the question or answer text |

Retention: traces and spans `MONITOR_RETENTION_DAYS` (default 14), minute rollups `METRIC_RETENTION_DAYS` (default 365).
Tables are partitioned by day, so expiry is a cheap partition drop. Rollups are written once per minute per process, so
an API with many workers adds no per-request database load beyond the trace itself.

## Alerts

Evaluated every 30 seconds. An alert opens once, updates while it keeps firing, and resolves by itself.

| Alert | Fires when |
|---|---|
| `control_lane_stuck` | A permission change or deletion has waited more than 5 minutes |
| `dead_letters` | Jobs failed for good |
| `budget_80` | Today's model spend passed 80% of `DAILY_BUDGET_USD` |
| `error_spike` | More than 20% of model calls failed in 5 minutes (with at least 10 calls) |
| `worker_missing` | No worker heartbeat for 2 minutes while jobs are waiting |
| `reconcile_repairs` | The daily reconcile found and repaired differences between Postgres and Qdrant |

Set `ALERT_WEBHOOK_URL` and `ALERT_WEBHOOK_KIND` (`teams`, `slack` or `generic`) to be told when an alert opens. The
generic payload is JSON with `rule`, `severity`, `message` and `details`. Alerts are also listed on the Monitor page.

## Which model is active

- Each answer shows the model that wrote it under the text (`SHOW_MODEL_BADGE`), with the vendor mark.
- The **Models** page lists every role, its current model, where it comes from (`.env` or an administrator's switch),
  missing keys, and a **Test** button that makes one real call.
- Every switch is recorded (who, when, from, to) and shown in the audit log.

## Prometheus and Grafana (optional)

```bash
scripts/monitoring.sh start       # Prometheus on 127.0.0.1:9090, Grafana on 127.0.0.1:3001
scripts/monitoring.sh stop
scripts/monitoring.sh status
```

Prometheus scrapes the API's `/metrics` over the internal network; nginx does not expose that path. Because the figures
come from the shared database rollups, one scrape covers every API and worker process. Metrics:

| Metric | Meaning |
|---|---|
| `rag_questions_5m{status}` | Questions in the last 5 minutes |
| `rag_answer_p95_ms_5m` | Answer time, 95th percentile |
| `rag_model_calls_24h{...}` | Model calls in 24 hours by role, model and outcome |
| `rag_model_latency_avg_ms{...}` | Average model latency over 5 minutes |
| `rag_cost_usd_today`, `rag_daily_budget_usd` | Spend and its budget |
| `rag_queue_jobs{lane,status}`, `rag_queue_oldest_ready_seconds{lane}` | Queue depth and age by lane |
| `rag_processes_online{kind}` | Live processes by kind |
| `rag_live_documents`, `rag_open_alerts` | Library size and open alerts |

Grafana is provisioned with the Prometheus data source and the *ALAIN overview* dashboard (15 panels). Sign in as
`admin` with `GRAFANA_ADMIN_PASSWORD` from `.env`. To expose Grafana to others, put it behind your VPN or a reverse
proxy with authentication; it is bound to localhost on purpose.

If you set `METRICS_TOKEN`, the API requires `Authorization: Bearer <token>` on `/metrics`; add the token to
Prometheus' scrape configuration (the commented `authorization` block in `deploy/prometheus/prometheus.yml`, with the
file mounted into the container). Qdrant's own metrics can be scraped too; the commented job in the same file explains
how.

## What to watch in production

| Symptom | Look at |
|---|---|
| Answers slow | Answer time chart, then the slowest trace: which span (embed, search, rerank, answer model) |
| Answers wrong or empty | Retrieval Lab with the question: which leg found what, and what rerank did; then the Evaluation page |
| Library not growing | Queues (waiting vs running), Processes (workers online), Failed jobs |
| Newly shared file not found | Source status and last sync on Sources; `control_lane_stuck`; job queue on the interactive lane |
| Cost climbing | Model calls by role; consider cheaper `LLM_ROUTER` / `LLM_ENRICH`, or lower `VISION_PAGES_PER_DAY` |
| One answer looks wrong | The **Details** (info) button under the answer: the live trace of that answer (plan, every search with candidate scores, files read, each model call with its exact masked messages, citation checks). It is not stored, so open it while the page is still loaded. |
| Qdrant `points/count` calls every few seconds in the logs | Before the 2026-10-23 scheduler fix, finished periodic jobs were queued again on every 5 s tick, so the daily `reconcile` ran continuously. After the fix, each periodic slot runs once. If the calls come back, check the `jobs` table for repeated `periodic:reconcile:*` keys. |

## Periodic jobs

Every 5 seconds the scheduler queues the jobs that are due, once per time slot (`jobs/scheduler.py`):

| Job | Every | What it does |
|---|---|---|
| `janitor` | 30 s | Puts back jobs whose worker stopped renewing its lease |
| `monitor_tick` | 30 s | Evaluates the alert rules |
| `resume_stalled` | 2 min | Picks up items that were read but never enriched |
| `maintenance` | 1 h | Partitions, retention, expired sessions, finished jobs, abandoned ZIP staging folders |
| `gc_blobs` | 6 h | Removes stored files nothing uses (up to 500 per run) |
| `reconcile` | 24 h | Compares Postgres with Qdrant per version and repairs differences (raises `reconcile_repairs`) |
