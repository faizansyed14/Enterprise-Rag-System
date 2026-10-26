"""`rag` command line: run the services and operate the system.

    rag api | rag worker | rag scheduler           run a process
    rag db upgrade                                 apply database migrations
    rag check                                      configuration and connectivity checks
    rag demo seed                                  demo library, sources and users
    rag users create --email ... --admin           local accounts
    rag sources list | add-folder | sync           sources
    rag ask "question" --as user@example.com       ask from the terminal
    rag index status | rebuild | activate | rollback
    rag eval run --set demo                        evaluation with metrics
    rag models list | pull | test                  models per role
    rag jobs stats | retry-dead                    queue
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import typer

from rag.config.settings import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Enterprise RAG command line")
db_app = typer.Typer(no_args_is_help=True, help="Database")
users_app = typer.Typer(no_args_is_help=True, help="Users")
sources_app = typer.Typer(no_args_is_help=True, help="Sources")
index_app = typer.Typer(no_args_is_help=True, help="Index builds")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation")
models_app = typer.Typer(no_args_is_help=True, help="Models")
jobs_app = typer.Typer(no_args_is_help=True, help="Job queue")
demo_app = typer.Typer(no_args_is_help=True, help="Demo data")
privacy_app = typer.Typer(no_args_is_help=True, help="Sensitive values and outside models")
for sub, name in ((db_app, "db"), (users_app, "users"), (sources_app, "sources"), (index_app, "index"),
                  (eval_app, "eval"), (models_app, "models"), (jobs_app, "jobs"), (demo_app, "demo"), (privacy_app, "privacy")):
    app.add_typer(sub, name=name)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _services():
    from rag.jobs.handlers import load_handlers
    from rag.services import Services

    load_handlers()
    return Services.create(get_settings())


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))


# ---- processes -----------------------------------------------------------------------------------------------------------


@app.command()
def api(reload: bool = typer.Option(False, help="Reload on code changes (development)"),
        workers: int = typer.Option(1, help="Uvicorn worker processes")) -> None:
    """Run the HTTP API."""
    import uvicorn

    s = get_settings()
    uvicorn.run("rag.api.main:app", host=s.api_host, port=s.api_port, reload=reload, workers=workers,
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                log_level=s.log_level.lower(), timeout_graceful_shutdown=30)


@app.command()
def worker(lanes: str = typer.Option("control,interactive,bulk", help="Comma-separated lanes to serve"),
           kinds: str = typer.Option("", help="Only these job kinds (comma-separated)"),
           exclude_kinds: str = typer.Option("", help="Never these job kinds"),
           concurrency: int = typer.Option(0, help="Parallel jobs (default WORKER_CONCURRENCY)")) -> None:
    """Run a job worker."""
    from rag import __version__
    from rag.jobs.worker import Worker
    from rag.logging_setup import configure_logging
    from rag.monitoring.recorder import get_recorder

    s = get_settings()
    configure_logging(s)

    async def main() -> None:
        services = await _services()
        recorder = get_recorder()
        recorder.start("worker", __version__)
        w = Worker(services, lanes=[x.strip() for x in lanes.split(",") if x.strip()],
                   kinds=[x.strip() for x in kinds.split(",") if x.strip()] or None,
                   exclude_kinds=[x.strip() for x in exclude_kinds.split(",") if x.strip()] or None,
                   concurrency=concurrency or s.worker_concurrency, lease_seconds=s.job_lease_seconds)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, w.stop)
        try:
            await w.run()
        finally:
            await recorder.stop()
            await services.aclose()

    _run(main())


@app.command()
def scheduler() -> None:
    """Run the scheduler (enqueues periodic jobs and source syncs)."""
    from rag import __version__
    from rag.jobs import scheduler as sched
    from rag.logging_setup import configure_logging
    from rag.monitoring.recorder import get_recorder

    s = get_settings()
    configure_logging(s)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        recorder = get_recorder()
        recorder.start("scheduler", __version__)
        try:
            await sched.run(s, stop=stop)
        finally:
            await recorder.stop()

    _run(main())


# ---- database ------------------------------------------------------------------------------------------------------------


def _migrations_root() -> Path:
    """Where alembic.ini lives: RAG_MIGRATIONS_DIR, the source checkout (backend/), or the working directory (the
    container image copies it to /app)."""
    candidates = [os.environ.get("RAG_MIGRATIONS_DIR", ""), str(Path(__file__).resolve().parents[2]), str(Path.cwd())]
    for candidate in candidates:
        if candidate and (Path(candidate) / "alembic.ini").is_file():
            return Path(candidate)
    raise typer.BadParameter("alembic.ini not found; run from the backend folder or set RAG_MIGRATIONS_DIR")


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations (safe to run on every deploy)."""
    from alembic import command
    from alembic.config import Config

    root = _migrations_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, revision)
    typer.echo("database is up to date")


