"""Alembic environment.

Async, because the app is: the engine is built with `async_engine_from_config` and the
migration body runs through `connection.run_sync`. Handing a `postgresql+asyncpg://` URL to a
synchronous engine fails with "the asyncio extension requires an async driver", which is the
single most common way an otherwise-correct Alembic setup dies on first run.

The URL comes from `app.config`, never from alembic.ini. Resolution order:
  1. `alembic -x url=...`   — one-off, e.g. pointing at a scratch database
  2. `DATABASE_URL`         — the real one; what Railway's preDeployCommand uses
  3. `PENNY_TEST_DATABASE_URL` — so `docker compose up -d` + one export is enough locally
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db import to_asyncpg_url
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate diffs against this. Every model must be imported by app.models for a table to
# be visible here; a model defined elsewhere and never imported gets silently DROPPED.
target_metadata = Base.metadata


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Never autogenerate a change to a table we do not own.

    GOWA's whatsmeow session store lives on this same Postgres, so `alembic revision
    --autogenerate` sees its `whatsmeow_*` tables as extras and would cheerfully emit
    `op.drop_table(...)` for every one of them. Dropping the session store means a manual QR
    re-pair requiring a family member's physical phone — a catastrophic outcome for a command
    that reads like a routine schema refresh.
    """
    return not (type_ == "table" and reflected and name not in target_metadata.tables)


def database_url() -> str:
    settings = get_settings()
    url = context.get_x_argument(as_dictionary=True).get("url")
    url = url or settings.database_url or settings.test_database_url
    if not url:
        raise RuntimeError(
            "No database URL. Set DATABASE_URL, or PENNY_TEST_DATABASE_URL, or pass -x url=..."
        )
    return to_asyncpg_url(url)


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catches a column whose type changed in models.py but not in the database — the
        # failure mode where everything looks migrated and inserts start rejecting.
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": database_url()},
        prefix="sqlalchemy.",
        # NullPool: this process runs one migration and exits. A pool would just delay it.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql` — emit DDL without connecting to anything."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
