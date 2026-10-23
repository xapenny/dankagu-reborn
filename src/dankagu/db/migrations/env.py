"""Alembic environment for dankagu-reborn.

Two execution modes are supported:

``embedded`` (the normal deployment path)
    :func:`dankagu.db.migrate.run_migrations` passes the *sync* SQLAlchemy
    connection it already holds in ``config.attributes["connection"]``. The
    migration therefore runs on the same connection that owns the PostgreSQL
    advisory lock, and no second engine is created.

``standalone`` (``alembic upgrade head``, ``alembic check``, CI)
    No connection is supplied, so an async engine is built from
    ``DANKAGU_DB_URL`` / ``.env``. This project only installs ``asyncpg`` - there
    is no synchronous driver - hence the async engine plus ``run_sync``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import dankagu.models  # noqa: F401  (import for side effect: registers every table)
from dankagu.config import settings
from dankagu.core.database import Base
from dankagu.db.schema_scope import include_object

config = context.config

# ``migrate.alembic_config()`` sets configure_logger=False because the application
# (or the CLI) already configured logging. Applying alembic.ini's logging on top
# would reset the root logger to WARNING and swallow the caller's own messages.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    # disable_existing_loggers=False: the application configures its own loggers
    # before migrations run, and Alembic must not silence them.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.db_url)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    """Point Alembic at one connection and the ORM metadata."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type / server-default drift as well as table presence.
        compare_type=True,
        compare_server_default=True,
        # One transaction per revision: a failing revision leaves the database at
        # the previous revision instead of half-applied.
        transaction_per_migration=True,
        include_schemas=False,
        # Audit tables written by data-repair revisions are not ORM models.
        include_object=include_object,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_async() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        _run_migrations(shared_connection)
    else:
        asyncio.run(_run_migrations_async())