# ---- checks ---------------------------------------------------------------------------------------------------------------


@app.command()
def check() -> None:
    """Check configuration, models, database, Qdrant, object store and index."""
    from rag.config.startup_checks import run_checks

    async def main() -> int:
        services = await _services()
        try:
            results = await run_checks(services)
        finally:
            await services.aclose()
        icons = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        for r in results:
            typer.echo(f"[{icons[r.status]}] {r.name:22} {r.detail}")
        return 1 if any(r.status == "fail" for r in results) else 0

    raise typer.Exit(_run(main()))


# ---- users ---------------------------------------------------------------------------------------------------------------


@users_app.command("create")
def users_create(email: str = typer.Option(...),
                 password: str = typer.Option(..., prompt=True, hide_input=True, envvar="RAG_NEW_PASSWORD"),
                 name: str = "", admin: bool = False, groups: str = "") -> None:
    """Create a local account (groups: comma-separated local group names)."""
    from rag.db.engine import transaction
    from rag.security.auth import create_user
    from rag.security.principals import replace_principals

    async def main() -> None:
        async with transaction() as conn:
            user_id = await create_user(conn, email=email, password=password, display_name=name, is_admin=admin)
            principals = [f"lgroup:{g.strip().lower()}" for g in groups.split(",") if g.strip()]
            await replace_principals(conn, user_id, principals, "local")
        typer.echo(f"created {email} ({'admin' if admin else 'user'})")

    _run(main())


@users_app.command("set-password")
def users_set_password(email: str = typer.Option(...),
                       password: str = typer.Option(..., prompt=True, hide_input=True, envvar="RAG_NEW_PASSWORD",
                                                    help="Or set RAG_NEW_PASSWORD, so it is not typed on a command line")) -> None:
    """Change the password of a local account. Unlocks it and ends its other sessions."""
    from sqlalchemy import update

    from rag.db import schema
    from rag.db.engine import transaction
    from rag.security.auth import hash_password

    async def main() -> int:
        async with transaction() as conn:
            row = (await conn.execute(
                update(schema.users)
                .where(schema.users.c.email == email.strip(), schema.users.c.auth_provider == "local")
                .values(password_hash=hash_password(password), failed_logins=0, locked_until=None)
                .returning(schema.users.c.id))).first()
            if row is None:
                typer.echo(f"no local account {email}")
                return 1
            await conn.execute(schema.sessions.delete().where(schema.sessions.c.user_id == row.id))
        typer.echo(f"password changed for {email}; signed out everywhere")
        return 0

    raise typer.Exit(_run(main()))


# ---- sources -------------------------------------------------------------------------------------------------------------


@sources_app.command("list")
def sources_list() -> None:
    from sqlalchemy import select

    from rag.db import schema
    from rag.db.engine import transaction

    async def main() -> None:
        async with transaction() as conn:
            rows = (await conn.execute(select(schema.sources))).all()
        for r in rows:
            typer.echo(f"{r.id}  {r.kind:10} {r.name:30} {r.last_sync_status or '-':8} {json.dumps(r.stats)}")

    _run(main())


