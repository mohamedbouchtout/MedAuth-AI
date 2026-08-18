"""Alembic environment for services/track-a-clinical.

Three things here are deliberate:

1. ``version_table`` is ``alembic_version_track_a_clinical``, not Alembic's
   default. This history shares a database with packages/hipaa-logger's; on the
   default table each would read the other's revision as its own head and both
   would corrupt. Every Alembic setup in this repo namespaces its own — see
   "Alembic version table isolation" in CLAUDE.md.
2. ``target_metadata`` is the models' metadata rather than ``None``. These tables
   have mapped classes that other services import, so autogenerate is what proves
   the classes and the migration history still describe the same schema.
3. The engine is async (SQLAlchemy + asyncpg), matching the driver the service
   uses at runtime, so no second sync driver has to be installed.

The settings this file shares with the service and its tests live in
``track_a_clinical.db`` — importing this module runs migrations, so nothing else
can import from it.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from track_a_clinical.db import VERSION_TABLE, database_url, include_object
from track_a_clinical.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-established connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run the migrations through it."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
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
