"""Alembic environment for packages/hipaa-logger.

Two things here are deliberate and load-bearing:

1. ``version_table`` is ``alembic_version_hipaa_logger``, not Alembic's default.
   Several migration setups in this monorepo share one database; on the default
   table each would read another's revision as its own head and the histories
   would corrupt each other. Every setup namespaces its own — see constraint 8
   in TASKS.md.
2. The engine is async (SQLAlchemy + asyncpg). The package uses raw asyncpg at
   runtime, so an async engine here avoids pulling in a second, sync driver.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from hipaa_logger.db import resolve_dsn, sqlalchemy_dsn

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# This package writes SQL by hand rather than reflecting models, so autogenerate
# has no metadata to compare against.
target_metadata = None

VERSION_TABLE = "alembic_version_hipaa_logger"


def get_url() -> str:
    """Resolve the database URL, normalizing whatever spelling DATABASE_URL uses."""
    return sqlalchemy_dsn(resolve_dsn())


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-established connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run the migrations through it."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for a normal (connected) migration run."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