@sources_app.command("add-folder")
def sources_add_folder(name: str = typer.Option(...), path: str = typer.Option(...),
                       acl: str = typer.Option("everyone", help="Comma-separated principals"),
                       schedule_minutes: int = 60) -> None:
    """Register a folder (local disk or mounted share) as a source."""
    from rag.db import schema
    from rag.db.engine import transaction

    root = str(Path(path).resolve())

    async def main() -> None:
        async with transaction() as conn:
            source_id = (await conn.execute(schema.sources.insert().values(
                name=name, kind="filesystem", config={"root": root},
                default_acl=[a.strip() for a in acl.split(",") if a.strip()], schedule_minutes=schedule_minutes)
                .returning(schema.sources.c.id))).scalar_one()
        typer.echo(f"source {name} = {source_id}")

    _run(main())


@sources_app.command("purge")
def sources_purge(name: str, yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation")) -> None:
    """Delete a source and everything derived from it: items, versions, chunks, pages, vectors and stored files.

    Files that another source also holds (same bytes) are kept. Unlike deleting a source in the UI (which hides its
    documents and lets the garbage collector remove their files a day later) this is immediate and complete, so the same
    files can be ingested again from scratch, for instance after changing how they are parsed."""
    from sqlalchemy import text

    from rag.db.engine import transaction
    from rag.providers.storage.objects import blob_key
    from rag.providers.vectorstore.qdrant import version_filter

    async def main() -> None:
        services = await _services()
        try:
            async with transaction() as conn:
                src = (await conn.execute(text("SELECT id FROM sources WHERE name = :n"), {"n": name})).first()
                if src is None:
                    raise typer.BadParameter(f"no source named {name}")
                shas = [r[0] for r in (await conn.execute(text(
                    "SELECT DISTINCT content_sha256 FROM source_items WHERE source_id = :s AND content_sha256 IS NOT NULL"),
                    {"s": src.id})).all()]
                shared = {r[0] for r in (await conn.execute(text(
                    "SELECT DISTINCT content_sha256 FROM source_items WHERE source_id <> :s AND content_sha256 = ANY(:h)"),
                    {"s": src.id, "h": shas})).all()}
                purge = sorted(set(shas) - shared)
                versions = [str(r[0]) for r in (await conn.execute(text(
                    "SELECT id FROM versions WHERE blob_sha256 = ANY(:h)"), {"h": purge})).all()]
            if not yes:
                typer.confirm(f"Delete source {name!r}: {len(shas)} files, {len(versions)} versions?", abort=True)
            for build in await services.builds(refresh=True):
                for version_id in versions:
                    await services.vectors.delete(build.collection, version_filter(version_id))
            async with transaction() as conn:
                await conn.execute(text("DELETE FROM chunks WHERE version_id = ANY(CAST(:v AS uuid[]))"), {"v": versions})
                await conn.execute(text("DELETE FROM sources WHERE id = :s"), {"s": src.id})  # cascades to its items
                await conn.execute(text("DELETE FROM versions WHERE blob_sha256 = ANY(:h)"), {"h": purge})
                await conn.execute(text(
                    "DELETE FROM documents d WHERE NOT EXISTS (SELECT 1 FROM versions v WHERE v.document_id = d.id)"))
                await conn.execute(text("DELETE FROM pages WHERE blob_sha256 = ANY(:h)"), {"h": purge})
                await conn.execute(text("DELETE FROM blobs WHERE sha256 = ANY(:h)"), {"h": purge})
            for sha in purge:
                await services.objects.delete(blob_key(sha))
            typer.echo(f"purged {name}: {len(purge)} files, {len(versions)} versions ({len(shared)} files kept, shared)")
        finally:
            await services.aclose()

    _run(main())


@app.command()
def reprocess(source: str = typer.Option("", help="Only this source (default: every source)"),
              force: bool = typer.Option(True, help="Also chunk and embed again (so the new chunks reach the index)")) -> None:
    """Run metadata extraction (and, with --force, chunking and embedding) again from the text already stored.

    Use it after improving how documents are read or described: no file is read again, so it costs only the embedding of
    chunks whose text changed (identical chunks reuse their stored vectors)."""
    from sqlalchemy import text

    from rag.db.engine import transaction
    from rag.jobs import queue
    from rag.jobs.queue import NewJob

    async def main() -> None:
        await _services()
        async with transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT i.id, i.content_sha256, i.source_id FROM source_items i JOIN sources s ON s.id = i.source_id "
                        "JOIN blobs b ON b.sha256 = i.content_sha256 AND b.extracted_at IS NOT NULL "
                        "WHERE i.status = 'active' AND (:name = '' OR s.name = :name)"
                    ),
                    {"name": source},
                )
            ).all()
            stamp = int(__import__("time").time())
            await queue.enqueue_many(
                conn,
                [
                    NewJob(
                        "enrich",
                        {"item_id": str(r.id), "force": force},
                        lane="bulk",
                        dedupe_key=f"enrich:{r.id}:{r.content_sha256}:re{stamp}",
                        source_id=r.source_id,
                    )
                    for r in rows
                ],
            )
        typer.echo(f"queued {len(rows)} documents for reprocessing")

    _run(main())


