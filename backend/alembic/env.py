"""Alembic environment: runs migrations with the synchronous psycopg driver against DATABASE_URL."""

from __future__ import annotations

import re

from alembic import context
from sqlalchemy import create_engine, pool

from rag.config.settings import get_settings
from rag.db.schema import metadata

target_metadata = metadata

# Partition children are created by SQL in the migrations and by the scheduler; autogenerate must not see them as
# tables to drop.
_PARTITION_CHILD = re.compile(r"^(pages|chunks)_p\d{2}$|^(traces|spans)_(\d{8}|default)$")


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001 - alembic hook signature
    return not (type_ == "table" and name and _PARTITION_CHILD.match(name))


def _url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or get_settings().sqlalchemy_url(async_driver=True)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The psycopg 3 dialect works synchronously too; the same URL serves the app (async) and migrations (sync).
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=False,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
