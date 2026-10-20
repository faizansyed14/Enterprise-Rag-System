"""Async database engine (SQLAlchemy Core over psycopg 3).

PgBouncer in transaction mode cannot keep server-side prepared statements, so DB_PGBOUNCER=true turns them off.
Nothing in this code base relies on session state (no session advisory locks, no temp tables, no SET without LOCAL),
so transaction pooling is safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from rag.config.settings import Settings, get_settings
from rag.db.schema import PARTITIONED_DAILY, PARTITIONED_HASH

_engine: AsyncEngine | None = None
_engine_loop: asyncio.AbstractEventLoop | None = None


def make_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict[str, object] = {
        "options": f"-c statement_timeout={settings.db_statement_timeout_ms} -c timezone=UTC",
        "application_name": f"rag-{settings.app_env}",
    }
    if settings.db_pgbouncer:
        connect_args["prepare_threshold"] = None
        connect_args.pop("options")  # PgBouncer rejects startup options; set them on the role instead
    return create_async_engine(
        settings.sqlalchemy_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


def get_engine() -> AsyncEngine:
    """Process-wide engine, re-created if the event loop changed (tests run one loop per test module)."""
    global _engine, _engine_loop
    loop = asyncio.get_running_loop()
    if _engine is None or _engine_loop is not loop:
        _engine = make_engine(get_settings())
        _engine_loop = loop
    return _engine


async def dispose_engine() -> None:
    global _engine, _engine_loop
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _engine_loop = None


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncConnection]:
    async with get_engine().begin() as conn:
        yield conn


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    async with get_engine().connect() as conn:
        yield conn


# ---- partition maintenance -------------------------------------------------------------------------------------------


def hash_partition_ddl(table: str, partitions: int) -> list[str]:
    return [
        f"CREATE TABLE IF NOT EXISTS {table}_p{i:02d} PARTITION OF {table} "
        f"FOR VALUES WITH (MODULUS {partitions}, REMAINDER {i})"
        for i in range(partitions)
    ]


def daily_partition_ddl(table: str, day: date) -> str:
    nxt = day + timedelta(days=1)
    return (
        f"CREATE TABLE IF NOT EXISTS {table}_{day:%Y%m%d} PARTITION OF {table} "
        f"FOR VALUES FROM ('{day.isoformat()}') TO ('{nxt.isoformat()}')"
    )


def initial_partition_ddl(today: date | None = None) -> list[str]:
    today = today or datetime.now(UTC).date()
    statements: list[str] = []
    for table, count in PARTITIONED_HASH.items():
        statements += hash_partition_ddl(table, count)
    for table in PARTITIONED_DAILY:
        statements.append(f"CREATE TABLE IF NOT EXISTS {table}_default PARTITION OF {table} DEFAULT")
        for offset in range(-1, 4):
            statements.append(daily_partition_ddl(table, today + timedelta(days=offset)))
    return statements


async def maintain_partitions(conn: AsyncConnection, retention_days: int, days_ahead: int = 3) -> dict[str, int]:
    """Create the next few daily partitions and drop days older than the retention window."""
    today = datetime.now(UTC).date()
    created = dropped = 0
    for table in PARTITIONED_DAILY:
        for offset in range(days_ahead + 1):
            await conn.execute(text(daily_partition_ddl(table, today + timedelta(days=offset))))
            created += 1
        rows = await conn.execute(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = :table"
            ),
            {"table": table},
        )
        cutoff = today - timedelta(days=retention_days)
        for (name,) in rows.all():
            suffix = name.rsplit("_", 1)[-1]
            if not (suffix.isdigit() and len(suffix) == 8):
                continue
            day = datetime.strptime(suffix, "%Y%m%d").replace(tzinfo=UTC).date()
            if day < cutoff:
                # Table name comes from the catalogue and matches <table>_<yyyymmdd>; it is not user input.
                await conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
                dropped += 1
    return {"ensured": created, "dropped": dropped}