@app.command()
def reparse(
    source: str = typer.Option("", help="Only this source (default: every source)"),
    file_format: str = typer.Option("", "--format", help="Only files of this format (pdf, docx, xlsx, ...)"),
) -> None:
    """Read the stored files again with the current parsers, then redo metadata, chunks and embeddings.

    Use it after a parser improvement (layout, tables, OCR, glyph repair): the original files are not fetched again, only the
    stored copies are read. Chunks whose text did not change reuse their stored vectors, so only changed text is embedded."""
    from sqlalchemy import text

    from rag.db.engine import transaction
    from rag.jobs import queue
    from rag.jobs.queue import NewJob

    async def main() -> None:
        await _services()
        async with transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT DISTINCT ON (b.sha256) b.sha256, i.name, i.source_id FROM source_items i "
                        "JOIN sources s ON s.id = i.source_id JOIN blobs b ON b.sha256 = i.content_sha256 "
                        "WHERE i.status = 'active' AND b.extracted_at IS NOT NULL AND (:name = '' OR s.name = :name) "
                        "AND (:fmt = '' OR b.format = :fmt) ORDER BY b.sha256"
                    ),
                    {"name": source, "fmt": file_format},
                )
            ).all()
            await queue.enqueue_many(
                conn,
                [
                    NewJob(
                        "extract",
                        {"sha256": r.sha256, "name": r.name, "force": True},
                        lane="bulk",
                        dedupe_key=f"extract:{r.sha256}:force",
                        source_id=r.source_id,
                    )
                    for r in rows
                ],
            )
        typer.echo(f"queued {len(rows)} files to be read again")

    _run(main())


@sources_app.command("sync")
def sources_sync(name: str, full: bool = False) -> None:
    from sqlalchemy import select

    from rag.db import schema
    from rag.db.engine import transaction
    from rag.jobs import queue
    from rag.jobs.queue import NewJob

    async def main() -> None:
        async with transaction() as conn:
            row = (await conn.execute(select(schema.sources.c.id).where(schema.sources.c.name == name))).first()
            if row is None:
                raise typer.BadParameter(f"no source named {name}")
            await queue.enqueue(
                conn,
                NewJob("sync_source", {"source_id": str(row.id), "full": full}, lane="interactive", dedupe_key=f"sync:{row.id}"),
            )
        typer.echo("sync queued; a worker will pick it up")

    _run(main())


# ---- demo ------------------------------------------------------------------------------------------------------------------


DEMO_USERS = [
    ("admin@demo.local", "Admin", True, []),
    ("engineer@demo.local", "Site Engineer", False, []),
    ("commercial@demo.local", "Commercial Manager", False, ["commercial"]),
]


@demo_app.command("seed")
def demo_seed(
    path: str = "./data/demo",
    password: str = typer.Option("ChangeMe-Demo-2025", help="Demo password"),
    wait: bool = typer.Option(False, help="Process the jobs here instead of waiting for workers"),
) -> None:
    """Write the demo library, register it as two sources and create demo users."""
    from sqlalchemy import func, select

    from rag.db import schema
    from rag.db.engine import transaction
    from rag.evaluation.demo_corpus import build_demo_corpus
    from rag.jobs import queue
    from rag.jobs.queue import NewJob
    from rag.security.auth import create_user
    from rag.security.principals import replace_principals

    root = Path(path).resolve()
    folders = build_demo_corpus(root)

    async def main() -> None:
        async with transaction() as conn:
            for email, name, is_admin, groups in DEMO_USERS:
                exists = (
                    await conn.execute(
                        select(func.count()).select_from(schema.users).where(func.lower(schema.users.c.email) == email)
                    )
                ).scalar_one()
                if not exists:
                    user_id = await create_user(conn, email=email, password=password, display_name=name, is_admin=is_admin)
                    await replace_principals(conn, user_id, [f"lgroup:{g}" for g in groups], "local")
            ids = []
            for name, folder, acl in (
                ("Demo library", folders["public"], ["everyone"]),
                ("Demo commercial", folders["restricted"], ["lgroup:commercial"]),
            ):
                row = (await conn.execute(select(schema.sources.c.id).where(schema.sources.c.name == name))).first()
                if row is None:
                    row = (
                        await conn.execute(
                            schema.sources.insert()
                            .values(
                                name=name, kind="filesystem", config={"root": str(folder)}, default_acl=acl, schedule_minutes=60
                            )
                            .returning(schema.sources.c.id)
                        )
                    ).first()
                ids.append(row.id)
                await queue.enqueue(
                    conn, NewJob("sync_source", {"source_id": str(row.id)}, lane="interactive", dedupe_key=f"sync:{row.id}")
                )
        typer.echo(f"demo library written to {root}; sources queued for sync")
        typer.echo("users: " + ", ".join(u[0] for u in DEMO_USERS) + f" (password: {password})")
        if wait:
            from rag.jobs.worker import Worker

            services = await _services()
            try:
                await Worker(services, concurrency=4, idle_poll_s=0.2).run(until_idle=True)
            finally:
                await services.aclose()
            typer.echo("demo library ingested")

    _run(main())


# ---- ask ------------------------------------------------------------------------------------------------------------------


@app.command()
def ask(
    question: str,
    as_user: str = typer.Option(..., "--as", help="E-mail of the user whose access applies"),
    model: str = typer.Option("", help="A MODEL_CHOICES line to use instead of LLM_ANSWER"),
) -> None:
    """Ask a question from the terminal (prints the answer, citations and timings)."""
    from sqlalchemy import func, select

    from rag.answering.agent import Answerer, AnswerOutcome
    from rag.db import schema
    from rag.db.engine import transaction
    from rag.security.principals import principals_for

    async def main() -> None:
        services = await _services()
        try:
            async with transaction() as conn:
                user = (
                    await conn.execute(select(schema.users).where(func.lower(schema.users.c.email) == as_user.lower()))
                ).first()
                if user is None:
                    raise typer.BadParameter(f"no user {as_user}")
                principals = await principals_for(conn, user)
            outcome = AnswerOutcome()
            async for event in Answerer(services).answer(
                question, principals=principals, model_choice=model or None, outcome=outcome
            ):
                if event["event"] == "delta":
                    typer.echo(event["data"]["text"], nl=False)
                elif event["event"] == "status":
                    typer.secho(f"\n» {event['data']['message']}", dim=True)
                elif event["event"] == "citation":
                    c = event["data"]
                    mark = "✓" if c["verified"] else "?"
                    typer.secho(
                        f"\n[{c['n']}] {mark} {c['doc_number'] or c['file_name']} " f"p.{c['page']}: “{c['quote'][:160]}”",
                        fg="cyan",
                    )
                elif event["event"] == "done":
                    d = event["data"]
                    typer.secho(
                        f"\nstatus={d['status']} model={d['model']} grounded={d['groundedness']} "
                        f"total={d['timings_ms'].get('total')}ms",
                        dim=True,
                    )
                elif event["event"] == "error":
                    typer.secho(f"\nerror: {event['data']['message']}", fg="red")
        finally:
            await services.aclose()

    _run(main())


# ---- index -----------------------------------------------------------------------------------------------------------------


@index_app.command("status")
def index_status() -> None:
    from rag.ingestion import index_builds

    async def main() -> None:
        services = await _services()
        try:
            _print(await index_builds.status(services))
        finally:
            await services.aclose()

    _run(main())


@index_app.command("rebuild")
def index_rebuild() -> None:
    """Build a new collection for the current EMBEDDER/analyzer/chunker; search switches when it is complete."""
    from rag.ingestion import index_builds

    async def main() -> None:
        services = await _services()
        try:
            _print(await index_builds.start_rebuild(services))
        finally:
            await services.aclose()

    _run(main())


@index_app.command("activate")
def index_activate(build_id: str) -> None:
    from rag.ingestion import index_builds

    async def main() -> None:
        services = await _services()
        try:
            _print(await index_builds.activate(services, build_id))
        finally:
            await services.aclose()

    _run(main())


@index_app.command("rollback")
def index_rollback() -> None:
    from rag.ingestion import index_builds

    async def main() -> None:
        services = await _services()
        try:
            _print(await index_builds.rollback(services))
        finally:
            await services.aclose()

    _run(main())


# ---- cache -----------------------------------------------------------------------------------------------------------------

cache_app = typer.Typer(help="The shared cache (Redis)", no_args_is_help=True)
app.add_typer(cache_app, name="cache")


@cache_app.command("stats")
def cache_stats() -> None:
    """Hit rates, entries and memory of the shared cache."""
    from rag.cache import get_cache

    async def main() -> None:
        _print(await get_cache().stats())

    _run(main())


@cache_app.command("clear")
def cache_clear() -> None:
    """Make every cached answer out of date at once (embeddings and rerank scores stay: they never change)."""
    from rag.cache import get_cache

    async def main() -> None:
        cache = get_cache()
        await cache.bump_epoch()
        typer.echo(f"cached answers invalidated (epoch {await cache.epoch()})" if cache.enabled else "Redis is not configured")

    _run(main())


# ---- evaluation ------------------------------------------------------------------------------------------------------------


@eval_app.command("run")
def eval_run(
    golden_set: str = typer.Option("demo", "--set"),
    answer: bool = True,
    as_user: str = typer.Option("", "--as", help="Search with this user's access (default: everyone)"),
    output: str = typer.Option("", help="Write per-question results as JSON to this file"),
    min_hit5: float = typer.Option(0.0, help="Exit 1 if hit@5 is below this (CI gate)"),
) -> None:
    from sqlalchemy import func, select

    from rag.db import schema
    from rag.db.engine import transaction
    from rag.evaluation.golden import load_set
    from rag.evaluation.runner import evaluate
    from rag.security.principals import principals_for

    async def main() -> int:
        services = await _services()
        try:
            principals = ["everyone"]
            if as_user:
                async with transaction() as conn:
                    user = (
                        await conn.execute(select(schema.users).where(func.lower(schema.users.c.email) == as_user.lower()))
                    ).first()
                    principals = await principals_for(conn, user)
            rows, metrics = await evaluate(services, load_set(golden_set), principals, answer=answer)
        finally:
            await services.aclose()
        for r in rows:
            m = r["metrics"]
            typer.echo(
                f"{r['item_id']:20} hit@5={m.get('hit@5', '-')!s:5} mrr={m.get('mrr', '-')!s:6} "
                f"facts={m.get('facts', '-')!s:5} status={m.get('status', '-')!s:10} {r['latency_ms']:.0f}ms"
            )
        _print(metrics)
        if output:
            payload = json.dumps({"metrics": metrics, "items": rows}, indent=2, default=str, ensure_ascii=False)
            await asyncio.to_thread(Path(output).write_text, payload, encoding="utf-8")
        return 1 if min_hit5 and metrics.get("hit@5", 0) < min_hit5 else 0

    raise typer.Exit(_run(main()))


@eval_app.command("bench")
def eval_bench(
    golden: str = typer.Option(..., help="JSONL question file (see rag.evaluation.retrieval_bench)"),
    as_user: str = typer.Option("", "--as", help="Search with this user's access (default: everyone)"),
    output: str = typer.Option("", help="Write per-question results and the summary as JSON"),
    rerank: bool = typer.Option(True, help="Use the reranker when one is configured"),
    set_: list[str] = typer.Option([], "--set", help="Override a setting for this run: name=value"),
) -> None:
    """Retrieval-only benchmark (no language model): document, page and citation hit rates per question kind."""
    from sqlalchemy import func, select

    from rag.db import schema
    from rag.db.engine import transaction
    from rag.evaluation import retrieval_bench
    from rag.security.principals import principals_for

    async def main() -> None:
        services = await _services()
        try:
            for pair in set_:
                key, _, value = pair.partition("=")
                current = getattr(services.settings, key)
                setattr(
                    services.settings,
                    key,
                    type(current)(value) if not isinstance(current, bool) else value.lower() in {"1", "true", "yes"},
                )
            principals = ["everyone"]
            if as_user:
                async with transaction() as conn:
                    user = (
                        await conn.execute(select(schema.users).where(func.lower(schema.users.c.email) == as_user.lower()))
                    ).first()
                    principals = await principals_for(conn, user)
            rows, summary = await retrieval_bench.run(services, retrieval_bench.load_questions(golden), principals, rerank=rerank)
        finally:
            await services.aclose()
        typer.echo(retrieval_bench.format_table(summary))
        if output:
            payload = json.dumps({"summary": summary, "items": rows}, indent=1, default=str, ensure_ascii=False)
            await asyncio.to_thread(Path(output).write_text, payload, encoding="utf-8")

    _run(main())


# ---- models ----------------------------------------------------------------------------------------------------------------


@models_app.command("list")
def models_list() -> None:
    """The model each role uses, where it is configured, and what is missing."""

    async def main() -> None:
        services = await _services()
        try:
            await services.hub.refresh_overrides()  # the Models page choices, not only .env
            rows = services.hub.describe()
        finally:
            await services.aclose()
        for row in rows:
            missing = f"  missing: {', '.join(row['missing'])}" if row["missing"] else ""
            where = "local" if row["local"] else "cloud"
            typer.echo(f"{row['role']:20} {row['label']:48} {where:5}{missing}")

    _run(main())


@models_app.command("pull")
def models_pull(names: list[str]) -> None:
    """Download local models into MODELS_DIR (runs the model server's downloader)."""
    try:
        from rag_model_server.cli import pull
    except ImportError:
        typer.echo("Run this on the model server: rag-model-server pull " + " ".join(names))
        raise typer.Exit(1) from None
    raise typer.Exit(1 if pull(names, Path(os.environ.get("MODELS_DIR", "./models"))) else 0)


@models_app.command("test")
def models_test(role: str) -> None:
    """Make one real call with the model of a role."""

    async def main() -> None:
        from rag.api.routers.models import run_role_test

        services = await _services()
        try:
            _print(await run_role_test(services, role.upper()))
        finally:
            await services.aclose()

    _run(main())


# ---- jobs ------------------------------------------------------------------------------------------------------------------


@jobs_app.command("stats")
def jobs_stats() -> None:
    from rag.db.engine import transaction
    from rag.jobs import queue

    async def main() -> None:
        async with transaction() as conn:
            _print(await queue.queue_stats(conn))

    _run(main())


@jobs_app.command("retry-dead")
def jobs_retry_dead() -> None:
    from sqlalchemy import text

    from rag.db.engine import transaction
    from rag.jobs import queue

    async def main() -> None:
        async with transaction() as conn:
            ids = [r[0] for r in (await conn.execute(text("SELECT id FROM jobs WHERE status = 'dead'"))).all()]
        typer.echo(f"re-queued {await queue.retry(ids)} jobs")

    _run(main())


@privacy_app.command("check")
def privacy_check(
    document: str = typer.Argument(..., help="Part of the file name"),
    full: bool = typer.Option(False, "--full", help="Print every chunk, not only those with a masked value"),
) -> None:
    """Show what a cloud model would receive from a document: its text with bank and id numbers replaced by placeholders.

    The page you see in the Documents view is your own server's copy and is not what is sent. This is what is sent."""
    import re

    from sqlalchemy import text

    from rag.db.engine import transaction
    from rag.privacy.shield import Vault, find

    settings = get_settings()
    kinds = settings.privacy_kind_list

    async def main() -> None:
        async with transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT i.name, c.chunk_no, c.page_start, c.header, c.text FROM chunks c "
                        "JOIN versions v ON v.id = c.version_id "
                        "JOIN source_items i ON i.content_sha256 = v.blob_sha256 WHERE v.state = 'live' AND i.name ILIKE :n "
                        "ORDER BY i.name, c.chunk_no"
                    ),
                    {"n": f"%{document}%"},
                )
            ).all()
        if not rows:
            typer.echo(f"no stored document matches {document!r}")
            raise typer.Exit(1)
        typer.echo(
            f"privacy shield: {'ON' if settings.privacy_mask_for_models else 'OFF'}   kinds: {', '.join(kinds)}   "
            f"whole-document hold-back: {'ON' if settings.privacy_auto_local_only else 'off'}\n"
        )
        total: dict[str, int] = {}
        unmasked_runs = 0
        for name, chunk_no, page, header, body in rows:
            raw = f"{header}\n{body}"
            vault = Vault(kinds)
            masked = vault.mask(raw)
            found = [f.kind for f in find(raw, kinds)]
            for kind in found:
                total[kind] = total.get(kind, 0) + 1
            leftovers = [line for line in masked.splitlines() if re.search(r"\d{9,}", line)]
            unmasked_runs += len(leftovers)
            if found or leftovers or full:
                typer.echo(f"--- {name}  chunk {chunk_no}  page {page}   masked: {', '.join(found) or 'nothing'}")
                typer.echo(masked[:1500])
                if leftovers:
                    typer.echo(
                        f"    ! {len(leftovers)} line(s) still hold a run of 9+ digits (not recognised as a bank or id value)"
                    )
        typer.echo(
            f"\n{len(rows)} chunks; masked values: {total or 'none'}; "
            f"lines with long digit runs left as they are: {unmasked_runs}"
        )

    _run(main())


@privacy_app.command("calls")
def privacy_calls(hours: int = typer.Option(24, help="How far back"), limit: int = typer.Option(30)) -> None:
    """Recent calls to cloud chat and vision models and how many sensitive values the shield masked in each (a count,
    never a value)."""
    from sqlalchemy import text

    from rag.db.engine import transaction

    async def main() -> None:
        async with transaction() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT started_at, kind, role, provider, model, status, attrs->>'masked_values' AS masked FROM spans "
                        "WHERE kind IN ('chat','vision') AND started_at > now() - make_interval(hours => :h) "
                        "ORDER BY started_at DESC LIMIT :n"
                    ),
                    {"h": hours, "n": limit},
                )
            ).all()
        if not rows:
            typer.echo("no model calls in that period")
            return
        typer.echo(f"{'time':<9}{'role':<20}{'model':<46}{'status':<8}shield")
        for at, kind, role, provider, model, status, masked in rows:
            shield = (
                "not recorded: an image sent after a local check, a local model, or a call made before this was tracked"
                if masked in (None, "null")
                else f"on, {masked} value(s) masked"
            )
            typer.echo(f"{at:%H:%M:%S}  {role or kind:<20}{provider}/{model}"[:77].ljust(77) + f"{status:<8}{shield}")

    _run(main())


if __name__ == "__main__":
    sys.exit(app())
